# -*- coding: utf-8 -*-
"""波动选股因子包 —— 策略线①「选波动大的标的做T」的选股层因子体检。

因子清单（全部日频，t 日收盘后可得，严格无未来函数）：
    ATR20     振幅因子      mean((high-low)/Ref(close,1), 20)
    AMP       振幅当前值    (high-low)/Ref(close,1)
    AMP_Q20   AMP 的 20 日滚动分位（当前值在过去 20 日窗口内的分位）
    VOLVOL    vol-of-vol    std(std(日收益,20),20)
    AMOUNT20  成交额因子    mean(amount,20)（流动性过滤兼做T容量）
    COMBO     波动×流动性   ATR20 * log(AMOUNT20)（候选池排名主因子）
    TR_SHARE  波幅占比      ATR20/close（归一化）

可交易性过滤器（评估与候选池共用同一宇宙）：
    剔除上市 <120 个交易日（按面板内 per-symbol 累计行数，PIT 安全）
    剔除 AMOUNT20 < 5000 万元
    剔除停牌日（volume=0）
    剔除 ST/*ST（按 universe 当前 sec_name；历史 ST 状态不可得，见报告限制声明）

IC 层依赖（任务2并行开发，严格遵守其 API）：
    ic_layer.load_panel(panel_dir) -> 长表 [symbol,date,open,high,low,close,volume,amount]
    ic_layer.evaluate_factor(factor, panel, horizons=(1,3,5), min_coverage=30) -> dict
    ic_layer.decile_analysis(factor, panel, horizon, n_groups=10) -> dict
    ic_layer.mc_baseline(factor, panel, horizon, n=200, seed=42) -> dict
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── IC 层导入（ic_layer.py 由任务2提供；本文件只照契约 import，不实现） ──
try:
    from ic_layer import decile_analysis, evaluate_factor, load_panel, mc_baseline
except ImportError:  # 允许从仓库根目录直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ic_layer import decile_analysis, evaluate_factor, load_panel, mc_baseline

# ── 路径常量 ────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parents[3]           # E:\superTrader
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
HOLDINGS_JSON = WORKSPACE / "t_io" / "state" / "holdings.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "volatility_screen_2026-09-18"

# ── 口径常量（预注册，不为结果好看而调） ────────────────────────────────
WIN = 20                      # 全部滚动窗口
MIN_LISTED_DAYS = 120         # 上市最少交易日数（面板内累计行数口径）
MIN_AMOUNT20 = 5e7            # AMOUNT20 下限：5000 万元
HORIZONS = (1, 3, 5)          # evaluate_factor 前瞻收益窗口
DECILE_HORIZON = 5            # 十分位分析窗口
MC_HORIZON = 5                # 蒙特卡洛基线窗口
MC_N = 200
MC_SEED = 42
TOP_N = 100

FACTOR_NAMES = ["ATR20", "AMP", "AMP_Q20", "VOLVOL", "AMOUNT20", "COMBO", "TR_SHARE"]


# ════════════════════════════════════════════════════════════════════════
# 1. 面板准备与向量化滚动工具
# ════════════════════════════════════════════════════════════════════════
def _prep_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """统一列名、排序、重置索引。接受 load_panel 长表；兼容 eob 日期列名。"""
    df = panel.copy()
    if "date" not in df.columns and "eob" in df.columns:
        df = df.rename(columns={"eob": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    need = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"面板缺列: {missing}")
    df = df[need].sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    return df


def _roll(s: pd.Series, g: pd.Series, n: int, fn: str) -> pd.Series:
    """按 symbol 分组的滚动统计，结果对齐回原索引（禁逐行 iterrows）。"""
    r = s.groupby(g, sort=False).rolling(n, min_periods=n)
    out = getattr(r, fn)()
    out.index = out.index.get_level_values(-1)
    return out.reindex(s.index)


def _roll_apply_last_pct(s: pd.Series, g: pd.Series, n: int) -> pd.Series:
    """当前值在 trailing n 日窗口内的分位（含自身，(x<=x[-1]).mean()）。

    实现：per-symbol numpy sliding_window_view，向量化比较，禁逐行。
    窗口内含 NaN 则结果为 NaN（与 rolling(min_periods=n) 口径一致）。
    """
    arr = s.to_numpy(dtype=float)
    codes = pd.factorize(g, sort=False)[0]
    out = np.full(len(arr), np.nan)
    bounds = np.flatnonzero(np.diff(codes)) + 1
    starts = np.r_[0, bounds]
    ends = np.r_[bounds, len(arr)]
    for st, en in zip(starts, ends):
        a = arr[st:en]
        if len(a) < n:
            continue
        sw = np.lib.stride_tricks.sliding_window_view(a, n)
        valid = ~np.isnan(sw).any(axis=1)
        pct = (sw <= sw[:, [-1]]).mean(axis=1)
        out[st + n - 1:en] = np.where(valid, pct, np.nan)
    return pd.Series(out, index=s.index)


# ════════════════════════════════════════════════════════════════════════
# 2. 因子计算（面板级向量化）
# ════════════════════════════════════════════════════════════════════════
def compute_factor_series(panel: pd.DataFrame) -> pd.DataFrame:
    """返回与 panel 同索引的 DataFrame，每列一个因子的原始（未过滤）值。"""
    df = _prep_panel(panel)
    g = df["symbol"]

    close_prev = df["close"].groupby(g, sort=False).shift(1)
    rng = (df["high"] - df["low"]) / close_prev.replace(0, np.nan)   # 日振幅
    ret = df["close"] / close_prev - 1.0                             # 日收益

    atr20 = _roll(rng, g, WIN, "mean")
    amp = rng
    amp_q20 = _roll_apply_last_pct(amp, g, WIN)
    ret_std = _roll(ret, g, WIN, "std")
    volvol = _roll(ret_std, g, WIN, "std")
    amount20 = _roll(df["amount"], g, WIN, "mean")
    combo = atr20 * np.log(amount20.clip(lower=1.0))
    tr_share = atr20 / df["close"].replace(0, np.nan)

    out = df[["symbol", "date"]].copy()
    out["ATR20"] = atr20
    out["AMP"] = amp
    out["AMP_Q20"] = amp_q20
    out["VOLVOL"] = volvol
    out["AMOUNT20"] = amount20
    out["COMBO"] = combo
    out["TR_SHARE"] = tr_share
    # 停牌日 volume=0：所有因子置 NaN（分子分母再小也不参与横截面）
    out.loc[df["volume"] <= 0, FACTOR_NAMES] = np.nan
    return out


# ════════════════════════════════════════════════════════════════════════
# 3. 可交易性过滤器
# ════════════════════════════════════════════════════════════════════════
def load_universe(panel_dir: Path = PANEL_DIR) -> pd.DataFrame | None:
    """读取 universe 文件（含 sec_name 则支持 ST 剔除；没有则返回 None）。"""
    for name in ("universe.parquet", "universe.csv"):
        p = Path(panel_dir) / name
        if p.exists():
            u = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
            return u
    return None


def _norm_sym(s: pd.Series) -> pd.Series:
    """与 ic_layer._norm_symbol 同规则：'SHSE.600519'/'600519.SH' -> '600519'。"""
    parts = s.astype(str).str.split(".")
    return parts.apply(lambda p: p[1] if len(p) == 2 and p[1].isdigit() else p[0])


def build_tradable_mask(panel: pd.DataFrame,
                        factors: pd.DataFrame,
                        universe: pd.DataFrame | None = None) -> pd.Series:
    """True=可交易。上市天数、流动性、停牌、ST 四道闸。"""
    df = _prep_panel(panel)
    g = df["symbol"]

    listed_ok = g.groupby(g, sort=False).cumcount() >= (MIN_LISTED_DAYS - 1)
    liquid_ok = factors["AMOUNT20"] >= MIN_AMOUNT20
    alive_ok = df["volume"] > 0

    mask = listed_ok & liquid_ok & alive_ok

    st_names: set[str] = set()
    if universe is not None and "sec_name" in universe.columns:
        names = universe["sec_name"].astype(str)
        st_set = set(_norm_sym(universe.loc[names.str.contains("ST", case=False, na=False), "symbol"]))
        st_names = st_set
        mask = mask & ~_norm_sym(df["symbol"]).isin(st_set).to_numpy()
    # universe 缺失或无名称字段时跳过 ST 闸（报告注明限制）
    mask.attrs["st_excluded"] = len(st_names)
    mask.attrs["st_filter_active"] = bool(st_names) or (
        universe is not None and "sec_name" in universe.columns
    )
    return mask


def apply_filter(factors: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """过滤器落地：不可交易处全部因子置 NaN（评估宇宙=可交易宇宙）。"""
    out = factors.copy()
    out.loc[~mask.to_numpy(), FACTOR_NAMES] = np.nan
    return out


# ════════════════════════════════════════════════════════════════════════
# 4. 因子长表转换 & 全检流程
# ════════════════════════════════════════════════════════════════════════
def to_factor_frame(factors: pd.DataFrame, name: str) -> pd.DataFrame:
    """[symbol,date,value] 长表，NaN 行剔除（ic_layer 契约）。"""
    f = factors[["date", "symbol"]].copy()
    f["value"] = factors[name]
    return f.dropna(subset=["value"]).reset_index(drop=True)


def run_full_check(factors_filtered: pd.DataFrame,
                   panel: pd.DataFrame,
                   names: list[str] | None = None,
                   mc_n: int = MC_N,
                   progress: bool = True) -> dict:
    """每个因子过 evaluate_factor + decile_analysis + mc_baseline 全检。

    返回 {name: {"evaluate": dict, "decile": dict, "mc": dict}}，原样保留
    ic_layer 的返回结构（键名以任务2实现为准，本层不臆造口径）。
    """
    results = {}
    for name in (names or FACTOR_NAMES):
        if progress:
            print(f"[check] {name} ...", flush=True)
        fdf = to_factor_frame(factors_filtered, name)
        results[name] = {
            "evaluate": evaluate_factor(fdf, panel, horizons=HORIZONS, min_coverage=30),
            "decile": decile_analysis(fdf, panel, horizon=DECILE_HORIZON, n_groups=10),
            "mc": mc_baseline(fdf, panel, horizon=MC_HORIZON, n=mc_n, seed=MC_SEED),
        }
    return results


# ════════════════════════════════════════════════════════════════════════
# 5. 高波动做T候选池（COMBO 最新截面 TOP100）
# ════════════════════════════════════════════════════════════════════════
def board_of(symbol: str) -> str:
    """按代码前缀标注板块归属。"""
    code = str(symbol).split(".")[-1]
    if code.startswith("68"):
        return "科创板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith(("92", "8", "4")):
        return "北交所"
    return "其他"


def load_holdings_symbols(holdings_path: Path = HOLDINGS_JSON) -> dict[str, str]:
    """holdings.json -> {纯代码: name}。只读，绝不写生产 state。"""
    p = Path(holdings_path)
    if not p.exists():
        return {}
    h = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for key, v in h.items():
        if isinstance(v, dict):
            gm = str(v.get("gm_symbol") or key)
            code = _norm_sym(pd.Series([gm])).iloc[0]
            out[code] = str(v.get("name", key))
    return out


def candidate_pool(factors_filtered: pd.DataFrame,
                   holdings_path: Path = HOLDINGS_JSON,
                   top_n: int = TOP_N,
                   asof: pd.Timestamp | None = None) -> pd.DataFrame:
    """COMBO 最新截面 TOP_N 榜单，含板块/AMOUNT20/ATR20/已持有标注。"""
    f = factors_filtered.dropna(subset=["COMBO"])
    last = pd.to_datetime(asof) if asof is not None else f["date"].max()
    cross = f[f["date"] == last].copy()
    cross = cross.sort_values("COMBO", ascending=False).head(top_n).reset_index(drop=True)

    held = load_holdings_symbols(holdings_path)
    cross.insert(0, "rank", np.arange(1, len(cross) + 1))
    cross["board"] = cross["symbol"].map(board_of)
    code = _norm_sym(cross["symbol"])
    cross["held"] = code.isin(held.keys())
    cross["held_name"] = code.map(held).fillna("")
    cols = ["rank", "symbol", "board", "COMBO", "ATR20", "AMOUNT20", "held", "held_name", "date"]
    return cross[cols]


# ════════════════════════════════════════════════════════════════════════
# 6. 主流程
# ════════════════════════════════════════════════════════════════════════
def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.ndarray,)):
        return [_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Series):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return {str(c): {str(i): _jsonable(v) for i, v in obj[c].items()} for c in obj.columns}
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = argv or sys.argv[1:]
    only = None
    skip_mc = "--skip-mc" in argv
    use_snapshot = "--use-snapshot" in argv
    for a in argv:
        if a.startswith("--only="):
            only = a.split("=", 1)[1].split(",")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[load] panel <- {PANEL_DIR}", flush=True)
    panel = load_panel(str(PANEL_DIR))
    panel = _prep_panel(panel)
    print(f"[load] {panel['symbol'].nunique()} symbols, {len(panel)} rows, "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}", flush=True)

    snap = RESULTS_DIR / "factors_filtered.parquet"
    if use_snapshot and snap.exists():
        factors_f = pd.read_parquet(snap)
        factors_f["date"] = pd.to_datetime(factors_f["date"])
        print(f"[factor] 复用快照 {snap.name} ({len(factors_f)} 行)", flush=True)
    else:
        universe = load_universe(PANEL_DIR)
        print(f"[universe] {'缺 universe 文件' if universe is None else f'{len(universe)} 行, 列={list(universe.columns)}'}",
              flush=True)
        print("[factor] 计算因子 ...", flush=True)
        factors = compute_factor_series(panel)
        mask = build_tradable_mask(panel, factors, universe)
        print(f"[filter] 可交易 {int(mask.sum())}/{len(mask)} 行 "
              f"({mask.mean():.1%}), ST闸={'生效' if mask.attrs.get('st_filter_active') else '未生效(缺名称字段)'}",
              flush=True)
        factors_f = apply_filter(factors, mask)
        factors_f.to_parquet(snap, index=False)
        print(f"[save] 因子值快照 -> {snap}", flush=True)

    names = only or FACTOR_NAMES
    results = {}
    for name in names:
        print(f"[check] {name} ...", flush=True)
        fdf = to_factor_frame(factors_f, name)
        res = {
            "evaluate": evaluate_factor(fdf, panel, horizons=HORIZONS, min_coverage=30),
            "decile": decile_analysis(fdf, panel, horizon=DECILE_HORIZON, n_groups=10),
        }
        if not skip_mc:
            res["mc"] = mc_baseline(fdf, panel, horizon=MC_HORIZON, n=MC_N, seed=MC_SEED)
        results[name] = res
        # 每个因子完成即落盘，防超时丢进度
        (RESULTS_DIR / f"check_{name}.json").write_text(
            json.dumps(_jsonable(res), ensure_ascii=False, indent=2), encoding="utf-8")

    (RESULTS_DIR / "factor_health_all.json").write_text(
        json.dumps(_jsonable(results), ensure_ascii=False, indent=2), encoding="utf-8")

    print("[pool] 生成 TOP100 候选池 ...", flush=True)
    pool = candidate_pool(factors_f)
    pool.to_csv(RESULTS_DIR / "top100_pool.csv", index=False, encoding="utf-8-sig")
    n_held = int(pool["held"].sum())
    print(f"[pool] 截面日={pool['date'].iloc[0].date()} 已持有重合={n_held}/{len(pool)}", flush=True)
    print(pool.head(15).to_string(index=False), flush=True)
    print(f"[done] 产物目录: {RESULTS_DIR}", flush=True)


if __name__ == "__main__":
    main()
