# -*- coding: utf-8 -*-
"""test_intercept_notice_cooldown.py — F-11 🔕 拦截可见性去重单测（2026-09-04）

回归 09-04 案：002451 BUY_LOW 10:34 与 10:57 两次共振拦截，旧"每股每向每日 1 条"布尔去重
把 10:57 那条吞掉（Q-20260904-5）。改为 20min 滚动冷却窗后应四笔四发。

覆盖：
1. 09-04 四笔拦截按时间序 → send_allowed 全 True（10:34→10:57 间隔 23m14s > 20min 放行）
2. 同 (code:action) 20min 内重复 → 抑制
3. 冷却期满（≥20min）→ 再次放行
4. 旧布尔 True 标记（升级当日存量）→ 视为过期，放行
5. send 失败不 mark（B3 语义）：未 mark 时紧邻事件仍放行
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import intercept_notice as intn  # noqa: E402

_DAY = "2026-09-04"


def _t(hms):
    return datetime.strptime(f"{_DAY} {hms}", "%Y-%m-%d %H:%M:%S")


def run():
    n = 0

    def check(name, got, want):
        nonlocal n
        assert got == want, f"{name}: got {got}, want {want}"
        n += 1
        print(f"  ok {name}: {got}")

    # 1) 09-04 四笔拦截：600481@10:11 / 002639@10:31 / 002451@10:34 / 002451@10:57(23m14s 后)
    st = {}
    seq = [("600481:BUY_LOW", "10:11:18"), ("002639:BUY_LOW", "10:31:35"),
           ("002451:BUY_LOW", "10:34:34"), ("002451:BUY_LOW", "10:57:48")]
    allowed = [intn.send_allowed(st, _DAY, k, _t(ts)) for k, ts in seq]
    for k, ts, a in zip([s[0] for s in seq], [s[1] for s in seq], allowed):
        if a:
            intn.mark_sent(st, _DAY, k, _t(ts))
    check("09-04 四笔拦截四发（含 002451 23min 复拦放行）", allowed, [True, True, True, True])

    # 2) 同 (code:action) 20min 内重复 → 抑制
    st2 = {}
    a1 = intn.send_allowed(st2, _DAY, "000988:BUY_LOW", _t("10:00:00"))
    intn.mark_sent(st2, _DAY, "000988:BUY_LOW", _t("10:00:00"))
    a2 = intn.send_allowed(st2, _DAY, "000988:BUY_LOW", _t("10:15:00"))   # 15min < 20min
    check("冷却内重复抑制", a2, False)
    check("首次放行", a1, True)

    # 3) 冷却期满（≥20min）→ 再次放行
    a3 = intn.send_allowed(st2, _DAY, "000988:BUY_LOW", _t("10:21:00"))   # 21min ≥ 20min
    check("冷却期满再放行", a3, True)

    # 4) 旧布尔 True 标记（老代码日级去重残留）→ 视为过期放行
    st3 = {_DAY: {"600481:BUY_LOW": True}}
    check("旧 True 标记迁移放行", intn.send_allowed(st3, _DAY, "600481:BUY_LOW", _t("10:30:00")), True)

    # 5) send 失败不 mark → 紧邻事件仍可补发（B3 语义）
    st4 = {}
    intn.send_allowed(st4, _DAY, "002451:BUY_LOW", _t("10:34:34"))   # 只判断未 mark（模拟失败）
    check("失败不占额→紧邻复拦放行", intn.send_allowed(st4, _DAY, "002451:BUY_LOW", _t("10:37:00")), True)

    print(f"PASS: F-11 拦截可见性冷却去重全绿（{n} 项）")


if __name__ == "__main__":
    run()
