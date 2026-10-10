"""复现「打开 GUI 冷启动爆发」：清空缓存 + 并发跑前端首帧扇出，看首帧要多久。

对照两组：
  A) 只跑首帧扇出（模拟缓存冷）
  B) 首帧扇出 + 同时起 main 里那 8 个预热线程（真实打开时的样子）
用法：python tmp/gui_profile_coldstart.py [A|B]
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, BASE)

import t_gui  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")

# 前端 loadAndRender(today) 首帧实际打的后端（见 web/app.js:185-227）
FIRST_FRAME = [
    "load_day",
    "load_ob_analysis",
    "load_quotes",
    "load_position_manager",
    "load_market_score",
    "load_auto_pnl",
    "load_auto_status",
    "load_buy_confirm_pending",
    "load_live",
    "load_indices",
    "load_market_turnover",
    "load_turnover_history",
    "load_index_divergence",
    "load_console",
    "poll_new_position_signals",
    "available_dates",
]

results = {}
lock = threading.Lock()


def run_one(api, name):
    fn = getattr(api, name, None)
    if fn is None:
        return
    t0 = time.perf_counter()
    try:
        fn(TODAY)
    except TypeError:
        try:
            fn()
        except Exception as e:
            with lock:
                results[name] = (time.perf_counter() - t0, f"ERR {type(e).__name__}")
            return
    except Exception as e:
        with lock:
            results[name] = (time.perf_counter() - t0, f"ERR {type(e).__name__}")
        return
    with lock:
        results[name] = (time.perf_counter() - t0, "ok")


def prewarm_storm(api):
    """复刻 t_gui.py __main__ 里的后台预热（8 个重活线程）。"""
    def _imports():
        for m in ("core.position_builder", "core.timing_gate", "core.chart_cache",
                  "core.market_data", "analysis.indicators", "analysis.m30_features",
                  "analysis.divergence", "analysis.trend30.adapter"):
            try:
                __import__(m)
            except Exception:
                pass
    for target in (_imports,
                   getattr(api, "prewarm_holdings_charts", lambda: None),
                   getattr(api, "prewarm_stock_tags", lambda: None),
                   getattr(api, "prewarm_overview", lambda: None),
                   lambda: api.compute_add_watch(TODAY)):
        threading.Thread(target=target, daemon=True).start()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "A"
    api = t_gui.Api()
    # 强制冷缓存
    t_gui.Api._turnover_cache.clear()
    t_gui.Api._turnover_hist_cache.clear()
    t_gui.Api._em_cache.clear()
    if hasattr(t_gui.Api, "_indices_cache"):
        t_gui.Api._indices_cache.clear()
    if hasattr(api, "_dates_cache"):
        api._dates_cache = None
    # "S" 后缀 = 删掉磁盘快照（模拟「首次运行，从没成功过」）
    if "S" in mode:
        import shutil as _sh
        _dir = getattr(t_gui, "_OVERVIEW_SNAPSHOT_DIR", None)
        if _dir is not None:
            _sh.rmtree(_dir, ignore_errors=True)
            print("[已删除磁盘快照 → 模拟首次运行]")
        else:
            print("[旧代码无快照层，忽略 S]")

    if mode == "B":
        prewarm_storm(api)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=run_one, args=(api, n), daemon=True)
               for n in FIRST_FRAME]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    total = time.perf_counter() - t0

    print(f"\n===== 模式 {mode}：首帧并发扇出完成耗时 {total:.2f}s =====")
    for n, (d, st) in sorted(results.items(), key=lambda kv: -kv[1][0]):
        print(f"  {n:<30}{d * 1000:8.0f}ms  {st}")
    print(f"  活线程数={threading.active_count()}")
    import collections as _c
    nm = _c.Counter()
    for th in threading.enumerate():
        n = th.name
        for pre in ("ThreadPoolExecutor-", "gm-call", "ovw-refresh-", "quotes-refresh",
                    "add-watch", "ob-analysis", "import-prewarm", "gm-health-probe"):
            if n.startswith(pre):
                n = pre + "*"
                break
        nm[n] += 1
    print("  线程构成:", dict(nm.most_common(14)))


if __name__ == "__main__":
    main()
