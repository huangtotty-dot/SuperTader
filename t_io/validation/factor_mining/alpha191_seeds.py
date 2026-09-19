# -*- coding: utf-8 -*-
"""Alpha191 分钟化种子库（任务 S2-4）—— 给 GP 搜索（S2-3/AlphaGen 臂）用的短周期价量种子因子。

## 来源

公式原文：国泰君安《基于短周期价量特征的多因子选股体系》（数量化专题之九十三）191 个
短周期交易型 Alpha，以聚宽 jqlib.alpha191 实现口径为准（JoinQuant 对原研报缺失/不合理
处做过调整）。每个因子的 docstring 附「Alpha191 编号 + 原公式 + 分钟化改动说明」。
其中若干公式与 WorldQuant Alpha101 同名因子同源（如 GTJA#139 ≡ WQ#6、GTJA#105 ≡ WQ#3、
GTJA#013 ≡ WQ#41、GTJA#038 ≡ WQ#23），在 docstring 中一并注明。

## 窗口换算纪律（所有种子统一，务必先读）

原公式全部为日频。分钟化时**不按「N 日 × 240 bar」粗暴展开**，而是**按交易日滚动**：

- 日频窗口 N 日 → 分钟**尾窗**，窗长 = **之前 N 个完整交易日**（不含当日）的实际
  bar 数之和——窗长在当日开盘前即已知，不含当日未来信息；缺根日按实际 bar 数计入。
  跨日边界由 `minute_data.iter_days`（S2-2 已交付）的日切分保证。
  均匀日长时窗长 = N×B bar，与 gp_ops 固定窗长算子逐位等价。
- `DELAY(x, n)` / `TSRANK(x, n)` 等跨日算子按**同时刻对齐**：delay 1 日 = 昨日同一
  分钟序位（日内第 m 根）的值。这保留了「同一时点跨日可比」的日内结构。
- 窗口样本不足时（warmup）：当日之前须满 N 个完整交易日且窗口内样本全有限才出值，
  否则为 NaN（与 gp_ops「满窗出值」口径一致）。
- VWAP = 当日累计 Σamt/Σv（与本仓库 `factor_ops` 口径钉死一致，不混用其它 VWAP 实现）。
- 原公式中的**截面 RANK 无法在单票分钟流上计算** → 一律替换为「过去 20 个交易日的
  时序分位 TSRANK」（同时刻对齐），各因子 docstring 逐一注明。

## 因果性

所有算子严格因果：bar i 的值只用 ≤ i 的 bar。`test_alpha191_seeds.py` 有截断不变性
检测（截掉尾部重算，重叠段必须逐位一致）。

## gp_ops 契约（任务 S2-3，已交付）

种子因子通过 `build_ops(ctx)` 取算子集。gp_ops 已就位（`ts_mean/ts_std/ts_max/ts_min/
ts_rank/ts_delta/ts_delay/ts_corr/decay_linear/vwap_dev/day_vwap`，窗口参数 = **bar 数**，
复用 factor_ops 因果滚动）→ `build_ops` 返回 `_GpOpsDayAdapter`：均匀日长时把
「N 个交易日」折算为 N×B bar 窗直接调 gp_ops（语义等价，parity 测试钉死）；
日长不均或大规模输入时回退本模块向量化日窗桩 `_MinuteOpsStub`（同因果语义）。
gp_ops 缺失时一律走桩——本库测试两条路径都跑。

## 输入输出

每个种子：`alpha_XXX(df) -> pd.Series`（也可直接传 `MinuteCtx` 复用预处理）。
`df` 为**单票**分钟 DataFrame，列名兼容两套：
`time/datetime` + `open/o, high/h, low/l, close/c, volume/vol/v, amount/amt`。
返回与 df 等长、索引对齐的逐 bar 因子值。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ── gp_ops（S2-3）契约接入：就位前用本地桩 ────────────────────────────────
try:
    import minute_data as _md  # S2 并行线分钟数据层
    _ITER_DAYS = getattr(_md, 'iter_days', None)
except ImportError:
    _md, _ITER_DAYS = None, None

try:
    import gp_ops as _gp  # S2-3 并行开发；未就位时走桩
except ImportError:
    _gp = None

EPS = 1e-12
RANK_DAYS = 20          # 截面 RANK 的分钟化替代：20 个交易日同时刻时序分位


# ── 交易日切分 ────────────────────────────────────────────────────────────
def _iter_days(df):
    """→ [(date_str, np.ndarray 位置索引)]。优先 minute_data.iter_days（S2-2 已交付），
    缺位时本地分组（语义一致：按交易日分组、日内按时间升序、组间按日期升序）。

    minute_data.iter_days 契约：yield (date_str, day_df)，df['time'] 须为 datetime64；
    本函数传入的 df 已 reset_index，day_df.index 即位置索引。
    """
    if _ITER_DAYS is not None:
        out = []
        for d, day_df in _ITER_DAYS(df):
            out.append((d, day_df.index.to_numpy()))
        out.sort(key=lambda t: t[0])
        return out
    t = df['time'].astype(str)
    dates = t.str[:10].to_numpy()
    order = np.argsort(t.to_numpy(), kind='stable')
    out, seen = [], {}
    for i in order:
        d = dates[i]
        seen.setdefault(d, []).append(i)
    for d in sorted(seen):
        out.append((d, np.asarray(seen[d], dtype=np.int64)))
    return out


_ALIASES = {
    'time': ['time', 'datetime', 'ts', 'date_time'],
    'o': ['o', 'open'], 'h': ['h', 'high'], 'l': ['l', 'low'], 'c': ['c', 'close'],
    'v': ['v', 'vol', 'volume'], 'amt': ['amt', 'amount'],
}


class MinuteCtx:
    """单票多日分钟序列的预处理上下文：列归一化 + 日切分 + 日内 VWAP/收益。"""

    def __init__(self, df):
        cols = {}
        low = {str(c).lower(): c for c in df.columns}
        for k, names in _ALIASES.items():
            for nm in names:
                if nm in low:
                    cols[k] = low[nm]
                    break
            if k not in cols:
                raise ValueError(f'缺少列 {k}（可选 {names}），实际列={list(df.columns)}')
        # 因果前提：序列必须按时间升序。乱序输入先排序；内部统一 canonical 列名
        # （time 为 datetime64，与 minute_data 契约一致），输出仍对齐原 df.index。
        inner = pd.DataFrame({
            'time': pd.to_datetime(df[cols['time']]),
            'open': df[cols['o']].to_numpy(float),
            'high': df[cols['h']].to_numpy(float),
            'low': df[cols['l']].to_numpy(float),
            'close': df[cols['c']].to_numpy(float),
            'volume': df[cols['v']].to_numpy(float),
            'amount': df[cols['amt']].to_numpy(float),
        }, index=df.index)
        inner = inner.sort_values('time', kind='stable')
        self.index = inner.index
        inner = inner.reset_index(drop=True)
        self.time = inner['time'].astype(str).to_numpy()
        self.o = inner['open'].to_numpy(float)
        self.h = inner['high'].to_numpy(float)
        self.l = inner['low'].to_numpy(float)
        self.c = inner['close'].to_numpy(float)
        self.v = inner['volume'].to_numpy(float)
        self.amt = inner['amount'].to_numpy(float)
        # ── 日切分（跨日边界唯一入口；inner 已 reset_index → 位置索引）──
        days = _iter_days(inner)
        self.N = len(self.c)
        self.day_id = np.full(self.N, -1, dtype=np.int64)
        self.minute_idx = np.zeros(self.N, dtype=np.int64)
        self.dates = []
        for k, (d, idx) in enumerate(days):
            self.day_id[idx] = k
            self.minute_idx[idx] = np.arange(len(idx))
            self.dates.append(d)
        self.K = len(self.dates)
        sizes = np.bincount(self.day_id[self.day_id >= 0], minlength=max(self.K, 1))
        self.day_sizes = sizes
        self.med_day = float(np.median(sizes)) if len(sizes) else 1.0
        self.day_first = np.concatenate(([0], np.cumsum(sizes)[:-1])) if self.K else np.zeros(0, int)
        self.M = int(sizes.max()) if len(sizes) else 0
        # ── 日内累计 VWAP（当日 Σamt/Σv，factor_ops 口径）──
        self.vwap = self._intraday_vwap()
        # ── 逐 bar 简单收益（首根 bar 对昨收）──
        prev_c = np.full(self.N, np.nan)
        prev_c[1:] = self.c[:-1]
        first_bar = self.minute_idx == 0
        self.ret = np.where(prev_c > 0, self.c / np.where(prev_c > 0, prev_c, 1.0) - 1.0, np.nan)
        self.ret[first_bar & ~np.isfinite(self.ret)] = np.nan

    def _intraday_vwap(self):
        out = np.full(self.N, np.nan)
        for k in range(self.K):
            idx = np.where(self.day_id == k)[0]
            cv = np.cumsum(self.v[idx])
            ca = np.cumsum(self.amt[idx])
            with np.errstate(divide='ignore', invalid='ignore'):
                w = np.where(cv > 0, ca / np.where(cv > 0, cv, 1.0), self.c[idx])
            out[idx] = w
        return out


# ── 本地迷你桩：gp_ops 契约语义（窗口=交易日数，严格因果）─────────────────
class _MinuteOpsStub:
    """gp_ops 就位前/不适用时的契约实现。所有窗口参数 = 交易日数 N：

    - ts_mean/ts_std/ts_sum/ts_max/ts_min/ts_corr/decay_linear：**尾窗**，窗长 =
      **之前 N 个完整交易日**（不含当日）的实际 bar 数之和——窗长在当日开盘前
      即已知（若含当日总根数则构成结构性前视，截断测试会抓出）。日界由
      iter_days 切分保证，缺根日按实际 bar 数计入。均匀日长时窗长 = N×B bar，
      与 gp_ops.ts_*(x, N×B) 逐位等价（parity 测试钉死）。
      统计类用前缀和 O(1)/bar；ts_max/ts_min 用单调队列 O(N)。
    - ts_delay/ts_delta/ts_rank：跨日**同时刻对齐**（昨日同一分钟序位）。
    - sma(x, n, m)：GTJA 递推 Y_i = (X_i·m + Y_{i-1}·(n-m))/n（= ewm(alpha=m/n)），
      在拼接序列上因果递推。
    - vwap_dev：c / 当日累计 vwap − 1。
    warmup：当日之前满 N 个完整交易日且窗口内样本全有限才出值，否则 NaN
    （与 gp_ops 满窗口径一致）。
    """

    def __init__(self, ctx: MinuteCtx):
        self.ctx = ctx
        self.N0 = ctx.N

    # ── 内部：2D 矩阵 [K 日 × M 分钟]（NaN 填充）──
    def _mat(self, x):
        # 不做 id 缓存：临时数组释放后 id 会被复用，id 键缓存会静默张冠李戴
        m = np.full((self.ctx.K, self.ctx.M), np.nan)
        ok = self.ctx.day_id >= 0
        m[self.ctx.day_id[ok], self.ctx.minute_idx[ok]] = x[ok]
        return m

    def _flat(self, X):
        return X[self.ctx.day_id, self.ctx.minute_idx]

    def _win_len(self, n):
        """逐 bar 窗长 L_k：之前 n 个完整交易日的实际 bar 数之和（当日开盘前已知）。"""
        c = self.ctx
        cs = np.concatenate(([0], np.cumsum(c.day_sizes)))
        lo = np.maximum(np.arange(c.K) - n, 0)
        L = cs[np.arange(c.K)] - cs[lo]
        return L[c.day_id].astype(float)

    def _starts(self, n):
        """每根 bar 的窗口起点（bar 级）。

        窗口语义（与 gp_ops 固定窗长在均匀日长下逐位等价）：
        窗长 L_k = **之前** n 个完整交易日（不含当日）的实际 bar 数之和 ——
        L_k 在当日开盘前即已知，不含任何当日未来信息（截断不变性测试钉死：
        若窗长含当日总根数，则因子值隐含依赖当日尚未发生的 bar 数，属结构性
        前视，必须排除）。日界与实际根数由 iter_days 的日切分保证，缺根日按
        实际 bar 数计入（「按交易日滚动」，而非粗暴 N×240）。窗口为结束于当前
        bar 的尾窗。均匀日长时 L_k = n×B，与 gp_ops.ts_*(x, n×B) 完全等价。
        """
        i = np.arange(self.N0)
        return np.maximum(i - self._win_len(n).astype(np.int64) + 1, 0)

    def _full_ok(self, n, cnt):
        """出值条件：当日之前已有 ≥ n 个完整交易日，且窗口内样本完整（全有限）。

        与 gp_ops「满窗且窗内无 NaN 才出值」口径一致——这是 parity 与
        截断不变性共同钉死的 warmup 纪律。
        """
        L = self._win_len(n)
        return (self.ctx.day_id >= n) & (cnt >= L * 0.999) & (L > 0)

    @staticmethod
    def _prefix(x):
        f = np.isfinite(x)
        xs = np.where(f, x, 0.0)
        return (np.concatenate(([0.0], np.cumsum(xs))),
                np.concatenate(([0], np.cumsum(f.astype(np.int64)))))

    # ── 滚动统计（尾窗，窗长 = 之前 N 个完整交易日的实际 bar 数）──
    def ts_sum(self, x, n):
        x = np.asarray(x, float)
        P, C = self._prefix(x)
        s, i1 = self._starts(n), np.arange(len(x)) + 1
        cnt = (C[i1] - C[s]).astype(float)
        tot = P[i1] - P[s]
        return np.where(self._full_ok(n, cnt), tot, np.nan)

    def ts_mean(self, x, n):
        x = np.asarray(x, float)
        P, C = self._prefix(x)
        s, i1 = self._starts(n), np.arange(len(x)) + 1
        cnt = (C[i1] - C[s]).astype(float)
        tot = P[i1] - P[s]
        ok = self._full_ok(n, cnt)
        return np.where(ok, tot / np.where(cnt > 0, cnt, 1.0), np.nan)

    def ts_std(self, x, n):
        """滚动标准差（ddof=1，与 factor_ops/gp_ops 口径一致）。"""
        x = np.asarray(x, float)
        P, C = self._prefix(x)
        P2, _ = self._prefix(np.where(np.isfinite(x), x * x, 0.0))
        s, i1 = self._starts(n), np.arange(len(x)) + 1
        cnt = (C[i1] - C[s]).astype(float)
        tot, tot2 = P[i1] - P[s], P2[i1] - P2[s]
        with np.errstate(invalid='ignore'):
            var = (tot2 - tot * tot / np.where(cnt > 0, cnt, 1.0)) / np.where(cnt > 1, cnt - 1, np.nan)
        var = np.where(var < 0, 0.0, var)  # 浮点误差钳位
        return np.where(self._full_ok(n, cnt) & (cnt > 1), np.sqrt(var), np.nan)

    def ts_corr(self, x, y, n):
        x, y = np.asarray(x, float), np.asarray(y, float)
        both = np.isfinite(x) & np.isfinite(y)
        xs = np.where(both, x, 0.0)
        ys = np.where(both, y, 0.0)
        Px, Cx = self._prefix(np.where(both, x, np.nan))
        Py, _ = self._prefix(np.where(both, y, np.nan))
        Pxy, _ = self._prefix(np.where(both, xs * ys, np.nan))
        Px2, _ = self._prefix(np.where(both, xs * xs, np.nan))
        Py2, _ = self._prefix(np.where(both, ys * ys, np.nan))
        s, i1 = self._starts(n), np.arange(len(x)) + 1
        cnt = (Cx[i1] - Cx[s]).astype(float)
        sx, sy = Px[i1] - Px[s], Py[i1] - Py[s]
        sxy, sx2, sy2 = Pxy[i1] - Pxy[s], Px2[i1] - Px2[s], Py2[i1] - Py2[s]
        with np.errstate(invalid='ignore', divide='ignore'):
            cov = sxy / cnt - (sx / cnt) * (sy / cnt)
            vx = sx2 / cnt - (sx / cnt) ** 2
            vy = sy2 / cnt - (sy / cnt) ** 2
            r = cov / np.sqrt(np.where(vx > 0, vx, np.nan) * np.where(vy > 0, vy, np.nan))
        return np.where(self._full_ok(n, cnt), np.clip(r, -1, 1), np.nan)

    def _varwin_extreme(self, x, n, is_max):
        """变长尾窗极值（窗长 = 最近 N 个交易日的实际 bar 数）。

        单调队列 O(N)；窗口起点非递减（日界处 L_k 变化 ≤ 相邻日 bar 差），
        若出现回退（极端缺根）则重建队列——罕见且摊销便宜。
        窗口内含 NaN 时跳过该 bar 的取值（与 gp_ops「窗内非有限→NaN」对齐：
        此处直接返回 NaN 更严格、更安全）。
        """
        from collections import deque
        x = np.asarray(x, float)
        starts = self._starts(n)
        day_ok = self.ctx.day_id >= n          # 与 gp_ops 满窗口径对齐
        out = np.full(len(x), np.nan)
        dq = deque()
        prev_s = 0
        for i in range(len(x)):
            s = int(starts[i])
            if s < prev_s:  # 起点回退（极端缺根日）→ 重建
                dq.clear()
                for j in range(s, i):
                    if np.isfinite(x[j]):
                        while dq and (x[dq[-1]] <= x[j]) == is_max:
                            dq.pop()
                        dq.append(j)
            prev_s = s
            while dq and dq[0] < s:
                dq.popleft()
            if np.isfinite(x[i]):
                while dq and (x[dq[-1]] <= x[i]) == is_max:
                    dq.pop()
                dq.append(i)
                if day_ok[i] and dq:
                    out[i] = x[dq[0]]
        return out

    def ts_max(self, x, n):
        return self._varwin_extreme(x, n, True)

    def ts_min(self, x, n):
        return self._varwin_extreme(x, n, False)

    def decay_linear(self, x, n):
        """线性衰减加权（最近 bar 权重最大），窗口 = N 个交易日的 bar，要求窗口内完整。"""
        x = np.asarray(x, float)
        f = np.isfinite(x)
        xs = np.where(f, x, 0.0)
        j = np.arange(len(x), dtype=float)
        P0 = np.concatenate(([0.0], np.cumsum(xs)))
        P1 = np.concatenate(([0.0], np.cumsum(j * xs)))
        C = np.concatenate(([0], np.cumsum(f.astype(np.int64))))
        s, i1 = self._starts(n), np.arange(len(x)) + 1
        L = (i1 - s).astype(float)
        cnt = (C[i1] - C[s]).astype(float)
        num = (P1[i1] - P1[s]) - (s - 1.0) * (P0[i1] - P0[s])
        den = L * (L + 1.0) / 2.0
        ok = self._full_ok(n, cnt) & (den > 0)
        return np.where(ok, num / np.where(den > 0, den, 1.0), np.nan)

    # ── 跨日同时刻算子 ──
    def ts_delay(self, x, n):
        """delay n 个交易日：同一分钟序位的 n 日前值。"""
        X = self._mat(np.asarray(x, float))
        out = np.full_like(X, np.nan)
        if self.ctx.K > n:
            out[n:] = X[:-n]
        return self._flat(out)

    def ts_delta(self, x, n):
        x = np.asarray(x, float)
        return x - self.ts_delay(x, n)

    def ts_rank(self, x, n):
        """时序分位：当前值在过去 n 个交易日同时刻取值中的排位（含当日），∈(0,1]。"""
        X = self._mat(np.asarray(x, float))
        K, M = X.shape
        if K == 0:
            return np.full(len(np.asarray(x, float)), np.nan)
        w = min(n, K)
        pad = np.full((w - 1, M), np.nan)
        XP = np.concatenate([pad, X], axis=0)
        SW = np.lib.stride_tricks.sliding_window_view(XP, w, axis=0)  # K×M×w
        cur = X[..., None]
        valid = np.isfinite(SW)
        cnt = valid.sum(-1)
        le = (valid & (SW <= cur)).sum(-1)
        ok = np.isfinite(X) & (cnt >= 3)
        out = np.where(ok, le / np.maximum(cnt, 1), np.nan)
        return self._flat(out)

    # ── 递推与日内 ──
    def sma(self, x, n, m):
        """GTJA SMA：Y_i = (X_i·m + Y_{i-1}·(n−m))/n，因果递推（ewm(alpha=m/n)）。"""
        return pd.Series(np.asarray(x, float)).ewm(alpha=m / n, adjust=False,
                                                    min_periods=1).mean().to_numpy()

    def vwap_dev(self):
        c = self.ctx
        return c.c / np.where(c.vwap > 0, c.vwap, np.nan) - 1.0


class _GpOpsDayAdapter(_MinuteOpsStub):
    """gp_ops（S2-3，已交付）就位时的适配层：种子因子经 build_ops 拿到的就是它。

    窗口纪律不变（n = 交易日数）。委托策略：

    - gp_ops.ts_*(x, n) 的窗口语义是「过去 n 根 bar」（复用 factor_ops 的
      Python 逐 bar 因果滚动）。当 ctx 日 bar 数**均匀**（每日等长完整日）时，
      「最近 N 个交易日的全部 bar」 ≡ 「最近 N×B 根 bar」（B = 日 bar 数），
      此时 ts_mean/ts_std/ts_max/ts_min/ts_corr/ts_delay/ts_delta/decay_linear
      直接委托 gp_ops，语义逐位等价（由 test_gp_ops_parity 钉死）。
    - 日长不均匀（缺根日）或输入超过 GP_MAX_BARS 时回退桩的向量化日窗引擎：
      factor_ops/gp_ops 的滚动是 O(n·w) Python 循环，在 5.8 万 bar/只 × 39 只的
      面板规模下不可用；桩实现是同因果语义的向量化等价物（parity 测试为证）。
    - ts_rank 契约为「同时刻跨日分位」，gp_ops.ts_rank 是 bar 窗分位，语义不同，
      **不委托**，恒用桩实现；sma（GTJA 递推）gp_ops 无对应算子，恒用桩；
      vwap_dev 口径与 gp_ops.vwap_dev 一致（当日累计 Σamt/Σv），直接用 ctx.vwap。
    - warmup 差异：gp_ops 与桩均为「满 N 日历史 + 窗内全有限」才出值；边界仅差
      第 N−1 日末根 bar（gp 在该根出值，桩从第 N 日首根出值），parity 测试
      对 i < N×B 的 warmup 边界放行。
    """

    GP_MAX_BARS = 4000  # gp_ops（factor_ops 滚动）为 Python 逐 bar 循环，限小规模用

    def __init__(self, ctx: MinuteCtx):
        super().__init__(ctx)
        self._uniform = bool(ctx.K > 0 and len(ctx.day_sizes)
                             and ctx.day_sizes.min() == ctx.day_sizes.max())
        self._day_bars = int(ctx.day_sizes[0]) if self._uniform else 0

    def _w(self, x, n):
        """均匀日 + 小规模 → 返回 bar 窗长；否则 None（回退桩）。"""
        if self._uniform and len(np.asarray(x)) <= self.GP_MAX_BARS:
            return n * self._day_bars
        return None

    def ts_mean(self, x, n):
        w = self._w(x, n)
        return _gp.ts_mean(x, w) if w else super().ts_mean(x, n)

    def ts_std(self, x, n):
        w = self._w(x, n)
        return _gp.ts_std(x, w) if w else super().ts_std(x, n)

    def ts_max(self, x, n):
        w = self._w(x, n)
        return _gp.ts_max(x, w) if w else super().ts_max(x, n)

    def ts_min(self, x, n):
        w = self._w(x, n)
        return _gp.ts_min(x, w) if w else super().ts_min(x, n)

    def ts_corr(self, x, y, n):
        w = self._w(x, n)
        return _gp.ts_corr(x, y, w) if w else super().ts_corr(x, y, n)

    def ts_delay(self, x, n):
        w = self._w(x, n)
        return _gp.ts_delay(x, w) if w else super().ts_delay(x, n)

    def ts_delta(self, x, n):
        w = self._w(x, n)
        return _gp.ts_delta(x, w) if w else super().ts_delta(x, n)

    def decay_linear(self, x, n):
        w = self._w(x, n)
        return _gp.decay_linear(x, w) if w else super().decay_linear(x, n)


def build_ops(ctx: MinuteCtx):
    """算子集入口：gp_ops（S2-3）就位 → `_GpOpsDayAdapter`；否则本地桩。

    两者同因果语义；均匀日长 + 小规模时适配层直接调 gp_ops 固定窗长算子，
    语义等价由 test_alpha191_seeds.py::test_gp_ops_parity 钉死。
    """
    if _gp is not None and hasattr(_gp, 'ts_mean') and hasattr(_gp, 'decay_linear'):
        return _GpOpsDayAdapter(ctx)
    return _MinuteOpsStub(ctx)


def _ctx(df_or_ctx):
    return df_or_ctx if isinstance(df_or_ctx, MinuteCtx) else MinuteCtx(df_or_ctx)


def _wrap(ctx, arr):
    return pd.Series(arr, index=ctx.index, name=None)


# ════════════════════════════════════════════════════════════════════════
# 种子因子（30 个）。编号 = 聚宽 jqlib.alpha191 口径。
# ════════════════════════════════════════════════════════════════════════

def alpha_001(df):
    """GTJA Alpha#001（量价背离）。
    原公式：(-1 * CORR(RANK(DELTA(LOG(VOLUME),1)), RANK((CLOSE-OPEN)/OPEN), 6))
    分钟化改动：DELTA(LOG(V),1) → 同时刻对数量的昨日差分；(C-O)/O 为 bar 级；
    两处截面 RANK → 20 日同时刻 TSRANK；CORR 窗口 6 日 → 6 个交易日的 bar。
    预期效应：量价背离（放量滞涨/缩量反弹）→ 日内反转先手。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    x = ops.ts_rank(ops.ts_delta(np.log(ctx.v + EPS), 1), RANK_DAYS)
    y = ops.ts_rank((ctx.c - ctx.o) / np.where(ctx.o > 0, ctx.o, np.nan), RANK_DAYS)
    return _wrap(ctx, -ops.ts_corr(x, y, 6))


def alpha_002(df):
    """GTJA Alpha#002（多空失衡变动）。
    原公式：-1 * DELTA(((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW), 1)
    分钟化改动：CLV 为 bar 级（bar 内多空位置）；DELTA 1 日 → 昨日同时刻差分。
    预期效应：CLV 环比恶化 → 短线走弱；捕捉日内多空力量边际变化。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    clv = ((ctx.c - ctx.l) - (ctx.h - ctx.c)) / (ctx.h - ctx.l + EPS)
    return _wrap(ctx, -ops.ts_delta(clv, 1))


def alpha_003(df):
    """GTJA Alpha#003（累积/派发线类）。
    原公式：SUM((CLOSE==DELAY(CLOSE,1)?0:CLOSE-(CLOSE>DELAY(CLOSE,1)?
            MIN(LOW,DELAY(CLOSE,1)):MAX(HIGH,DELAY(CLOSE,1)))), 6)
    分钟化改动：DELAY 1 日 → 昨日同时刻收盘；SUM 6 日 → 6 个交易日的 bar。
    预期效应：相对昨同时刻的真实涨跌累积 → 中线日内动量/反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    dc = ops.ts_delay(ctx.c, 1)
    up = ctx.c > dc
    dn = ctx.c < dc
    x = np.where(up, ctx.c - np.minimum(ctx.l, dc),
                 np.where(dn, ctx.c - np.maximum(ctx.h, dc), 0.0))
    x = np.where(np.isfinite(dc), x, np.nan)
    return _wrap(ctx, ops.ts_sum(x, 6))


def alpha_005(df):
    """GTJA Alpha#005（量价高位共振反向）。
    原公式：(-1 * TSMAX(CORR(TSRANK(VOLUME,5), TSRANK(HIGH,5),5),3))
    分钟化改动：TSRANK 5 日 → 5 日同时刻分位；CORR 5 日、TSMAX 3 日 →
    5/3 个交易日的 bar 窗口。
    预期效应：量、价同时处于各自高位且联动走强 → 见顶回落概率大（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    r = ops.ts_corr(ops.ts_rank(ctx.v, 5), ops.ts_rank(ctx.h, 5), 5)
    return _wrap(ctx, -ops.ts_max(r, 3))


def alpha_007(df):
    """GTJA Alpha#007（VWAP 偏离极值 × 量能异动）。
    原公式：(RANK(MAX((VWAP-CLOSE),3)) + RANK(MIN((VWAP-CLOSE),3))) * RANK(DELTA(VOLUME,3))
    分钟化改动：VWAP → 当日累计 VWAP（因子口径钉死）；MAX/MIN 3 日 → 3 个交易日 bar；
    三处截面 RANK → 20 日同时刻 TSRANK；DELTA(V,3) → 3 日前同时刻量差。
    预期效应：价对 VWAP 的极端偏离叠加量能突变 → 均值回复/恐慌反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    dev = ctx.vwap - ctx.c
    part = ops.ts_rank(ops.ts_max(dev, 3), RANK_DAYS) + \
        ops.ts_rank(ops.ts_min(dev, 3), RANK_DAYS)
    return _wrap(ctx, part * ops.ts_rank(ops.ts_delta(ctx.v, 3), RANK_DAYS))


def alpha_008(df):
    """GTJA Alpha#008（加权价 4 日变动反向）。
    原公式：RANK(DELTA(((HIGH+LOW)/2*0.2 + VWAP*0.8), 4)) * -1
    分钟化改动：VWAP → 当日累计 VWAP；DELTA 4 日 → 4 日前同时刻；
    截面 RANK → 20 日同时刻 TSRANK。
    预期效应：加权均价中期抬升过头 → 反转做空信号（反向因子）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    mid = (ctx.h + ctx.l) / 2 * 0.2 + ctx.vwap * 0.8
    return _wrap(ctx, -ops.ts_rank(ops.ts_delta(mid, 4), RANK_DAYS))


def alpha_009(df):
    """GTJA Alpha#009（资金流强度，SMA 平滑）。
    原公式：SMA(((HIGH+LOW)/2-(DELAY(HIGH,1)+DELAY(LOW,1))/2)*(HIGH-LOW)/VOLUME, 7, 2)
    分钟化改动：DELAY 1 日 → 昨日同时刻 H/L；SMA(7,2) 为因果递推（ewm α=2/7），
    在跨日拼接序列上递推，不重置。
    预期效应：价移 × 振幅 / 量 = 单位资金推动效率，平滑后捕捉趋势质量。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    x = ((ctx.h + ctx.l) / 2 - (ops.ts_delay(ctx.h, 1) + ops.ts_delay(ctx.l, 1)) / 2) \
        * (ctx.h - ctx.l) / (ctx.v + EPS)
    return _wrap(ctx, ops.sma(x, 7, 2))


def alpha_011(df):
    """GTJA Alpha#011（量能加权多空失衡 CLV×V，6 日）。
    原公式：SUM(((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW) .* VOLUME, 6)
    分钟化改动：bar 级 CLV×V；SUM 6 日 → 6 个交易日的 bar。
    预期效应：带量收在 bar 高位 → 主动买压累积（资金吸筹痕迹）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    clv = ((ctx.c - ctx.l) - (ctx.h - ctx.c)) / (ctx.h - ctx.l + EPS)
    return _wrap(ctx, ops.ts_sum(clv * ctx.v, 6))


def alpha_012(df):
    """GTJA Alpha#012（VWAP 偏离复合，反向）。
    原公式：RANK(OPEN - SUM(VWAP,10)/10) * (-1 * RANK(ABS(CLOSE-VWAP)))
    分钟化改动：VWAP → 当日累计 VWAP；SUM(VWAP,10)/10 → 10 个交易日 bar 均值；
    两处截面 RANK → 20 日同时刻 TSRANK。
    预期效应：开盘相对 VWAP 中枢偏高且现价偏离大 → 高估回落（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    a = ops.ts_rank(ctx.o - ops.ts_mean(ctx.vwap, 10), RANK_DAYS)
    b = -ops.ts_rank(np.abs(ctx.c - ctx.vwap), RANK_DAYS)
    return _wrap(ctx, a * b)


def alpha_013(df):
    """GTJA Alpha#013（几何均价 − VWAP；≡ WorldQuant Alpha#41 同构）。
    原公式：((HIGH*LOW)^0.5 - VWAP)
    分钟化改动：bar 级 H/L 几何均值 − 当日累计 VWAP；无窗口。
    预期效应：bar 中枢低于成交均价 → 卖压过重后的均值回复先手。"""
    ctx = _ctx(df)
    return _wrap(ctx, np.sqrt(ctx.h * ctx.l + EPS) - ctx.vwap)


def alpha_014(df):
    """GTJA Alpha#014（5 日反转）。
    原公式：CLOSE - DELAY(CLOSE, 5)
    分钟化改动：DELAY 5 日 → 5 日前同时刻收盘。
    预期效应：经典短周期反转——相对 5 日前同时刻超跌 → 反弹。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, ctx.c - ops.ts_delay(ctx.c, 5))


def alpha_020(df):
    """GTJA Alpha#020（6 日动量/反转，百分比）。
    原公式：(CLOSE-DELAY(CLOSE,6))/DELAY(CLOSE,6)*100
    分钟化改动：DELAY 6 日 → 6 日前同时刻收盘。
    预期效应：6 日尺度动量，A 股短周期上通常反向使用（反转）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    d = ops.ts_delay(ctx.c, 6)
    return _wrap(ctx, (ctx.c - d) / np.where(d > 0, d, np.nan) * 100)


def alpha_023(df):
    """GTJA Alpha#023（上行波动占比，类 RSI 的波动版）。
    原公式：SMA((CLOSE>DELAY(CLOSE,1)?STD(CLOSE,20):0),20,1) /
            (SMA(up,20,1)+SMA((CLOSE<=DELAY(CLOSE,1)?STD(CLOSE,20):0),20,1)) * 100
    分钟化改动：方向判定 DELAY 1 日 → 昨日同时刻；STD 20 日 → 20 个交易日 bar；
    SMA(20,1) 因果递推（ewm α=1/20）。
    预期效应：上涨日贡献的波动占比高 → 强势；极端高位易反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    sd = ops.ts_std(ctx.c, 20)
    up = np.where(ops.ts_delta(ctx.c, 1) > 0, sd, 0.0)
    dn = np.where(ops.ts_delta(ctx.c, 1) <= 0, sd, 0.0)
    su, sdwn = ops.sma(up, 20, 1), ops.sma(dn, 20, 1)
    return _wrap(ctx, su / (su + sdwn + EPS) * 100)


def alpha_028(df):
    """GTJA Alpha#028（KDJ 变种：3K−2D）。
    原公式：3*SMA((CLOSE-TSMIN(LOW,9))/(TSMAX(HIGH,9)-TSMIN(LOW,9))*100,3,1)
            - 2*SMA(SMA(same,3,1),3,1)
    分钟化改动：TSMAX/TSMIN 9 日 → 9 个交易日的 bar；SMA(3,1) 因果递推。
    预期效应：价格在 9 日区间内的相对位置及其加速度 → 超买超卖反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    rsv = (ctx.c - ops.ts_min(ctx.l, 9)) / \
        (ops.ts_max(ctx.h, 9) - ops.ts_min(ctx.l, 9) + EPS) * 100
    k = ops.sma(rsv, 3, 1)
    d = ops.sma(k, 3, 1)
    return _wrap(ctx, 3 * k - 2 * d)


def alpha_029(df):
    """GTJA Alpha#029（6 日涨幅 × 量）。
    原公式：(CLOSE-DELAY(CLOSE,6))/DELAY(CLOSE,6)*VOLUME
    分钟化改动：DELAY 6 日 → 6 日前同时刻；量为 bar 级。
    预期效应：带量的中期涨幅 → 放量冲高后的回落风险（常反向用）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    d = ops.ts_delay(ctx.c, 6)
    return _wrap(ctx, (ctx.c - d) / np.where(d > 0, d, np.nan) * ctx.v)


def alpha_034(df):
    """GTJA Alpha#034（均值回复，12 日）。
    原公式：MEAN(CLOSE,12)/CLOSE
    分钟化改动：MEAN 12 日 → 12 个交易日的 bar 均值。
    预期效应：现价低于 12 日均值 → 值 >1 → 均值回复买入区。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, ops.ts_mean(ctx.c, 12) / np.where(ctx.c > 0, ctx.c, np.nan))


def alpha_038(df):
    """GTJA Alpha#038（新高回落；≡ WorldQuant Alpha#23 同构）。
    原公式：((SUM(HIGH,20)/20) < HIGH) ? (-1*DELTA(HIGH,2)) : 0
    分钟化改动：SUM(HIGH,20)/20 → 20 个交易日 bar 均价；DELTA(HIGH,2) →
    2 日前同时刻最高价差。
    预期效应：价站上 20 日中枢后，若高点回落则做空信号（条件反转）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    cond = ops.ts_mean(ctx.h, 20) < ctx.h
    return _wrap(ctx, np.where(cond, -ops.ts_delta(ctx.h, 2), 0.0))


def alpha_040(df):
    """GTJA Alpha#040（涨跌成交量比 VR，26 日）。
    原公式：SUM((CLOSE>DELAY(CLOSE,1)?VOLUME:0),26) /
            SUM((CLOSE<=DELAY(CLOSE,1)?VOLUME:0),26) * 100
    分钟化改动：方向 → 相对昨日同时刻；SUM 26 日 → 26 个交易日 bar。
    预期效应：上涨日量能占比极端 → 情绪过热（反向）/ 主升确认（正向，待 IC 判定）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    up = np.where(ops.ts_delta(ctx.c, 1) > 0, ctx.v, 0.0)
    dn = np.where(ops.ts_delta(ctx.c, 1) <= 0, ctx.v, 0.0)
    return _wrap(ctx, ops.ts_sum(up, 26) / (ops.ts_sum(dn, 26) + EPS) * 100)


def alpha_042(df):
    """GTJA Alpha#042（高价波动 × 量价相关；≈ WorldQuant Alpha#40 同构）。
    原公式：(-1 * RANK(STD(HIGH,10))) * CORR(HIGH, VOLUME, 10)
    分钟化改动：STD 10 日 → 10 个交易日 bar；截面 RANK → 20 日同时刻 TSRANK；
    CORR 10 日 → 10 个交易日 bar。
    预期效应：波动放大且量价齐升 → 过热反转（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, -ops.ts_rank(ops.ts_std(ctx.h, 10), RANK_DAYS)
                 * ops.ts_corr(ctx.h, ctx.v, 10))


def alpha_043(df):
    """GTJA Alpha#043（带符号量能流，6 日 OBV 变体）。
    原公式：SUM((CLOSE>DELAY(CLOSE,1)?VOLUME:(CLOSE<DELAY(CLOSE,1)?-VOLUME:0)), 6)
    分钟化改动：方向 → 相对昨日同时刻；SUM 6 日 → 6 个交易日 bar。
    预期效应：6 日尺度主动买/卖盘净额 → 资金流向惯性或反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    sv = np.sign(ops.ts_delta(ctx.c, 1)) * ctx.v
    return _wrap(ctx, ops.ts_sum(sv, 6))


def alpha_053(df):
    """GTJA Alpha#053（上涨日占比，12 日）。
    原公式：COUNT(CLOSE>DELAY(CLOSE,1),12)/12*100
    分钟化改动：方向 → 相对昨日同时刻；COUNT 12 日 → 12 个交易日 bar 求和均值。
    预期效应：连涨天数占比极端 → 情绪透支反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    up = (ops.ts_delta(ctx.c, 1) > 0).astype(float)
    up = np.where(np.isfinite(ops.ts_delay(ctx.c, 1)), up, np.nan)
    return _wrap(ctx, ops.ts_mean(up, 12) * 100)


def alpha_054(df):
    """GTJA Alpha#054（K 线实体波动 + 量价一致性，反向）。
    原公式：(-1 * RANK(STD(ABS(CLOSE-OPEN)) + (CLOSE-OPEN) + CORR(CLOSE,OPEN,10)))
    （JoinQuant 口径中 STD 窗口缺省，取 20）
    分钟化改动：STD(ABS(C-O),20) → 20 个交易日 bar；CORR(C,O,10) → 10 个交易日 bar；
    截面 RANK → 20 日同时刻 TSRANK。
    预期效应：实体波动放大 + 收开强一致 → 单边行情末端反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    x = ops.ts_std(np.abs(ctx.c - ctx.o), 20) + (ctx.c - ctx.o) + ops.ts_corr(ctx.c, ctx.o, 10)
    return _wrap(ctx, -ops.ts_rank(x, RANK_DAYS))


def alpha_062(df):
    """GTJA Alpha#062（高点 − 量排名相关；≈ WorldQuant Alpha#44 同构）。
    原公式：(-1 * CORR(HIGH, RANK(VOLUME), 5))
    分钟化改动：截面 RANK(V) → 20 日同时刻 TSRANK；CORR 5 日 → 5 个交易日 bar。
    预期效应：价格高点与放量同步 → 冲高派筹（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, -ops.ts_corr(ctx.h, ops.ts_rank(ctx.v, RANK_DAYS), 5))


def alpha_063(df):
    """GTJA Alpha#063（RSI(6) 同构）。
    原公式：SMA(MAX(CLOSE-DELAY(CLOSE,1),0),6,1)/SMA(ABS(CLOSE-DELAY(CLOSE,1)),6,1)*100
    分钟化改动：DELAY 1 日 → 昨日同时刻收盘；SMA(6,1) 因果递推。
    预期效应：6 日尺度超买超卖 → 极端值反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    dc = ops.ts_delta(ctx.c, 1)
    num = ops.sma(np.where(dc > 0, dc, 0.0), 6, 1)
    den = ops.sma(np.abs(np.where(np.isfinite(dc), dc, 0.0)), 6, 1)
    return _wrap(ctx, num / (den + EPS) * 100)


def alpha_070(df):
    """GTJA Alpha#070（成交额波动率，6 日）。
    原公式：STD(AMOUNT, 6)
    分钟化改动：STD 6 日 → 6 个交易日 bar 的成交额标准差。
    预期效应：资金活跃度波动 → 日内波动率前瞻（波动聚集）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, ops.ts_std(ctx.amt, 6))


def alpha_076(df):
    """GTJA Alpha#076（单位量价格波动率变化，20 日）。
    原公式：STD(ABS(CLOSE/DELAY(CLOSE,1)-1)/VOLUME,20) /
            MEAN(ABS(CLOSE/DELAY(CLOSE,1)-1)/VOLUME,20)
    分钟化改动：收益 → 相对昨日同时刻；STD/MEAN 20 日 → 20 个交易日 bar。
    预期效应：单位量能推动的价格波动离散度 → 流动性冲击/筹码不稳。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    d = ops.ts_delay(ctx.c, 1)
    x = np.abs(ctx.c / np.where(d > 0, d, np.nan) - 1.0) / (ctx.v + EPS)
    return _wrap(ctx, ops.ts_std(x, 20) / (ops.ts_mean(x, 20) + EPS))


def alpha_085(df):
    """GTJA Alpha#085（量比 × 反转复合）。
    原公式：TSRANK((VOLUME/MEAN(VOLUME,20)),20) * TSRANK((-1*DELTA(CLOSE,7)),8)
    分钟化改动：MEAN(V,20) → 20 个交易日 bar；TSRANK 20/8 日 → 同时刻时序分位；
    DELTA(C,7) → 7 日前同时刻。
    预期效应：异常放量叠加超跌 → 恐慌见底反弹（正向反转）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    vr = ctx.v / (ops.ts_mean(ctx.v, 20) + EPS)
    return _wrap(ctx, ops.ts_rank(vr, 20) * ops.ts_rank(-ops.ts_delta(ctx.c, 7), 8))


def alpha_104(df):
    """GTJA Alpha#104（量价相关性突变 × 波动；≈ WorldQuant Alpha#22 同构）。
    原公式：(-1 * (DELTA(CORR(HIGH,VOLUME,5),5) * RANK(STD(CLOSE,20))))
    分钟化改动：CORR 5 日 → 5 个交易日 bar；DELTA 5 日 → 5 日前同时刻；
    STD 20 日 → 20 个交易日 bar；截面 RANK → 20 日同时刻 TSRANK。
    预期效应：量价联动快速恶化且高波动 → 趋势衰竭反转。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, -ops.ts_delta(ops.ts_corr(ctx.h, ctx.v, 5), 5)
                 * ops.ts_rank(ops.ts_std(ctx.c, 20), RANK_DAYS))


def alpha_105(df):
    """GTJA Alpha#105（开盘-量排名相关；≡ WorldQuant Alpha#3 同构）。
    原公式：(-1 * CORR(RANK(OPEN), RANK(VOLUME), 10))
    分钟化改动：两处截面 RANK → 20 日同时刻 TSRANK；CORR 10 日 → 10 个交易日 bar。
    预期效应：开盘价高位与放量共振 → 冲高回落（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, -ops.ts_corr(ops.ts_rank(ctx.o, RANK_DAYS),
                                   ops.ts_rank(ctx.v, RANK_DAYS), 10))


def alpha_139(df):
    """GTJA Alpha#139（开盘-量相关；≡ WorldQuant Alpha#6 同构）。
    原公式：(-1 * CORR(OPEN, VOLUME, 10))
    分钟化改动：CORR 10 日 → 10 个交易日 bar 窗口，逐 bar 滚动。
    预期效应：价量正相关走强（放量上行）→ 短期过热反转（反向）。"""
    ctx = _ctx(df)
    ops = build_ops(ctx)
    return _wrap(ctx, -ops.ts_corr(ctx.o, ctx.v, 10))


# ── 注册表 ────────────────────────────────────────────────────────────────
SEEDS = {
    'a001': alpha_001, 'a002': alpha_002, 'a003': alpha_003, 'a005': alpha_005,
    'a007': alpha_007, 'a008': alpha_008, 'a009': alpha_009, 'a011': alpha_011,
    'a012': alpha_012, 'a013': alpha_013, 'a014': alpha_014, 'a020': alpha_020,
    'a023': alpha_023, 'a028': alpha_028, 'a029': alpha_029, 'a034': alpha_034,
    'a038': alpha_038, 'a040': alpha_040, 'a042': alpha_042, 'a043': alpha_043,
    'a053': alpha_053, 'a054': alpha_054, 'a062': alpha_062, 'a063': alpha_063,
    'a070': alpha_070, 'a076': alpha_076, 'a085': alpha_085, 'a104': alpha_104,
    'a105': alpha_105, 'a139': alpha_139,
}

assert len(SEEDS) == 30


def compute_all(df):
    """→ DataFrame：列为 30 个种子因子，行与 df 对齐。ctx 只预处理一次。"""
    ctx = _ctx(df)
    return pd.DataFrame({name: fn(ctx) for name, fn in SEEDS.items()}, index=ctx.index)


def load_minute_csv(path_or_code, csv_dir=None):
    """读取本仓库 39 只分钟子集 CSV（t_io/backtest_1year_data），→ 归一化 df。"""
    if csv_dir is None:
        csv_dir = os.path.abspath(os.path.join(HERE, '..', '..', 'backtest_1year_data'))
    p = path_or_code
    if not os.path.exists(p):
        import glob
        fs = glob.glob(os.path.join(csv_dir, p + '*1min.csv'))
        if not fs:
            raise FileNotFoundError(f'{p} 在 {csv_dir} 无 1min CSV')
        p = fs[0]
    df = pd.read_csv(p)
    return df.sort_values('time').reset_index(drop=True)
