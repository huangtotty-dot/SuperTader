# -*- coding: utf-8 -*-
"""GP 搜索引擎主引擎（因子挖掘阶段2 · 任务 S2-6，2026-09-19）。

严格按 `doc/research/2026-09-19_AlphaGen适配设计.md`（S2-1）施工：

- **引擎**：vendor 的 AlphaGen 改造版 gplearn 0.4.2（`gp_vendor/`，见其 README）。
  GP 不在数值上进化，而在**表达式字符串**上进化：X = 1×N object 字符串数组，
  算子 = 字符串拼接器，个体 execute(X) 的产物是表达式字符串，适应度 eval 字符串。
- **算子**：从 `gp_ops.build_function_set()` 取（只读复用），`cs_rank` 默认排除
  （单票时序口径下对 1D 序列做截面秩语义扭曲；--include-csrank 可开）。
  **常数终端不注册**：gp_ops 算子集无元素级算术（add/sub/mul/div），常数没有
  合法消费者，只会产生形状错配的垃圾个体——与设计文档 §5.2 的常数表刻意不同，
  理由在此声明。
- **适应度**（§3.3 候选二，主用）：单票时序 IC。
  每只股票：因子先做同日同时刻历史 z-score（过去 14 日同一 bar 序位，
  复用 eval_factor.zscore_matrix 口径），再与 label 序列做**逐 bar** 相关
  （§3.3：统计单元 = 单票时序 240 bar/日）。fitness = mean_i(IC_i) − λ·neg_frac，
  neg_frac = IC_i ≤ 0 的票占比（票间一致性惩罚）。
  **只用 IS 段**（2025-09-14 ~ 2026-05-31）；OOS（2026-06-01 起）只在 finalize
  时记录一次，不参与任何选择。
- **label**（§3.2 口径A 的修正版，默认 close30）：r[t] = C[t+30]/O[t+1] − 1。
  设计文档 §6.3 标注原口径A（max(high[t+1..t+30])/open[t+1]−1）存在乐观偏差
  （不可保证卖在最高），故默认改用 30 根后**收盘价**这一可成交口径；
  原口径保留为 --label maxh30 仅作参照。两口径尾部都在日内截断（NaN），
  禁止跨日、禁止跨 IS/OOS 边界。
- **callback 三件事**（§五/§六）：每 4 代（--mc-every）
  ① 候选入池互相关 |ρ|<0.7 去重（时序口径：逐票 z 序列相关再取均值，取绝对值）；
  ② top-20 对 label 做**日块级打乱** MC（保留日内形态、破坏可预测对齐），
     真实 IC ≤ null 95 分位 → 标 SUSPECT；
  ③ top-k 费后净均抽检（双边 0.136%，次根开盘成交、30 根后收盘出场、
     每日 ≤3 次穿越触发，配逐腿随机基线）。
- **产出**：候选因子 JSON 台账 + `final_review()` 终审桥（复用 eval_factor
  的 load_code/zscores/run_entries/random_baseline/_aggregate，不改其源码）。
- **性能纪律**：n_jobs=1 单进程；适应度按表达式字符串全局 cache；
  z 序列 cache 只保留当前池成员；--sample-every 隔日抽样仅为冒烟加速。

用法：
  python gp_miner.py --smoke                      # pop=50/gen=3 冒烟（隔日抽样）
  python gp_miner.py --pop 1000 --gen 40 --seed 0 # 正式轮（预估 1-3 小时）
  python gp_miner.py --review "ts_mean_w10(close)" --sign 1 --exit hold  # 终审桥
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
FM_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(FM_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from t_io.validation.factor_mining import gp_fast_ops, gp_ops, minute_data  # noqa: E402
from t_io.validation.factor_mining.gp_vendor.fitness import make_fitness  # noqa: E402
from t_io.validation.factor_mining.gp_vendor.functions import make_function  # noqa: E402
from t_io.validation.factor_mining.gp_vendor.genetic import SymbolicRegressor  # noqa: E402

# ── 常量（口径集中在此，全部可在 CLI 覆盖）────────────────────────────────
IS_START, IS_END = "2025-09-14", "2026-05-31"
OOS_START, OOS_END = "2026-06-01", "2026-08-26"
LABEL_H = 30                # label 前瞻 bar 数（次根开盘成交，30 根后收盘出场）
from core.cost_model import round_trip as _cost_rt  # noqa: E402
FEE = _cost_rt()            # 单常数往返成本，真源 core/cost_model.py
                            # （默认股票 0.0006908；ST_COST_VENUE=legacy → 0.00136）
RHO_THR = 0.7               # 入池互相关阈值（取绝对值，时序口径）
TOKEN_MAX = 20              # 表达式括号 token 硬闸（alphagen 口径）
POOL_CAP = 20               # 候选池容量
Z_LOOK, Z_MIN_HIST = 14, 5  # 同日同时刻 z-score 窗口/最小历史日（eval_factor 口径）
CONS_PEN = 0.05             # 票间一致性惩罚系数 λ（neg_frac 每 +1，fitness −0.05）
TRIG_LO, TRIG_HI = 0.005, 0.10   # 全域触发率闸门（§3.3 推荐组合）
MAX_ENTRIES = 3             # 费后抽检每日触发上限（生产 config 口径）
# ── 2026-09-21 结构约束（G1 一轮教训：深嵌套×长窗口→NaN 覆盖→触发稀疏不可判）──
WIN_BUDGET = 60             # 表达式窗口预算：根到叶路径上 _wN 窗口和上限（根因闸门）
MIN_FINITE = 0.5            # 单票因子矩阵有限值占比下限，低于则该票不计入（稀疏判死）
TERMINALS = ["open", "high", "low", "close", "volume", "vwap", "amount"]

RESULTS_DIR = FM_DIR / "results" / "gp_mine"

# ── z-score：热路径用本地向量化 fast_zscore；与 eval_factor.zscore_matrix ──
# 逐点对照见 test_gp_miner.py::test_fast_zscore_matches_eval_factor。
def fast_zscore(M, look=Z_LOOK, min_hist=Z_MIN_HIST):
    """[n_days, L] 上做过去 look 日同刻 z-score（严格因果，不含当日）。

    与 eval_factor.zscore_matrix 同式（nanmean/nanstd ddof=1，min_hist 按日数），
    滑窗视图 + 二遍方差全向量化：~100ms → ~3ms（217 日 × 241 根实测）。
    顶部补 look 行 NaN，使每行的「过去 look 日」窗口定长；min_hist 按日数门控
    （k < min_hist 的行掩出），窗内数据 NaN 由计数剔除（同 nanmean 语义）。
    """
    n, L = M.shape
    Z = np.full_like(M, np.nan)
    if n < min_hist + 1:
        return Z
    Mpad = np.vstack([np.full((look, L), np.nan), M])
    W = np.lib.stride_tricks.sliding_window_view(Mpad, look, axis=0)  # (n+1, L, look)
    fin = np.isfinite(W)
    W0 = np.where(fin, W, 0.0)
    cnt = fin.sum(axis=-1)                              # (n+1, L)
    # W[k] = Mpad[k:k+look] = M 行 [k-look, k)，即 M 行 k 的历史窗口（严格因果）
    m = W0.sum(axis=-1) / np.maximum(cnt, 1)
    dev2 = (np.where(fin, (W - m[..., None]) ** 2, 0.0)).sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        var = dev2 / np.where(cnt > 1, cnt - 1, np.nan)
    s = np.sqrt(np.maximum(var, 0.0))
    day_ok = np.arange(n + 1) >= min_hist               # M 行 k 的历史日数 = min(k, look) ≥ min_hist
    ok = (cnt >= 2) & np.isfinite(s) & (s > 1e-12) & day_ok[:, None]
    m, s, ok = m[:n], s[:n], ok[:n]
    with np.errstate(invalid="ignore", divide="ignore"):
        Z = np.where(ok, (M - m) / np.where(ok, s, np.nan), np.nan)
    return Z


_zscore_matrix = fast_zscore


# ══════════════════════════════════════════════════════════════════════════
# 1. 字符串表达式进化层（vendor gplearn 的字符串用法）
# ══════════════════════════════════════════════════════════════════════════
def _ctor1(name):
    """一元字符串拼接器：f(a) -> 'name(a)'。逐元素构造，numpy2 兼容。

    （alphagen 原版靠 object/unicode 数组与 str 的逐元素加法，numpy≥2.0 已删
    字符串 add ufunc，故改为显式逐元素构造；同时天然通过 make_function 的
    形状探针——任意数值输入也能返回同形 object 数组。）
    """
    def f(a):
        return np.array([name + "(" + str(x) + ")" for x in a], dtype=object)
    return f


def _ctor2(name):
    def f(a, b):
        return np.array([name + "(" + str(x) + "," + str(y) + ")" for x, y in zip(a, b)],
                        dtype=object)
    return f


def _ctor3(name):
    def f(a, b, c):
        return np.array([name + "(" + str(x) + "," + str(y) + "," + str(z) + ")"
                         for x, y, z in zip(a, b, c)], dtype=object)
    return f


_CTORS = {1: _ctor1, 2: _ctor2, 3: _ctor3}


def build_gp_function_set(windows=None, include_csrank=False):
    """从 gp_ops.build_function_set() 取算子名/元数，返回 (gp_functions, eval_namespace)。

    gp_functions: vendor gplearn make_function 包装后的字符串拼接器列表；
    eval_namespace: {算子名: 数值函数}，来自 `gp_fast_ops`（gp_ops 的向量化
    对齐实现，逐点对照见测试）——GP 进化的名字/签名以 gp_ops 为准。
    """
    fns = gp_ops.build_function_set() if windows is None else gp_ops.build_function_set(windows)
    fast = gp_fast_ops.build_fast_namespace(
        gp_ops.DEFAULT_WINDOWS if windows is None else windows,
        include_csrank=include_csrank)
    gp_fns, ns = [], {}
    for name, func, arity in fns:
        if name == "cs_rank" and not include_csrank:
            continue
        gp_fns.append(make_function(function=_CTORS[arity](name), name=name, arity=arity))
        ns[name] = fast[name]
    return gp_fns, ns


def make_terminal_X():
    """X_train = 1×N object 字符串数组（alphagen 用法）；y 为占位。"""
    return np.array([TERMINALS], dtype=object), np.array([[1]])


# ══════════════════════════════════════════════════════════════════════════
# 2. 表达式求值（逐日切片，天然杜绝 rolling 跨日拼接——§6.3 风险项）
# ══════════════════════════════════════════════════════════════════════════
class ExprEvaluator:
    """表达式 → 每票 (n_days, L) 因子矩阵。compile/子树结果按字符串缓存。"""

    def __init__(self, ops_ns, memo_on=True, memo_cap=1500):
        self._globals = {"__builtins__": {}}
        self._globals.update(ops_ns)
        self._compile_cache = {}
        self._memo = OrderedDict()             # (symbol, 子表达式) -> float32 数组
        self.memo_on = memo_on
        self.memo_cap = int(memo_cap)

    def compile(self, expr):
        code = self._compile_cache.get(expr)
        if code is None:
            code = compile(expr, "<gp_expr>", "eval")
            self._compile_cache[expr] = code
        return code

    def eval_day(self, expr, day_terms):
        """单日求值：day_terms = {terminal: 1D array}。异常/非标量数组 → None。"""
        try:
            out = eval(self.compile(expr), self._globals, day_terms)
        except Exception:
            return None
        out = np.asarray(out, dtype=float).ravel()
        n = len(day_terms["close"])
        if out.shape[0] == 1:      # 退化为标量（如纯常数表达式）→ 常数列
            return np.full(n, out[0])
        if out.shape[0] != n:
            return None
        return out

    def eval_matrix(self, expr, panel, upto_row=None):
        """整票求值 → (upto_row, L) 矩阵：2D 终端 + AST 子树记忆化。

        GP 种群共享大量子树（如 ts_mean_w10(close) 出现在数百个个体中），
        记忆化以 (panel.symbol, 子表达式源串) 为键、float32 存储、LRU 上限
        自我管理——这是正式轮（4 万评估）能跑进小时级的关键杠杆。
        逐日语义由 gp_fast_ops 的 axis=-1 滚动保证，天然不跨日。
        表达式异常 → 全 NaN。upto_row：只求前 upto_row 日（不含）。
        """
        n = panel.n_days if upto_row is None else min(int(upto_row), panel.n_days)
        terms = {k: v[:n] for k, v in panel.term_mats.items()}
        try:
            out = self._eval_node(ast.parse(expr, mode="eval").body, terms,
                                  expr, panel.symbol)
            out = np.asarray(out, dtype=float)
        except Exception:
            return np.full((n, panel.L), np.nan)
        if out.ndim == 0:                      # 纯常数表达式 → 常数矩阵
            return np.full((n, panel.L), float(out))
        if out.ndim == 1:
            out = np.broadcast_to(out, (n, out.shape[0])).copy()
        if out.shape != (n, panel.L):
            return np.full((n, panel.L), np.nan)
        return out

    def _eval_node(self, node, terms, src, sym):
        """AST 递归求值 + 子树 memo（键 = (票, 子表达式源串)，纯函数语义安全）。"""
        if isinstance(node, ast.Name):
            return terms[node.id]              # 终端直接引用（不复制）
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = self._globals[node.func.id]   # 未知名 → KeyError → 上层捕
            key = None
            if self.memo_on:
                n_rows = next(iter(terms.values())).shape[0]
                key = (sym, n_rows, ast.get_source_segment(src, node))
                hit = self._memo.get(key)
                if hit is not None:
                    self._memo.move_to_end(key)
                    return hit
            args = [self._eval_node(a, terms, src, sym) for a in node.args]
            out = fn(*args)
            if key is not None:
                self._memo[key] = np.asarray(out, dtype=np.float32)
                if len(self._memo) > self.memo_cap:      # LRU 批量驱逐
                    for old in list(self._memo)[: self.memo_cap // 2]:
                        del self._memo[old]
            return out
        raise ValueError("非法表达式节点")


# ══════════════════════════════════════════════════════════════════════════
# 3. 面板：每票对齐 (n_days, L) 矩阵 + label（IS/OOS 硬边界）
# ══════════════════════════════════════════════════════════════════════════
class StockPanel:
    """单票分钟面板：模长对齐日 × 7 终端矩阵 + label 矩阵 + IS 掩码。"""

    def __init__(self, symbol, dates, R, term_mats):
        self.symbol = symbol
        self.dates = dates                       # list[str]，升序
        self.term_mats = term_mats               # dict[terminal -> (n_days, L)]
        self.R = R                               # (n_days, L) label
        self.n_days = len(dates)
        self.L = R.shape[1]
        self.is_mask = np.array([d < OOS_START for d in dates], dtype=bool)

    def is_rows(self):
        return np.flatnonzero(self.is_mask)


def _label_matrix(O, H, C, label_kind):
    """label 矩阵（日内截断，不跨日不跨边界），向量化实现。

    close30（默认，修正口径）: r[t] = C[t+30]/O[t+1] − 1，t ≤ L−31
    maxh30（原口径A，仅参照）: r[t] = max(H[t+1..t+30])/O[t+1] − 1
    closeeod（2026-09-21 G1 二轮后新增）: r[t] = C[L−2]/O[t+1] − 1，
        持有到日内倒数第 2 根（≈14:55 尾盘强平口径），t ≤ L−3。
        用途：把单笔毛利润空间从 30 分钟放大到「入场→尾盘」，对冲 0.136% 成本墙。
    """
    n_days, L = C.shape
    R = np.full((n_days, L), np.nan)
    if label_kind == "closeeod":
        m = L - 2                                    # t ∈ [0, L−3]，入场 t+1 ≤ L−2
        if m <= 0:
            return R
        entry = O[:, 1:m + 1]
        exit_px = C[:, L - 2:L - 1]                  # (n_days,1) 广播到 m 列
        with np.errstate(divide="ignore", invalid="ignore"):
            val = exit_px / np.where(entry > 0, entry, np.nan) - 1.0
        R[:, :m] = np.where(np.isfinite(entry) & (entry > 0), val, np.nan)
        return R
    m = L - LABEL_H                                  # 有效 t 数：t ∈ [0, L−31]
    if m <= 0:
        return R
    entry = O[:, 1:m + 1]                            # t+1 ∈ [1, L−30]
    with np.errstate(divide="ignore", invalid="ignore"):
        if label_kind == "maxh30":
            win = np.lib.stride_tricks.sliding_window_view(H, LABEL_H, axis=-1)
            top = np.nanmax(win[:, 1:m + 1], axis=-1)      # H[t+1..t+30]
            val = top / np.where(entry > 0, entry, np.nan) - 1.0
        else:
            val = C[:, LABEL_H:LABEL_H + m] / np.where(entry > 0, entry, np.nan) - 1.0
    R[:, :m] = np.where(np.isfinite(entry) & (entry > 0), val, np.nan)
    return R


def build_stock_panel(symbol, df, label_kind="close30", max_is_days=None):
    """DataFrame → StockPanel。模长对齐 + 时间标签一致性校验（eval_factor 同款纪律）。

    max_is_days：只保留最近 N 个 IS 日（**仅冒烟加速用**，正式跑必须为 None；
    截断的是 IS 样本量，不动 OOS 边界）。
    """
    if df.empty:
        return None
    t = df["time"]
    day_codes = (t.dt.year * 10000 + t.dt.month * 100 + t.dt.day).to_numpy()
    hhmm = (t.dt.hour * 100 + t.dt.minute).to_numpy()
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    amt = df["amount"].to_numpy(float)

    uniq, starts = np.unique(day_codes, return_index=True)
    ends = np.r_[starts[1:], len(day_codes)]
    lens = ends - starts
    L = int(np.bincount(lens).argmax())

    # 模长 + 时间标签一致性过滤（先对齐全部日，再截 IS）
    ref_hhmm = None
    kept = []                                    # (date_str, slice_start)
    for u, s, e in zip(uniq, starts, ends):
        if e - s != L:
            continue
        lab = hhmm[s:e]
        if ref_hhmm is None:
            ref_hhmm = lab
        elif not np.array_equal(lab, ref_hhmm):
            continue                             # 标签不一致日剔除（对齐 eval_factor）
        kept.append((f"{u // 10000:04d}-{u // 100 % 100:02d}-{u % 100:02d}", int(s)))

    if max_is_days is not None:
        is_kept = [k for k in kept if k[0] < OOS_START]
        oos_kept = [k for k in kept if k[0] >= OOS_START]
        kept = is_kept[-int(max_is_days):] + oos_kept
    if len(kept) < 40:
        return None

    idx = np.concatenate([np.arange(s, s + L) for _d, s in kept])
    n_days = len(kept)
    term_mats = {
        "open": o[idx].reshape(n_days, L), "high": h[idx].reshape(n_days, L),
        "low": lo[idx].reshape(n_days, L), "close": c[idx].reshape(n_days, L),
        "volume": v[idx].reshape(n_days, L), "amount": amt[idx].reshape(n_days, L),
    }
    cum_v = np.cumsum(term_mats["volume"], axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        term_mats["vwap"] = np.where(
            cum_v > 0,
            np.cumsum(term_mats["amount"], axis=-1) / np.where(cum_v > 0, cum_v, 1.0),
            term_mats["close"])
    R = _label_matrix(term_mats["open"], term_mats["high"], term_mats["close"],
                      label_kind)
    return StockPanel(symbol, [d for d, _s in kept], R, term_mats)


# ══════════════════════════════════════════════════════════════════════════
# 4. 适应度：单票时序 IC（§3.3 候选二）+ cache + 硬闸
# ══════════════════════════════════════════════════════════════════════════
def pearson_ic(a, b):
    """有限对上的 Pearson 相关；对数 <30 或零方差 → NaN。"""
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return np.nan
    x, y = a[m], b[m]
    sx, sy = x.std(), y.std()
    if sx < 1e-12 or sy < 1e-12:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def spearman_ic(a, b):
    from scipy.stats import rankdata
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return np.nan
    return pearson_ic(rankdata(a[m]), rankdata(b[m]))


_IC_FNS = {"pearson": pearson_ic, "spearman": spearman_ic}


def token_len(expr):
    return expr.count("(") + expr.count(")")


def window_path_budget(expr):
    """根到叶路径上 _wN 窗口参数和的最大值（日内暖机期 ≈ 路径窗口和，兄弟分支不叠加）。

    深嵌套长窗口（如 amihud_w60∘ts_cov_w30∘ts_std_w5 = 95 根暖机）会大面积吃掉
    每日 241 根有效 bar——G1 一轮 top 候选在 000506 上 99.4% NaN 的根因。
    解析失败返回 inf（交给上层判死，不在此处兜底）。
    """
    def _walk(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            w = 0
            name = node.func.id
            if "_w" in name:
                try:
                    w = int(name.rsplit("_w", 1)[1])
                except ValueError:
                    w = 0
            return w + max((_walk(a) for a in node.args), default=0)
        return 0
    try:
        return _walk(ast.parse(expr, mode="eval").body)
    except Exception:
        return float("inf")


class FitnessEvaluator:
    """gplearn metric 闭包：fitness = mean_i(IC_i) − λ·neg_frac（只用 IS）。

    cache 按表达式字符串去重（alphagen 的隐形记忆库模式）；
    detail cache 保留每票 IC / 触发率，供 callback 与台账复用。
    """

    DEATH = -1.0

    def __init__(self, panels, evaluator, corr="pearson", cons_pen=CONS_PEN,
                 trig_gate=True, sample_every=1, min_stocks=10, expr_budget=30.0):
        self.panels = panels
        self.ev = evaluator
        self.ic_fn = _IC_FNS[corr]
        self.cons_pen = cons_pen
        self.trig_gate = trig_gate
        self.sample_every = int(sample_every)
        self.min_stocks = int(min_stocks)
        self.expr_budget = float(expr_budget)   # 单表达式全池评估超时（秒）→ 判死
        self.cache = {}          # expr -> fitness float
        self.detail = {}         # expr -> dict（mean_ic/ics/neg_frac/trig_rate）

    def metric(self, _y, y_pred, _w):
        expr = str(y_pred[0])
        if expr not in self.cache:
            self.detail[expr] = self._eval(expr)
            self.cache[expr] = self.detail[expr]["fitness"]
        return self.cache[expr]

    def _eval(self, expr):
        if token_len(expr) > TOKEN_MAX:
            return {"fitness": self.DEATH, "death": "token_len"}
        if window_path_budget(expr) > WIN_BUDGET:
            return {"fitness": self.DEATH, "death": "window_budget"}
        ics, trig_nums, trig_dens = [], 0, 0
        t_start = time.perf_counter()
        for p in self.panels:
            if time.perf_counter() - t_start > self.expr_budget:
                return {"fitness": self.DEATH, "death": "timeout",
                        "n_stocks": len(ics)}
            is_last = int(p.is_rows()[-1]) + 1
            try:
                F = self.ev.eval_matrix(expr, p, upto_row=is_last)
            except Exception:
                return {"fitness": self.DEATH, "death": "eval_error"}
            if float(np.isfinite(F).mean()) < MIN_FINITE:
                continue                     # 稀疏票不计入（2026-09-21 结构约束）
            Z = _zscore_matrix(F, Z_LOOK, Z_MIN_HIST)
            rows = p.is_rows()[:: self.sample_every]
            z = Z[rows].ravel()
            r = p.R[rows].ravel()
            ic = self.ic_fn(z, r)
            if np.isfinite(ic):
                ics.append(ic)
            # 触发率：|z| 穿越 ±1（双向）/ 有限 bar 数
            zf = Z[rows]
            prev, cur = zf[:, :-1], zf[:, 1:]
            up = (cur > 1.0) & (prev <= 1.0)
            dn = (cur < -1.0) & (prev >= -1.0)
            trig_nums += int(np.nansum(up) + np.nansum(dn))
            trig_dens += int(np.isfinite(cur).sum())
        if len(ics) < self.min_stocks:
            return {"fitness": self.DEATH, "death": "too_few_stocks"}
        rate = trig_nums / max(trig_dens, 1)
        if self.trig_gate and not (TRIG_LO <= rate <= TRIG_HI):
            return {"fitness": self.DEATH, "death": f"trig_rate={rate:.4f}",
                    "trig_rate": rate}
        mean_ic = float(np.mean(ics))
        neg_frac = float(np.mean([ic <= 0 for ic in ics]))
        fit = mean_ic - self.cons_pen * neg_frac
        return {"fitness": float(fit), "mean_ic": mean_ic, "ics": ics,
                "neg_frac": neg_frac, "trig_rate": rate, "n_stocks": len(ics)}


# ══════════════════════════════════════════════════════════════════════════
# 5. callback 三件事：|ρ|<0.7 去重组池 / 日块 MC / 费后抽检
# ══════════════════════════════════════════════════════════════════════════
def day_block_shuffle(R, rows, rng):
    """label 日块打乱：只在给定日行集合内整行置换，保留日内形态。

    返回打乱后的 (len(rows), L) 矩阵；不跨日块（每行整体搬运）。
    """
    perm = rng.permutation(len(rows))
    return R[rows[perm]]


def z_flat_is(evaluator, fit_ev, expr, panel):
    """池成员的 IS z 序列（float32 缓存）。"""
    is_last = int(panel.is_rows()[-1]) + 1
    F = evaluator.eval_matrix(expr, panel, upto_row=is_last)
    Z = _zscore_matrix(F, Z_LOOK, Z_MIN_HIST)
    return Z[panel.is_rows()].ravel().astype(np.float32)


def fee_check(expr, ev_expr, panels, sign, seed=20260919, k_rand=5, sample_every=1,
              label_kind="close30"):
    """费后净均抽检（§3.3 候选三简化版，0.136% 双边）。

    触发：sign*z 上穿 +1（穿越口径，同 eval_factor）；成交：次根开盘；
    出场：close30 → 30 根后收盘或日内倒数第 2 根；closeeod → 日内倒数第 2 根（持有到尾盘）；
    每日 ≤MAX_ENTRIES 次。配逐腿随机基线（同票同日同方向随机入场）。
    """
    rng = np.random.RandomState(seed)
    nets, rands = [], []
    for p in panels:
        is_last = int(p.is_rows()[-1]) + 1
        F = ev_expr.eval_matrix(expr, p, upto_row=is_last)
        Z = _zscore_matrix(F, Z_LOOK, Z_MIN_HIST)
        rows = p.is_rows()[::sample_every]
        L = p.L
        O2, C2 = p.term_mats["open"], p.term_mats["close"]
        for r in rows:
            o, c = O2[r], C2[r]
            z = sign * Z[r]
            entries = []
            for t in range(L - 2):
                if not (np.isfinite(z[t]) and np.isfinite(z[t - 1] if t > 0 else np.nan)):
                    continue
                if z[t - 1] <= 1.0 < z[t]:
                    entries.append(t + 1)          # 次根开盘成交
                if len(entries) >= MAX_ENTRIES:
                    break
            for e in entries:
                if not np.isfinite(o[e]) or o[e] <= 0:
                    continue
                x = L - 2 if label_kind == "closeeod" else min(e + LABEL_H - 1, L - 2)
                if not np.isfinite(c[x]):
                    continue
                nets.append(sign * (c[x] / o[e] - 1.0) - FEE)
                for _ in range(k_rand):
                    b = int(rng.randint(1, L - 1))
                    xb = L - 2 if label_kind == "closeeod" else min(b + LABEL_H - 1, L - 2)
                    if np.isfinite(o[b]) and o[b] > 0 and np.isfinite(c[xb]):
                        rands.append(sign * (c[xb] / o[b] - 1.0) - FEE)
    if not nets:
        return {"n": 0}
    rm = float(np.mean(rands)) if rands else 0.0
    return {"n": len(nets), "net_mean": round(float(np.mean(nets)), 4),
            "rand_mean": round(rm, 4),
            "delta": round(float(np.mean(nets)) - rm, 4)}


class GPCallback:
    """每 mc_every 代：① |ρ|<0.7 去重组池 ② top-20 日块 MC ③ top-k 费后抽检。"""

    def __init__(self, fit_ev, ev_expr, panels, ledger_path, mc_every=4, n_mc=30,
                 topk=5, seed=0, verbose=True, extra_meta=None, label_kind="close30"):
        self.fit_ev = fit_ev
        self.ev_expr = ev_expr
        self.panels = panels
        self.label_kind = label_kind
        self.ledger_path = Path(ledger_path)
        self.mc_every = mc_every
        self.n_mc = n_mc
        self.topk = topk
        self.extra_meta = extra_meta or {}
        self.rng = np.random.RandomState(20260919 + seed)
        self.gen = 0
        self.verbose = verbose
        self.ledger = {}            # expr -> 台账条目
        self.pool = []              # 当前池 [expr]
        self._zcache = {}           # expr -> {symbol: z_flat}（仅池成员）
        self.records = []           # 每次 callback 的摘要

    def __call__(self):
        self.gen += 1
        if self.gen % self.mc_every != 0:
            return
        t0 = time.perf_counter()
        cands = sorted((e for e, f in self.fit_ev.cache.items() if f > -0.5),
                       key=lambda e: -self.fit_ev.cache[e])[:60]   # 组池只看 top-60
        # ① 组池去重（时序口径 |ρ|<0.7）
        self._zcache = {}
        self.pool = []
        for e in cands:
            if len(self.pool) >= POOL_CAP:
                break
            zs = {p.symbol: z_flat_is(self.ev_expr, self.fit_ev, e, p) for p in self.panels}
            ok = True
            for pe in self.pool:
                rhos = [pearson_ic(zs[p.symbol], self._zcache[pe][p.symbol])
                        for p in self.panels]
                rhos = [r for r in rhos if np.isfinite(r)]
                if rhos and abs(float(np.mean(rhos))) >= RHO_THR:
                    ok = False
                    break
            if ok:
                self.pool.append(e)
                self._zcache[e] = zs
        # ② 日块 MC（top-20 = 池成员）
        for e in self.pool:
            det = self.fit_ev.detail.get(e, {})
            real_ic = det.get("mean_ic")
            if real_ic is None:
                continue
            null = []
            for _ in range(self.n_mc):
                nics = []
                for p in self.panels:
                    rows = p.is_rows()
                    R_sh = day_block_shuffle(p.R, rows, self.rng)
                    ic = pearson_ic(self._zcache[e][p.symbol].astype(float), R_sh.ravel())
                    if np.isfinite(ic):
                        nics.append(ic)
                null.append(float(np.mean(nics)) if nics else np.nan)
            null = [x for x in null if np.isfinite(x)]
            q95 = float(np.percentile(null, 95)) if null else np.nan
            verdict = "PASS" if (np.isfinite(q95) and real_ic > q95) else "SUSPECT"
            self._entry(e)["mc"] = {"n": len(null),
                                    "null_mean": round(float(np.mean(null)), 5) if null else None,
                                    "null_q95": round(q95, 5),
                                    "real_ic": round(real_ic, 5),
                                    "verdict": verdict}
        # ③ 费后净均抽检 top-k
        for e in self.pool[: self.topk]:
            det = self.fit_ev.detail.get(e, {})
            sign = 1 if (det.get("mean_ic") or 0) >= 0 else -1
            self._entry(e)["fee"] = fee_check(e, self.ev_expr, self.panels, sign,
                                              label_kind=self.label_kind)
            self._entry(e)["sign_hint"] = sign
        dt = time.perf_counter() - t0
        rec = {"gen": self.gen, "pool": len(self.pool), "sec": round(dt, 1),
               "top": [{"expr": e[:60], "fit": round(self.fit_ev.cache[e], 4)}
                       for e in self.pool[:3]]}
        self.records.append(rec)
        if self.verbose:
            print(f"  [callback] gen={self.gen} 池={len(self.pool)} "
                  f"cache={len(self.fit_ev.cache)} 用时{dt:.0f}s")
            for t in rec["top"]:
                print(f"    top fit={t['fit']:.4f}  {t['expr']}")
        self.dump()

    def _entry(self, expr):
        if expr not in self.ledger:
            det = self.fit_ev.detail.get(expr, {})
            self.ledger[expr] = {
                "expr": expr,
                "token_len": token_len(expr),
                "fitness": round(self.fit_ev.cache.get(expr, np.nan), 5),
                "is_ic": _r5(det.get("mean_ic")),
                "is_ic_std": _r5(float(np.std(det["ics"])) if det.get("ics") else None),
                "neg_frac": _r5(det.get("neg_frac")),
                "trig_rate": _r5(det.get("trig_rate")),
                "n_stocks": det.get("n_stocks"),
                "in_pool": False, "oos_ic": None, "mc": None, "fee": None,
                "final_review": None,
            }
        self.ledger[expr]["in_pool"] = True
        return self.ledger[expr]

    def finalize(self):
        """收尾：池成员记录 OOS-IC（仅记录、不参与选择）+ 终审桥提示，落盘。"""
        for e in self.pool:
            oos = []
            for p in self.panels:
                F = self.ev_expr.eval_matrix(e, p)
                Z = _zscore_matrix(F, Z_LOOK, Z_MIN_HIST)
                rows = np.flatnonzero(~p.is_mask)
                if len(rows) == 0:
                    continue
                ic = pearson_ic(Z[rows].ravel(), p.R[rows].ravel())
                if np.isfinite(ic):
                    oos.append(ic)
            self._entry(e)["oos_ic"] = _r5(float(np.mean(oos)) if oos else None)
            self._entry(e)["final_review"] = (
                f"python gp_miner.py --review {json.dumps(e)} "
                f"--sign {self._entry(e).get('sign_hint', 1)} --exit hold")
        self.dump()

    def dump(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {**{"is": [IS_START, IS_END], "oos": [OOS_START, OOS_END],
                     "label_h": LABEL_H, "fee": FEE, "rho_thr": RHO_THR,
                     "token_max": TOKEN_MAX,
                     "note": "label=close30 修正口径（C[t+30]/O[t+1]-1）；"
                             "OOS-IC 仅记录不参与选择；终审走 eval_factor 桥"},
                     **self.extra_meta},
            "callbacks": self.records,
            "candidates": sorted(self.ledger.values(),
                                 key=lambda x: -(x["fitness"] or -9)),
        }
        self.ledger_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                    encoding="utf-8")


def _r5(x):
    return round(float(x), 5) if x is not None and np.isfinite(x) else None


# ══════════════════════════════════════════════════════════════════════════
# 6. 终审桥：GP 表达式 → eval_factor 固定规则裁决（不改 eval_factor 源码）
# ══════════════════════════════════════════════════════════════════════════
def final_review(expr, sign=1, exit_name="hold", codes=None, label_kind="close30"):
    """复用 eval_factor 的 load_code/zscores/run_entries/random_baseline/_aggregate。

    表达式在 eval_factor 的 day_ctx 上逐日 eval（终端映射 o/h/l/c/v/amt/vwap），
    之后走与基线因子完全相同的 z-score 触发 → 交易 → 随机基线 → 预注册闸门。
    """
    import eval_factor as ef

    ef.v2.END = ef.WIN_END
    _, ns = build_gp_function_set()
    ev_expr = ExprEvaluator(ns)
    if codes is None:
        codes = minute_data.pool_symbols()
    parts = []
    for code in codes:
        panel = ef.load_code(code)
        if not panel:
            continue
        fvals = {}
        for d in panel["dates"]:
            ctx = panel["days"][d]
            terms = {"open": ctx["o"], "high": ctx["h"], "low": ctx["l"],
                     "close": ctx["c"], "volume": ctx["v"], "vwap": ctx["vwap"],
                     "amount": ctx["amt"]}
            v = ev_expr.eval_day(expr, terms)
            fvals[d] = v if v is not None else np.full(ctx["n"], np.nan)
        zm = ef.zscores(fvals, panel)
        legs = ef.run_entries(panel, zm, sign, exit_name, code)
        rnd = ef.random_baseline(panel, legs, exit_name, code)
        parts.append({"legs": legs, "rand": rnd, "n_days": len(panel["dates"])})
        print(f"  [review] {code}: legs={len(legs)}")
    return ef._aggregate(f"gp:{expr[:40]}", sign, exit_name, parts)


# ══════════════════════════════════════════════════════════════════════════
# 7. 主流程 / CLI
# ══════════════════════════════════════════════════════════════════════════
def run_gp(pop, gen, seed, out, codes=None, windows=None, corr="pearson",
           cons_pen=CONS_PEN, trig_gate=True, sample_every=1, label_kind="close30",
           mc_every=4, n_mc=30, topk=5, include_csrank=False, max_is_days=None,
           init_depth=(2, 6), expr_budget=30.0, verbose=1):
    t0 = time.perf_counter()
    if codes is None:
        codes = minute_data.pool_symbols()
    print(f"[gp] 加载 {len(codes)} 只分钟数据（{IS_START}~{OOS_END}）…", flush=True)
    raw = minute_data.load_pool(codes, IS_START, OOS_END)
    panels = []
    for s in codes:
        p = build_stock_panel(s, raw[s], label_kind, max_is_days=max_is_days)
        if p is not None and p.is_mask.sum() >= 30:
            panels.append(p)
    print(f"[gp] 面板就绪 {len(panels)} 只，用时 {time.perf_counter() - t0:.0f}s；"
          f"示例 {panels[0].symbol}: {panels[0].n_days} 日 × {panels[0].L} 根 "
          f"(IS {int(panels[0].is_mask.sum())} 日)", flush=True)

    gp_fns, ns = build_gp_function_set(windows, include_csrank)
    ev_expr = ExprEvaluator(ns)
    fit_ev = FitnessEvaluator(panels, ev_expr, corr=corr, cons_pen=cons_pen,
                              trig_gate=trig_gate, sample_every=sample_every,
                              expr_budget=expr_budget)
    def _metric(_y, y_pred, _w):        # make_fitness 要求自由函数（co_argcount=3）
        return fit_ev.metric(_y, y_pred, _w)

    Metric = make_fitness(function=_metric, greater_is_better=True)
    cb = GPCallback(fit_ev, ev_expr, panels, out, mc_every=mc_every, n_mc=n_mc,
                    topk=topk, seed=seed, label_kind=label_kind,
                    extra_meta={"pop": pop, "gen": gen, "seed": seed,
                                "sample_every": sample_every,
                                "max_is_days": max_is_days, "corr": corr,
                                "label_kind": label_kind, "trig_gate": trig_gate,
                                "init_depth": list(init_depth),
                                "expr_budget": expr_budget,
                                "win_budget": WIN_BUDGET, "min_finite": MIN_FINITE})
    X, y = make_terminal_X()
    est = SymbolicRegressor(
        population_size=pop, generations=gen, init_depth=init_depth,
        tournament_size=min(600, pop // 2), p_crossover=0.3, p_subtree_mutation=0.1,
        p_hoist_mutation=0.01, p_point_mutation=0.1, p_point_replace=0.6,
        max_samples=1.0, parsimony_coefficient=0.0, stopping_criteria=1.0,
        const_range=None, n_jobs=1, function_set=gp_fns, metric=Metric,
        random_state=seed, verbose=verbose)
    print(f"[gp] 开跑 pop={pop} gen={gen} seed={seed} 算子={len(gp_fns)} "
          f"sample_every={sample_every} trig_gate={trig_gate} "
          f"init_depth={init_depth} expr_budget={expr_budget}s", flush=True)
    est.fit(X, y, callback=cb)
    cb.finalize()
    dt = time.perf_counter() - t0
    print(f"[gp] 完成：cache={len(fit_ev.cache)} 唯一表达式，池={len(cb.pool)}，"
          f"总用时 {dt / 60:.1f} min → {out}", flush=True)
    return {"ledger": str(out), "pool": cb.pool, "n_unique": len(fit_ev.cache),
            "minutes": round(dt / 60, 1)}


def main():
    ap = argparse.ArgumentParser(description="GP 因子搜索引擎（S2-6）")
    ap.add_argument("--smoke", action="store_true", help="冒烟：pop=50/gen=3/隔日抽样")
    ap.add_argument("--pop", type=int, default=1000)
    ap.add_argument("--gen", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--codes", default=None, help="逗号分隔；缺省=全池 39 只")
    ap.add_argument("--corr", choices=["pearson", "spearman"], default="pearson")
    ap.add_argument("--label", choices=["close30", "maxh30", "closeeod"], default="close30",
                    help="close30=30根后出场（默认）；closeeod=持有到尾盘（G1三轮）；maxh30=原口径A（乐观偏差，仅参照）")
    ap.add_argument("--sample-every", type=int, default=1, help="IS 隔 N 日抽样（冒烟加速）")
    ap.add_argument("--max-is-days", type=int, default=None,
                    help="只保留最近 N 个 IS 日（仅冒烟加速；正式跑勿用）")
    ap.add_argument("--init-depth", default="2,6", help="GP 初始树深，如 2,6")
    ap.add_argument("--expr-timeout", type=float, default=30.0,
                    help="单表达式全池评估超时秒数，超时判死防重树拖垮种群")
    ap.add_argument("--no-trig-gate", action="store_true")
    ap.add_argument("--include-csrank", action="store_true")
    ap.add_argument("--mc-every", type=int, default=4)
    ap.add_argument("--n-mc", type=int, default=30)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--out", default=None)
    ap.add_argument("--review", default=None, help="终审桥：对单个表达式跑 eval_factor")
    ap.add_argument("--sign", type=int, default=1, choices=[1, -1])
    ap.add_argument("--exit", dest="exit_name", default="hold", choices=["hold", "native"])
    args = ap.parse_args()

    if args.review:
        r = final_review(args.review, sign=args.sign, exit_name=args.exit_name,
                         codes=args.codes.split(",") if args.codes else None)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return

    if args.smoke:
        args.pop, args.gen, args.sample_every = 50, 3, 2
        args.mc_every, args.n_mc, args.topk = 2, 20, 3
        args.init_depth, args.expr_timeout = "2,4", 8.0
        if args.max_is_days is None:
            args.max_is_days = 45          # 冒烟默认截 IS 样本量（非口径改动）
    out = args.out or str(RESULTS_DIR /
                          f"gp_ledger_{'smoke' if args.smoke else f'seed{args.seed}'}.json")
    codes = args.codes.split(",") if args.codes else None
    init_depth = tuple(int(x) for x in args.init_depth.split(","))
    run_gp(args.pop, args.gen, args.seed, out, codes=codes, corr=args.corr,
           trig_gate=not args.no_trig_gate, sample_every=args.sample_every,
           label_kind=args.label, mc_every=args.mc_every, n_mc=args.n_mc,
           topk=args.topk, include_csrank=args.include_csrank,
           max_is_days=args.max_is_days, init_depth=init_depth,
           expr_budget=args.expr_timeout)


if __name__ == "__main__":
    main()
