# -*- coding: utf-8 -*-
"""
韭研概念打分报告生成器 - 主入口
版本：v8.0
用法：python main.py [--date YYYYMMDD] [--data-dir ./data] [--output ./output]
"""
import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

# 将 modules 目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.data_loader import DataLoader
from modules.market_data import MarketDataFetcher
from modules.scorer import ConceptScorer
from modules.ranker import Top5Ranker, SectorRanker, DetailRanker
from modules.reporter import ReportBuilder
from modules.validator import ReportValidator
from modules.push_feishu import send_report_summary, send_error_alert
import pandas as pd


def load_config(config_path: str = "config.json") -> dict:
    """加载配置文件"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(base_dir, config_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return json.load(f)


def prepare_data(config: dict, date_str: str, save_spot: bool = False) -> dict:
    """
    以 watchlist_jiuyan.json 为唯一数据源，获取真实行情数据，生成全部报告数据
    :param date_str: 日期 'YYYY-MM-DD'（用于获取行情和保存缓存）
    :param save_spot: 是否保存行情快照到本地
    :return: {
        "watchlist": pd.DataFrame,      # 原始 watchlist + 行情数据
        "summary": pd.DataFrame,          # 概念总排名（Sheet 1）
        "detail": pd.DataFrame,           # 详细个股排名（Sheet 2）
        "sectors": Dict[str, pd.DataFrame],  # 各板块明细
        "six_dim": None
    }
    """
    loader = DataLoader(config=config)

    print("[加载] 加载数据中...")
    data = {}

    # 1. 加载 watchlist（唯一数据源，从脚本自身目录读取）
    data["watchlist"] = loader.load_watchlist()
    if data["watchlist"] is None:
        print("[ERR] 无法继续：watchlist_jiuyan.json 未在脚本目录找到")
        return data

    watchlist_df = data["watchlist"]
    # 只保留带韭研概念的标的作为打分池
    df_pool = watchlist_df[watchlist_df["韭研概念"].str.strip().ne("")].copy()
    codes = df_pool["代码"].astype(str).tolist()
    # 去重：多分类股票在 df_pool 中有多行，但行情只需获取一次
    codes = list(dict.fromkeys(codes))
    print(f"  打分池: {len(df_pool)} 行数据，{len(codes)} 只不重复标的")

    # 2. 日期防呆：检查传入日期是否为今天，并记录实际数据日期
    from datetime import datetime, timedelta
    today_str = datetime.now().strftime("%Y-%m-%d")
    if date_str != today_str:
        gap_days = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(today_str, "%Y-%m-%d")).days
        prefix = "⚠️ [WARN] 传入日期"
        if gap_days < -30:
            print(f"\n{prefix} {date_str} 比今天 ({today_str}) 早 {abs(gap_days)} 天，将获取历史数据！")
        elif gap_days != 0:
            print(f"\n{prefix} {date_str} 不是今天 ({today_str})，相差 {gap_days} 天，将获取历史数据。")
    data["report_date"] = date_str
    data["actual_data_dates"] = set()

    # 3. 获取真实行情数据
    print(f"\n[网络] 获取行情数据 ({date_str})...")
    # 从 watchlist 提取 ST 股票代码
    st_codes = set()
    if watchlist_df is not None and "名称" in watchlist_df.columns:
        st_mask = watchlist_df["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
        st_codes = set(watchlist_df.loc[st_mask, "代码"].astype(str).tolist())
    if st_codes:
        print(f"  [INFO] 识别到 {len(st_codes)} 只 ST/*ST 股票，使用 5% 涨停阈值")
    fetcher = MarketDataFetcher(data_dir=os.path.join(os.path.dirname(__file__), "data"), st_codes=st_codes)
    market_df = fetcher.fetch_for_date(codes, date_str)
    data["failed_codes"] = fetcher.last_failed  # 传递失败股票代码到报告

    if market_df.empty:
        print("[WARN] 行情数据获取为空，将使用默认值（排名无意义）")
    else:
        print(f"  [OK] 行情数据: {len(market_df)} 只")
        # 记录实际数据日期范围（用于报告标注）
        if "数据日期" in market_df.columns:
            data["actual_data_dates"] = set(market_df["数据日期"].dropna().unique())
            if data["actual_data_dates"]:
                print(f"  [INFO] 实际数据日期: {', '.join(sorted(data['actual_data_dates']))}")
        # 合并行情数据到 watchlist（避免列名冲突，先丢弃 market_df 中的名称）
        if "名称" in market_df.columns:
            market_df = market_df.drop(columns=["名称"])
        watchlist_df = watchlist_df.merge(market_df, on="代码", how="left")
        data["watchlist"] = watchlist_df
        # 将合并后的数据传回 loader，使后续生成函数使用真实行情
        loader.set_watchlist(watchlist_df)

        # 保存行情快照（可选）
        if save_spot and not market_df.empty:
            fetcher.save_spot(market_df, date_str)

    return data, loader


def score_stocks(data: dict, config: dict) -> list:
    """
    对 watchlist 中带 jiuyan_concept 的标的进行评分（使用真实行情数据）
    :return: 评分后的股票列表（字典列表）
    """
    dimensions = config.get("scoring", {}).get("dimensions", [])
    scorer = ConceptScorer(dimensions=dimensions if dimensions else None)

    stock_list = []
    watchlist_df = data.get("watchlist")
    if watchlist_df is None or watchlist_df.empty:
        print("[WARN] watchlist 为空，无法评分")
        return stock_list

    # 只对有韭研概念的标的进行评分
    df_pool = watchlist_df[watchlist_df["韭研概念"].str.strip().ne("")].copy()
    print(f"\n[评分] 评分计算中... (共 {len(df_pool)} 只带概念标的)")

    for _, row in df_pool.iterrows():
        stock = row.to_dict()
        # 使用真实行情数据（如果获取成功），否则用默认值
        # 真实数据字段：涨跌幅, 涨停, 成交额, 换手率, 振幅, 量比, 现价, 最高, 最低, 今开, 昨收
        stock.setdefault("涨停", int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0)
        stock.setdefault("连板天数", 0)
        stock.setdefault("暗线概念数", len(str(stock.get("韭研概念", "")).split("_")))
        stock.setdefault("量比", float(row.get("量比", 1.0)) if pd.notna(row.get("量比")) else 1.0)
        stock_list.append(stock)

    scored_list = scorer.compute_batch(stock_list)
    print(f"  [OK] 评分完成: {len(scored_list)} 只")

    # 将得分写回 watchlist_df，使后续 loader 可以使用
    score_map = {str(s.get("代码", "")): s for s in scored_list}
    for col in ["总得分", "涨停",
                "D1强势形态且新高", "D2强势形态",
                "D4首板资金池", "D5潜在突破10日", "D6潜在突破5日",
                "D7持续性", "D8情绪分数", "D9活跃程度", "大成交额额外加分"]:
        watchlist_df[col] = watchlist_df["代码"].map(lambda x: score_map.get(str(x), {}).get(col, 0))
    data["watchlist"] = watchlist_df

    # 打印得分分布（调试）
    if scored_list:
        scores = [s.get("总得分", 0) for s in scored_list]
        print(f"     得分范围: {min(scores)} ~ {max(scores)}, 平均: {sum(scores)/len(scores):.1f}")

    # 六维前置筛选（若外部数据接入）
    six_dim_df = data.get("six_dim")
    if six_dim_df is not None and not six_dim_df.empty:
        min_score = config.get("scoring", {}).get("min_score_for_pool", 60)
        six_dim_map = {}
        for _, row in six_dim_df.iterrows():
            six_dim_map[str(row.get("代码", ""))] = row.get("六维得分", 0)

        filtered = []
        for stock in scored_list:
            code = str(stock.get("代码", ""))
            six_score = six_dim_map.get(code, 100)  # 无六维数据默认放行
            if six_score >= min_score:
                filtered.append(stock)
            else:
                stock["_filtered_reason"] = f"六维得分 {six_score} < {min_score}"
        print(f"  [OK] 六维筛选后: {len(filtered)}/{len(scored_list)} 只入池")
        scored_list = filtered

    return scored_list


def generate_top5(scored_list: list, config: dict) -> list:
    """
    生成 TOP5
    """
    ranking_cfg = config.get("ranking", {})
    ranker = Top5Ranker(
        max_same_sector=ranking_cfg.get("max_same_sector_in_top5", 2),
        min_diversity=ranking_cfg.get("top5_sector_diversity_min", 3),
        top_count=ranking_cfg.get("top5_count", 5)
    )

    print(f"\n[TOP5] 生成 TOP5...")
    top5 = ranker.select(scored_list)
    print(f"  [OK] TOP5 生成完成: {len(top5)} 条")
    for item in top5:
        print(f"     #{item.get('排名')} {item.get('代码')} {item.get('名称')} "
              f"(板块: {item.get('所属板块', item.get('板块', '未知'))}, "
              f"得分: {item.get('总得分', 0)})")
    return top5


def build_report(data: dict, top5_list: list, config: dict, output_path: str) -> str:
    """
    组装并输出报告
    :return: 输出文件路径
    """
    builder = ReportBuilder(config_path=None)
    builder.config = config  # 使用已加载的配置
    builder.reporter.config = config

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    def _get_df(key):
        val = data.get(key)
        if val is None or (hasattr(val, 'empty') and val.empty):
            return __empty_df()
        return val

    output_path = builder.build_all(
        df_summary=_get_df("summary"),
        df_detail=_get_df("detail"),
        top5_list=top5_list,
        sector_data=data.get("sectors", {}),
        stats=data.get("stats", {}),
        output_path=output_path
    )
    return output_path


def __empty_df():
    """返回空 DataFrame"""
    import pandas as pd
    return pd.DataFrame()


def validate_report(output_path: str, config: dict) -> bool:
    """
    验证报告
    """
    validator = ReportValidator(config=config)
    return validator.validate_and_print(output_path)


def main():
    parser = argparse.ArgumentParser(description="韭研概念打分报告生成器 v8.0")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y%m%d"),
                        help="报告日期 (YYYYMMDD)")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="数据目录路径（默认从 config.json 读取）")
    parser.add_argument("--output", type=str, default=None,
                        help="输出文件路径（默认自动生成）")
    parser.add_argument("--skip-validate", action="store_true",
                        help="跳过验证步骤")
    parser.add_argument("--save-spot", action="store_true",
                        help="保存行情快照到本地 data/spot_YYYYMMDD.csv，供后续回测")
    args = parser.parse_args()

    # 日期格式化
    date_str = args.date
    if len(date_str) == 8 and date_str.isdigit():
        date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    else:
        date_fmt = date_str

    print("=" * 60)
    print("[报告] 韭研概念打分报告生成器 v8.0")
    print("=" * 60)

    # 1. 加载配置
    config = load_config()
    base_dir = config.get("base_dir", os.path.dirname(os.path.abspath(__file__)))

    # 2. 确定数据目录
    data_dir = args.data_dir or os.path.join(base_dir, config.get("data_dir", "data"))
    print(f"\n[目录] 脚本目录: {base_dir}")
    print(f"   watchlist_jiuyan.json 预期路径: {os.path.join(base_dir, 'watchlist_jiuyan.json')}")
    print(f"   输出目录: {os.path.join(base_dir, config.get('output_dir', 'output'))}")

    # 3. 确定输出路径
    if args.output:
        output_path = args.output
    else:
        output_dir = os.path.join(base_dir, config.get("output_dir", "output"))
        output_path = os.path.join(output_dir, f"韭研概念打分报告_{args.date}.xlsx")
    print(f"[文件] 输出路径: {output_path}")

    # 4. 加载数据（以 watchlist 为唯一数据源）+ 获取行情
    data, loader = prepare_data(config, date_fmt, save_spot=args.save_spot)

    # 若 watchlist 加载失败，提前退出
    if data.get("watchlist") is None:
        print("\n[ERR] 数据加载失败，程序终止")
        return 1

    # 5. 评分计算
    scored_list = score_stocks(data, config)

    # 6. 将评分后的数据写回 loader，生成各类排名
    loader.set_watchlist(data["watchlist"])
    data["summary"] = loader.load_concept_summary()
    print(f"\n  [OK] 概念总排名: {len(data['summary'])} 个板块")
    data["detail"] = loader.load_detail_ranking()
    print(f"  [OK] 详细个股排名: {len(data['detail'])} 只标的")
    data["sectors"] = loader.load_all_sectors()
    print(f"  [OK] 板块明细: {len(data['sectors'])} 个板块")
    for name, df in data["sectors"].items():
        print(f"     - {name}: {len(df)} 条")

    # === 计算各板块前排率 / 后排率 ===
    front_back_rows = []
    for sector_name, df_sector in data["sectors"].items():
        if df_sector.empty:
            continue
        total = len(df_sector)
        front_count = int(((df_sector["D1强势形态且新高"] > 0) | (df_sector["D2强势形态"] > 0)).sum())
        back_count = int(((df_sector["D5潜在突破10日"] > 0) | (df_sector["D6潜在突破5日"] > 0)).sum())
        front_ratio = round(front_count / total * 100, 1) if total else 0
        back_ratio = round(back_count / total * 100, 1) if total else 0
        front_back_rows.append({
            "板块": sector_name,
            "股票数量": total,
            "前排数量": front_count,
            "后排数量": back_count,
            "前排率%": front_ratio,
            "后排率%": back_ratio,
        })
    fb_df = pd.DataFrame(front_back_rows)
    # 合并到 summary
    if not fb_df.empty and not data.get("summary", pd.DataFrame()).empty:
        data["summary"] = data["summary"].merge(fb_df, on="板块", how="left")

    # === 板块热度分计算 ===
    print(f"\n[热度] 计算板块热度分...")
    from modules.heat_tracker import compute_heat_scores
    # 注意：history 键统一使用 YYYYMMDD（与 push_feishu 的历史约定一致），不能传带横线的 date_fmt
    data["summary"] = compute_heat_scores(data["summary"], data["sectors"], args.date)
    if "热度分" in data["summary"].columns:
        print(f"  [OK] 热度分 TOP3:")
        for _, r in data["summary"].head(3).iterrows():
            print(f"     {r['板块']}: 热度分 {r['热度分']} {r.get('趋势', '')}")
    else:
        print(f"  [INFO] 热度分计算跳过（数据不足）")

    # === 热度分最高/最低板块 ===
    heat_top_sector = None
    heat_bottom_sector = None
    if "热度分" in data["summary"].columns and data["summary"]["热度分"].notna().any():
        hs = data["summary"]["热度分"]
        heat_top_sector = f"{data['summary'].loc[hs.idxmax(), '板块']} ({round(float(hs.max()), 1)})"
        heat_bottom_sector = f"{data['summary'].loc[hs.idxmin(), '板块']} ({round(float(hs.min()), 1)})"

    # === 计算总体平均分（基于概念总排名 summary 的 平均分/股票数量 列）===
    overall_avg_weighted = None  # 股票数加权: Σ(板块平均分×板块股票数量)/Σ(板块股票数量)
    overall_avg_simple = None    # 板块等权: 各板块平均分的算术平均
    overall_top_sector = None    # 平均分最高板块
    overall_bottom_sector = None # 平均分最低板块
    summary_df = data.get("summary")
    if summary_df is not None and not summary_df.empty and "平均分" in summary_df.columns:
        avg_series = pd.to_numeric(summary_df["平均分"], errors="coerce")
        if "股票数量" in summary_df.columns:
            cnt_series = pd.to_numeric(summary_df["股票数量"], errors="coerce").fillna(0)
            total_cnt = cnt_series.sum()
            if total_cnt > 0:
                overall_avg_weighted = round(float((avg_series.fillna(0) * cnt_series).sum() / total_cnt), 2)
        if avg_series.notna().any():
            overall_avg_simple = round(float(avg_series.mean()), 2)
            top_idx = avg_series.idxmax()
            bottom_idx = avg_series.idxmin()
            overall_top_sector = f"{summary_df.loc[top_idx, '板块']} ({round(float(avg_series.loc[top_idx]), 2)})"
            overall_bottom_sector = f"{summary_df.loc[bottom_idx, '板块']} ({round(float(avg_series.loc[bottom_idx]), 2)})"
        print(f"  [OK] 总体平均分: 加权 {overall_avg_weighted} / 等权 {overall_avg_simple}")
    else:
        print(f"  [WARN] 概念总排名为空或缺少'平均分'列，跳过总体平均分计算")
    data["six_dim"] = loader.load_six_dim_scores()

    # 7. 生成 TOP5
    top5_list = generate_top5(scored_list, config)
    data["top5_list"] = top5_list  # 供飞书推送使用

    # 8. 收集统计指标（用于说明页）
    watchlist_df = data.get("watchlist")
    df_pool = watchlist_df[watchlist_df["韭研概念"].str.strip().ne("")] if watchlist_df is not None else pd.DataFrame()
    pool_count = len(df_pool)
    market_success = df_pool["涨跌幅"].notna().sum() if watchlist_df is not None and "涨跌幅" in watchlist_df.columns else 0
    market_failed = pool_count - int(market_success)
    sector_count = len(data.get("summary", pd.DataFrame()))
    detail_count = len(data.get("detail", pd.DataFrame()))
    top5_count = len(top5_list)
    top5_sectors = len(set([item.get('所属板块', item.get('板块', '未知')) for item in top5_list])) if top5_list else 0
    top5_diversity_ok = top5_sectors >= 3
    scores = [item.get("总得分", 0) for item in scored_list] if scored_list else [0]
    score_max = max(scores) if scores else 0
    score_min = min(scores) if scores else 0
    score_avg = round(sum(scores) / len(scores), 1) if scores else 0

    # 计算各板块所含不重复股票数
    sector_stocks = {}
    if "sectors" in data and data["sectors"]:
        for name, df in data["sectors"].items():
            if "代码" in df.columns and not df.empty:
                sector_stocks[name] = int(df["代码"].nunique())
            else:
                sector_stocks[name] = 0

    data["stats"] = {
        "pool_count": pool_count,
        "market_success": int(market_success),
        "market_failed": market_failed,
        "failed_codes": data.get("failed_codes", []),
        "sector_count": sector_count,
        "detail_count": detail_count,
        "sector_stocks": sector_stocks,
        "report_date": data.get("report_date", date_str),
        "actual_data_dates": ", ".join(sorted(data.get("actual_data_dates", set()))),
        "top5_count": top5_count,
        "top5_sectors": top5_sectors,
        "top5_diversity_ok": top5_diversity_ok,
        "score_range": f"{score_min} ~ {score_max}",
        "score_avg": score_avg,
        "overall_avg_weighted": overall_avg_weighted,
        "overall_avg_simple": overall_avg_simple,
        "overall_top_sector": overall_top_sector,
        "overall_bottom_sector": overall_bottom_sector,
        "heat_top_sector": heat_top_sector,
        "heat_bottom_sector": heat_bottom_sector,
        "validate_result": "通过"  # 后续验证步骤后更新
    }

    # 9. 组装报告
    print(f"\n[组装] 组装报告...")
    output_path = build_report(data, top5_list, config, output_path)

    # 10. 验证报告
    if not args.skip_validate:
        print(f"\n[验证] 验证报告...")
        validate_ok = validate_report(output_path, config)
        data["stats"]["validate_result"] = "通过" if validate_ok else "失败"

    # 11. 飞书推送报告摘要（文字 + Excel文件）
    feishu_cfg = config.get("feishu", {})
    if feishu_cfg.get("webhook_url") or (feishu_cfg.get("app_id") and feishu_cfg.get("app_secret") and feishu_cfg.get("chat_id")):
        print(f"\n[推送] 正在推送到飞书...")
        try:
            result = send_report_summary(config, data, output_path, args.date)
            if not result.get("ok"):
                print(f"[WARN] 飞书推送可能未成功: {result.get('error', 'unknown')}")
            else:
                if result.get("file_ok"):
                    print(f"[OK] 文字摘要 + Excel 文件均已推送到飞书")
                elif result.get("text_ok"):
                    print(f"[OK] 文字摘要已推送，Excel文件未上传")
        except Exception as e:
            print(f"[ERR] 飞书推送异常: {e}")
            try:
                send_error_alert(config, str(e), args.date)
            except Exception:
                pass
    else:
        print(f"\n[INFO] 飞书配置未完整（需要 webhook_url 或 app_id+app_secret+chat_id），跳过推送")

    print(f"\n{'=' * 60}")
    print(f"[OK] 全部完成！")
    print(f"[文件] 输出文件: {output_path}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
