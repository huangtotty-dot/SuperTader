# -*- coding: utf-8 -*-
"""P1-10 冒烟：_write_daily_holdings 同内容第二次调用应跳过写盘（mtime 不变）。
所有状态路径 monkeypatch 到临时目录，绝不触碰真实持仓/账户文件。"""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("t_gui", r"E:\superTrader\t_gui.py")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception as e:
    print("IMPORT_FAIL", type(e).__name__, str(e)[:200])
    sys.exit(2)

tmp = Path(tempfile.mkdtemp(prefix="p110_smoke_"))
m.STATE_DIR = tmp
m.HOLDINGS_MANUAL = tmp / "holdings.json"
m.HOLDINGS_MANUAL.write_text(json.dumps({
    "600000": {"name": "测试股", "qty": 1000, "cost": 10.0, "pre_close": 10.5}
}, ensure_ascii=False), encoding="utf-8")
m._reconcile_cash = lambda: (12345.0, "ok")
m._account_of = lambda c: "A"

api = m.Api()
q = {"source": "live", "quotes": [
    {"code": "600000", "name": "测试股", "price": 11.0, "change_pct": 4.76,
     "pnl_pct": 10.0, "offline": False},
]}

r1 = api._write_daily_holdings(q)
fp = tmp / ("holdings_daily_%s.json" % __import__("datetime").datetime.now().strftime("%Y-%m-%d"))
mtime1 = fp.stat().st_mtime_ns
time.sleep(1.1)  # 确保若误写 mtime 必变
r2 = api._write_daily_holdings(q)
mtime2 = fp.stat().st_mtime_ns

# 内容变化 ⇒ 必须写
q2 = {"source": "live", "quotes": [dict(q["quotes"][0], price=11.5)]}
time.sleep(0.05)
r3 = api._write_daily_holdings(q2)
mtime3 = fp.stat().st_mtime_ns

# force=True 同内容 ⇒ 必须写
time.sleep(1.1)
r4 = api._write_daily_holdings(q2, force=True)
mtime4 = fp.stat().st_mtime_ns

d = json.loads(fp.read_text(encoding="utf-8"))
print("r1", r1)
print("r2", r2, "mtime_unchanged:", mtime2 == mtime1)
print("r3", r3, "mtime_changed:", mtime3 != mtime2)
print("r4", r4, "mtime_changed:", mtime4 != mtime3)
print("file_price:", d["holdings"][0]["price"], "cash:", d["summary"]["cash"])

ok = (r1.get("ok") and not r1.get("skipped")
      and r2.get("skipped") and mtime2 == mtime1
      and not r3.get("skipped") and mtime3 != mtime2
      and not r4.get("skipped") and mtime4 != mtime3
      and d["holdings"][0]["price"] == 11.5)
print("SMOKE", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
