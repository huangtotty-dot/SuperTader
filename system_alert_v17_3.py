# -*- coding: utf-8 -*-
"""
系统报警模块V17.3 - 优化版本
Windows系统级声音报警 - 1.5秒快速报警
支持多种报警模式：普通、重要、加急
"""
import sys
import time
import logging

# 只在Windows系统导入winsound
if sys.platform == 'win32':
    import winsound
else:
    winsound = None

log = logging.getLogger("系统报警V17.3")

class SystemAlert:
    """Windows系统报警类 - V17.3优化版本"""

    def __init__(self, enabled=True):
        """初始化报警模块

        Args:
            enabled: 是否启用报警
        """
        self.enabled = enabled
        self.is_windows = sys.platform == 'win32'

        if not self.is_windows:
            log.warning("⚠️  当前系统不是Windows，系统报警功能不可用")

    def _play_fallback_sound(self):
        if not self.is_windows or winsound is None:
            return
        try:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            try:
                winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:
                pass

    def _beep(self, frequency, duration):
        if not self.enabled or not self.is_windows or winsound is None:
            return False
        try:
            winsound.Beep(frequency, duration)
            return True
        except Exception as e:
            log.debug(f"⚠️  Beep 失败: {str(e)}")
            self._play_fallback_sound()
            return False

    def gentle_alarm(self):
        """温和报警 - 3次，800Hz，总时长约1.5秒

        用于：普通信号提醒
        """
        if not self.enabled or not self.is_windows:
            return

        try:
            log.debug("🔔 触发温和报警...")
            for i in range(3):
                winsound.Beep(800, 400)
                if i < 2:
                    time.sleep(0.05)
            log.debug("✓ 温和报警完成 (约1.35s)")
        except Exception as e:
            log.debug(f"⚠️  报警异常: {str(e)}")

    def normal_alarm(self):
        """普通报警 - 4次，1000Hz，总时长约1.5秒

        用于：重要信号提醒
        """
        if not self.enabled or not self.is_windows:
            return

        try:
            log.debug("🔔 触发普通报警...")
            self._play_fallback_sound()
            for i in range(4):
                if not self._beep(1000, 350):
                    break
                if i < 3:
                    time.sleep(0.05)
            log.debug("✓ 普通报警完成 (约1.4s)")
        except Exception as e:
            log.debug(f"⚠️  报警异常: {str(e)}")

    def urgent_alarm(self):
        """加急报警 - 5次，2000Hz，总时长约1.5秒

        用于：加急信号提醒（最高优先级）
        """
        if not self.enabled or not self.is_windows:
            return

        try:
            log.debug("🔔 触发加急报警...")
            self._play_fallback_sound()
            for i in range(5):
                if not self._beep(2000, 250):
                    break
                if i < 4:
                    time.sleep(0.05)
            log.debug("✓ 加急报警完成 (约1.45s)")
        except Exception as e:
            log.debug(f"⚠️  报警异常: {str(e)}")

    def critical_alarm(self):
        """严重报警 - 4次，交替频率，总时长约1.5秒

        用于：严重信号提醒（最高优先级）
        """
        if not self.enabled or not self.is_windows:
            return

        try:
            log.debug("🔔 触发严重报警...")
            self._play_fallback_sound()
            for i in range(4):
                freq = 2500 if i % 2 == 0 else 1500
                if not self._beep(freq, 300):
                    break
                if i < 3:
                    time.sleep(0.05)
            log.debug("✓ 严重报警完成 (约1.35s)")
        except Exception as e:
            log.debug(f"⚠️  报警异常: {str(e)}")

    def custom_alarm(self, frequency=1000, duration=150, repeat=5, interval=0.1):
        """自定义报警

        Args:
            frequency: 频率(Hz)，范围100-20000
            duration: 时长(ms)，范围1-1000
            repeat: 重复次数
            interval: 间隔(秒)
        """
        if not self.enabled or not self.is_windows:
            return

        try:
            total_time = (duration + interval * 1000) * repeat / 1000
            log.debug(f"🔔 触发自定义报警 (频率:{frequency}Hz, 时长:{duration}ms, 重复:{repeat}次, 总时长:{total_time:.0f}ms)...")

            # 参数验证
            frequency = max(100, min(20000, frequency))
            duration = max(1, min(1000, duration))
            repeat = max(1, min(100, repeat))
            interval = max(0.01, min(1, interval))

            self._play_fallback_sound()
            for i in range(repeat):
                if not self._beep(frequency, duration):
                    break
                if i < repeat - 1:
                    time.sleep(interval)

            log.debug(f"✓ 自定义报警完成 ({total_time:.0f}ms)")
        except Exception as e:
            log.debug(f"⚠️  报警异常: {str(e)}")

    def alarm_by_signal_type(self, signal_type):
        """根据信号类型自动选择报警模式

        Args:
            signal_type: 信号类型
                - "🎯 突破回踩" -> 加急报警
                - "⭐ 箱体突破" -> 加急报警
                - "👑 突破先手" -> 普通报警
                - "🚀 A区初显" -> 普通报警
                - "🔥 B区起航" -> 普通报警
                - "💎 底部缩量" -> 温和报警
                - "⚖️ B区潜伏" -> 温和报警
        """
        if not self.enabled or not self.is_windows:
            return

        # 根据信号类型选择报警模式
        if signal_type in ["🎯 突破回踩", "⭐ 箱体突破"]:
            self.urgent_alarm()
        elif signal_type in ["👑 突破先手", "🚀 A区初显", "🔥 B区起航"]:
            self.normal_alarm()
        else:
            self.gentle_alarm()

    def test_all_alarms(self):
        """测试所有报警模式"""
        if not self.is_windows:
            log.warning("⚠️  当前系统不是Windows，无法测试")
            return

        print("\n" + "="*70)
        print("系统报警V17.3测试 - 1.5秒快速报警")
        print("="*70)

        print("\n1️⃣  温和报警 (3次, 800Hz, 750ms)...")
        self.gentle_alarm()
        time.sleep(0.3)

        print("2️⃣  普通报警 (5次, 1000Hz, 1250ms)...")
        self.normal_alarm()
        time.sleep(0.3)

        print("3️⃣  加急报警 (7次, 2000Hz, 1400ms)...")
        self.urgent_alarm()
        time.sleep(0.3)

        print("4️⃣  严重报警 (10次, 交替频率, 1500ms)...")
        self.critical_alarm()
        time.sleep(0.3)

        print("\n✅ 所有报警测试完成！\n")


# 全局报警实例
_global_alert = None


def init_alert(enabled=True):
    """初始化全局报警实例"""
    global _global_alert
    _global_alert = SystemAlert(enabled=enabled)
    return _global_alert


def get_alert():
    """获取全局报警实例"""
    global _global_alert
    if _global_alert is None:
        _global_alert = SystemAlert(enabled=True)
    return _global_alert


def trigger_alert(alert_type="normal", signal_type=None):
    """触发报警

    Args:
        alert_type: 报警类型 (gentle, normal, urgent, critical)
        signal_type: 信号类型（如果指定，则自动选择报警模式）
    """
    alert = get_alert()

    if signal_type:
        alert.alarm_by_signal_type(signal_type)
    elif alert_type == "gentle":
        alert.gentle_alarm()
    elif alert_type == "normal":
        alert.normal_alarm()
    elif alert_type == "urgent":
        alert.urgent_alarm()
    elif alert_type == "critical":
        alert.critical_alarm()


if __name__ == "__main__":
    # 测试
    alert = SystemAlert(enabled=True)
    alert.test_all_alarms()
