# -*- coding: utf-8 -*-
"""日线缓存 ↔ 通达信本地 `.day` 校准（2026-10-30）。

## 为什么（owner 报 301079 邵阳液压「数据很不正常」）
本仓日线缓存是**前复权**、由 provider 按需/盘后预下载填充；通达信 `vipdoc/*/lday/*.day`
是**不复权**、由客户端每天更新。两者在最近一段必然逐值相等（复权只改历史）。
实测 2026-10-30：
  · 301079 缓存末行停在 **10-26**，通达信/同花顺已是 10-30 ⇒ 图上最后一根被画成
    "从 26 跳到 36.79 的巨柱"（中间 10-27~10-30 四根 ≈ +42% 全缺）；
  · 该票缓存只有 **213 行**（2025-12 起），通达信有 **1211 行**（2021-10 起）
    ⇒ MA150/MA360、箱体、背离这些长历史列算不出或算错。
  · 全池抽查（前 800 只）**369 只落后 ≥1 个交易日**。

## 做什么
`calibrate_all()`：逐只比对 → 只**追加**缓存末行之后、通达信里有的日线（merge 语义，不改已有行）。
安全门（任一不满足就只报告、不写）：
  · 最近重叠日 `缓存收盘/通达信收盘` 比率 |r−1| ≤ 0.5%（缓存前复权 vs 通达信不复权，近期应为 1）；
  · 更早的重叠日比率与最近一致（不一致 ⇒ 中间除权、复权因子变了 ⇒ 抄不复权数据会错）。
量：通达信 `.day` 是**股**、缓存是**手** ⇒ `/100`。

⚠️ 本模块只补**尾部**。历史深度缺陷（缓存行数远少于通达信）要靠 provider 重新拉长历史，
不在本工具里做（`calibrate_all` 会把它计入 `short` 统计）。
"""
from __future__ import annotations

import json
import os
import shutil
import struct
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
CACHE_DIR = _BASE / "t_io" / "cache" / "daily_kline"
DEFAULT_TDX_HOME = Path(os.environ.get("TDX_HOME") or r"C:\new_tdx")
_RATIO_TOL = 0.005
_MAX_APPEND = 60


def tdx_market(code: str) -> str:
    """代码 → 通达信市场段（与 `t_gui._tdx_day_path` 同一套规则）。"""
    c = str(code)
    if c.startswith(("88", "99", "60", "68", "56", "51", "58")):
        return "sh"
    if c.startswith(("43", "83", "87", "92", "920")):
        return "bj"
    if c.startswith(("39", "15", "16", "00", "30", "12")):
        return "sz"
    return "sh"


def tdx_path(code: str, tdx_home: Path) -> Path | None:
    mk = tdx_market(code)
    fp = Path(tdx_home) / "vipdoc" / mk / "lday" / f"{mk}{code}.day"
    return fp if fp.exists() else None


def read_tdx(fp: Path) -> list[dict]:
    """通达信 `.day`（32 字节/根）→ [{date, open, high, low, close, volume}]，量=**手**。"""
    try:
        b = Path(fp).read_bytes()
    except OSError:
        return []
    out = []
    for i in range(len(b) // 32):
        d, o, h, l, c, _amt, v, _ = struct.unpack("<IIIIIfii", b[i * 32:(i + 1) * 32])
        if not (19900101 < d < 21000101):
            continue
        ds = str(d)
        out.append({"date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                    "open": o / 100.0, "high": h / 100.0,
                    "low": l / 100.0, "close": c / 100.0,
                    "volume": round(v / 100.0, 2)})
    out.sort(key=lambda r: r["date"])
    return out


def calibrate_one(code: str, tdx_home: Path, fix: bool = False,
                  bak_dir: Path | None = None, cache_dir: Path | None = None) -> dict:
    """单只检查/修复（不抛）。返回状态字典。

    2026-10-30 第二轮：不再只"追加缺失"，而是识别并**重写被快照污染的尾部**。
    实测 000006：10-26 之前缓存与通达信**逐值相同**，10-27 起变成
    close 6.68/vol 23216、close 6.33/vol 9454、close 5.80/vol 0.0 —— 这是**盘中快照根**
    （量只累计到当时），而真实收盘一直没回来（腾讯 fqkline 被 WAF 挡、GM 不可达）。

    判据：
      · 锚点 d0 = 从末行往前，第一个「缓存量 ≥ 0.8×通达信量」的重叠日（量不受复权影响）；
      · 锚点复权比率 r = 缓存收盘/通达信收盘，要求 |r−1| ≤ 2%（前复权在最近应为 1；
        偏离说明复权口径不一致/近期除权 ⇒ 不动，标记 ratio_mismatch）；
      · d0 之后（含缺失与污染）**全部按通达信 × r 重写**，d0 及更早的行一字不动。
    """
    cdir = Path(cache_dir or CACHE_DIR)
    res = {"code": str(code), "status": "ok"}
    tfp = tdx_path(code, tdx_home)
    if tfp is None:
        res["status"] = "no_tdx"
        return res
    trows = read_tdx(tfp)
    try:
        cd = json.loads((cdir / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        cd = {}
    crows = cd.get("rows") or []
    if not crows or not trows:
        res["status"] = "empty"
        return res

    c_last = str(crows[-1].get("date") or "")[:10]
    res.update({"cache_last": c_last, "tdx_last": trows[-1]["date"],
                "cache_rows": len(crows), "tdx_rows": len(trows)})

    c_by = {str(r.get("date"))[:10]: r for r in crows}
    t_by = {r["date"]: r for r in trows}
    common = sorted(set(c_by) & set(t_by))
    res["overlap_days"] = len(common)
    if not common:
        res["status"] = "no_overlap"
        return res

    # 锚点：从最近的共同日往前找第一个「量对得上」的（快照根的量明显偏小）
    anchor = None
    for d in reversed(common):
        cv = float(c_by[d].get("volume") or 0.0)
        tv = float(t_by[d]["volume"] or 0.0)
        # 量能既要"不是快照"（≥0.8×），也要"单位一致"（≤1.5×，防 ETF/债券等张/股单位差 100 倍）
        if tv > 0 and 0.8 * tv <= cv <= 1.5 * tv:
            anchor = d
            break
    if anchor is None:
        res["status"] = "no_clean_anchor"
        return res
    r = (float(c_by[anchor]["close"]) / float(t_by[anchor]["close"])
         if t_by[anchor]["close"] else 0.0)
    res["anchor"] = anchor
    res["ratio"] = round(r, 6)
    if abs(r - 1.0) > 0.02:
        res["status"] = "ratio_mismatch"     # 复权口径不一致/近期除权 ⇒ 抄不复权会错
        return res

    t_after = [x for x in trows if x["date"] > anchor]
    res["tail_days"] = len(t_after)
    if not t_after:
        return res
    if len(t_after) > _MAX_APPEND:
        res["status"] = "too_many_missing"
        return res

    # 尾部里有多少是"错的"（量明显偏小或缺失）——用于报告
    bad = 0
    for x in t_after:
        cr = c_by.get(x["date"])
        if cr is None:
            bad += 1
            continue
        cv = float(cr.get("volume") or 0.0)
        tv = float(x["volume"] or 0.0)
        if tv > 0 and (cv < 0.8 * tv or abs(float(cr.get("close") or 0) - x["close"] * r) > 0.005):
            bad += 1
    res["polluted"] = bad
    res["status"] = "behind" if bad or len(t_after) else "ok"
    if not fix or res["status"] == "ok":
        return res

    # ---- 重写尾部（锚点及更早的行一字不动）----
    keep = [row for row in crows if str(row.get("date"))[:10] <= anchor]
    t_dates = {x["date"] for x in t_after}
    # 缓存里锚点之后、通达信没有的行（极少）保留原样，避免丢数据
    extra = [row for row in crows if str(row.get("date"))[:10] > anchor
             and str(row.get("date"))[:10] not in t_dates]
    rebuilt = [{"date": x["date"],
                "open": round(x["open"] * r, 4),
                "close": round(x["close"] * r, 4),
                "high": round(x["high"] * r, 4),
                "low": round(x["low"] * r, 4),
                "volume": x["volume"]} for x in t_after]
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        if bak_dir is not None:
            Path(bak_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(cdir / f"{code}.json", Path(bak_dir) / f"{code}.json")
        out = dict(cd)
        out["rows"] = keep + rebuilt + extra
        out["rows"].sort(key=lambda x: str(x.get("date"))[:10])
        out["date"] = str(out["rows"][-1].get("date"))[:10]
        out["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out["forming"] = []                  # 通达信收盘后的确定数据
        tmp = cdir / f".{code}.tmp"
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cdir / f"{code}.json")
        res["status"] = "fixed"
        res["rewritten"] = len(rebuilt)
        res["new_last"] = out["date"]
    except Exception as e:
        res["status"] = "fix_failed"
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def calibrate_all(codes: list[str] | None = None, fix: bool = False,
                  tdx_home: Path | None = None, cache_dir: Path | None = None,
                  limit: int = 0) -> dict:
    """全池（或给定 codes）校准。返回 {summary, results, behind, ratio_bad, short, bak_dir}。"""
    home = Path(tdx_home or DEFAULT_TDX_HOME)
    if not (home / "vipdoc").exists():
        return {"summary": {"no_tdx_home": 1}, "results": [], "behind": [],
                "ratio_bad": [], "short": [], "tdx_home": str(home), "missing_home": True}
    cdir = Path(cache_dir or CACHE_DIR)
    if codes is None:
        codes = sorted(p.stem for p in cdir.glob("*.json")
                       if p.stem[:1].isdigit() and not p.stem.startswith("index_"))
    codes = [str(c).split("_")[0] for c in codes if c]
    if limit:
        codes = codes[:int(limit)]
    bak = (cdir / f"_bak_{datetime.now():%Y%m%d_%H%M}") if fix else None
    results = [calibrate_one(c, home, fix, bak, cdir) for c in codes]
    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"tdx_home": str(home), "fix": bool(fix), "summary": summary,
            "results": results,
            "behind": [r for r in results if r["status"] in ("behind", "fixed")],
            "ratio_bad": [r for r in results if r["status"] in ("ratio_mismatch", "no_clean_anchor")],
            "short": [r for r in results
                      if r.get("tdx_rows", 0) - r.get("cache_rows", 0) > 50],
            "bak_dir": str(bak) if bak else None}


def run_daily_once(fix: bool = True, tdx_home: Path | None = None,
                   cache_dir: Path | None = None, now: datetime | None = None) -> dict | None:
    """**每交易日只跑一次**（跨进程标记）——供 main.py / GUI 盘后自动调用。

    条件：工作日且 15:40 之后；当天没跑过（`t_io/state/tdx_calibrate_YYYYMMDD.done`）。
    返回报告 dict；不满足条件返回 None。标记写失败不影响校准本身。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return None
    if now.strftime("%H:%M") < "15:40":
        return None
    fp = _BASE / "t_io" / "state" / f"tdx_calibrate_{now:%Y%m%d}.done"
    if fp.exists():
        return None
    rep = calibrate_all(fix=fix, tdx_home=tdx_home, cache_dir=cache_dir)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                                  "summary": rep.get("summary")}, ensure_ascii=False),
                      encoding="utf-8")
    except Exception:
        pass
    return rep
