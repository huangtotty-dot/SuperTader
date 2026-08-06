# -*- coding: utf-8 -*-
"""
板块轮动检测模块 v1.0
职责：基于多日热度分历史数据，检测板块轮动信号
信号类型：
  - 初升信号：前日冰点/退潮 → 今日升温，提示关注
  - 加速信号：连续升温 → 今日加速，提示加仓
  - 退潮预警：加速/升温 → 今日退潮，提示减仓
  - 冰点反转：连续冰点 → 有标的涨停，提示观察
"""
import os
import json
import pandas as pd
from typing import Dict, List, Optional, Tuple


def _history_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "history", "daily_summary.json")


def load_history() -> dict:
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


class RotationDetector:
    """板块轮动检测器"""

    # 信号定义
    SIGNAL_EMERGING = "初升信号"       # 冰点/退潮 → 升温
    SIGNAL_ACCELERATING = "加速信号"    # 连续升温 → 加速
    SIGNAL_EBING = "退潮预警"           # 加速/升温 → 退潮
    SIGNAL_REVERSAL = "冰点反转"        # 冰点但有涨停
    SIGNAL_BREAKOUT = "突破信号"        # 热度创新高

    def __init__(self, history: dict = None):
        self.history = history or load_history()
        self._sorted_dates = sorted(self.history.keys())

    def get_sector_history(self, sector: str, days: int = 5) -> List[dict]:
        """获取板块最近 N 天的热度记录"""
        records = []
        for date in self._sorted_dates[-days:]:
            day_data = self.history.get(date, [])
            for entry in day_data:
                if entry.get("板块") == sector:
                    records.append({
                        "日期": date,
                        "热度分": entry.get("热度分", 0),
                        "趋势": entry.get("趋势", ""),
                        "排名": entry.get("排名", 0),
                        "平均分": entry.get("平均分", 0),
                        "涨停数": entry.get("涨停数", 0),
                        "成交额总额": entry.get("成交额总额", 0),
                    })
        return records

    def detect_signals(self, sectors: List[str] = None) -> pd.DataFrame:
        """
        检测所有板块的轮动信号。

        返回 DataFrame，列含：
        板块, 当前热度分, 当前趋势, 信号, 信号强度, 历史摘要
        """
        if not self._sorted_dates or len(self._sorted_dates) < 2:
            return pd.DataFrame()

        today_str = self._sorted_dates[-1]
        yesterday_str = self._sorted_dates[-2]
        today_data = self.history.get(today_str, [])
        yesterday_data = self.history.get(yesterday_str, [])

        # 构建今日/昨日查找表
        today_map = {e["板块"]: e for e in today_data if e.get("板块")}
        yesterday_map = {e["板块"]: e for e in yesterday_data if e.get("板块")}

        if sectors is None:
            sectors = list(today_map.keys())

        signals = []
        for sector in sectors:
            today = today_map.get(sector, {})
            yesterday = yesterday_map.get(sector, {})

            if not today:
                continue

            current_heat = today.get("热度分", 0)
            current_trend = today.get("趋势", "")
            yesterday_trend = yesterday.get("趋势", "")

            signal = None
            strength = 0

            # 1. 初升信号：昨日冰点/退潮 → 今日升温
            if yesterday_trend in ("🧊冰点", "📉退潮") and current_trend == "📈升温":
                signal = self.SIGNAL_EMERGING
                strength = current_heat - yesterday.get("热度分", 0)
                if strength < 5:
                    signal = None  # 幅度不够

            # 2. 加速信号
            if current_trend == "🔥加速":
                # 回溯 2 天看是否是连续升温
                prev_trends = []
                for i in range(-3, -1):
                    if abs(i) <= len(self._sorted_dates):
                        day_key = self._sorted_dates[i]
                        day_map = {e["板块"]: e for e in self.history.get(day_key, []) if e.get("板块")}
                        if sector in day_map:
                            prev_trends.append(day_map[sector].get("趋势", ""))
                if len(prev_trends) >= 2 and all(t in ("📈升温", "🔥加速") for t in prev_trends):
                    signal = self.SIGNAL_ACCELERATING
                    strength = current_heat - (yesterday.get("热度分", 0))

            # 3. 退潮预警：昨日加速/升温 → 今日退潮
            if yesterday_trend in ("🔥加速", "📈升温") and current_trend == "📉退潮":
                signal = self.SIGNAL_EBING
                strength = -(current_heat - yesterday.get("热度分", 0))

            # 4. 冰点反转：冰点但有涨停
            if current_trend == "🧊冰点" and today.get("涨停数", 0) > 0:
                signal = self.SIGNAL_REVERSAL
                strength = today.get("涨停数", 0)

            # 5. 突破信号：热度创新高
            sector_history = self.get_sector_history(sector, days=10)
            if len(sector_history) >= 5:
                prev_heats = [h["热度分"] for h in sector_history[:-1]]
                if prev_heats and current_heat > max(prev_heats):
                    if signal is None:
                        signal = self.SIGNAL_BREAKOUT
                        strength = round(current_heat - max(prev_heats), 1)

            if signal:
                signals.append({
                    "板块": sector,
                    "当前热度分": current_heat,
                    "当前趋势": current_trend,
                    "昨日趋势": yesterday_trend,
                    "信号": signal,
                    "信号强度": round(strength, 1),
                    "涨停数": today.get("涨停数", 0),
                    "排名": today.get("排名", 0),
                    "热度变化": round(current_heat - yesterday.get("热度分", 0), 1),
                })

        if not signals:
            return pd.DataFrame()

        df = pd.DataFrame(signals)
        # 按信号优先级排序：加速 > 初升 > 突破 > 退潮 > 反转
        priority = {
            self.SIGNAL_ACCELERATING: 0,
            self.SIGNAL_EMERGING: 1,
            self.SIGNAL_BREAKOUT: 2,
            self.SIGNAL_EBING: 3,
            self.SIGNAL_REVERSAL: 4,
        }
        df["_priority"] = df["信号"].map(priority).fillna(99)
        df = df.sort_values("_priority").drop(columns=["_priority"]).reset_index(drop=True)
        return df

    def get_top_picks(self, sector: str, top_n: int = 3) -> List[str]:
        """
        对发出信号的板块，推荐该板块的 TOP N 标的。
        需要配合 screener 使用。
        """
        from modules.screener import WatchlistScreener

        s = WatchlistScreener()
        pool = s.filter(categories=[sector])
        if pool.empty:
            return []

        # 如果有得分列，按得分排序
        if "总得分" in pool.columns:
            pool = pool.sort_values("总得分", ascending=False)
            pool = pool.drop_duplicates(subset=["代码"])

        result = []
        for _, row in pool.head(top_n).iterrows():
            result.append(f"{row['代码']} {row['名称']}")
        return result

    def summary(self) -> str:
        """生成轮动检测摘要（文本）"""
        df = self.detect_signals()
        if df.empty:
            return "[轮动检测] 当前无显著轮动信号"

        lines = ["=== 板块轮动检测 ===", f"检测日期: {self._sorted_dates[-1] if self._sorted_dates else 'N/A'}", ""]

        for signal_type in [self.SIGNAL_ACCELERATING, self.SIGNAL_EMERGING,
                            self.SIGNAL_BREAKOUT, self.SIGNAL_EBING, self.SIGNAL_REVERSAL]:
            subset = df[df["信号"] == signal_type]
            if not subset.empty:
                lines.append(f"\n[{signal_type}]")
                for _, row in subset.iterrows():
                    picks = self.get_top_picks(row["板块"])
                    lines.append(f"  {row['板块']} (热度{row['当前热度分']}, 强度{row['信号强度']})")
                    if picks:
                        lines.append(f"    推荐: {', '.join(picks[:3])}")

        return "\n".join(lines)


# ── CLI ──
if __name__ == "__main__":
    detector = RotationDetector()
    print(detector.summary())
