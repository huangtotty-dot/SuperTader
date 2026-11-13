# -*- coding: utf-8 -*-
"""回放 / 回测 CLI（2026-11-13 自 main.py 抽出）。

`replay_today`（--replay-today）与 `tushare_replay`（--tushare-replay）是**纯 CLI 路径**，
不参与实盘主循环。它们共同点：就地改共享单例（HOLDINGS / MINUTE_FETCH_STATUS 等 .clear/.update，
保对象同一性），并读/写回放时钟 `SIM_NOW`。

⚠️ `SIM_NOW` 仅驱动回放**显示**，不驱动引擎时钟——引擎时钟走 `EngineContext.now=_now`
（见 [[main-sim-now-vestigial]]）。它随 replay 函数一起迁到本模块当模块全局。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd

from config import HOLDINGS, SNAPSHOT_DIR, SIGNAL_OUTCOME_TRACKER, TRACE_DIR, log
from core.utils import (
    AI_REVIEW_STATS, DAILY_DECISION_STATS, _default_daily_context,
    _ensure_ai_review_stats, _ensure_daily_decision_stats,
)
from core.data_fetcher import (
    MINUTE_FETCH_DETAIL, MINUTE_FETCH_STATUS, _now, get_today_str, load_holdings,
)
from core.signal_engine import SignalEngine
from analysis.indicators import add_indicators
from core.engine_ctx import make_engine_ctx


SIM_NOW = None


def replay_today():
    today = get_today_str()
    snapshot_files = []
    snapshot_days = set()
    for root, _, files in os.walk(SNAPSHOT_DIR):
        for name in files:
            if not name.endswith(".json") or "_" not in name:
                continue
            day_part = name.rsplit("_", 1)[-1].removesuffix(".json")
            snapshot_days.add(day_part)
            if day_part == today:
                snapshot_files.append(os.path.join(root, name))
    if not snapshot_files:
        if not snapshot_days:
            log.info(f"未找到当日快照: {today}")
            return
        today = sorted(snapshot_days)[-1]
        snapshot_files = []
        for root, _, files in os.walk(SNAPSHOT_DIR):
            for name in files:
                if name.endswith(f"_{today}.json"):
                    snapshot_files.append(os.path.join(root, name))
        log.info(f"未找到今日快照，改用最近快照日: {today}")

    HOLDINGS_LOCAL = load_holdings()
    stats = {"total": 0, "buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0, "by_code": {}}
    global SIM_NOW
    prev_sim_now = SIM_NOW
    try:
        for path in sorted(snapshot_files):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except Exception as e:
                log.warning(f"⚠️  快照加载异常: {str(e)[:120]}")
                continue

            code = str(snap.get("code", "")).strip()
            if not code:
                continue
            bars = snap.get("bars", []) if isinstance(snap, dict) else []
            if not bars:
                continue

            holding = HOLDINGS_LOCAL.get(code, {"name": snap.get("name", code), "t_qty": 0, "qty": 0, "type": "stock", "cost": 0})
            state = {
                "name": snap.get("name", code),
                "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
                "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
                "type": holding.get("type", "stock"),
                "cost": float(holding.get("cost") or 0),
            }

            engine_local = SignalEngine(make_engine_ctx())
            engine_local.state_reset_date = today
            engine_local.buy_count_per_stock[code] = 0
            engine_local.sell_count_per_stock[code] = 0
            got_buy = False
            got_sell = False
            code_stats = {"buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0}
            stats["total"] += 1
            MINUTE_FETCH_STATUS[code] = "ok"

            for i in range(25, len(bars) + 1):
                df = pd.DataFrame(bars[:i])
                if df.empty:
                    continue
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)
                if df.empty or len(df) < 25:
                    continue

                try:
                    current_time = df.iloc[-1]["time"]
                    if hasattr(current_time, "to_pydatetime"):
                        SIM_NOW = current_time.to_pydatetime()
                    elif isinstance(current_time, datetime):
                        SIM_NOW = current_time
                    daily_ctx = snap.get("daily_context") if isinstance(snap, dict) else None
                    if not isinstance(daily_ctx, dict):
                        daily_ctx = _default_daily_context(code, status="replay_missing", reason="snapshot missing daily_context")
                    # 2026-09-22：_attach_dynamic_t_decision 已随做T引擎删除。
                    buy_score, sell_score, sig = engine_local.evaluate(code, snap.get("name", code), add_indicators(df), state, daily_ctx=daily_ctx)
                except Exception:
                    continue

                if sig and sig.action in ["BUY_LOW", "ADD_POS"]:
                    got_buy = True
                    stats["buy_ok"] += 1
                    code_stats["buy_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                elif sig and sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    got_sell = True
                    stats["sell_ok"] += 1
                    code_stats["sell_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                else:
                    diag = getattr(engine_local, "diagnostics", {}).get(code, {}) if isinstance(getattr(engine_local, "diagnostics", None), dict) else {}
                    if diag.get("buy_candidate_preheat") and sig is None:
                        stats["buy_candidate_preheat_rejected"] += 1
                        code_stats["buy_candidate_preheat_rejected"] += 1
                    if diag.get("buy_candidate") and sig is None:
                        stats["buy_candidate_but_rejected"] += 1
                        code_stats["buy_candidate_but_rejected"] += 1
                        for reason in diag.get("buy_block_reasons", []) or ["unknown"]:
                            stats["buy_block_by_reason"][reason] = stats["buy_block_by_reason"].get(reason, 0) + 1
                            code_stats["buy_block_by_reason"][reason] = code_stats["buy_block_by_reason"].get(reason, 0) + 1
                    if diag.get("sell_candidate") and sig is None:
                        for reason in diag.get("sell_block_reasons", []) or ["unknown"]:
                            stats["sell_block_by_reason"][reason] = stats["sell_block_by_reason"].get(reason, 0) + 1
                            code_stats["sell_block_by_reason"][reason] = code_stats["sell_block_by_reason"].get(reason, 0) + 1
                    if diag.get("preempted_by_sell_fast_path"):
                        stats["preempt_by_sell_fast_path"] += 1
                        code_stats["preempt_by_sell_fast_path"] += 1

            if not got_buy:
                stats["buy_blocked"] += 1
                code_stats["buy_blocked"] += 1
            if not got_sell:
                stats["sell_blocked"] += 1
                code_stats["sell_blocked"] += 1
            stats["by_code"][code] = code_stats
    finally:
        SIM_NOW = prev_sim_now

    out = os.path.join(TRACE_DIR, f"replay_compare_{today}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}, f, ensure_ascii=False, indent=2)
    log.info(f"回放完成: {out}")
    log.info(f"总快照={stats['total']} 买触发={stats['buy_ok']} 卖触发={stats['sell_ok']} 卖后可买回={stats['rebuild_buy_ok']} 买被挡={stats['buy_blocked']} 卖被挡={stats['sell_blocked']} 买候选预热未成={stats['buy_candidate_preheat_rejected']} 买候选未成交={stats['buy_candidate_but_rejected']} 卖快路径抢占={stats['preempt_by_sell_fast_path']}")
    if stats["buy_block_by_reason"]:
        log.info("买阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["buy_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats["sell_block_by_reason"]:
        log.info("卖阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["sell_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats.get("by_code"):
        try:
            with open(out, "r", encoding="utf-8") as f:
                replay_doc = json.load(f)
        except Exception:
            replay_doc = {"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}
        replay_doc["stats"]["by_code"] = stats["by_code"]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(replay_doc, f, ensure_ascii=False, indent=2)


def tushare_replay(date_str=None):
    """
    V1.19: 使用 Tushare 分钟数据复测今日表现（可指定历史日期）
    仅输出会触发飞书通知的信号（score >= notify_threshold）
    """
    import tushare as ts
    token = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
    ts.set_token(token)
    pro = ts.pro_api()

    global SIM_NOW, HOLDINGS, MINUTE_FETCH_STATUS, MINUTE_FETCH_DETAIL
    global DAILY_DECISION_STATS, AI_REVIEW_STATS, SIGNAL_OUTCOME_TRACKER

    today = date_str or get_today_str()
    # 原地更新，保持 config.HOLDINGS、EngineContext 与 daily_sentiment 的对象同一性。
    HOLDINGS.clear()
    HOLDINGS.update(load_holdings())
    holdings = HOLDINGS

    results = []
    print(f"[replay] 日期={today} 持仓={len(holdings)} 只")

    for code, holding in holdings.items():
        # 转换代码为 tushare 格式（去除 _A/_B 等账户后缀）
        api_code = code.split("_")[0] if "_" in code else code
        if api_code.startswith(("6", "9", "5")):
            ts_code = f"{api_code}.SH"
        else:
            ts_code = f"{api_code}.SZ"

        print(f"[replay] {code} {holding.get('name', '')} 拉取 {ts_code} 分钟线...")
        try:
            df = pro.stk_mins(ts_code=ts_code, freq='1min',
                              start_date=f"{today} 09:00:00",
                              end_date=f"{today} 19:00:00")
            if df is None or df.empty:
                print(f"[WARN] {code} 无分钟数据")
                continue
        except Exception as e:
            print(f"[WARN] {code} 获取失败: {e}")
            continue
        print(f"[replay] {code} 分钟线 {len(df)} 根，开始逐分钟模拟...")
        
        # 转换列名
        df = df.rename(columns={
            'trade_time': 'time',
            'vol': 'volume',
            'amount': 'amount'
        })
        df['time'] = pd.to_datetime(df['time'])
        df['date'] = df['time'].dt.date
        df = df.sort_values('time').reset_index(drop=True)
        
        # 确保列类型正确
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 添加指标
        df = add_indicators(df)
        
        # 模拟状态
        state = {
            "name": holding.get("name", code),
            "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
            "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
            "type": holding.get("type", "stock"),
            "cost": float(holding.get("cost") or 0),
        }
        
        # 初始化引擎
        engine = SignalEngine(make_engine_ctx())
        engine.state_reset_date = today
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0

        # 初始化统计
        DAILY_DECISION_STATS[code] = _ensure_daily_decision_stats(code, holding)
        AI_REVIEW_STATS[code] = _ensure_ai_review_stats(code, holding)
        
        # 模拟逐分钟
        for i in range(25, len(df) + 1):
            sub_df = df.iloc[:i].copy()
            if len(sub_df) < 25:
                continue
            
            # 设置模拟时间
            current_time = sub_df.iloc[-1]["time"]
            if hasattr(current_time, "to_pydatetime"):
                SIM_NOW = current_time.to_pydatetime()
            else:
                SIM_NOW = current_time
            
            t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
            
            # 设置分钟状态
            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = "tushare"
            
            # 获取 daily_ctx（简单版）
            daily_ctx = _default_daily_context(code)
            # 回测不调 _attach_dynamic_t_decision：它内部会拉日线特征做网络调用，本机可能挂起
            # manual 做T 下线（2026-09-14）：T_MODE 全局已删除，回测固定注入 long（正T）。
            daily_ctx["t_mode"] = "long"
            daily_ctx["effective_t_mode"] = "long"
            daily_ctx["t_mode_source"] = "replay_static"
            daily_ctx["t_pos_factor"] = 1.0
            daily_ctx["t_trade_gate"] = "normal"
            daily_ctx["t_reason"] = "replay静态T模式"

            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding.get("name", code), sub_df, state, daily_ctx=daily_ctx
                )
            except Exception as e:
                print(f"[WARN] {code} {SIM_NOW.strftime('%H:%M')} evaluate 失败: {e}")
                continue
            
            if sig and sig.action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
                # 计算 notify_threshold
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    notify_threshold = 68
                else:
                    if t_val >= 1000:
                        notify_threshold = 65
                    else:
                        notify_threshold = 75
                
                result = {
                    "time": SIM_NOW.strftime("%H:%M:%S"),
                    "code": code,
                    "name": holding.get("name", code),
                    "action": sig.action,
                    "score": sig.score,
                    "price": sig.price,
                    "reasons": sig.reasons,
                    "notify": sig.score >= notify_threshold,
                    "vwap": float(sub_df.iloc[-1]["vwap"]) if "vwap" in sub_df.columns else sig.price,
                }
                results.append(result)
                
                if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    engine.record_trade_action(code, sig.action, sig.hold_qty)
                elif sig.action in ["BUY_LOW", "ADD_POS"]:
                    engine.record_trade_action(code, sig.action, sig.hold_qty)
    
    # 生成报告
    report_lines = []
    report_lines.append(f"# Tushare 复测报告 ({today})")
    # V1.2.1 (2026-08-11 01:11 用户拍板): 取消 sizing 冻结——手动跟单场景信号达标即推送
    # （卖出保底100股 / 放开满仓买建议 / 底仓地板默认关 sell_floor_enabled=False；纯底仓 t_qty=0 与大盘熔断/防守风控保留）
    # 补充（同日拍板覆盖引擎层）：signal_engine sell_floor_protect 闸接入同一 sell_floor_enabled 开关，
    # 默认放开（原地板压制 tick 自此可出卖信号；轮次上限 max_sell_times 保留不动）
    report_lines.append(f"## 版本: V1.2.1")
    report_lines.append(f"")
    report_lines.append(f"## 总信号统计")
    report_lines.append(f"- 总信号数: {len(results)}")
    report_lines.append(f"- 飞书通知信号数: {sum(1 for r in results if r['notify'])}")
    report_lines.append(f"")
    
    # 按代码分组
    notify_results = [r for r in results if r["notify"]]
    by_code = {}
    for r in notify_results:
        by_code.setdefault(r["code"], []).append(r)
    
    for code in sorted(by_code.keys()):
        items = by_code[code]
        report_lines.append(f"## {items[0]['name']} ({code})")
        report_lines.append(f"")
        for item in items:
            action_cn = {"BUY_LOW": "🟢 低吸", "ADD_POS": "🟢 加仓", "SELL_HIGH": "🔴 高抛", "PANIC_SELL": "🔴 恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"### {item['time']} {action_cn}")
            report_lines.append(f"- 价格: {item['price']:.2f}")
            report_lines.append(f"- 得分: {item['score']:.0f}")
            report_lines.append(f"- VWAP: {item['vwap']:.2f}")
            report_lines.append(f"- 原因: {', '.join(item['reasons'][:5])}")
            report_lines.append(f"")
        report_lines.append(f"---")
        report_lines.append(f"")
    
    # 非通知信号（简要）
    non_notify = [r for r in results if not r["notify"]]
    if non_notify:
        report_lines.append(f"## 未达通知阈值信号（简要）")
        report_lines.append(f"")
        for item in non_notify[:20]:
            action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"- {item['time']} {item['name']} {action_cn} 得分{item['score']:.0f} (阈值未达)")
        report_lines.append(f"")
    
    report_text = "\n".join(report_lines)
    report_path = os.path.join(TRACE_DIR, f"tushare_replay_report_{today}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"\n{'='*60}")
    print(f"报告已保存: {report_path}")
    print(f"总信号: {len(results)} | 飞书通知: {len(notify_results)}")
    print(f"{'='*60}")
    
    # 打印飞书命令摘要
    print(f"\n【飞书通知摘要】")
    for item in notify_results:
        action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
        print(f"{item['time']} {item['name']}({item['code']}) {action_cn} 得分{item['score']:.0f} 价格{item['price']:.2f}")
    
    return report_path
