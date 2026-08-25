# -*- coding: utf-8 -*-
"""
optuna_parameter_optimization.py - 基于Optuna的建仓参数智能优化

用途: 对L2/L3参数进行多维度优化，基于历史回测数据
      目标: 最大化 综合评分 = 40%收益 + 35%保护 + 25%稳定性

使用: python optuna_parameter_optimization.py
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import optuna
    from optuna.pruners import MedianPruner
    from optuna.samplers import TPESampler
except ImportError:
    print("[ERROR] Optuna not installed. Run: pip install optuna")
    sys.exit(1)

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from position_builder import fetch_daily_kline

# ========================================================================
# 数据准备与标记
# ========================================================================

class HistoricalBuypointExtractor:
    """从历史数据中提取理想买点"""

    def __init__(self, lookback_days: int = 180):
        self.lookback_days = lookback_days

    def extract_buypoints(self, code: str) -> List[Dict]:
        """
        提取股票的历史理想买点

        返回 [{
          'date': '2026-08-24',
          'price': 7.39,
          'fwd5_return': 0.0997,    # 5天后收益
          'fwd5_maxdd': -0.05,      # 5天内最大回撤
        }, ...]
        """
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return []

        df["date"] = df["date"].astype(str)
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)

        # 过滤到lookback_days
        cutoff_date = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        df = df[df["date"] >= cutoff_date].copy()

        if len(df) < 30:
            return []

        buypoints = []

        # 识别放量+上涨的买点
        df["ma20_vol"] = df["volume"].rolling(20).mean()
        df["daily_gain"] = df["close"].pct_change()

        for i in range(len(df) - 6):  # 至少需要5天后数据
            row = df.iloc[i]
            vol_ratio = row["volume"] / row["ma20_vol"] if row["ma20_vol"] > 0 else 0

            # 买点条件：放量 + 上涨
            if vol_ratio > 1.5 and row["daily_gain"] > 0.02:
                # 计算5天后的指标
                fwd_data = df.iloc[i + 1 : i + 6]["close"].values
                if len(fwd_data) < 5:
                    continue

                fwd5_return = (fwd_data[-1] - row["close"]) / row["close"]
                fwd5_maxdd = (
                    min(fwd_data) - row["close"]
                ) / row["close"]

                buypoints.append({
                    "code": code,
                    "date": row["date"],
                    "price": row["close"],
                    "fwd5_return": fwd5_return,
                    "fwd5_maxdd": fwd5_maxdd,
                })

        return buypoints

    def extract_from_watchlist(self, watchlist_file: str) -> List[Dict]:
        """从watchlist中所有候选股提取买点"""
        with open(watchlist_file, "r", encoding="utf-8") as f:
            watchlist = json.load(f)

        all_buypoints = []
        stocks = watchlist.get("stocks", {})

        for code, info in stocks.items():
            if info.get("status") not in ("monitoring", "signal"):
                continue

            try:
                buypoints = self.extract_buypoints(code)
                all_buypoints.extend(buypoints)
            except Exception as e:
                print(f"[WARN] {code}: {e}")
                continue

        return all_buypoints


# ========================================================================
# 评分函数
# ========================================================================

class CompositeScorer:
    """三维综合评分"""

    @staticmethod
    def score_upside(returns: List[float]) -> float:
        """
        收益潜力评分 (0-1)
        目标: fwd5 ≥ 3%
        """
        if not returns:
            return 0.0
        success_pct = sum(1 for r in returns if r >= 0.03) / len(returns)
        return min(1.0, success_pct * 1.2)

    @staticmethod
    def score_downside(maxdds: List[float]) -> float:
        """
        回撤保护评分 (0-1)
        目标: fwd5_maxdd ≥ -3%
        """
        if not maxdds:
            return 0.0
        safety_pct = sum(1 for d in maxdds if d >= -0.03) / len(maxdds)
        return min(1.0, safety_pct * 1.2)

    @staticmethod
    def score_consistency(hit_rate: float) -> float:
        """
        稳定性评分 (0-1)
        目标: 70%-90%的覆盖度
        """
        if hit_rate < 0.5 or hit_rate > 1.0:
            return 0.0
        # 最高分在70%-90%
        if 0.7 <= hit_rate <= 0.9:
            return 1.0
        else:
            return max(0, 0.8 - abs(hit_rate - 0.8) * 2)

    @staticmethod
    def composite_score(upside: float, downside: float, consistency: float) -> float:
        """综合评分 = 40% upside + 35% downside + 25% consistency"""
        return 0.40 * upside + 0.35 * downside + 0.25 * consistency


# ========================================================================
# 回测引擎（简化版，仅用于评分）
# ========================================================================

class SimpleBacktester:
    """简化的回测引擎，用于Optuna优化"""

    def __init__(self, buypoints: List[Dict]):
        self.buypoints = buypoints

    def simulate_l2_l3(
        self,
        l2_shrink: float,
        l2_support_tol: float,
        l2_trend_days: int,
        l3_vol_ratio: float,
        l3_vwap_tol: float,
    ) -> Tuple[List[Dict], float]:
        """
        使用给定参数模拟L2/L3判定

        返回 (命中的买点列表, 命中率)

        ⚠️ 重要: 这是一个【参数评估代理】，不是真实的L2/L3判定
        原因：真实的L2/L3判定需要完整日线+日内分钟线数据（只有w35/l123_entry_backtest有）

        简化假设（需要参数真正参与判定）:
          • L2: 缩量参数越严(越小)，覆盖率越低但质量越高 → 用hit_rate反映
          • L3: 放量参数越严(越大)，覆盖率越低但确认信号越强 → 用hit_rate反映
          • 判定口径: fwd5≥3%的买点比例——参数越严，这个比例应该越高（因为过滤了弱点）

        真实验证：见 t_io/validation/l123_entry_backtest.py（同w35规格，无未来函数）
        """
        hit_buypoints = []

        for bp in self.buypoints:
            fwd5 = bp.get("fwd5_return", 0)
            maxdd = bp.get("fwd5_maxdd", 0)

            # 参数评估: 根据L2/L3严格度估计这个买点是否能通过
            # L2_shrink越小（越严）→ 会过滤掉更多弱点 → 指标选择性: 用缩量倍数估计
            # L3_vol_ratio越大（越严）→ 放量要求更高 → 指标选择性: 用vol_ratio乘数
            # 组合逻辑: 参数越严，应该只有高质量买点通过 → fwd5>3%的占比应该越高

            # 基准: 不加参数过滤时，285个买点中39%超过fwd5≥3%
            # L2/L3越严格，我们期望通过的买点fwd5应该更好
            # 简化模型: 用参数严格度调整通过阈值

            # L2严格度因子 (0.65~1.2, 越小越严)
            l2_strictness = 1.2 - l2_shrink  # 范围 [0.0, 0.55]
            # L3严格度因子 (1.0~1.5, 越大越严)
            l3_strictness = l3_vol_ratio - 1.0  # 范围 [0.0, 0.5]
            # 总严格度 (0~1.05)
            strictness = min(1.0, l2_strictness + l3_strictness)

            # 预期改善: 严格参数应该提高高质量买点比例
            # 基准fwd5≥3% → 严格时提高到 fwd5≥(3%-strictness×1%)
            adjusted_threshold = max(0.01, 0.03 - strictness * 0.02)

            if fwd5 >= adjusted_threshold and maxdd >= -0.05:  # 同时保护回撤
                hit_buypoints.append(bp)

        hit_rate = len(hit_buypoints) / len(self.buypoints) if self.buypoints else 0

        return hit_buypoints, hit_rate

    def evaluate_parameters(
        self,
        l2_shrink: float,
        l2_support_tol: float,
        l2_trend_days: int,
        l3_vol_ratio: float,
        l3_vwap_tol: float,
    ) -> float:
        """
        评估参数组合的综合评分

        返回 0-1 的综合评分
        """
        hit_buypoints, hit_rate = self.simulate_l2_l3(
            l2_shrink, l2_support_tol, l2_trend_days, l3_vol_ratio, l3_vwap_tol
        )

        if not hit_buypoints:
            return 0.0

        # 计算三维评分
        returns = [bp["fwd5_return"] for bp in hit_buypoints]
        maxdds = [bp["fwd5_maxdd"] for bp in hit_buypoints]

        upside = CompositeScorer.score_upside(returns)
        downside = CompositeScorer.score_downside(maxdds)
        consistency = CompositeScorer.score_consistency(hit_rate)

        composite = CompositeScorer.composite_score(upside, downside, consistency)

        return composite


# ========================================================================
# Optuna优化
# ========================================================================

class OptunaOptimizer:
    """Optuna参数优化主类"""

    def __init__(self, buypoints: List[Dict]):
        self.backtester = SimpleBacktester(buypoints)
        self.buypoints = buypoints

    def objective(self, trial: optuna.Trial) -> float:
        """Optuna优化的目标函数"""

        # 采样参数空间
        l2_shrink = trial.suggest_float("l2_shrink", 0.3, 1.2, step=0.05)
        l2_support_tol = trial.suggest_float(
            "l2_support_tol", 0.005, 0.03, step=0.005
        )
        l2_trend_days = trial.suggest_int("l2_trend_days", 2, 6)

        l3_vol_ratio = trial.suggest_float("l3_vol_ratio", 1.0, 1.5, step=0.05)
        l3_vwap_tol = trial.suggest_float("l3_vwap_tol", 0.005, 0.03, step=0.005)

        # 评估参数组合
        score = self.backtester.evaluate_parameters(
            l2_shrink, l2_support_tol, l2_trend_days, l3_vol_ratio, l3_vwap_tol
        )

        return score

    def optimize(self, n_trials: int = 500) -> Dict:
        """运行优化"""

        # 创建study
        sampler = TPESampler(seed=42)
        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            pruner=MedianPruner(),
        )

        # 优化
        study.optimize(self.objective, n_trials=n_trials, show_progress_bar=True)

        # 提取结果
        best_params = study.best_params
        best_score = study.best_value

        # 计算详细统计
        _, hit_rate = self.backtester.simulate_l2_l3(**best_params)

        hit_buypoints = []
        for bp in self.buypoints:
            if bp["fwd5_return"] >= 0.03:
                hit_buypoints.append(bp)

        if hit_buypoints:
            returns = [bp["fwd5_return"] for bp in hit_buypoints]
            maxdds = [bp["fwd5_maxdd"] for bp in hit_buypoints]

            upside_score = CompositeScorer.score_upside(returns)
            downside_score = CompositeScorer.score_downside(maxdds)
            consistency_score = CompositeScorer.score_consistency(hit_rate)
        else:
            upside_score = downside_score = consistency_score = 0.0

        return {
            "best_params": best_params,
            "best_score": best_score,
            "n_trials": n_trials,
            "n_buypoints": len(self.buypoints),
            "n_hit_buypoints": len(hit_buypoints),
            "hit_rate": hit_rate,
            "upside_score": upside_score,
            "downside_score": downside_score,
            "consistency_score": consistency_score,
            "stats": {
                "mean_return": np.mean(returns) if returns else 0,
                "std_return": np.std(returns) if returns else 0,
                "mean_maxdd": np.mean(maxdds) if maxdds else 0,
                "std_maxdd": np.std(maxdds) if maxdds else 0,
            },
        }


# ========================================================================
# 主程序
# ========================================================================

def main():
    """主程序"""

    print("=" * 100)
    print("【Optuna参数优化】建仓L2/L3参数智能寻优")
    print("=" * 100)
    print()
    print("⚠️  【重要限制】此脚本的优化是基于【简化参数评估模型】，而非真实L2/L3日线判定")
    print("    真实验证需要：t_io/validation/l123_entry_backtest.py（包含完整日线+分钟线回放）")
    print()

    # Step 1: 提取历史买点
    print("Step 1: 从watchlist提取历史理想买点...")
    watchlist_file = BASE / "watchlist_buy.json"

    if not watchlist_file.exists():
        print(f"[ERROR] {watchlist_file} not found")
        return

    extractor = HistoricalBuypointExtractor(lookback_days=180)
    buypoints = extractor.extract_from_watchlist(str(watchlist_file))

    print(f"  ✓ 提取了 {len(buypoints)} 个历史理想买点")

    if len(buypoints) < 50:
        print(f"  ⚠ 警告: 买点数量较少 ({len(buypoints)})，优化可能不稳定")
    print()

    # Step 2: 启动优化
    print("Step 2: 启动Optuna参数优化...")
    print("  目标函数: 最大化综合评分 = 40% upside + 35% downside + 25% consistency")
    print("  试验次数: 500")
    print()

    optimizer = OptunaOptimizer(buypoints)
    result = optimizer.optimize(n_trials=500)

    print()
    print("=" * 100)
    print("【优化完成】")
    print("=" * 100)
    print()

    # 输出结果
    print("【最优参数】")
    print()
    print(f"  L2缩量倍数:      {result['best_params']['l2_shrink']:.3f}x")
    print(f"  L2支撑容错:      ±{result['best_params']['l2_support_tol']*100:.1f}%")
    print(f"  L2递减天数:      {result['best_params']['l2_trend_days']}天")
    print(f"  L3放量倍数:      {result['best_params']['l3_vol_ratio']:.3f}x")
    print(f"  L3 VWAP容错:     ±{result['best_params']['l3_vwap_tol']*100:.1f}%")
    print()

    print("【评分详情】")
    print()
    print(f"  综合评分:        {result['best_score']:.3f}/1.000")
    print(f"  收益潜力(40%):   {result['upside_score']:.3f}")
    print(f"  回撤保护(35%):   {result['downside_score']:.3f}")
    print(f"  稳定性(25%):     {result['consistency_score']:.3f}")
    print()

    print("【统计信息】")
    print()
    print(f"  总买点数:        {result['n_buypoints']}")
    print(f"  命中买点:        {result['n_hit_buypoints']}")
    print(f"  命中率:          {result['hit_rate']*100:.1f}%")
    print(f"  平均收益:        {result['stats']['mean_return']*100:+.2f}%")
    print(f"  收益标准差:      {result['stats']['std_return']*100:.2f}%")
    print(f"  平均回撤:        {result['stats']['mean_maxdd']*100:+.2f}%")
    print(f"  回撤标准差:      {result['stats']['std_maxdd']*100:.2f}%")
    print()

    # 保存结果
    output_file = BASE / "t_io" / "validation" / "optuna_optimization_result.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"结果已保存: {output_file}")
    print()
    print("⚠️  【下一步】")
    print("    1. 这些参数需要在 t_io/validation/l123_entry_backtest.py 中真实回放验证")
    print("    2. 将验证结果（fwd5收益、胜率、回撤）与现行 timing_gate 对比")
    print("    3. 仅在验证通过后才集成进 position_builder 生产代码")
    print()


if __name__ == "__main__":
    main()
