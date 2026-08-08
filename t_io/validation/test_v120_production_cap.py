# -*- coding: utf-8 -*-
"""
test_v120_production_cap.py — V1.2.0 C1' 生产化回归（2026-08-08 用户拍板上线）
两项机制：① buyback_bypass_gates 生产默认开  ② buy_daily_cap=7 生产默认开（记录层拦截）
本测试（AST 抽取 + 桩环境，仿 W32-B1/B2 模式）：
  A. 引擎谓词语义：cap 关/=0 恒 False；cap=7 时 record_signal 计数到 7 才 True；
     卖信号不计数；跨日重置（get_today_str 变化）后归零
  B. 生产默认值静态断言：config.py PARAMS 含 buy_daily_cap=7 / buyback_bypass_gates=True
  C. main.py 实盘链路断言：cap 拦截在 notify 阈值判定之后、notify/record 之前；
     record_signal 两处调用点存在（计数口径与 harness 一致）
  D. harness 向后兼容断言：T_BUYBACK_BYPASS_GATES="0"/T_BUY_DAILY_CAP="0" 显式关闭路径存在
"""
import ast
import types
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
ENGINE = BASE / "signal_engine.py"
MAIN = BASE / "main.py"
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
          "_engine_now": lambda: datetime(2026, 8, 10, 9, 35),
          "get_today_str": lambda: ns["_today"][0],
          "_today": ["2026-08-10"]}
    for fn in ("record_signal", "buy_daily_cap_reached", "_reset_daily_state_if_needed"):
        exec(compile(extract_method(ENGINE, fn), str(ENGINE), "exec"), ns)

    class E:
        pass
    e = E()
    e.buy_cooldown, e.sell_cooldown = {}, {}
    e.buy_recorded_today = {}
    e.buy_count_per_stock, e.sell_count_per_stock = {}, {}
    e.t_cycle_start_time, e.last_signal_state, e.last_trade_state = {}, {}, {}
    e.cycle_count, e.cycle_direction, e.post_sell_block_until = {}, {}, {}
    e.awaiting_buyback, e.pending_sells = {}, {}
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
    assert '"buyback_bypass_gates": True' in cfg, "config.py 须落 buyback_bypass_gates=True 生产默认"

    # ── C: main.py 实盘链路顺序断言 ──
    src = MAIN.read_text(encoding="utf-8")
    i_push = src.index("pushed = sig.score >= notify_threshold")
    i_cap = src.index("engine.buy_daily_cap_reached(code)")
    i_notify = src.index("notify(sig, holding)", i_push)
    i_rec = src.index("engine.record_signal(code, sig.action, sig.price, sig.score)", i_push)
    assert i_push < i_cap < i_notify < i_rec, \
        "cap 拦截须在 notify 阈值判定之后、notify/record 之前（与 harness 记录层同序）"
    assert src.count("engine.record_signal(code, sig.action, sig.price, sig.score)") >= 2, \
        "qty>0 与 qty=0 两路 record_signal 调用点须存在（计数口径与 harness 一致）"
    assert "买信号日限cap拦截" in src, "cap 拦截须落 shadow_signals（失败显式化）"

    # ── D: harness 向后兼容断言 ──
    hb = HARNESS.read_text(encoding="utf-8")
    assert 'T_BUYBACK_BYPASS_GATES") == "0"' in hb, "harness 须支持 T_BUYBACK_BYPASS_GATES=0 显式关闭"
    assert 'PARAMS["buyback_bypass_gates"] = False' in hb
    assert 'T_BUY_DAILY_CAP")' in hb and 'int(os.environ["T_BUY_DAILY_CAP"])' in hb, \
        "harness T_BUY_DAILY_CAP 覆盖路径须保留（0=关闭/N=自定义）"

    print("PASS: V1.2.0 生产化回归全绿（谓词语义/默认值/链路顺序/向后兼容）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
