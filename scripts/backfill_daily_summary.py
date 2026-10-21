"""按**当前池子**重算最近 N 个交易日的 daily_summary（2026-10-21）。

owner：「板块星空图前 9 日没有相应的更新」。星空图读 `stock_hunter/history/daily_summary.json`
的逐日快照，而新加的板块那天之前根本没有条目 ⇒ 轨迹只有一个点。
`t_gui._load_hunter_impl(date)` 正是"按任意日期重跑猎手并落盘当日汇总"的入口
（内部 `save_daily_summary`），盘后跑 ≈20s/天（行情走 `market_{date}.csv` +
本地日线缓存补缺，零网络）。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import t_gui
from datetime import datetime

N = int(sys.argv[1]) if len(sys.argv) > 1 else 12
idx = json.load(open(os.path.join(r"E:\superTrader", "t_io", "cache", "daily_kline",
                                  "index_sh000001.json"), encoding="utf-8"))["rows"]
dates = [str(r["date"])[:10] for r in idx][-N:]
api = t_gui.Api()
for d in dates:
    t = time.time()
    try:
        r = api._load_hunter_impl(d)
        rows = r.get("summary_rows") or []
        top = [(x.get("板块"), x.get("排名")) for x in rows[:3]]
        print(f"{d} {time.time()-t:5.1f}s rows={len(rows)} top3={top}", flush=True)
    except Exception as e:
        print(f"{d} ERR {type(e).__name__}: {e}", flush=True)
