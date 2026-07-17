# -*- coding: utf-8 -*-
"""
扫描引擎模块
"""
import os
import sys
import time
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple, List, Dict

import pandas as pd

import config
from config import (
    log, SCAN_WORKERS, ALERT_MAX_SIGNALS, QT_SNAPSHOT_ENABLED,
    FEISHU_ENABLED, FEISHU_WEBHOOK, VERBOSE_SCAN_LOG, system_alert,
    PRIORITY_ORDER, SIGNAL_EMOJI, LOG_DIR, today_str,
    WEEKLY_AMOUNT_MIN, WEEKLY_AMOUNT_MAX, DAILY_AMOUNT_MIN, DAILY_AMOUNT_MAX,
    EXCLUDED_SECTORS
)

from utils import (
    save_scan_results, group_signals_by_sector, sector_summary_line,
    truncate_reason, format_sector_chain, signal_priority_label,
    current_amount_range_text, summary_brief
)
from stock_pool import (
    load_a_share_pool, is_st_stock, is_a_share_code,
    resolve_stock_concept, resolve_business_summary, clear_cache, _normalize_sector_text
)
from data_fetcher import (
    fetch_data, fetch_weekly_context, ensure_amount_column,
    pick_target_row, apply_qt_snapshot_amount, fetch_qt_snapshot_map,
    load_cache, save_cache, log_amount_check
)
from weekly_strategies import check_weekly_strategies, normalize_weekly_target, sector_top3_brief
from signal_detector import check_strategies, detect_limit_up_board
from feishu import send_to_feishu
from regression import validate_trading_date

def merge_duplicate_signals(signals: List[Dict]) -> List[Dict]:
    """合并同一只股票的多个策略信号
    将同一只股票的多个策略合并为一条，策略优先级按PRIORITY_ORDER排序
    """
    if not signals:
        return []

    signal_map = {}
    for signal in signals:
        code = signal.get('code', '')
        if code not in signal_map:
            signal_map[code] = signal
        else:
            # 比较策略优先级，保留优先级更高的（数值更小）
            existing = signal_map[code]
            existing_priority = PRIORITY_ORDER.get(existing.get('type', ''), 99)
            new_priority = PRIORITY_ORDER.get(signal.get('type', ''), 99)

            if new_priority < existing_priority:
                signal_map[code] = signal
            elif new_priority == existing_priority:
                # 优先级相同，需要合并策略描述
                existing_type = existing.get('type', '')
                new_type = signal.get('type', '')
                if existing_type != new_type:
                    # 将新策略追加到reason中
                    existing_reason = existing.get('reason', '')
                    new_reason = signal.get('reason', '')
                    combined_reason = f"{existing_reason} | 同时触发: {new_reason}"
                    existing['reason'] = truncate_reason(combined_reason, limit=120)
                    existing['combined_types'] = f"{existing_type}、{new_type}"

    return list(signal_map.values())


def print_limit_up_signals(signals: List[Dict]) -> None:
    """单独打印涨停板信号"""
    limit_up_signals = [s for s in signals if s.get('is_limit_up', False)]
    if not limit_up_signals:
        return

    print("\n" + "━" * 60)
    print("🔴 涨停板信号")
    print("━" * 60)
    for idx, s in enumerate(limit_up_signals, 1):
        consecutive = s.get('consecutive_limit_up_days', 0)
        board_tag = f"({consecutive}连板)" if consecutive > 1 else "(首板)"
        amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
        print(f"  {idx}. {s['name']} ({s['code']}) {board_tag} | 现价:{s['price']:.2f} | {s.get('limit_up_tag', '')} | 策略:{s['type']}")
        print(f"     {truncate_reason(s['reason'])}")
    print("━" * 60)


def run_scan():
    # 全局变量通过 config 模块访问和修改
    scan_mode = os.getenv("SCAN_MODE", "daily").strip().lower()
    if scan_mode == "monthly_stats":
        scan_mode = "monthly_gain"
    scan_light = os.getenv("SCAN_LIGHT", "0").strip() == "1"
    scan_test_mode = os.getenv("SCAN_TEST_MODE", "0").strip() == "1"
    scan_limit_env = os.getenv("SCAN_LIMIT", "").strip()
    scan_limit = 0
    if scan_test_mode and scan_limit_env:
        try:
            scan_limit = max(1, int(scan_limit_env))
        except ValueError:
            scan_limit = 0
    elif scan_limit_env and not scan_test_mode:
        log.debug(f"⚠️  已忽略 SCAN_LIMIT={scan_limit_env}，因为未开启测试模式")
    if scan_light and scan_mode == "weekly":
        scan_mode = "weekly_light"
    env_date = os.getenv("SCAN_DATE", "").strip()
    if scan_mode == "monthly_gain" or os.getenv("MONTHLY_STATS_ONLY", "0").strip() == "1":
        print("📊 已进入月度涨幅统计模式")
        return run_monthly_gain_ranking()
    is_weekly = scan_mode.startswith("weekly")
    if is_weekly:
        weekly_amount_min_env = os.getenv("WEEKLY_AMOUNT_MIN", "").strip()
        weekly_amount_max_env = os.getenv("WEEKLY_AMOUNT_MAX", "").strip()
        if weekly_amount_min_env:
            try:
                config.WEEKLY_AMOUNT_MIN = float(weekly_amount_min_env)
            except ValueError:
                pass
        if weekly_amount_max_env:
            try:
                config.WEEKLY_AMOUNT_MAX = float(weekly_amount_max_env)
            except ValueError:
                pass
        print("📌 周线模式：按周均成交额过滤")
    if not is_weekly:
        # 日线模式：手动输入成交额区间
        pipeline_mode = os.getenv("PIPELINE_MODE", "0").strip() == "1"
        daily_amount_min_env = os.getenv("DAILY_AMOUNT_MIN", "").strip()
        daily_amount_max_env = os.getenv("DAILY_AMOUNT_MAX", "").strip()
        if daily_amount_min_env:
            try:
                config.DAILY_AMOUNT_MIN = float(daily_amount_min_env)
            except ValueError:
                pass
        if daily_amount_max_env:
            try:
                config.DAILY_AMOUNT_MAX = float(daily_amount_max_env)
            except ValueError:
                pass
        elif not pipeline_mode:
            try:
                min_input = input("请输入日线模式的最小成交额（默认4000000000，直接回车则使用默认）: ").strip()
            except EOFError:
                min_input = ""
            if min_input:
                try:
                    config.DAILY_AMOUNT_MIN = float(min_input)
                except ValueError:
                    print("⚠️  最小值输入无效，继续使用默认值")
            try:
                max_input = input("请输入日线模式的最大成交额（默认无限制，直接回车则使用默认）: ").strip()
            except EOFError:
                max_input = ""
            if max_input:
                try:
                    config.DAILY_AMOUNT_MAX = float(max_input)
                except ValueError:
                    print("⚠️  最大值输入无效，继续使用默认值")
        max_text = "∞" if DAILY_AMOUNT_MAX == float('inf') else f"{DAILY_AMOUNT_MAX:.0f}"
        print(f"📌 日线模式：成交额区间 {DAILY_AMOUNT_MIN:.0f} ~ {max_text} 的标的将被保留")

        # 策略配置已通过菜单设置，不再询问
        enable_momentum_strategies = True
        print(f"📌 策略已启用（通过配置菜单）")
    else:
        enable_momentum_strategies = True
    if is_weekly:
        print("📌 周线模式：按周均成交额过滤")

    scan_stats = {
        "amount_filtered": 0,
        "amount_soft_warned": 0,
        "daily_amount_filtered": 0,
        "data_failed": 0,
        "weekly_context_missing": 0,
        "strategy_miss": 0,
        "concept_filtered": 0,
    }

    def print_scan_stats():
        if is_weekly:
            print(
                f"📊 周线统计：周均过滤 {scan_stats['amount_filtered']} | 软提示 {scan_stats['amount_soft_warned']} | 上下文缺失 {scan_stats['weekly_context_missing']} | "
                f"数据失败 {scan_stats['data_failed']} | 策略未命中 {scan_stats['strategy_miss']} | 概念剔除 {scan_stats['concept_filtered']}"
            )
        else:
            print(
                f"📊 日线统计：成交额过滤 {scan_stats['daily_amount_filtered']} | 数据失败 {scan_stats['data_failed']} | "
                f"策略未命中 {scan_stats['strategy_miss']} | 概念剔除 {scan_stats['concept_filtered']}"
            )

    def scan_one(code: str, info: Dict[str, str], target_date: str):
        try:
            if is_weekly:
                df, weekly_total_amount, weekly_avg_amount, target_snapshot_amount, weekly_total_source, weekly_confidence = fetch_weekly_context(code, target_date)
                if df.empty:
                    scan_stats["weekly_context_missing"] += 1
                    return None, None, None, None
                df = ensure_amount_column(df, code=code)
                target_row, _ = pick_target_row(df, target_date, code=code)
                if target_row.empty:
                    scan_stats["weekly_context_missing"] += 1
                    return None, None, None, None
                row = target_row.iloc[-1]
                raw_amount = float(row.get("amount_raw", 0) or 0)
                normalized_amount = float(row.get("amount", 0) or 0)
                amount_source = str(row.get("amount_source", row.get("fetch_source", weekly_total_source or "unknown")) or "unknown")
                amount_for_filter = float(weekly_avg_amount or 0)
                target_amount = float(weekly_total_amount or 0)
                if raw_amount > 0:
                    log.debug(f"[金额单位][weekly] {code} {info.get('name', code)} | source={amount_source} | ratio={normalized_amount / raw_amount:.2f}x")
                log_amount_check(code, info.get("name", code), amount_for_filter, amount_for_filter, WEEKLY_AMOUNT_MIN, WEEKLY_AMOUNT_MAX, scan_mode="weekly")
                hard_filter = weekly_confidence == "high" or amount_source == "qt_snapshot" or weekly_total_source == "qt_snapshot"
                if hard_filter and (amount_for_filter < WEEKLY_AMOUNT_MIN or amount_for_filter > WEEKLY_AMOUNT_MAX):
                    scan_stats["amount_filtered"] += 1
                    return None, None, None, None
                if not hard_filter and (amount_for_filter < WEEKLY_AMOUNT_MIN or amount_for_filter > WEEKLY_AMOUNT_MAX):
                    scan_stats["amount_soft_warned"] += 1
                target_week = normalize_weekly_target(target_date)
                sig_type, reason = check_weekly_strategies(df, target_week, target_amount=target_amount)
            else:
                df = fetch_data(code, target_date, scan_mode=scan_mode)
                if df.empty:
                    scan_stats["data_failed"] += 1
                    return None, None, None, None
                df = ensure_amount_column(df, code=code)
                target_row, target_date_str = pick_target_row(df, target_date, code=code)
                if target_row.empty:
                    scan_stats["data_failed"] += 1
                    return None, None, None, None
                row = target_row.iloc[-1]
                raw_amount = float(row.get("amount_raw", 0) or 0)
                normalized_amount = float(row.get("amount", 0) or 0)
                amount_source = str(row.get("amount_source", row.get("fetch_source", "unknown")) or "unknown")
                if raw_amount > 0:
                    log.debug(f"[金额单位][daily] {code} {info.get('name', code)} | source={amount_source} | ratio={normalized_amount / raw_amount:.2f}x")
                log_amount_check(code, info.get("name", code), raw_amount, normalized_amount, DAILY_AMOUNT_MIN, DAILY_AMOUNT_MAX, scan_mode="daily")

                # 日线严格过滤：不在你输入的成交额区间内直接剔除
                if normalized_amount < DAILY_AMOUNT_MIN or normalized_amount > DAILY_AMOUNT_MAX:
                    scan_stats["daily_amount_filtered"] += 1
                    return None, None, None, None

                target_amount = normalized_amount
                sig_type, reason = check_strategies(df, enable_momentum_strategies=enable_momentum_strategies)

            # 【修复】确保变量初始化 - 日线分支缺失的变量
            if not is_weekly:
                target_snapshot_amount = target_amount
                weekly_total_amount = None
                weekly_avg_amount = None
                weekly_total_source = "daily"
                weekly_confidence = "low"

            scan_date_local = str(df.iloc[-1]['date'])[:10]
            signal = None
            if sig_type:
                concept = resolve_stock_concept(code, info.get("sector", "未知板块"))
                if _normalize_sector_text(concept) not in {_normalize_sector_text(item) for item in EXCLUDED_SECTORS}:
                    # 计算当日涨幅
                    today_close = float(df.iloc[-1]['close'])
                    yesterday_close = float(df.iloc[-2]['close']) if len(df) >= 2 else today_close
                    daily_pct_change = ((today_close - yesterday_close) / yesterday_close * 100) if yesterday_close > 0 else 0.0

                    # 检测涨停板
                    is_limit_up, consecutive_days = detect_limit_up_board(df, code=code)
                    limit_up_tag = ""
                    if is_limit_up:
                        limit_up_tag = f"🔴{consecutive_days}连板"
                        if VERBOSE_SCAN_LOG:
                            log.debug(f"✓ {code} 检测到涨停板: {consecutive_days}连板")

                    signal = {
                        "name": info["name"],
                        "code": code,
                        "sector": concept,
                        "business_summary": resolve_business_summary(info),
                        "price": today_close,
                        "daily_pct_change": daily_pct_change,
                        "amount": float(target_snapshot_amount or target_amount or 0),
                        "week_total_amount": float(weekly_total_amount or 0),
                        "week_avg_amount": float(weekly_avg_amount or 0),
                        "week_total_source": str(weekly_total_source or "unknown"),
                        "week_confidence": str(weekly_confidence or "low"),
                        "amount_source": str(target_row.iloc[-1].get("amount_source", target_row.iloc[-1].get("fetch_source", "unknown")) or "unknown"),
                        "type": sig_type,
                        "reason": reason,
                        "is_limit_up": is_limit_up,
                        "consecutive_limit_up_days": consecutive_days,
                        "limit_up_tag": limit_up_tag
                    }
                else:
                    scan_stats["concept_filtered"] += 1
            else:
                scan_stats["strategy_miss"] += 1
            return scan_date_local, signal, None, df
        except Exception as e:
            log.debug(f"处理 {code} 异常: {type(e).__name__}: {str(e)}\n{traceback.format_exc(limit=3)}")
            return None, None, None, None

    print("\n" + "="*70)
    print(" [Horse] 欢迎使用『三度操盘·实战量化机 V17.10 全A股扫描版』")
    mode_name = '周线轻量' if scan_light else ('周线' if is_weekly else '日线')
    print(f" [Gear] 全A股扫描 + 板块TOP3 + 最强信号优先 + {mode_name}复盘提示")
    print(" [Chart] V17.10 版本：全A股扫描 + 飞书推送 + 系统报警")
    print("="*70 + "\n")

    print(f"[配置] 飞书推送: {'✓ 已启用' if FEISHU_ENABLED else '✗ 已禁用'}")
    if FEISHU_ENABLED and FEISHU_WEBHOOK:
        print(f"[配置] Webhook: {FEISHU_WEBHOOK[:50]}...")
    print()

    pipeline_mode = os.getenv("PIPELINE_MODE", "0").strip() == "1"
    if env_date:
        target_date = env_date
    elif not pipeline_mode:
        try:
            target_date = input("👉 请输入历史复盘日期 (直接回车则默认最新交易日): ").strip()
        except EOFError:
            target_date = ""
    else:
        target_date = ""
    if not target_date:
        target_date = datetime.now().strftime("%Y%m%d")
    else:
        target_date = target_date.replace("-", "")

    if len(target_date) != 8 or not target_date.isdigit():
        print(f"❌ 日期格式错误！请使用 YYYYMMDD 格式")
        return

    if not validate_trading_date(target_date) and not is_weekly:
        print(f"⚠️  {target_date} 可能不是交易日（周末或节假日）")
        if not pipeline_mode:
            try:
                confirm = input("   是否继续？(y/n): ").strip().lower()
            except EOFError:
                confirm = "y"
        else:
            confirm = "y"
        if confirm != 'y':
            return

    print(f"\n🔄 清空旧缓存...")
    clear_cache()

    print(f"\n🔍 目标锚定：获取截止至 [{target_date}] 的数据进行深度扫描...")
    print(f"📝 详细日志: {os.path.join(LOG_DIR, f'hunter_sys_{today_str}.log')}\n")

    pool = load_a_share_pool()
    if not pool:
        print("❌ 无法加载全A股股票清单")
        return

    if QT_SNAPSHOT_ENABLED and target_date == datetime.now().strftime("%Y%m%d"):
        print("🛰️  正在批量获取 qt 快照成交额...")
        fetch_qt_snapshot_map(list(pool.keys()))
        sample_codes = list(pool.keys())[:3]
        for sample_code in sample_codes:
            sample_df = fetch_data(sample_code, target_date, scan_mode="daily")
            if sample_df.empty:
                print(f"🛰️  样本 {sample_code}: 数据为空")
                continue
            sample_row = sample_df.iloc[-1]
            sample_source = str(sample_row.get("amount_source", sample_row.get("fetch_source", "unknown")) or "unknown")
            sample_amount = float(sample_row.get("amount", 0) or 0)
            print(f"🛰️  样本 {sample_code}: source={sample_source} amount={sample_amount/100000000:.2f}亿")

    signals = []
    total = len(pool)
    scan_date = "未知"
    success_count = 0

    items = [
        (code, info)
        for code, info in pool.items()
        if not is_st_stock(code, info.get('name', '')) and is_a_share_code(code)
    ]
    st_count = len(pool) - len(items)
    if st_count > 0:
        print(f"🧹 已忽略 ST 标的: {st_count} 只")
    if scan_limit:
        items = items[:scan_limit]
        print(f"🧪 测试模式：仅扫描前 {len(items)} 只标的")
        log.debug(f"🧪 测试模式开启：SCAN_LIMIT={scan_limit}")

    total = len(items)
    log.debug(f"✓ 本次扫描有效标的: {total} 只")
    max_workers = max(1, min(SCAN_WORKERS, 16, total if total else 1))
    print(f"🔧 扫描并发数: {max_workers} | 有效标的: {total}")

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {}
    interrupted = False
    try:
        for code, info in items:
            print(f"\r⏳ 扫描排队: {info['name']} ({code}) ...", end="", flush=True)
            futures[executor.submit(scan_one, code, info, target_date)] = (code, info)

        for idx, future in enumerate(as_completed(futures), 1):
            code, info = futures[future]
            if idx == 1 or idx % 100 == 0 or idx == total:
                print(f"\r⏳ 扫描进度: [{idx}/{total}] 正在把脉: {info['name']} ({code}) ...", end="", flush=True)
            try:
                scan_date_local, signal, observer, df = future.result()
                if df is None or getattr(df, 'empty', True):
                    continue

                success_count += 1
                if scan_date == "未知" or scan_date_local > scan_date:
                    scan_date = scan_date_local

                if signal:
                    signals.append(signal)

            except Exception as e:
                log.debug(f"处理 {code} 异常: {str(e)}")
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n👋 程序已中断")
        log.debug("⚠️ 扫描被用户中断，开始取消未完成任务")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if interrupted:
        partial_scan_date = scan_date if scan_date != "未知" else target_date
        if success_count > 0:
            save_scan_results(partial_scan_date, scan_mode, total, success_count, signals)
            print(f"\n💾 已保存中断前的部分结果：{len(signals)} 个信号")
            send_to_feishu(signals, partial_scan_date, total, success_count, scan_mode=scan_mode, status_note="扫描中断，以下为已完成部分")
        print_scan_stats()
        return

    print("\n\n✅ 扫描完成！正在生成作战简报...\n")

    if success_count == 0:
        print(f"\n⚠️  本次扫描没有留下有效数据，可能全部被成交额或上下文条件过滤掉了。\n")
        print_scan_stats()
        return

    if not signals:
        msg = f"📅 实际复盘日期：{scan_date}\n\n在 {total} 只标的中，未发现{'周线突破前高、底部企稳或止跌反抽' if is_weekly else '『突破先手』或成型的『A/B区』'}。耐心等待主力动作。"
        print(msg)
        send_to_feishu([], scan_date, total, success_count, scan_mode=scan_mode)
        return

    # 【新增】合并同一只股票的多个策略信号
    signals = merge_duplicate_signals(signals)

    signals.sort(key=lambda x: (PRIORITY_ORDER.get(x['type'], 99), x.get('sector', '未知板块'), x['code']))

    grouped = group_signals_by_sector(signals)
    print(summary_brief(scan_date, total, success_count, signals, grouped, scan_mode=scan_mode))
    print(f"🔎 仅供{'周线' if is_weekly else '日线'}复盘参考")
    print(f"🏅 板块TOP3（热度+最强信号）: {sector_top3_brief(grouped)}")

    print("━" * 60)
    print("🏆 今日最强板块")
    print("━" * 60)
    print(f"  {sector_summary_line(grouped[0][0], grouped[0][1])}")
    print("━" * 60)
    print("📋 主线概念热度排行")
    print("━" * 60)
    print("📋 全量信号明细")
    print("━" * 60)
    for sector, sector_signals in grouped:
        print(f"  {sector_summary_line(sector, sector_signals)}")
    print("━" * 60)
    print("🔥 核心信号 TOP5")
    print("━" * 60)
    for idx, s in enumerate(signals[:5], 1):
        amount_source = str(s.get("amount_source", "unknown") or "unknown")
        entry_date = str(s.get("entry_date", scan_date) or scan_date)
        amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
        limit_up_tag = s.get('limit_up_tag', '')
        limit_up_display = f" {limit_up_tag}" if limit_up_tag else ""
        print(f"  {idx}. {signal_priority_label(s['type'])} [{s['type']}] {s['name']} ({s['code']}) | {s['sector']} | 现价:{s['price']:.2f} | 成交额:{amount_text} | 日期:{entry_date} | 来源:{amount_source}{limit_up_display}")
        print(f"     {truncate_reason(s['reason'])}")
    print("━" * 60)

    # 【新增】打印涨停板信号
    print_limit_up_signals(signals)

    for sector, sector_signals in grouped:
        sector_signals.sort(key=lambda s: PRIORITY_ORDER.get(s['type'], 99))
        print(f"【{sector}】（{len(sector_signals)} 只）")
        for s in sector_signals:
            amount_source = str(s.get("amount_source", "unknown") or "unknown")
            entry_date = str(s.get("entry_date", scan_date) or scan_date)
            amount_text = f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿" if s.get('amount') is not None else "未知"
            limit_up_tag = s.get('limit_up_tag', '')
            limit_up_display = f" {limit_up_tag}" if limit_up_tag else ""
            print(f"  {SIGNAL_EMOJI.get(s['type'], '')} [{s['type']}] {s['name']} ({s['code']}) | 现价:{s['price']:.2f} | 成交额:{amount_text} | 日期:{entry_date} | 来源:{amount_source}{limit_up_display}")
            print(f"    {truncate_reason(s['reason'])}")
        print(f"  共 {len(sector_signals)} 只，已全量展示")
    print("✅ 扫描完成！")
    print_scan_stats()

    # V17.2 新增：发送飞书推送
    send_to_feishu(signals, scan_date, total, success_count, scan_mode=scan_mode)

    # V17.5 新增：保存扫描结果用于回测
    save_scan_results(scan_date, scan_mode, total, success_count, signals)

    # V17.2 新增：触发系统报警
    try:
        print("\n🔔 触发系统报警...")
        alert_list = signals[:max(1, min(ALERT_MAX_SIGNALS, len(signals)))]
        for s in alert_list:
            system_alert.alarm_by_signal_type(s['type'])
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n👋 程序已中断")
        log.debug("⚠️ 扫描结果已生成，但报警阶段被用户中断")

