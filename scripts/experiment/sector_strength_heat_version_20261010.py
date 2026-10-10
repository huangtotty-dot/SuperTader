"""验证热度分「口径版本闸」：跨版本分值不得参与趋势回归。

背景：`趋势` 是对该板块**自身近 5 日热度分**做线性回归取斜率。权重一改，新旧分值不可比，
        混着回归出的斜率是假的（切换点附近全体板块会被误判"退潮"约 5 个交易日）。

用法：PYTHONIOENCODING=utf-8 python tmp/probe_heat_version.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
from pathlib import Path

sys.path.insert(0, BASE)
sys.path.insert(0, _os.path.join(BASE, "stock_hunter"))

import modules.heat_tracker as ht  # noqa: E402

# 隔离（同 probe_heat_lead）：绝不写生产历史
ht._history_path = lambda: str(Path(__file__).with_name("_heat_probe_history.json"))

ok = True


def check(c, m):
    global ok
    print(("  PASS  " if c else "  FAIL  ") + m)
    if not c:
        ok = False


print(f"=== 1) 当前版本常量 ===\n  _HEAT_VERSION = {ht._HEAT_VERSION}")
check(isinstance(ht._HEAT_VERSION, int) and ht._HEAT_VERSION >= 2, "版本号存在且 ≥2")

DATES = ["20990101", "20990102", "20990103", "20990104", "20990105", "20990106"]
SEC = "测试板块"


def mkhist(scores_with_ver):
    """scores_with_ver: [(score, ver or None)]，None=旧条目（无字段）"""
    h = {}
    for d, item in zip(DATES, scores_with_ver):
        score, ver = item
        e = {"板块": SEC, "热度分": score}
        if ver is not None:
            e["热度版本"] = ver
        h[d] = [e]
    return h


print("\n=== 2) 同版本 → 全部取到 ===")
h = mkhist([(40, 2), (42, 2), (44, 2), (46, 2), (48, 2), (50, 2)])
got = ht._get_past_scores(SEC, h, DATES, 5, 5)
print(f"  {got}")
# 契约：以 end_idx 结尾的最近 count 天 ⇒ indices [end_idx-count+1, end_idx] = 1..5
check(got == [42, 44, 46, 48, 50], "v2 条目全部取到（以 end_idx=5 结尾的最近 5 天）")

print("\n=== 3) 全旧版本（无字段）→ 一条都不取（这是当前生产历史的处境）===")
h = mkhist([(20, None), (22, None), (24, None), (26, None), (28, None), (30, None)])
got = ht._get_past_scores(SEC, h, DATES, 5, 5)
print(f"  {got}")
check(got == [], "旧条目（无 热度版本 字段）被全部跳过")

print("\n=== 4) 混合版本 → 只取 v2，旧的丢掉（不跨标尺回归）===")
h = mkhist([(20, None), (22, 1), (44, 2), (46, 2), (48, 2), (50, 2)])
got = ht._get_past_scores(SEC, h, DATES, 5, 5)
print(f"  {got}")
check(got == [44, 46, 48, 50], "混合历史只取 v2 的条目（旧的被挡掉）")

print("\n=== 5) 有闸 vs 无闸（核心对照：闸真的改变了结论）===")
# 构造最典型的失真：**新标尺在上升，但绝对值低于旧标尺**
#   v1(旧,虚高)=[20,22,24]   v2(新,上升)=[10,12,14]   今日=16
#   有闸 ⇒ [10,12,14,16] 单调上升 ⇒ 升温/加速（正确）
#   无闸 ⇒ [20,22,24,10,12,14,16] 被 24→10 的大落差主导 ⇒ 误判成下跌
h = mkhist([(20, None), (22, None), (24, None), (10, 2), (12, 2), (14, 2)])
gated = ht._get_past_scores(SEC, h, DATES, 4, 5)
t_gated = ht._compute_trend(SEC, 16, h, DATES, 5)
print(f"  有闸：参与回归={gated + [16]} → 趋势={t_gated}")
check(gated == [10, 12], "有闸只取到 v2 的两条（end_idx=昨日，今日不参与回归）")

_orig = ht._get_past_scores


def _ungated(sector, history, sorted_dates, end_idx, count):
    out = []
    for i in range(max(0, end_idx - count + 1), end_idx + 1):
        for s_ in history.get(sorted_dates[i], []):
            if s_.get("板块") == sector and "热度分" in s_:
                out.append(s_["热度分"])
    return out


ht._get_past_scores = _ungated
try:
    ungated = ht._get_past_scores(SEC, h, DATES, 4, 5)
    t_ungated = ht._compute_trend(SEC, 16, h, DATES, 5)
finally:
    ht._get_past_scores = _orig
print(f"  无闸：参与回归={ungated + [16]} → 趋势={t_ungated}")
check(ungated == [20, 22, 24, 10, 12], "无闸会把跨标尺数据混进来")
check(t_gated != t_ungated, f"闸改变了结论（有闸={t_gated} vs 无闸={t_ungated}）")
check(t_gated in ("📈升温", "🔥加速"), f"有闸正确判为上行（{t_gated}）")
check(t_ungated in ("📉退潮", "➡️平稳", "🧊冰点"),
      f"无闸被跨标尺落差误导为下行（{t_ungated}）—— 这正是版本闸要挡住的失真")

print("\n=== 6) 落盘条目带版本号 ===")
import pandas as pd  # noqa: E402
tmp_fp = Path(__file__).with_name("_heat_probe_hist2.json")
ht._history_path = lambda: str(tmp_fp)
tmp_fp.unlink(missing_ok=True)
summary = pd.DataFrame({"板块": ["甲板块"], "股票数量": [10]})
secs = {"甲板块": pd.DataFrame({"代码": [f"{600000+i}" for i in range(10)],
                                "涨跌幅": [1.0] * 10, "涨停": [0] * 10, "成交额": [1e8] * 10,
                                "D1强势形态且新高": [0] * 10, "D2强势形态": [0] * 10})}
ht.compute_heat_scores(summary, secs, "20990110")
import json  # noqa: E402
saved = json.loads(tmp_fp.read_text(encoding="utf-8"))["20990110"][0]
print(f"  落盘字段: 热度版本={saved.get('热度版本')}  热度分={saved.get('热度分')}")
check(saved.get("热度版本") == ht._HEAT_VERSION, "落盘条目带当前 _HEAT_VERSION")
tmp_fp.unlink(missing_ok=True)

print("\n=== 7) 生产历史文件未被本探针改动 ===")
prod = Path(BASE) / "stock_hunter" / "history" / "daily_summary.json"
import re  # noqa: E402
d = json.loads(prod.read_text(encoding="utf-8"))
bad = [k for k in d if not re.fullmatch(r"\d{8}", k)]
print(f"  生产历史日期数={len(d)}  非生产格式日期={bad if bad else '无 ✓'}")
check(not bad, "生产历史无探针污染")

print("\n判定:", "PASS" if ok else "FAIL")
