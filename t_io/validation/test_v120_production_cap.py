# -*- coding: utf-8 -*-
"""
test_v120_production_cap.py — buy_daily_cap 记录层计数谓词回归（V1.2.0 C1' 口径B）
2026-08-13 纯两点改造后更新：生产 main.py 已移除单股日限拦截（两点恒推送），
本谓词仅剩 harness_backtest 回测 A/B 挂载点消费（buy_daily_cap_reached）。
buyback_bypass_gates 机制已整体移除，相关断言随之删除。
本测试（AST 抽取 + 桩环境，仿 W32-B1/B2 模式）：
  A. 引擎谓词语义：cap 关/=0 恒 False；cap=7 时 record_signal 计数到 7 才 True；
     卖信号不计数；跨日重置（get_today_str 变化）后归零
  B. 生产默认值静态断言：config.py PARAMS 含 buy_daily_cap=7
  D. harness 向后兼容断言：T_BUY_DAILY_CAP="0" 显式关闭路径存在
"""
import ast
import types
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
ENGINE = BASE / "signal_engine.py"
CONFIG = BASE / "config.py"
HARNESS = BASE / "harness_backtest.py"


def extract_method(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    raise AssertionError(f"{name} not found in {path}")


def build_engine_stub(params):
    """桩 SignalEngine：绑定 AST 抽取的 3 个真实方法。"""
    ns = {"PARAMS": params, "datetime": datetime,
          "_now": lambda: datetime(2026, 8, 10, 9, 35),
          "get_today_str": lambda: ns["_today"][0],
          "_today": ["2026-08-10"]}
    for fn in ("record_signal", "buy_daily_cap_reached", "_reset_daily_state_if_needed"):
        exec(compile(extract_method(ENGINE, fn), str(ENGINE), "exec"), ns)

    class E:
        pass
    e = E()
    e.buy_recorded_today = {}
    e.buy_count_per_stock, e.sell_count_per_stock = {}, {}
    e.t_cycle_start_time, e.last_signal_state, e.last_trade_state = {}, {}, {}
    e.cycle_count, e.cycle_direction, e.post_sell_block_until = {}, {}, {}
    e.daily_realized_loss_monitor = 0.0
    e.morning_alert_state, e._5min_cache, e.trend_regimes = {}, {}, {}
    e.state_reset_date = "2026-08-10"
    e._last_sig_price = 0
    e._persist_intraday_state = lambda: None
    for fn in ("record_signal", "buy_daily_cap_reached", "_reset_daily_state_if_needed"):
        setattr(e, fn, types.MethodType(ns[fn], e))
    return e, ns


def main() -> int:
    # ── A1: cap 未配置（旧世界）→ 恒 False ──
    e, _ = build_engine_stub({})
    for _i in range(99):
        e.record_signal("000988", "BUY_LOW", 100.0, 42.0)
    assert e.buy_daily_cap_reached("000988") is False, "cap 未配置应恒 False"
    assert e.buy_recorded_today.get("000988", 0) == 0, "cap 关时计数器不递增（零行为变化）"

    # ── A2: cap=0（env 显式关闭）→ 恒 False ──
    e, _ = build_engine_stub({"buy_daily_cap": 0})
    for _i in range(7):
        e.record_signal("000988", "BUY_LOW", 100.0, 42.0)
    assert e.buy_daily_cap_reached("000988") is False, "cap=0 应恒 False"

    # ── A3: cap=7 → 第 7 条记录后 True；卖信号不计数 ──
    e, ns = build_engine_stub({"buy_daily_cap": 7})
    for _i in range(6):
        e.record_signal("000988", "BUY_LOW", 100.0, 42.0)
        assert e.buy_daily_cap_reached("000988") is False, "前 6 条不应触发 cap"
    e.record_signal("000988", "SELL_HIGH", 101.0, 60.0)          # 卖信号不计数
    assert e.buy_recorded_today["000988"] == 6, "卖信号不得计入买 cap 计数"
    assert e.buy_daily_cap_reached("000988") is False
    e.record_signal("000988", "BUY_LOW", 100.0, 42.0)            # 第 7 条
    assert e.buy_daily_cap_reached("000988") is True, "记录满 7 条应触发 cap"
    assert e.buy_recorded_today["000988"] == 7
    e.record_signal("600176", "BUY_LOW", 10.0, 42.0)             # 他股独立计数
    assert e.buy_daily_cap_reached("600176") is False, "cap 应按股独立"

    # ── A4: 跨日重置 → 计数归零 ──
    ns["_today"][0] = "2026-08-11"
    assert e.buy_daily_cap_reached("000988") is False, "跨日重置后应放行"
    assert e.buy_recorded_today == {}, "跨日计数器应清空"

    # ── B: 生产默认值静态断言 ──
    cfg = CONFIG.read_text(encoding="utf-8")
    assert '"buy_daily_cap": 7' in cfg, "config.py 须落 buy_daily_cap=7 生产默认"

    # ── D: harness 向后兼容断言 ──
    hb = HARNESS.read_text(encoding="utf-8")
    assert 'T_BUY_DAILY_CAP")' in hb and 'int(os.environ["T_BUY_DAILY_CAP"])' in hb, \
        "harness T_BUY_DAILY_CAP 覆盖路径须保留（0=关闭/N=自定义）"

    print("PASS: buy_daily_cap 记录层计数谓词回归全绿（cap 关/开/跨日/他股/harness 覆盖）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
