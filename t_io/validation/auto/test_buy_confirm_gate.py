# coding=utf-8
"""
t_io/validation/auto/test_buy_confirm_gate.py — 人工确认闸（2026-08-30 建仓/加仓/底仓回补人工把关）验证

覆盖:
  T1  回测模式（MODE_BACKTEST）→ "allow" 且不产生 BUY_PENDING.json（防回测卡死）
  T2  主开关 human_confirm_buy_enabled=False → "allow"
  T3  MODE_LIVE 无 pending → 发请求、返回 "pending"、请求字段完整、事件留痕
  T4  pending 无回复 → "pending" 且不重复覆写文件
  T5  pending + GUI confirm → "allow"，pending 清空，approved 留痕
  T6  pending + GUI reject → "rejected_today"，当日拒绝持久化，再调不再写请求
  T7  action 错配（BASE pending 时 BUY_LOW）→ "pending" 不消费不覆盖
  T8  needs_confirm=False（做T回补路径）→ "allow" 且不发请求
  T9  跨日 D1 重置 → pending 作废（expired 留痕）+ rejected 清空 + 空请求文件

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  python t_io/validation/auto/test_buy_confirm_gate.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_AUTO = os.path.join(_ST, "execution", "auto")
for _p in (_ST, _AUTO, os.path.join(_AUTO, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gm_buyconf_")
writer.BRIDGE_DIR = TMP

import gm_main as main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_buyconf.jsonl")

PENDING_PATH = os.path.join(TMP, "BUY_PENDING.json")
DECISION_PATH = os.path.join(TMP, "BUY_DECISION.json")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def read_events():
    ep = os.path.join(TMP, f"events_{datetime.now().strftime('%Y%m%d')}.jsonl")
    if not os.path.exists(ep):
        return []
    with open(ep, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def mock_context(mode=None):
    ctx = SimpleNamespace()
    ctx.mode = mode if mode is not None else main.MODE_LIVE
    ctx._buy_confirm_pending = {}
    ctx._buy_confirm_rejected = set()
    ctx.cur_date = None
    return ctx


def write_decision(code, request_id, decision):
    data = {"date": datetime.now().strftime("%Y-%m-%d"),
            "decisions": {code: {"request_id": request_id, "decision": decision}}}
    with open(DECISION_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


NOW = datetime.now()
_BASE_KW = dict(action="BASE", price=10.0, qty_proj=500, pos_qty=0,
                reasons=["初始建仓"], needs_confirm=True, kind="build")

# ── T1: 回测模式 → allow，无文件 ──
ctx = mock_context(mode=main.MODE_BACKTEST)
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
check("T1 回测模式 allow", g == "allow", f"gate={g}")
check("T1 回测不产生 BUY_PENDING", not os.path.exists(PENDING_PATH))

# ── T2: 主开关关闭 → allow ──
_old = main.PARAMS.get("human_confirm_buy_enabled")
main.PARAMS["human_confirm_buy_enabled"] = False
ctx = mock_context()
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
main.PARAMS["human_confirm_buy_enabled"] = _old
check("T2 主开关关闭 allow", g == "allow", f"gate={g}")

# ── T3: MODE_LIVE 无 pending → 发请求 pending ──
ctx = mock_context()
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
check("T3 发请求 pending", g == "pending", f"gate={g}")
_req = read_json(PENDING_PATH).get("pending", {}).get("600176")
check("T3 请求字段完整", bool(_req and _req.get("qty") == 500
      and _req.get("kind") == "build" and _req.get("action") == "BASE"
      and _req.get("code") == "600176" and _req.get("request_id")),
      json.dumps(_req, ensure_ascii=False))
check("T3 内存 pending 一致", "600176" in ctx._buy_confirm_pending)
check("T3 请求留痕", any(e.get("event") == "buy_confirm_request"
                          and e.get("code") == "600176" for e in read_events()))

# ── T4: pending 无回复 → pending 不重复覆写 ──
_before = read_json(PENDING_PATH)
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
check("T4 无回复 pending", g == "pending", f"gate={g}")
check("T4 不重复覆写文件", read_json(PENDING_PATH) == _before)

# ── T5: confirm → allow，清空 ──
_req_id = ctx._buy_confirm_pending["600176"]["request_id"]
write_decision("600176", _req_id, "confirm")
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
check("T5 confirm allow", g == "allow", f"gate={g}")
check("T5 pending 清空", "600176" not in ctx._buy_confirm_pending)
check("T5 approved 留痕", any(e.get("event") == "buy_confirm_approved" for e in read_events()))

# ── T6: reject → rejected_today，持久化，再调不再写 ──
ctx = mock_context()
_KW6 = dict(action="ADD_POS", price=11.0, qty_proj=300, pos_qty=1000,
            reasons=["加仓"], needs_confirm=True, kind="add")
g = main._buy_confirm_gate(ctx, "002451", NOW, **_KW6)
check("T6 发请求 pending", g == "pending", f"gate={g}")
_req_id = ctx._buy_confirm_pending["002451"]["request_id"]
write_decision("002451", _req_id, "reject")
g = main._buy_confirm_gate(ctx, "002451", NOW, **_KW6)
check("T6 reject rejected_today", g == "rejected_today", f"gate={g}")
check("T6 当日拒绝入集", "002451" in ctx._buy_confirm_rejected)
check("T6 拒绝持久化到文件", "002451" in (read_json(PENDING_PATH).get("rejected_today") or []))
g2 = main._buy_confirm_gate(ctx, "002451", NOW, **_KW6)
check("T6 再调 rejected_today 不写请求", g2 == "rejected_today", f"gate={g2}")

# ── T7: action 错配 ──
ctx = mock_context()
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
check("T7 BASE 发请求 pending", g == "pending", f"gate={g}")
_KW7 = dict(action="BUY_LOW", price=10.0, qty_proj=300, pos_qty=0,
            reasons=["做T"], needs_confirm=True, kind="build")
g = main._buy_confirm_gate(ctx, "600176", NOW, **_KW7)
check("T7 action 错配 pending 不消费", g == "pending", f"gate={g}")
check("T7 请求未被覆盖", ctx._buy_confirm_pending["600176"]["action"] == "BASE")

# ── T8: needs_confirm=False（做T回补）→ allow ──
ctx = mock_context()
_KW8 = dict(action="BUY_LOW", price=10.0, qty_proj=300, pos_qty=1000,
            reasons=["回补"], needs_confirm=False, kind="add")
g = main._buy_confirm_gate(ctx, "600176", NOW, **_KW8)
check("T8 做T回补 allow", g == "allow", f"gate={g}")
check("T8 不发请求", "600176" not in ctx._buy_confirm_pending)

# ── T9: 跨日 D1 重置（复刻 gm_main on_bar D1 块语义） ──
ctx = mock_context()
g = main._buy_confirm_gate(ctx, "600176", NOW, **_BASE_KW)
ctx._buy_confirm_rejected.add("002451")
_today2 = (NOW + timedelta(days=1)).date()
_old_pending = dict(ctx._buy_confirm_pending)
for _c, _req in _old_pending.items():
    main.write_confirm(str(NOW), _c, "expired", detail="跨日作废",
                       request_id=_req.get("request_id"))
ctx._buy_confirm_rejected = set()
ctx._buy_confirm_pending = {}
main.write_buy_pending({"date": str(_today2), "updated_at": str(NOW),
                        "rejected_today": [], "pending": {}})
check("T9 rejected 清空", not ctx._buy_confirm_rejected)
check("T9 pending 清空", not ctx._buy_confirm_pending)
check("T9 expired 留痕", any(e.get("event") == "buy_confirm_expired" for e in read_events()))
_p = read_json(PENDING_PATH)
check("T9 空请求文件 date 更新", _p.get("date") == str(_today2) and not _p.get("pending"))

# 汇总
_fail = [r for r in results if not r[1]]
print(f"\n{len(results) - len(_fail)}/{len(results)} PASS")
if _fail:
    print("FAILED:", [r[0] for r in _fail])
    sys.exit(1)
sys.exit(0)
