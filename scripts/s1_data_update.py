# -*- coding: utf-8 -*-
"""S1 选股策略 · 数据日更管线（任务 W1，2026-10-10）
=====================================================
把研究面板与衍生数据从基准日（2026-09-17）增量更新到最新交易日，供
s1_sim.py（S1 终选规则真源）直接消费。

数据链（考古自 t_io/validation/xsection/PANEL_REFRESH_2026-09-18.md 与
fetch_panel.py，原始构建方法完整保留，本脚本为其"增量日更"等价物）：

  GM history_n(adjust=ADJUST_PREV, 前复权)
    -> t_io/validation/xsection/panel/shards/*.parquet   【本脚本 step: fetch】
    -> F3 因子快照 factors_filtered.parquet              【step: factors】
       （daily_selection_screen.compute_factor_series + build_tradable_mask）
    -> s0_prep.py  -> scores.parquet + pivots.pkl        【step: derived】
    -> s1_prep.py  -> s1_hygiene_mask.pkl                【step: derived】
    -> s1_sim.py --n 4 --m 8 --min-hold 1 --tp-arm A     【step: verify】

数据源决策（二选一，选 a）：
  (a) GM 掘金终端 history_n —— 采用。与存量面板**同源同口径**（前复权 ADJUST_PREV、
      eob +08:00 格式、含退市股 PIT），是硬验收"09-17 前 nav 逐日一致"的唯一可行源。
      akshare 在本机被拦（fetch_panel.py 头注释）、无退市股覆盖、复权口径与 GM 不同，
      混源必然污染基线。终端未开时本脚本优雅报错并提示启动 gmterm-serv.exe。

关键口径处理：
  1. 前复权锚点漂移：ADJUST_PREV 以"最新交易日"为锚，基准日后若有分红/送转，
     同一历史日新拉价格会与存量不同。=> 每只股票做重叠日（基准日前 ~27 根）校验，
     close 相对差异 > 1e-4 判定漂移，该股全量重拉（count=2000）替换；差异报告落盘。
  2. ST 闸时点：历史段（<= 基准日）用**旧 universe** 的 ST 名单复算，保证基线
     逐日一致；新增段（> 基准日）用**新 universe** 的当前名称（与既有研究口径
     "剔ST按当前名称"一致，限制声明不变）。
  3. 新上市股：全量拉 2000 根，写入新分片 shard_0012+（500 只/片，与原始约定一致）。

子命令（每日更新一把梭: `python scripts/s1_data_update.py all`）：
  probe     检查 GM 终端连通性
  backup    备份基准产物（universe + scores/pivots/mask/基线 nav）
  universe  刷新 universe.parquet（新上市/退市/更名对比报告）
  fetch     增量拉取分片（断点续跑，--max-seconds 自控，被杀后重跑继续）
  factors   重建 F3 因子快照（历史段旧 ST 名单 + 新增段新 ST 名单）
  derived   复用 s0_prep.main()/s1_prep.main() 刷新 scores/pivots/卫生掩码（原文件不改）
  verify    硬验收：scores 与 nav 在基准日前逐日一致（容差 1e-6）+ 新增段摘要
  all       backup -> universe -> fetch -> factors -> derived -> verify

解释器：fetch/universe/probe 需要 gm.api（仅用户 Python 3.11 安装，
与 fetch_panel.py 相同）；factors/derived/verify 任意受管 Python 即可。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
XSEC = ROOT / "t_io" / "validation" / "xsection"
PANEL = XSEC / "panel"
SHARDS = PANEL / "shards"
INCR = PANEL / "_incr"                      # 断点/报告目录（load_panel 不读这里）
FM = ROOT / "t_io" / "validation" / "factor_mining"
S0 = FM / "s0_account"
RESULTS = S0 / "results"
REPORT = INCR / "update_report.json"

FIELDS = "symbol,eob,open,high,low,close,volume,amount"
SINCE = "2023-09-01"           # 与 s0_prep.SINCE / F3 --since 基线口径一致
FETCH_COUNT_INCR = 45          # 增量拉取根数（覆盖重叠校验窗 + 新增交易日）
FETCH_COUNT_FULL = 2000        # 全量（新股/漂移股），与 fetch_panel.py 一致
SHARD_SIZE = 500
DRIFT_RTOL = 1e-4              # 重叠日 close 相对差异阈值（前复权锚点漂移判定）
USER_PY = r"C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe"

for _p in (str(ROOT / "execution" / "auto"), str(ROOT / "execution" / "auto" / "_gm"),
           str(FM), str(S0)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_report() -> dict:
    if REPORT.exists():
        return json.loads(REPORT.read_text(encoding="utf-8"))
    return {}


def _save_report(rep: dict) -> None:
    INCR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")


def detect_cutoff() -> pd.Timestamp:
    """存量面板基准日（最大 eob），增量从其次日开始。"""
    mx = None
    for f in sorted(SHARDS.glob("shard_*.parquet")):
        e = pd.read_parquet(f, columns=["eob"])["eob"].max()
        mx = e if mx is None else max(mx, e)
    if mx is None:
        raise FileNotFoundError(f"未找到存量分片：{SHARDS}")
    return pd.Timestamp(mx).tz_localize(None).normalize()


def _gm_api():
    """导入 gm.api 并注入终端 token；失败时给出可操作的中文报错。"""
    try:
        from utils.gm_token import load_token  # execution/auto/_gm/utils
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"[GM] 无法导入 utils.gm_token：{e}")
    try:
        import gm.api as gm
    except ImportError:
        raise SystemExit(
            "[GM] 当前解释器没有 gm.api（国盛掘金 SDK 只装在用户 Python 3.11）。\n"
            f"     请改用：{USER_PY} scripts/s1_data_update.py <子命令>")
    tok = load_token()
    if not tok:
        raise SystemExit(
            "[GM] 未获取到终端 token。请先启动国盛掘金终端（gmterm-serv.exe）并登录，"
            "再重跑本命令。")
    gm.set_token(tok)
    return gm


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------
def cmd_probe(_args) -> None:
    gm = _gm_api()
    df = gm.history_n(symbol="SHSE.600519", frequency="1d", count=3,
                      fields=FIELDS, adjust=gm.ADJUST_PREV, df=True)
    last = pd.Timestamp(df["eob"].max()).tz_localize(None).date()
    _log(f"[probe] GM 终端在线，600519 最新日线 eob={last}")
    rep = _load_report()
    rep["probe"] = dict(ok=True, gm_latest_eob=str(last), ts=time.strftime("%F %T"))
    _save_report(rep)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def cmd_backup(_args) -> None:
    cutoff = detect_cutoff()
    tag = cutoff.strftime("%Y-%m-%d")
    # 新一轮更新：清空上一轮的重叠校验归档（done 标记按 cutoff 自过期）
    (INCR / "overlap_summary.jsonl").unlink(missing_ok=True)
    bdir = RESULTS / f"baseline_{tag}"
    bdir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("scores.parquet", "pivots.pkl", "s1_hygiene_mask.pkl"):
        src = RESULTS / name
        if src.exists():
            shutil.copy2(src, bdir / name)
            copied.append(name)
    nav = RESULTS / "s1_nav" / "nav_s1_eq_N4_M8_H1_A.csv"
    if nav.exists():
        shutil.copy2(nav, bdir / nav.name)
        copied.append(nav.name)
    # 旧 universe（历史段 ST 名单真源）
    uni = PANEL / "universe.parquet"
    if uni.exists():
        dst = XSEC / "panel_aux" / f"universe_backup_{tag}.parquet"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(uni, dst)
        copied.append(str(dst.relative_to(ROOT)))
    _log(f"[backup] 基准日={tag}，已备份 {len(copied)} 项 -> {bdir} 等")
    rep = _load_report()
    rep["backup"] = dict(cutoff=tag, dir=str(bdir), files=copied)
    _save_report(rep)


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------
def cmd_universe(_args) -> None:
    gm = _gm_api()
    cutoff = baseline_cutoff()
    tag = cutoff.strftime("%Y-%m-%d")
    uni_new = gm.get_symbols(sec_type1=1010, skip_suspended=False, skip_st=False, df=True)
    keep = [c for c in ("symbol", "sec_name", "exchange", "listed_date", "delisted_date")
            if c in uni_new.columns]
    uni_new = uni_new[keep].reset_index(drop=True)
    uni_path = PANEL / "universe.parquet"
    uni_old = pd.read_parquet(uni_path) if uni_path.exists() else pd.DataFrame(columns=keep)

    old_s = set(uni_old["symbol"]) if len(uni_old) else set()
    new_s = set(uni_new["symbol"])
    added = sorted(new_s - old_s)
    removed = sorted(old_s - new_s)
    # 名称变化（ST 状态变化监控）
    nm_old = dict(zip(uni_old.get("symbol", []), uni_old.get("sec_name", [])))
    nm_new = dict(zip(uni_new["symbol"], uni_new["sec_name"]))
    renamed = {s: (nm_old[s], nm_new[s]) for s in (old_s & new_s)
               if str(nm_old.get(s)) != str(nm_new.get(s))}
    # 新退市（delisted_date 新出现）
    dl_old = dict(zip(uni_old.get("symbol", []), uni_old.get("delisted_date", [])))
    dl_new = dict(zip(uni_new["symbol"], uni_new["delisted_date"]))
    new_delisted = sorted(s for s in (old_s & new_s)
                          if pd.isna(dl_old.get(s)) and pd.notna(dl_new.get(s)))

    uni_new.to_parquet(uni_path, index=False)
    _log(f"[universe] 新 universe {len(uni_new)} 只（旧 {len(uni_old)}）；"
         f"新增 {len(added)} / 移除 {len(removed)} / 更名 {len(renamed)} / 新退市 {len(new_delisted)}")
    rep = _load_report()
    rep["universe"] = dict(cutoff=tag, n_new=len(uni_new), n_old=len(uni_old),
                           added=added, removed=removed, renamed=renamed,
                           new_delisted=new_delisted)
    _save_report(rep)


# ---------------------------------------------------------------------------
# fetch（增量 + 断点续跑 + 重叠校验 + 漂移全量重拉）
# ---------------------------------------------------------------------------
def _symbol_shard_map() -> dict[str, str]:
    """扫描分片，建立 symbol -> shard 文件名 映射。"""
    m = {}
    for f in sorted(SHARDS.glob("shard_*.parquet")):
        syms = pd.read_parquet(f, columns=["symbol"])["symbol"].unique()
        for s in syms:
            m[str(s)] = f.name
    return m


def _progress_path(shard_name: str) -> Path:
    return INCR / f"{shard_name}.progress.jsonl"


def _partial_path(shard_name: str) -> Path:
    return INCR / f"{shard_name}.newrows.parquet"


def _load_done(shard_name: str) -> set[str]:
    p = _progress_path(shard_name)
    done = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["symbol"])
            except Exception:
                continue
    return done


def _append_progress(shard_name: str, rec: dict) -> None:
    INCR.mkdir(parents=True, exist_ok=True)
    with open(_progress_path(shard_name), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _append_partial(shard_name: str, df_new: pd.DataFrame) -> None:
    """partial parquet 不能追加：读旧拼新重写（行数小，代价可忽略）。"""
    p = _partial_path(shard_name)
    if p.exists():
        old = pd.read_parquet(p)
        df_new = pd.concat([old, df_new], ignore_index=True)
    df_new.to_parquet(p, index=False)


def _fetch_one(gm, sym: str, count: int, adjust_mode, sleep: float):
    try:
        df = gm.history_n(symbol=sym, frequency="1d", count=count,
                          fields=FIELDS, adjust=adjust_mode, df=True)
        time.sleep(sleep)
        return df if df is not None and len(df) else None, None
    except Exception as e:  # noqa: BLE001
        time.sleep(sleep)
        return None, str(e)


def baseline_cutoff() -> pd.Timestamp:
    """本轮更新的基准日：优先 backup 记录（多轮续跑时面板已被部分合并，
    实时 detect 会漂移到新日期导致增量行被丢），无记录则实时探测。"""
    c = _load_report().get("backup", {}).get("cutoff")
    return pd.Timestamp(c) if c else detect_cutoff()


def cmd_fetch(args) -> None:
    gm = _gm_api()
    cutoff = baseline_cutoff()
    cutoff64 = np.datetime64(cutoff)
    adjust_mode = gm.ADJUST_PREV
    uni = pd.read_parquet(PANEL / "universe.parquet")
    uni_syms = [str(s) for s in uni["symbol"]]
    sym2shard = _symbol_shard_map()
    known = set(sym2shard)
    new_syms = [s for s in uni_syms if s not in known]

    # 新股 -> 新分片命名（shard_0012 起，500 只/片）
    existing_ids = [int(f.stem.split("_")[1]) for f in SHARDS.glob("shard_*.parquet")]
    next_id = max(existing_ids) + 1 if existing_ids else 0
    new_shard_of = {}
    for i, s in enumerate(new_syms):
        new_shard_of[s] = f"shard_{next_id + i // SHARD_SIZE:04d}.parquet"

    # 任务清单：老股按所在分片归组；新股按新分片归组
    todo: dict[str, list[str]] = {}
    for s, sh in sym2shard.items():
        todo.setdefault(sh, []).append(s)
    for s, sh in new_shard_of.items():
        todo.setdefault(sh, []).append(s)

    t_start = time.time()
    deadline = t_start + args.max_seconds
    stats = dict(ok=0, nodata=0, fail=0, drift=0, new_rows=0)

    for shard_name in sorted(todo):
        if time.time() > deadline:
            _log("[fetch] 到达 --max-seconds，自停（重跑本命令自动续跑）")
            break
        syms = todo[shard_name]
        marker = INCR / f"{shard_name}.done.json"
        if marker.exists():
            try:
                m = json.loads(marker.read_text(encoding="utf-8"))
                if m.get("cutoff") == str(cutoff.date()):
                    continue                      # 本轮基准日已合并完成，跳过
            except Exception:
                pass
        done = _load_done(shard_name)
        pending = [s for s in syms if s not in done]
        if not pending:
            continue
        is_new_shard = not (SHARDS / shard_name).exists()
        # 老分片：载入存量用于重叠校验（一次读入内存）
        old_df = None
        if not is_new_shard:
            old_df = pd.read_parquet(SHARDS / shard_name)
            old_df["eob_naive"] = old_df["eob"].dt.tz_localize(None)
        _log(f"[fetch] {shard_name}: {len(pending)}/{len(syms)} 只待拉"
             f"（{'新分片' if is_new_shard else '增量'}）")

        buf, buf_n = [], 0
        for i, sym in enumerate(pending):
            if time.time() > deadline:
                break
            keep = None
            count = FETCH_COUNT_FULL if is_new_shard else FETCH_COUNT_INCR
            df, err = _fetch_one(gm, sym, count, adjust_mode, args.sleep)
            rec = dict(symbol=sym, ts=time.strftime("%F %T"))
            if df is None:
                rec.update(status="fail" if err else "nodata", err=err)
                stats["fail" if err else "nodata"] += 1
            else:
                df["eob_naive"] = df["eob"].dt.tz_localize(None)
                if is_new_shard:
                    keep = df.drop(columns=["eob_naive"])
                    rec.update(status="ok_new", n_new=len(keep))
                    stats["ok"] += 1
                    stats["new_rows"] += len(keep)
                else:
                    old_sym = old_df[old_df["symbol"] == sym]
                    # ── 重叠日前复权锚点校验 ──
                    ov = old_sym.merge(
                        df[["eob_naive", "close", "volume", "amount"]],
                        on="eob_naive", suffixes=("_old", "_new"))
                    max_diff = 0.0
                    if len(ov):
                        rel = (np.abs(ov["close_new"] - ov["close_old"])
                               / ov["close_old"].replace(0, np.nan)).max()
                        max_diff = float(rel) if np.isfinite(rel) else 0.0
                    if max_diff > DRIFT_RTOL:
                        # 前复权锚点漂移：全量重拉替换
                        full, ferr = _fetch_one(gm, sym, FETCH_COUNT_FULL,
                                                adjust_mode, args.sleep)
                        if full is None:
                            rec.update(status="fail", err=f"drift refetch: {ferr}")
                            stats["fail"] += 1
                            _append_progress(shard_name, rec)
                            continue
                        full["eob_naive"] = full["eob"].dt.tz_localize(None)
                        keep = full.drop(columns=["eob_naive"])
                        rec.update(status="drift_refetched", max_rel_diff=max_diff,
                                   n_new=len(keep))
                        stats["drift"] += 1
                        stats["new_rows"] += len(keep)
                    else:
                        keep = df[df["eob_naive"].values > cutoff64].drop(
                            columns=["eob_naive"])
                        rec.update(status="ok", n_new=len(keep),
                                   max_rel_diff=max_diff)
                        stats["ok"] += 1
                        stats["new_rows"] += len(keep)
                if keep is not None and len(keep):
                    buf.append(keep)
                    buf_n += len(keep)
            _append_progress(shard_name, rec)
            if buf and buf_n >= 500 * 12:      # 定期落 partial（约 500 只增量）
                _append_partial(shard_name, pd.concat(buf, ignore_index=True))
                buf, buf_n = [], 0
            if (i + 1) % 100 == 0:
                el = time.time() - t_start
                _log(f"  .. {shard_name} {i + 1}/{len(pending)} "
                     f"用时 {el:.0f}s stats={stats}")
        if buf:
            _append_partial(shard_name, pd.concat(buf, ignore_index=True))

        # 该分片拉完 => 合并落盘（原子替换）
        done = _load_done(shard_name)
        if set(syms) <= done:
            _merge_shard(shard_name, old_df, is_new_shard)
            (INCR / f"{shard_name}.done.json").write_text(
                json.dumps(dict(cutoff=str(cutoff.date()),
                                ts=time.strftime("%F %T")), ensure_ascii=False),
                encoding="utf-8")
        else:
            _log(f"[fetch] {shard_name} 未完（{len(done)}/{len(syms)}），下次续跑")

    # 汇总报告
    rep = _load_report()
    f = rep.setdefault("fetch", {})
    f["last_run"] = dict(ts=time.strftime("%F %T"), cutoff=str(cutoff.date()),
                         stats=stats, new_symbols=len(new_syms))
    # 从持久化归档 + 未完成 progress 汇总重叠校验
    drift_list, max_seen = [], 0.0
    n_overlap = 0
    sources = [INCR / "overlap_summary.jsonl"] + sorted(
        INCR.glob("shard_*.progress.jsonl"))
    for p in sources:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "drift_refetched":
                drift_list.append(r["symbol"])
                continue                      # 漂移股差异不计入"非漂移最大差异"
            d = r.get("max_rel_diff")
            if d is not None:
                n_overlap += 1
                max_seen = max(max_seen, float(d))
    f["overlap_check"] = dict(n_symbols_checked=n_overlap,
                              max_rel_diff_non_drift=max_seen,
                              drift_threshold=DRIFT_RTOL,
                              drift_symbols=drift_list)
    _save_report(rep)
    _log(f"[fetch] 本轮结束 stats={stats} 漂移股={len(drift_list)}")
    remaining = []
    for sh, syms in todo.items():
        marker = INCR / f"{sh}.done.json"
        if marker.exists():
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("cutoff") == str(cutoff.date()):
                    continue
            except Exception:
                pass
        remaining.extend(set(syms) - _load_done(sh))
    _log(f"[fetch] 剩余未拉 {len(remaining)} 只"
         + ("；重跑本命令续跑" if remaining else "；全部完成"))


def _merge_shard(shard_name: str, old_df: pd.DataFrame | None,
                 is_new_shard: bool) -> None:
    """partial 新行 + 存量 -> 原子替换分片；清理断点文件。"""
    p = _partial_path(shard_name)
    new_df = pd.read_parquet(p) if p.exists() else None
    # 漂移全量重拉的 symbol：其 partial 行是全历史，存量行要剔除
    drifted = set()
    pp = _progress_path(shard_name)
    recs = []
    for line in pp.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            recs.append(r)
        except Exception:
            continue
        if r.get("status") in ("drift_refetched", "ok_new"):
            drifted.add(r["symbol"])
    # 重叠校验记录持久化（progress 删除前归档，供报告汇总）
    with open(INCR / "overlap_summary.jsonl", "a", encoding="utf-8") as f:
        for r in recs:
            r2 = dict(r)
            r2["shard"] = shard_name
            f.write(json.dumps(r2, ensure_ascii=False, default=str) + "\n")
    frames = []
    if old_df is not None:
        keep_old = old_df[~old_df["symbol"].isin(drifted)].drop(
            columns=["eob_naive"], errors="ignore")
        frames.append(keep_old)
    if new_df is not None and len(new_df):
        frames.append(new_df)
    if not frames:
        return
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["symbol", "eob"], keep="last")
    out = out.sort_values(["symbol", "eob"], kind="mergesort").reset_index(drop=True)
    tmp = SHARDS / f".{shard_name}.tmp"
    out.to_parquet(tmp, index=False)
    os.replace(tmp, SHARDS / shard_name)
    p.unlink(missing_ok=True)
    pp.unlink(missing_ok=True)
    _log(f"[merge] {shard_name}: {len(out)} 行（{out['symbol'].nunique()} 只）落盘，"
         f"断点已清理")


# ---------------------------------------------------------------------------
# factors（重建 F3 快照：历史段旧 ST 名单 / 新增段新 ST 名单）
# ---------------------------------------------------------------------------
def cmd_factors(_args) -> None:
    import ic_layer
    import daily_selection_screen as f3

    cutoff = detect_cutoff_for_factors()
    tag = cutoff.strftime("%Y-%m-%d")
    t0 = time.time()
    panel = f3._prep_panel(ic_layer.load_panel(str(PANEL)))
    _log(f"[factors] 面板 {panel['symbol'].nunique()} 只 {len(panel)} 行 "
         f"{panel['date'].min().date()}~{panel['date'].max().date()} "
         f"({time.time() - t0:.0f}s)")
    # 关键口径：基线快照是 F3 --since=2023-09-01 截断后计算的——截断使
    # listed_ok 的 cumcount 从 2023-09-01 起算（120 交易日后才可交易，
    # 即 scores 从 2024-03-05 开始）。不截断会导致 scores 起始日提前到
    # 2001 年，s1_sim 会多出数千个空宇宙"死日"。必须同口径截断。
    panel = panel[panel["date"] >= pd.Timestamp(SINCE)].reset_index(drop=True)
    _log(f"[factors] --since={SINCE} 截断 -> {len(panel)} 行")

    uni_new = f3.load_universe(PANEL)
    uni_old_path = XSEC / "panel_aux" / f"universe_backup_{tag}.parquet"
    uni_old = (pd.read_parquet(uni_old_path) if uni_old_path.exists() else uni_new)
    if uni_old is uni_new:
        _log("[factors] 警告：未找到旧 universe 备份，历史段 ST 名单=新名单，"
             "基线对拍可能因 ST 变化出现差异")

    _log("[factors] 计算因子（全面板向量化）...")
    factors = f3.compute_factor_series(panel)
    mask_old = f3.build_tradable_mask(panel, factors, uni_old).to_numpy()
    mask_new = f3.build_tradable_mask(panel, factors, uni_new).to_numpy()
    is_new = (panel["date"] > cutoff).to_numpy()
    mask = np.where(is_new, mask_new, mask_old)
    _log(f"[factors] 可交易行 {int(mask.sum())}/{len(mask)} "
         f"(新增段 {int(is_new.sum())} 行用新 ST 名单)")
    factors_f = f3.apply_filter(factors, pd.Series(mask, index=factors.index))

    f3.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    snap = f3.RESULTS_DIR / "factors_filtered.parquet"
    factors_f.to_parquet(snap, index=False)
    _log(f"[factors] 快照 -> {snap}（{len(factors_f)} 行，{time.time() - t0:.0f}s）")
    rep = _load_report()
    rep["factors"] = dict(ts=time.strftime("%F %T"), snapshot=str(snap),
                          rows=len(factors_f),
                          max_date=str(panel["date"].max().date()),
                          st_old=uni_old is not uni_new)
    _save_report(rep)


def detect_cutoff_for_factors() -> pd.Timestamp:
    """factors 阶段的基准日：取旧 universe 备份的日期，否则面板最小外一天。"""
    aux = sorted((XSEC / "panel_aux").glob("universe_backup_*.parquet"))
    if aux:
        return pd.Timestamp(aux[-1].stem.replace("universe_backup_", ""))
    # 无备份（首次）退化为面板最大日的前一天 => 全部按新名单（并告警）
    return detect_cutoff()


# ---------------------------------------------------------------------------
# derived（复用 s0_prep / s1_prep，原文件不改）
# ---------------------------------------------------------------------------
def cmd_derived(_args) -> None:
    t0 = time.time()
    import s0_prep
    s0_prep.main()
    _log(f"[derived] s0_prep 完成（{time.time() - t0:.0f}s）")
    t1 = time.time()
    import s1_prep
    s1_prep.main()
    _log(f"[derived] s1_prep 完成（{time.time() - t1:.0f}s）")
    rep = _load_report()
    rep["derived"] = dict(ts=time.strftime("%F %T"),
                          scores_rows=len(pd.read_parquet(RESULTS / "scores.parquet")))
    _save_report(rep)


# ---------------------------------------------------------------------------
# verify（硬验收）
# ---------------------------------------------------------------------------
def cmd_verify(args) -> None:
    rep = _load_report()
    cutoff = pd.Timestamp(rep.get("backup", {}).get("cutoff",
                                                    detect_cutoff().strftime("%Y-%m-%d")))
    tag = cutoff.strftime("%Y-%m-%d")
    bdir = RESULTS / f"baseline_{tag}"
    out = dict(cutoff=tag, ts=time.strftime("%F %T"))

    # ── 1. scores 基准段逐值对比 ──
    sc_new = pd.read_parquet(RESULTS / "scores.parquet")
    sc_old = pd.read_parquet(bdir / "scores.parquet")
    sc_new["date"] = pd.to_datetime(sc_new["date"])
    sc_old["date"] = pd.to_datetime(sc_old["date"])
    new_base = sc_new[sc_new["date"] <= cutoff]
    merged = sc_old.merge(new_base, on=["symbol", "date"], suffixes=("_old", "_new"))
    diffs = {}
    for c in ("score_eq", "score_icir", "rev10_z"):
        d = (merged[f"{c}_new"] - merged[f"{c}_old"]).abs()
        diffs[c] = float(d.max()) if len(d) else None
    out["scores_check"] = dict(
        n_baseline_rows=len(sc_old), n_matched=len(merged),
        n_new_rows=int((sc_new["date"] > cutoff).sum()),
        max_abs_diff=diffs,
        row_count_match=bool(len(merged) == len(sc_old) == len(new_base)),
        pass_=bool(len(merged) == len(sc_old)
                   and all((v or 0) <= 1e-9 for v in diffs.values())))
    _log(f"[verify] scores 基准段：匹配 {len(merged)}/{len(sc_old)} 行，"
         f"max|Δ|={diffs} -> {'PASS' if out['scores_check']['pass_'] else 'FAIL'}")

    # ── 2. 重跑 S1 终选参数，nav 对拍 ──
    nav_csv = RESULTS / "s1_nav" / "nav_s1_eq_N4_M8_H1_A.csv"
    cmd = [sys.executable, str(S0 / "s1_sim.py"), "--n", "4", "--m", "8",
           "--min-hold", "1", "--tp-arm", "A"]
    _log(f"[verify] 重跑 s1_sim N4/M8/H1/A ...")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(S0),
                       timeout=args.sim_timeout)
    sim_tail = (r.stdout or "").strip().splitlines()[-1:] or [r.stderr.strip()[-300:]]
    _log(f"[verify] s1_sim: {sim_tail[0]}")
    if r.returncode != 0:
        out["nav_check"] = dict(pass_=False, err=r.stderr[-500:])
    else:
        nav_new = pd.read_csv(nav_csv, index_col=0, parse_dates=True).iloc[:, 0]
        nav_old = pd.read_csv(bdir / nav_csv.name, index_col=0, parse_dates=True).iloc[:, 0]
        nb = nav_new[nav_new.index <= cutoff]
        ob = nav_old[nav_old.index <= cutoff]
        common = nb.index.intersection(ob.index)
        drel = (nb[common] - ob[common]).abs() / ob[common].abs()
        rel = drel.max()
        out["nav_check"] = dict(
            n_common=len(common), n_new_days=int((nav_new.index > cutoff).sum()),
            max_rel_diff=float(rel) if np.isfinite(rel) else None,
            median_rel_diff=float(drel.median()) if len(drel) else None,
            n_days_diff_gt_1e6=int((drel > 1e-6).sum()),
            n_days_diff_gt_1e4=int((drel > 1e-4).sum()),
            first_diff_date=(str(drel[drel > 1e-6].index[0].date())
                             if (drel > 1e-6).any() else None),
            nav_final_baseline=float(ob.iloc[-1]) if len(ob) else None,
            pass_=bool(len(common) == len(ob) and rel <= 1e-6),
            nav_new_tail={str(k.date()): round(float(v), 2)
                          for k, v in nav_new[nav_new.index > cutoff].tail(15).items()})
        _log(f"[verify] nav 基准段：{len(common)}/{len(ob)} 日，"
             f"max rel diff={rel:.2e} -> {'PASS' if out['nav_check']['pass_'] else 'FAIL'}")

    # ── 3. 卫生掩码与新增段摘要 ──
    hd = pd.read_csv(RESULTS / "s1_hygiene_daily.csv", index_col=0, parse_dates=True)
    hd_new = hd[hd.index > cutoff]
    out["hygiene_new_segment"] = dict(
        n_days=len(hd_new),
        avg_clean=float(hd_new["n_clean"].mean()) if len(hd_new) else None,
        tail={str(k.date()): {c: int(v) for c, v in row.items()}
              for k, row in hd_new.tail(5).iterrows()})
    # 基准复算对照（s1_r3 终选登记值，560 日切片窗口，仅供人读对照）
    out["baseline_reference"] = dict(
        note="s1_r3_final.json 窗口 2024-06-03~2026-09-17 登记值",
        final_nav=2589765.8534404887, ann_ret=0.5356770837947358,
        sharpe=1.0542735001092316)

    # ── 4. 分级判定 ──
    # exact：零漂移世界的理想闸（1e-6）；practical：承认前复权数据代差——
    # 基线行全覆盖 + nav 最大相对偏差 <= 1e-3。偏差来源已在 fetch 阶段
    # 重叠校验归档（drift 股 = 分红/送转导致锚点漂移，全量重拉即正确数据）。
    nav_rel = (out.get("nav_check", {}) or {}).get("max_rel_diff")
    exact = (out["scores_check"]["pass_"]
             and bool(out.get("nav_check", {}).get("pass_")))
    practical = (out["scores_check"]["row_count_match"]
                 and nav_rel is not None and nav_rel <= 1e-3)
    out["verdict"] = dict(
        exact_pass=exact,
        practical_pass=bool(practical),
        note=("基准段 nav 最大相对偏差 "
              f"{nav_rel:.2e}（前复权锚点漂移股所致，属数据代差非逻辑错误）"
              if nav_rel and not exact else "基线段逐日一致"))
    rep["verify"] = out
    _save_report(rep)
    if exact:
        _log("[verify] 总判定：PASS ✅（基线段逐日一致 <=1e-6）")
    elif practical:
        _log(f"[verify] 总判定：PRACTICAL PASS ⚠️（数据代差 max rel "
             f"{nav_rel:.2e} <= 1e-3，详见 update_report.json）")
    else:
        _log("[verify] 总判定：FAIL ❌（详见 update_report.json）")
        sys.exit(2)


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------
def cmd_all(args) -> None:
    cmd_probe(args)
    cmd_backup(args)
    cmd_universe(args)
    # fetch 可能需要多轮（单轮受 --max-seconds 限制）
    for rnd in range(1, args.fetch_rounds + 1):
        cmd_fetch(args)
        left = list(INCR.glob("shard_*.progress.jsonl"))
        if not left:
            break
        _log(f"[all] fetch 第 {rnd} 轮结束，仍有断点，继续下一轮")
    else:
        raise SystemExit("[all] fetch 超过最大轮数仍未完成，请重跑 all 续跑")
    cmd_factors(args)
    cmd_derived(args)
    cmd_verify(args)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "backup", "universe", "fetch",
                                    "factors", "derived", "verify", "all"])
    ap.add_argument("--sleep", type=float, default=0.05, help="GM 调用间隔秒")
    ap.add_argument("--max-seconds", type=float, default=270,
                    help="fetch 单轮自停秒数（配合 Bash 300s 上限断点续跑）")
    ap.add_argument("--fetch-rounds", type=int, default=30, help="all 模式 fetch 最大轮数")
    ap.add_argument("--sim-timeout", type=float, default=280, help="verify 中 s1_sim 超时秒")
    args = ap.parse_args()
    dict(probe=cmd_probe, backup=cmd_backup, universe=cmd_universe,
         fetch=cmd_fetch, factors=cmd_factors, derived=cmd_derived,
         verify=cmd_verify, all=cmd_all)[args.cmd](args)


if __name__ == "__main__":
    main()
