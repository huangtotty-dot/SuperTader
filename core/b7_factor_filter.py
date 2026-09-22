# -*- coding: utf-8 -*-
"""b7_factor_filter.py — B7 择时过滤层（s0#3 × F3 变体）生产模块（2026-09-22 施工）。

策略依据：doc/experiment/2026-09-21_B7择时层因子过滤实验.md（预注册，F3 过闸）；
集成方案：doc/solutions/2026-09-22_B7择时过滤层生产集成方案.md（owner 已审批，
审批修订：取消 shadow 态，开关 off/on 两态；判定日志全量保留；上线目标=掘金模拟盘）。

口径冻结（与实验逐点一致，改任何一项 = 新实验）：
  因子  F = ts_min_w30(ts_delay_w20(tail30_ret(vwap)))，在信号日 14:30 bar 处取值；
  z     = 逐票时序 z：过去 14 个历史日同刻（14:30 列）的均值/标准差(ddof=1)，
          严格因果不含当日；历史日数 < 5 或窗内有限值 < 2 或 std ≤ 1e-12 → z=None；
          （对应 gp_miner._zscore_matrix = fast_zscore，gp_miner.py:88-119。
           注意：是逐票时序 z，不是横截面；参数 5 是 min_hist，无 MAD 裁剪。）
  判定  F3：z < -1.0 → 放行；z ≥ -1.0 → 拦截；z=None（缺数据）→ 默认放行+na_allow 留痕。

对齐守卫（防分钟线缺口导致列错位、值漂移）：
  当日 ≤14:30 的 1min bar 切片必须恰为 211 根、首根标签 09:30、末根标签 14:30
  （gm 60s 数据源实测布局：全日 241 根 09:30~15:00，14:30 = 索引 210，
   验证见 t_io/validation/t0_schemes/parity_check_b7ff.py 与 minute_data 抽样）。
  不满足 → F=None（放行+告警），绝不在错位数据上算因子。

parity 硬闸：t_io/validation/t0_schemes/parity_check_b7ff.py 用本模块对历史信号日重算，
与 _cache_b7ff_z.json / _cache_b7ff_legs.json 对拍，82 腿放行/拦截集合 100% 一致才 PASS。

本模块只依赖 numpy + 标准库，不 import gm.api / 不读 PARAMS，可被 gm_main（生产）、
parity 脚本（验证）、bootstrap 脚本（回填）三方共用。所有写文件走 tmp+os.replace。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

import numpy as np

# ── 冻结口径常量（实验值，不许盘中改） ─────────────────────────────────────
FACTOR_EXPR = "ts_min_w30(ts_delay_w20(tail30_ret(vwap)))"
Z_THRESHOLD = -1.0          # F3 变体：z < -1 放行
Z_LOOK = 14                 # 同 gp_miner.Z_LOOK
Z_MIN_HIST = 5              # 同 gp_miner.Z_MIN_HIST
EPS = 1e-12                 # 同 gp_fast_ops.EPS

# 日内标签布局守卫（gm 60s 实测口径）
DAY_FIRST_LABEL = "09:30"
REF_LABEL = "14:30"
REF_SLICE_LEN = 211         # 09:30~14:30 共 211 根（14:30 = 全日第 210 根，0基）

# 持久化文件默认路径（t_io/state/，与 buyback_chains.json 同目录）
_DEFAULT_HIST = os.path.join(
    os.environ.get("SUPERTRADER_ROOT",
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "t_io", "state", "b7ff_factor_hist.json")
_DEFAULT_LOG_DIR = os.path.join(
    os.environ.get("SUPERTRADER_ROOT",
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "t_io", "logs")

HIST_KEEP_DAYS = 40         # 每票保留最近 40 个条目（z 只用 14，留余量供复盘）


# ══════════════════════════════════════════════════════════════════════════
# 1. 最小算子集（逐字复刻 gp_fast_ops 语义；parity 对拍为硬闸）
# ══════════════════════════════════════════════════════════════════════════
def _ts_delay(x: np.ndarray, n: int) -> np.ndarray:
    """右移 n 根：out[i] = x[i−n]；前 n 根 NaN。同 gp_fast_ops.ts_delay（:95-99）。"""
    x = np.asarray(x, dtype=float)
    if n >= x.shape[-1]:
        return np.full_like(x, np.nan)
    return np.concatenate([np.full(n, np.nan), x[: x.shape[-1] - n]])


def _ts_min(x: np.ndarray, n: int) -> np.ndarray:
    """滚动最小（含当前，窗内含 NaN → NaN；前 n−1 根 NaN）。同 gp_fast_ops.ts_min（:80-84）。"""
    x = np.asarray(x, dtype=float)
    v = np.lib.stride_tricks.sliding_window_view(x, n, axis=-1)
    ok = np.isfinite(v).all(axis=-1)
    out = np.where(np.isfinite(v), v, np.inf).min(axis=-1)
    return np.concatenate([np.full(n - 1, np.nan), np.where(ok, out, np.nan)])


def _tail30_ret(c: np.ndarray) -> np.ndarray:
    """尾盘30min收益 = c[i]/c[i−30]−1。同 gp_fast_ops.tail30_ret（:215-218）。"""
    c = np.asarray(c, dtype=float)
    base = _ts_delay(c, 30)
    with np.errstate(divide="ignore", invalid="ignore"):
        return c / np.where(base > EPS, base, np.nan) - 1.0


def _vwap_series(bars: list) -> np.ndarray:
    """当日累计 vwap = cumsum(amount)/cumsum(volume)；cum_v ≤ EPS 回退 close。

    同 gp_miner.build_stock_panel 的 vwap 构造（gp_miner.py:371-376，列内逐日口径）。
    bars: 当日 1min bar dict 序列（升序），须含 close/volume/amount。
    """
    c = np.array([float(b.get("close") or 0) for b in bars], dtype=float)
    v = np.array([float(b.get("volume") or 0) for b in bars], dtype=float)
    a = np.array([float(b.get("amount") or 0) for b in bars], dtype=float)
    cum_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(cum_v > 0,
                        np.cumsum(a) / np.where(cum_v > 0, cum_v, 1.0), c)


# ══════════════════════════════════════════════════════════════════════════
# 2. 因子值与 z 值
# ══════════════════════════════════════════════════════════════════════════
def _bar_hhmm(bar: dict) -> str:
    t = str(bar.get("time") or "")
    if " " in t:
        t = t.rsplit(" ", 1)[-1]
    return t[:5]


def factor_at_1430(day_bars: list) -> tuple:
    """从当日 bar 序列计算 F(14:30)。返回 (F 或 None, 诊断 dict)。

    输入可以是当日全量 bar（内部按 ≤14:30 切片）；切片未过对齐守卫 → (None, diag)。
    只使用 ≤14:30 的 bar，无未来函数（14:55 惰性计算与 14:30 当场计算逐点等价，
    证明见方案 §1.3：因子最深回看至 13:11 的 bar）。
    """
    slice_bars = [b for b in (day_bars or []) if _bar_hhmm(b) <= REF_LABEL]
    diag = {"n_slice": len(slice_bars),
            "first": _bar_hhmm(slice_bars[0]) if slice_bars else None,
            "last": _bar_hhmm(slice_bars[-1]) if slice_bars else None}
    if (len(slice_bars) != REF_SLICE_LEN
            or diag["first"] != DAY_FIRST_LABEL or diag["last"] != REF_LABEL):
        diag["guard"] = "align_fail"
        return None, diag
    vw = _vwap_series(slice_bars)
    F = _ts_min(_ts_delay(_tail30_ret(vw), 20), 30)
    f = float(F[-1])
    diag["guard"] = "ok"
    return (f if np.isfinite(f) else None), diag


def compute_z(today_f: float | None, hist: list, today: str) -> tuple:
    """逐票时序 z（复刻 fast_zscore 在 14:30 列的语义）。返回 (z 或 None, hist_days)。

    hist: [{date, f}] 升序条目列表（f 可为 None=当日对齐守卫失败的历史留痕）。
    严格因果：只用 date < today 的条目；窗口 = 最近 ≤Z_LOOK 个历史条目；
    门控（同 gp_miner.py:98-115）：
      历史日数 < Z_MIN_HIST → None（fast_zscore 的 day_ok，按日数不按有限值）；
      窗内有限值 cnt < 2 → None；std(ddof=1) ≤ 1e-12 → None；today_f None → None。
    """
    prior = [h for h in (hist or [])
             if str(h.get("date", "")) < str(today)]
    hist_days = len(prior)
    if today_f is None or hist_days < Z_MIN_HIST:
        return None, hist_days
    win = [float(h["f"]) for h in prior[-Z_LOOK:]
           if h.get("f") is not None and np.isfinite(float(h["f"]))]
    if len(win) < 2:
        return None, hist_days
    m = float(np.mean(win))
    s = float(np.std(win, ddof=1))
    if not np.isfinite(s) or s <= EPS:
        return None, hist_days
    return (float(today_f) - m) / s, hist_days


def decide(z: float | None) -> tuple:
    """F3 判定。返回 (decision, reason)：
      z=None   → ("na_allow",  "z不可用，默认放行")
      z <  -1  → ("pass",      "z<-1 放行")
      z ≥  -1  → ("block",     "z≥-1 拦截")
    """
    if z is None:
        return "na_allow", "z=None 默认放行"
    if z < Z_THRESHOLD:
        return "pass", f"z={z:.4f} < {Z_THRESHOLD} 放行"
    return "block", f"z={z:.4f} >= {Z_THRESHOLD} 拦截"


# ══════════════════════════════════════════════════════════════════════════
# 3. 因子历史持久化（t_io/state/b7ff_factor_hist.json）
# ══════════════════════════════════════════════════════════════════════════
def load_hist(path: str | None = None) -> dict:
    """读历史文件 → {code: [{date, f}]}。读失败/腐坏 → {}（fail-open，调用方按 z=None 放行）。"""
    path = path or _DEFAULT_HIST
    try:
        with open(path, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_hist(hist: dict, path: str | None = None) -> bool:
    """tmp + os.replace 原子写（沿用 core/utils.py C19 修复模式）。失败返回 False。"""
    path = path or _DEFAULT_HIST
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".b7ff_", suffix=".tmp",
                                   dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(hist, fp, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def record_today_f(hist: dict, code: str, date: str, f: float | None) -> dict:
    """把当日 F（可为 None）写入 hist[code]，幂等（同日重写覆盖），按日期升序、
    只留最近 HIST_KEEP_DAYS 条。返回新 hist（不原地改调用方 dict 语义外的结构）。"""
    entries = [e for e in (hist.get(code) or []) if e.get("date") != date]
    entries.append({"date": date,
                    "f": (round(float(f), 8) if f is not None else None)})
    entries.sort(key=lambda e: str(e.get("date", "")))
    hist[code] = entries[-HIST_KEEP_DAYS:]
    return hist


# ══════════════════════════════════════════════════════════════════════════
# 4. 判定日志（owner 审批口径②：off/on 两态，但每次 14:55 判定全量落 JSONL）
# ══════════════════════════════════════════════════════════════════════════
def append_decision_log(rec: dict, log_dir: str | None = None) -> None:
    """追加一行到 t_io/logs/b7ff_filter_{date}.jsonl。fail-open 静默（不炸主流程）。"""
    try:
        log_dir = log_dir or _DEFAULT_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)
        date = str(rec.get("date") or datetime.now().strftime("%Y-%m-%d"))
        rec = dict(rec)
        rec.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(os.path.join(log_dir, f"b7ff_filter_{date}.jsonl"),
                  "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def evaluate_and_log(day_bars: list, code: str, date: str,
                     hist_path: str | None = None,
                     log_dir: str | None = None,
                     persist: bool = True,
                     extra: dict | None = None) -> dict:
    """一站式：算 F → 算 z → 判定 → （可选）写历史 + 判定日志。返回判定 dict。

    gm_main 挂钩只调这一个函数；persist=False 供 parity/bootstrap 复用不碰生产文件。
    extra：合并进判定日志的附加上下文字段（如 tail30_pct），不影响判定。
    返回 {code, date, f1430, z, hist_days, decision, reason, guard}。
    """
    f, diag = factor_at_1430(day_bars)
    hist = load_hist(hist_path)
    z, hist_days = compute_z(f, hist.get(code) or [], date)
    decision, reason = decide(z)
    rec = {"code": code, "date": date,
           "f1430": (round(f, 8) if f is not None else None),
           "z": (round(z, 6) if z is not None else None),
           "hist_days": hist_days, "decision": decision, "reason": reason,
           "guard": diag}
    if extra:
        rec.update(extra)
    if persist:
        hist = record_today_f(hist, code, date, f)
        save_hist(hist, hist_path)
        append_decision_log(rec, log_dir)
    return rec


# ══════════════════════════════════════════════════════════════════════════
# 5. 离线自测（python core/b7_factor_filter.py）
# ══════════════════════════════════════════════════════════════════════════
def _mk_day_bars(closes, vol=1000.0, amt_per_px=1000.0) -> list:
    """合成标准布局当日 bar：09:30 起每分钟一根，amount=close×vol 使 vwap 可算。"""
    import itertools
    labels = []
    for hm in itertools.chain(
            (f"09:{m:02d}" for m in range(30, 60)),
            (f"10:{m:02d}" for m in range(0, 60)),
            (f"11:{m:02d}" for m in range(0, 31)),
            (f"13:{m:02d}" for m in range(1, 60)),
            (f"14:{m:02d}" for m in range(0, 60)),
            ("15:00",)):
        labels.append(hm)
    bars = []
    for lab, c in zip(labels, closes):
        bars.append({"time": f"2026-09-22 {lab}:00", "open": c, "high": c,
                     "low": c, "close": c, "volume": vol,
                     "amount": c * vol * amt_per_px})
    return bars


def _selftest() -> int:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    fails = []
    n = [0]

    def check(name, cond):
        n[0] += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("== 1. 算子语义（对照 gp_fast_ops 定义手算） ==")
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    d = _ts_delay(x, 2)
    check("ts_delay 右移", np.isnan(d[0]) and np.isnan(d[1])
          and d[2] == 1.0 and d[4] == 3.0)
    m = _ts_min(x, 3)
    check("ts_min 滚动最小", np.isnan(m[0]) and np.isnan(m[1])
          and m[2] == 1.0 and m[4] == 3.0)
    xn = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
    mn = _ts_min(xn, 3)
    check("ts_min 窗内NaN→NaN", np.isnan(mn[2]) and np.isnan(mn[3])
          and mn[4] == 3.0)
    t = _tail30_ret(np.arange(1.0, 42.0))
    check("tail30_ret 前30根NaN", np.isnan(t[:30]).all()
          and abs(t[30] - (31.0 / 1.0 - 1)) < 1e-12)

    print("== 2. 对齐守卫 ==")
    closes = list(np.linspace(10.0, 10.5, 241))
    bars = _mk_day_bars(closes)
    f, diag = factor_at_1430(bars)
    check("标准日 F 可算", f is not None and diag["guard"] == "ok")
    check("切片=211 首09:30 末14:30",
          diag["n_slice"] == 211 and diag["first"] == "09:30"
          and diag["last"] == "14:30")
    f2, d2 = factor_at_1430(bars[:200])          # 缺 14:30 段
    check("缺bar → None(align_fail)", f2 is None and d2["guard"] == "align_fail")
    f3, _d3 = factor_at_1430([])
    check("空输入 → None", f3 is None)

    print("== 3. 因子公式手算对照（vwap=close 时退化为 close 口径） ==")
    # amount = close*vol → 累计 vwap = 加权均价 ≠ close；改用恒定价格段验证：
    # 价格恒定段内 tail30_ret=0，突变段产生非零。构造：前181根=10.0，14:00起=11.0
    c2 = [10.0] * 181 + [11.0] * 60
    b2 = _mk_day_bars(c2, vol=100.0, amt_per_px=1.0)
    f2v, _ = factor_at_1430(b2)
    # vwap 在 14:30 处为 181 根 10.0 与 30 根 11.0 的加权 = (1810+330)/211 ≈ 10.142
    # tail30_ret(vwap)[210] = vw[210]/vw[180]-1；delay20 → t=190 处的值；min30 → min(t=161..190)
    # 手算锚点：vw[160]/vw[130]（delay 后 min 窗最旧端）
    vol = 100.0
    cv = np.cumsum([vol] * 211)
    ca = np.cumsum([c * vol for c in c2[:211]])
    vw = ca / cv
    t30 = vw[30:] / vw[:-30] - 1
    t30 = np.concatenate([np.full(30, np.nan), t30])
    dl = np.concatenate([np.full(20, np.nan), t30[:-20]])
    expect = np.nanmin(dl[210 - 29: 211])
    check(f"手算对照 F={f2v:.8f} vs {expect:.8f}", abs(f2v - expect) < 1e-10)

    print("== 4. z 值与判定（复刻 fast_zscore 门控） ==")
    hist = [{"date": f"2026-09-{d:02d}", "f": 0.01 * d} for d in range(1, 8)]
    z, hd = compute_z(0.10, hist, "2026-09-08")
    win = [0.01 * d for d in range(1, 8)]
    ez = (0.10 - np.mean(win)) / np.std(win, ddof=1)
    check("z 手算一致", abs(z - ez) < 1e-12 and hd == 7)
    check("历史<5日 → None",
          compute_z(0.1, hist[:4], "2026-09-08")[0] is None)
    check("today=None → None", compute_z(None, hist, "2026-09-08")[0] is None)
    check("严格因果(含当日条目被排除)",
          compute_z(0.1, hist + [{"date": "2026-09-08", "f": 99.0}],
                    "2026-09-08")[1] == 7)
    zc, _ = compute_z(0.1, [{"date": f"2026-09-0{d}", "f": 0.05}
                            for d in range(1, 8)], "2026-09-09")
    check("std=0 → None", zc is None)
    hnan = hist[:6] + [{"date": "2026-09-07", "f": None}]
    zn, hdn = compute_z(0.10, hnan, "2026-09-08")
    check("窗内NaN被剔除、日数照计", zn is not None and hdn == 7)
    # 回归（2026-09-22 parity 实证）：f=0.0 是有限值必须计入窗口——
    # `h.get('f') or np.nan` 写法会把 0.0 误判为缺失踢出窗口（falsy-zero bug）。
    hz = [{"date": f"2026-09-{d:02d}", "f": (0.0 if d == 3 else 0.01 * d)}
          for d in range(1, 8)]
    z0, _ = compute_z(0.10, hz, "2026-09-08")
    w0 = [0.0 if d == 3 else 0.01 * d for d in range(1, 8)]
    check("f=0.0 计入窗口", abs(z0 - (0.10 - np.mean(w0)) / np.std(w0, ddof=1)) < 1e-12)
    check("decide: z<-1 pass", decide(-1.5)[0] == "pass")
    check("decide: z≥-1 block", decide(-0.5)[0] == "block")
    check("decide: 边界 z=-1.0 block", decide(-1.0)[0] == "block")
    check("decide: None → na_allow", decide(None)[0] == "na_allow")

    print("== 5. 持久化与日志（临时目录，不碰生产文件） ==")
    with tempfile.TemporaryDirectory() as td:
        hp = os.path.join(td, "hist.json")
        ld = os.path.join(td, "logs")
        r1 = evaluate_and_log(bars, "000988", "2026-09-22", hp, ld)
        check("一站式返回 decision", r1["decision"] in ("pass", "block", "na_allow"))
        check("首日正式 z=None(无历史)", r1["z"] is None and r1["decision"] == "na_allow")
        h = load_hist(hp)
        check("历史落盘 1 条", len(h.get("000988") or []) == 1)
        logs = open(os.path.join(ld, "b7ff_filter_2026-09-22.jsonl"),
                    encoding="utf-8").read().strip().split("\n")
        check("判定日志 1 行", len(logs) == 1)
        rec = json.loads(logs[0])
        check("日志含 code/z/decision/guard",
              rec["code"] == "000988" and "z" in rec
              and rec["decision"] == "na_allow" and rec["guard"]["guard"] == "ok")
        # 幂等：同日重写不叠加
        evaluate_and_log(bars, "000988", "2026-09-22", hp, ld)
        check("同日幂等", len(load_hist(hp)["000988"]) == 1)
        # 腐坏文件 fail-open
        open(hp, "w").write("{broken")
        check("腐坏历史 → load 返回 {}", load_hist(hp) == {})

    print(f"\n自测结果: {n[0]} 项断言，"
          f"{'全部通过' if not fails else '失败 ' + str(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
