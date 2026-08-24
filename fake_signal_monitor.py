#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
虚假信号监控系统 - 风险3修复
用于监控禁用共振门控后产生的虚假信号，并在需要时触发动态回退机制。
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from datetime import datetime, timedelta
from pathlib import Path
import json
import logging

BASE = Path(__file__).resolve().parent

# 获取logger
log = logging.getLogger("FalseSignalMonitor")


class FalseSignalMonitor:
    """虚假信号监控与动态回退机制"""

    def __init__(self):
        self.pushed_signals = []  # [{'code': '588170', 'price': 0.996, 'action': 'BUY_LOW', 'time': datetime, 'checked': False}]
        self.false_count = 0
        self.true_count = 0
        self.false_ratio = 0.0
        self.last_rollback_check = None
        self.rollback_triggered = False
        self.rollback_reason = ""

    def record_signal(self, code: str, price: float, action: str, timestamp=None):
        """记录已推送的信号

        Args:
            code: 标的代码
            price: 推送时的价格
            action: 信号类型 (BUY_LOW/ADD_POS/SELL_HIGH/PANIC_SELL)
            timestamp: 记录时间，默认当前时间
        """
        if timestamp is None:
            timestamp = datetime.now()

        self.pushed_signals.append({
            'code': code,
            'price': float(price),
            'action': action,
            'time': timestamp,
            'checked': False,
            'outcome': None,  # True=有效信号, False=虚假信号
        })
        log.debug(f"📝 记录推送信号: {code} {action} @ {price:.2f}")

    def check_signal_outcome(self, code: str, current_price: float, original_price: float) -> bool or None:
        """检查信号后续表现

        Args:
            code: 标的代码
            current_price: 当前价格
            original_price: 推送时的价格

        Returns:
            True: 有效信号（没有继续下跌）
            False: 虚假信号（下跌>3%）
            None: 无法判断
        """
        if original_price == 0 or current_price == 0:
            return None

        try:
            drawdown = (original_price - current_price) / original_price

            if drawdown > 0.03:  # 下跌>3%
                self.false_count += 1
                log.warning(f"⚠️  虚假信号确认: {code} 推送@{original_price:.2f} -> 当前@{current_price:.2f} (下跌{drawdown*100:.1f}%)")
                return False
            else:
                self.true_count += 1
                log.info(f"✅ 有效信号确认: {code} 推送@{original_price:.2f} -> 当前@{current_price:.2f} (下跌{drawdown*100:.1f}%)")
                return True
        except Exception as e:
            log.warning(f"⚠️  信号检查异常 {code}: {e}")
            return None

    def check_expired_signals(self, current_prices: dict, hours_elapsed: int = 1) -> dict:
        """检查已过期的信号（推送超过N小时的信号）

        Args:
            current_prices: {'588170.SH': 10.5, ...}
            hours_elapsed: 多少小时后判断信号是否虚假，默认1小时

        Returns:
            {'checked': N, 'false': N, 'true': N, 'new_ratio': 0.XX}
        """
        now = datetime.now()
        checked_count = 0
        new_false = 0
        new_true = 0

        for sig in self.pushed_signals:
            if sig['checked']:
                continue

            time_diff = now - sig['time']
            if time_diff < timedelta(hours=hours_elapsed):
                continue  # 还没到检查时间

            code = sig['code']
            current_price = current_prices.get(code, 0)

            if current_price == 0:
                continue  # 无法获取现价

            outcome = self.check_signal_outcome(code, current_price, sig['price'])
            if outcome is not None:
                sig['checked'] = True
                sig['outcome'] = outcome
                checked_count += 1

                if outcome:
                    new_true += 1
                else:
                    new_false += 1

        self.false_ratio = self.get_false_ratio()

        return {
            'checked': checked_count,
            'false': new_false,
            'true': new_true,
            'total_false': self.false_count,
            'total_true': self.true_count,
            'ratio': self.false_ratio,
        }

    def get_false_ratio(self) -> float:
        """获取虚假信号比例"""
        total = self.true_count + self.false_count
        if total == 0:
            return 0.0
        self.false_ratio = self.false_count / total
        return self.false_ratio

    def should_rollback(self, threshold: float = 0.05) -> bool:
        """是否应该回退方案A

        Args:
            threshold: 虚假比例阈值，默认5%

        Returns:
            True: 虚假比例超过阈值，应该回退
            False: 虚假比例正常，继续执行
        """
        ratio = self.get_false_ratio()
        return ratio > threshold

    def get_daily_report(self) -> str:
        """生成每日监控报告"""
        ratio = self.get_false_ratio()
        total = self.true_count + self.false_count

        status = "✅ 正常" if ratio < 0.05 else "⚠️ 警告" if ratio < 0.10 else "❌ 触发回退"

        report = f"""
╔════════════════════════════════════════════════════════════════════════════════╗
║                        【虚假信号监控报告】                                    ║
╠════════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║  总信号数:    {total:>3}                                                       ║
║  有效信号:    {self.true_count:>3}                                                       ║
║  虚假信号:    {self.false_count:>3}                                                       ║
║  虚假比例:    {ratio:>6.2%}                                                     ║
║  状态:        {status}                                                           ║
║                                                                                ║
╚════════════════════════════════════════════════════════════════════════════════╝
"""
        return report

    def dump_state(self, filepath: str = None) -> dict:
        """保存监控状态到文件，便于持久化"""
        if filepath is None:
            filepath = str(BASE / "t_io" / "state" / "false_signal_monitor.json")

        try:
            state = {
                'timestamp': datetime.now().isoformat(),
                'total_false': self.false_count,
                'total_true': self.true_count,
                'false_ratio': self.false_ratio,
                'signals_count': len(self.pushed_signals),
                'rollback_triggered': self.rollback_triggered,
                'rollback_reason': self.rollback_reason,
            }

            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            log.debug(f"💾 监控状态已保存: {filepath}")
            return state
        except Exception as e:
            log.warning(f"⚠️  保存监控状态失败: {e}")
            return {}

    def load_state(self, filepath: str = None) -> dict:
        """从文件加载历史监控状态"""
        if filepath is None:
            filepath = str(BASE / "t_io" / "state" / "false_signal_monitor.json")

        try:
            if not Path(filepath).exists():
                return {}

            with open(filepath, 'r', encoding='utf-8') as f:
                state = json.load(f)

            log.debug(f"📂 监控状态已加载: {filepath}")
            return state
        except Exception as e:
            log.warning(f"⚠️  加载监控状态失败: {e}")
            return {}


# 全局监控器实例
_false_signal_monitor = None


def get_monitor() -> FalseSignalMonitor:
    """获取全局监控器实例"""
    global _false_signal_monitor
    if _false_signal_monitor is None:
        _false_signal_monitor = FalseSignalMonitor()
    return _false_signal_monitor


def init_monitor():
    """初始化监控器"""
    global _false_signal_monitor
    _false_signal_monitor = FalseSignalMonitor()
    return _false_signal_monitor


if __name__ == "__main__":
    # 测试监控器
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    m = FalseSignalMonitor()

    # 模拟记录信号
    m.record_signal('588170.SH', 10.5, 'BUY_LOW')
    m.record_signal('300153.SZ', 20.3, 'BUY_LOW')
    m.record_signal('588170.SH', 10.6, 'SELL_HIGH')

    print(f"记录了 {len(m.pushed_signals)} 条信号")

    # 模拟检查结果
    current_prices = {
        '588170.SH': 10.2,  # 虚假信号（下跌2.9%）
        '300153.SZ': 20.8,  # 有效信号
    }

    result = m.check_expired_signals(current_prices, hours_elapsed=0)
    print(f"检查结果: {result}")
    print(m.get_daily_report())

    m.dump_state()
