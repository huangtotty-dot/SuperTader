# -*- coding: utf-8 -*-
"""收盘后 EOD 任务簇（2026-11-13 从 main.py 抽出）：每日复盘 / K线预下载 / 新鲜度巡检 / 每日盈亏落库。

四个都是「交易时段后一次性触发」的调度器，各自带当日占位 + 失败重试状态，互不共享引擎
状态。与盯盘热路径的耦合仅剩三处只读引用：`HOLDINGS`（config，被 `_maybe_record_daily_pnl`
原地更新 pre_close）、`DAILY_DECISION_STATS`（core.utils，只读）、`_now`（core.data_fetcher）。
"""
from __future__ import annotations

import json
import os
import sys
import threading as _threading
from datetime import datetime, time as dtime

from config import HOLDINGS, log
from core.data_fetcher import _now
from core.utils import DAILY_DECISION_STATS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_daily_review_state = {"date": "", "next_try": 0.0}
_chart_prefetch_state = {"date": "", "next_try": 0.0}
_freshness_patrol_state = {"date": ""}   # 产物新鲜度巡检（2026-10-12）当日占位
_daily_pnl_push_date = ""  # 模块级：14:59推送防重复


def _maybe_run_daily_review(now: datetime) -> None:
    """15:10 后交易日自动触发每日复盘——子进程 fire-and-forget，内部串行跑
    equity_daily → daily_review → forward_tracker（后者读前者产出）；成功才占位，失败 10 分钟节流重试。
    P0(2026-10-13): 链首补 equity_daily——主基准 sh000300 仅被该脚本刷新（其余四指数
    走 facade 盘中路径），脚本不跑则指数缓存断更 + 净值文件缺日（10-12/10-13 实证断档）。"""
    global _daily_review_state
    try:
        t = now.time()
        if now.weekday() >= 5 or t < dtime(15, 10):
            return
        today = now.strftime("%Y-%m-%d")
        if _daily_review_state.get("date") == today:
            return
        if _now().timestamp() < _daily_review_state.get("next_try", 0):
            return
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_dir = os.path.join(_ROOT, "t_io", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_fp = os.path.join(log_dir, f"daily_review_{today}.log")
        # 子进程内部串行：equity_daily → daily_review → forward_tracker（同一进程不阻塞主循环）
        # T-1(2026-09-02): 内嵌 -c 用换行分隔（此前分号后接 for 是语法错误，子进程启动即 SyntaxError）
        _dr_dir = os.path.join(_ROOT, "t_io", "validation", "daily_review")
        # Q-20260914-4': forward_tracker 回填源改真实复盘路径 doc/review/复盘清单.md（owner 拍板 09-14）。
        # 旧口径 doc/每日复盘/ 整目录不存在 → 每日空转；按方案的①（每日 md）实测无前瞻表，仍会空转。
        # 前瞻表（加仓价/次日浮盈%、确认价/3日浮盈%）现由合并后唯一清单承载，08-28 起取代每日模板。
        _py = (
            "import subprocess, sys, os\n"
            f"dr = {_dr_dir!r}\n"
            f"base = {_ROOT!r}\n"
            f"log = open({log_fp!r}, 'a', encoding='utf-8')\n"
            "date = sys.argv[1]\n"
            "failed = []\n"
            "for sp in ['equity_daily.py', 'daily_review.py', 'forward_tracker.py']:\n"
            "    path = os.path.join(base, 'scripts', sp) if sp == 'equity_daily.py' else os.path.join(dr, sp)\n"
            "    cmd = [sys.executable, path, '--date', date]\n"
            "    if sp == 'forward_tracker.py':\n"
            "        cmd += ['--report', os.path.join(base, 'doc', 'review', '复盘清单.md')]\n"
            "    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)\n"
            "    rc = p.wait(timeout=600)\n"
            "    print('[child] ' + sp + ' exit=' + str(rc), flush=True)\n"
            "    if rc != 0:\n"
            "        failed.append(sp + '(rc=' + str(rc) + ')')\n"
            "if failed:\n"
            "    print('[child] FAILED: ' + ', '.join(failed), flush=True)\n"
            "sys.exit(1 if failed else 0)\n"
        )
        out_fp = open(log_fp, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen([sys.executable, "-c", _py, today],
                                    cwd=_ROOT, creationflags=flags,
                                    stdout=out_fp, stderr=subprocess.STDOUT)
        finally:
            out_fp.close()
        _daily_review_state.update({"date": today, "next_try": 0})

        def _watch_daily_review(pr=proc, _today=today):
            # Q-20260914-3': 退出码守护——非 0 撤销当日占位并 600s 后重试。
            # 原 fire-and-forget 不查退出码：子进程中途崩 → 当日静默断档，次日复盘才发现。
            try:
                rc = pr.wait()
            except Exception as _we:
                rc = -1
                log.warning(f"⚠️ 收盘复盘子进程等待异常: {str(_we)[:100]}")
            if rc != 0:
                log.warning(f"⚠️ 收盘复盘子进程失败(rc={rc})，撤销 {_today} 占位并 600s 后重试 → {log_fp}")
                if _daily_review_state.get("date") == _today:
                    _daily_review_state.update({"date": "", "next_try": _now().timestamp() + 600})

        _threading.Thread(target=_watch_daily_review, daemon=True).start()
        log.info(f"📊 收盘复盘已自动触发: equity_daily+daily_review+forward_tracker → {log_fp} (pid={proc.pid})")
    except Exception as e:
        _daily_review_state["next_try"] = _now().timestamp() + 600
        log.warning(f"⚠️ 收盘复盘自动触发失败（10 分钟重试）: {str(e)[:120]}")


def _maybe_run_chart_prefetch(now: datetime) -> None:
    """15:10 后交易日触发 K线缓存预下载（子进程 fire-and-forget）。

    幂等由 `core.chart_cache.run_prefetch` 的跨进程锁 + 当日标记保证（与 gui 侧互斥）。
    成功才占位，失败 600s 重试。子进程不构建图表 payload（那是 gui 的活）。
    """
    global _chart_prefetch_state
    try:
        t = now.time()
        if now.weekday() >= 5 or t < dtime(15, 10):
            return
        today = now.strftime("%Y-%m-%d")
        if _chart_prefetch_state.get("date") == today:
            return
        if _now().timestamp() < _chart_prefetch_state.get("next_try", 0):
            return
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_dir = os.path.join(_ROOT, "t_io", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_fp = os.path.join(log_dir, f"chart_prefetch_{today}.log")
        _py = (
            "import sys\n"
            f"sys.path.insert(0, {_ROOT!r})\n"
            "from core.chart_cache import run_prefetch\n"
            "res = run_prefetch()\n"
            "print('prefetch:', res, flush=True)\n"
            "# 锁被占（GUI 小池预取/另一个进程）不是成功：退非 0 ⇒ main 的 watcher 600s 后重试。\n"
            "# 旧逻辑 rc=0 被当成跑完，当天全池日线再也不会补（2026-11-11 实测 full 标记一直缺）。\n"
            "if isinstance(res, dict) and res.get('skipped') == 'locked':\n"
            "    sys.exit(3)\n"
        )
        out_fp = open(log_fp, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen([sys.executable, "-c", _py],
                                    cwd=_ROOT, creationflags=flags,
                                    stdout=out_fp, stderr=subprocess.STDOUT)
        finally:
            out_fp.close()
        _chart_prefetch_state.update({"date": today, "next_try": 0})

        def _watch_chart_prefetch(pr=proc, _today=today):
            try:
                rc = pr.wait()
            except Exception:
                rc = -1
            if rc != 0:
                log.warning(f"⚠️ K线预下载子进程失败(rc={rc})，撤销 {_today} 占位并 600s 后重试 → {log_fp}")
                if _chart_prefetch_state.get("date") == _today:
                    _chart_prefetch_state.update({"date": "", "next_try": _now().timestamp() + 600})

        _threading.Thread(target=_watch_chart_prefetch, daemon=True).start()
        log.info(f"📈 K线缓存预下载已触发 → {log_fp} (pid={proc.pid})")
    except Exception as e:
        _chart_prefetch_state["next_try"] = _now().timestamp() + 600
        log.warning(f"⚠️ K线预下载触发失败（10 分钟重试）: {str(e)[:120]}")


def _maybe_run_freshness_patrol(now: datetime) -> None:
    """15:40 后每日一次**产物新鲜度巡检** → 飞书（2026-10-12 owner P1）。

    为什么在 15:40 而不是 15:10：要等 `_maybe_run_chart_prefetch` 把当日日线/分钟线写完，
    否则会把"预下载还没跑完"误报成"陈旧"。
    为什么每类**恰好一条**：owner 说「直接推飞书，同步检查误报率」⇒ 每日一张卡（含全部数字），
    并落历史 JSONL 供事后算误报率；⚠️ 只给"与上一轮相比**新出现**"的陈旧，持续陈旧标"持续"。

    幂等：`core.freshness_patrol.run_once` 用当日标记文件判（跨重启也成立）。
    巡检本体**零网络**，在后台线程跑（读几千个小 JSON，别占主循环）。
    """
    global _freshness_patrol_state
    try:
        t = now.time()
        if now.weekday() >= 5 or t < dtime(15, 40):
            return
        today = now.strftime("%Y-%m-%d")
        if _freshness_patrol_state.get("date") == today:
            return

        def _worker(day: str) -> None:
            try:
                from core.freshness_patrol import run_once
                res = run_once()
                if res.get("skipped"):
                    _freshness_patrol_state.update({"date": day})   # 已跑过也占位，免得每轮都起线程
                    log.info(f"产物新鲜度巡检：{day} 已跑过，跳过")
                elif res.get("error"):
                    log.warning(f"⚠️ 产物新鲜度巡检失败: {res['error']}")
                else:
                    _freshness_patrol_state.update({"date": day})
                    log.info(f"产物新鲜度巡检完成（{day}）pushed={res.get('pushed')} "
                             f"新出现陈旧={res.get('newly_stale')}")
            except Exception as e:                 # 不吞：链路失败必须留痕
                log.error(f"❌ 产物新鲜度巡检链路异常: {type(e).__name__}: {e}")

        _threading.Thread(target=_worker, args=(today,), name="freshness-patrol", daemon=True).start()
    except Exception as e:
        log.warning(f"⚠️ 产物新鲜度巡检触发失败: {str(e)[:120]}")


def _maybe_record_daily_pnl(now: datetime) -> None:
    """V1.29 → 2026-09-11: 14:59 后每日一次，**只落盘** daily_pnl.jsonl（供复盘 A-1）+ 更新收盘 pre_close。
    飞书「收益汇总」推送已按 owner 裁决删除（无效信息）。"""
    global _daily_pnl_push_date
    try:
        t = now.time()
        # P1-2(2026-09-10): 去右界——原 (14:59~15:01) 窗口一旦该段无扫描（如主循环挂死/卡顿恢复晚）
        # 就永久错过当日 daily_pnl；改为 t>=14:59 即可补扫（每日一次去重仍由 _daily_pnl_push_date 保证）。
        if now.weekday() >= 5 or t < dtime(14, 59):
            return
        today = now.strftime("%Y-%m-%d")
        if _daily_pnl_push_date == today:
            return
        _daily_pnl_push_date = today

        if not HOLDINGS:
            return

        stock_records = []  # for JSONL logging
        total_t0_pnl = 0.0
        total_value = 0.0
        total_cost_value = 0.0

        for code, holding in sorted(HOLDINGS.items()):
            name = holding.get("name", code)
            qty = int(holding.get("qty", 0) or 0)
            cost = float(holding.get("cost", 0) or 0)
            pre_close = float(holding.get("pre_close", 0) or 0)

            # 当前价：从扫描缓存取
            dec = DAILY_DECISION_STATS.get(code) or {}
            price = float(dec.get("last_price") or 0) or pre_close

            # 当日浮动盈亏
            day_pnl = (price - pre_close) * qty if pre_close > 0 else 0.0
            day_pct = (price / pre_close - 1) * 100 if pre_close > 0 else 0.0

            # 持仓市值
            mkt_val = price * qty
            cost_val = cost * qty
            total_value += mkt_val
            total_cost_value += cost_val

            # manual 做T 实盈已随 manual 做T 下线（2026-09-14）删除——VIRTUAL_TRADES 台账不再存在。
            # （做T口径已于 2026-10-12 随手动做T退场移除：闭环审计/连亏熔断/做T费率工具全部删除）
            # 2026-09-15 阶段0-1 起 est_pnl 费用按真实双边分腿（卖出腿 0.00121 / 买入腿 0.00015，owner 2026-09-14 裁决）。
            t0_pnl = 0.0

            stock_records.append({
                "code": code, "name": name, "qty": qty, "price": round(price, 2),
                "day_pnl": round(day_pnl, 2), "day_pct": round(day_pct, 2),
                "mkt_val": round(mkt_val, 0), "t0_pnl": t0_pnl,
            })

        total_day_float = sum(
            (float(dec.get("last_price") or h.get("pre_close", 0) or 0) - float(h.get("pre_close", 0) or 0))
            * int(h.get("qty", 0) or 0)
            for code, h in HOLDINGS.items()
            for dec in [DAILY_DECISION_STATS.get(code) or {}]
        )

        # 2026-09-11 owner 裁决：删除 14:59「收益汇总」飞书推送（属无效信息）；
        # 保留下方 daily_pnl.jsonl 落盘（复盘 A-1 依赖）与收盘 pre_close 更新。

        # V1.29: 写 JSONL 日志供复盘
        _pnl_log_dir = os.path.join(_ROOT, "t_io", "logs")
        os.makedirs(_pnl_log_dir, exist_ok=True)
        _pnl_log_path = os.path.join(_pnl_log_dir, "daily_pnl.jsonl")
        _pnl_record = {
            "date": today,
            "pushed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost_value, 2),
            "total_pnl": round(total_value - total_cost_value, 2),
            "total_pnl_pct": round((total_value - total_cost_value) / total_cost_value * 100 if total_cost_value > 0 else 0.0, 2),
            "day_pnl_float": round(total_day_float, 2),
            "t0_realized": round(total_t0_pnl, 2),
            "stocks": stock_records,
        }
        with open(_pnl_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_pnl_record, ensure_ascii=False) + "\n")
        log.info(f"📝 收益汇总已写入日志: {_pnl_log_path}")

        # V1.30: 收盘自动更新持仓真源 pre_close 为当日收盘价
        # （2026-10-04 拆分后：一次写全所有副本——手动/自动两文件都更新，防某侧 pre_close 冻结）
        _prices = {}
        for code, dec in (DAILY_DECISION_STATS or {}).items():
            try:
                _cp = float((dec or {}).get("last_price") or 0)
            except (TypeError, ValueError):
                _cp = 0.0
            if _cp > 0:
                _prices[code] = _cp
                # Q-20260914-1: 同步内存 HOLDINGS，消除"磁盘新/内存旧"被后续整写回滚的源头
                if code in (HOLDINGS or {}):
                    HOLDINGS[code]["pre_close"] = _cp
        if _prices:
            try:
                from core.holdings_repo import save_pre_close  # P0-6: 改走 repo 强制审计
                _n = save_pre_close(_prices, actor="main", reason="eod_pre_close")
                log.info(f"📝 pre_close 已更新为当日收盘价（{_n} 票，审计留痕）")
            except Exception as e:
                log.warning(f"⚠️ pre_close 更新失败: {str(e)[:80]}")

    except Exception as e:
        log.warning(f"⚠️ 收益汇总推送异常（已吞掉，不影响主循环）: {str(e)[:120]}")
