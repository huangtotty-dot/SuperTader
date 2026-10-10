# -*- coding: utf-8 -*-
"""
弱转强研究线 · 全池数据铺数（GM 批量，两频段）
==============================================
GM 权限实测（2026-10-10）：**日线 1d 全历史可用**（无 180 天限制）；**分钟 1800s 仅最近
180 自然日**（起始不得早于 2026-04-13）。故拆两段：

  日线 1d   全窗口 2025-03-31 ~ 2026-10-09  → 算 MA20/prev_close/未来5日收益（信号前提）
  分钟 1800s 180天 2026-04-13 ~ 2026-10-09  → 盘中 10:00 强开盘确认（信号本身）

两段都前复权(ADJUST_PREV)、单位 volume=股/amount=元（与 tushare 面板一致）。
输出：
  t_io/validation/weak_strong/data/daily_raw_full.parquet   1d 长表(全窗口)
  t_io/validation/weak_strong/data/min30_raw_full.parquet   1800s 8棒/天(180天)
（再由 build_panel_full.py 合成 09:30 棒 + 派生 daily 的 prev_close/ma 列）

GM `history` 总行数≈200k/批 ⇒ 日线 500 只/批(370行×500≈18.5万)、分钟 180 只/批(960行×180≈17.3万)。
北交所(4/8/920) GM 一律返 0 行，直接剔除。断点续跑：落 _gm_chunks/{daily,min}/ + _gm_state.json。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = Path(r"E:\superTrader\t_io\validation\weak_strong\data")
WATCHLIST = ROOT / "stock_hunter" / "watchlist_jiuyan.json"


def universe_codes() -> list[str]:
    d = json.load(open(WATCHLIST, encoding="utf-8"))
    return [c for c in d if not (c.startswith("920") or c[0] in "48")]


def to_gm(c: str) -> str:
    return f"{'SHSE' if c[0] in '569' else 'SZSE'}.{c}"


def clean(df: pd.DataFrame, with_amount: bool) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    dt = pd.to_datetime(df["eob"])
    if getattr(dt.dt, "tz", None) is not None:
        dt = dt.dt.tz_localize(None)
    df["dt"] = dt
    df["date"] = dt.dt.strftime("%Y-%m-%d")
    df["symbol"] = df["symbol"].astype(str).str.split(".").str[-1]
    df["volume"] = df["volume"].astype(float)   # 股，不÷100
    cols = ["symbol", "dt", "date", "open", "high", "low", "close", "volume"]
    if with_amount:
        df["amount"] = df["amount"].astype(float)   # 元
        cols.append("amount")
    return df[cols].reset_index(drop=True)


def _history_timeout(gma, symbols, freq: str, start: str, end: str, fields: str,
                     timeout: int):
    """GM `history` 无超时参数，偶发连接停滞会永久阻塞。放 daemon 线程里跑 + join 超时。"""
    box: dict = {}

    def _run():
        try:
            box["df"] = gma.history(symbol=symbols, frequency=freq, start_time=start,
                                    end_time=end, fields=fields,
                                    adjust=gma.ADJUST_PREV, df=True)
        except Exception as e:  # noqa: BLE001
            box["err"] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "timeout"
    if "err" in box:
        return None, box["err"]
    return box.get("df"), None


def fetch_phase(gma, name: str, codes: list[str], freq: str, start: str, end: str,
                batch: int, with_amount: bool, sleep_between: float = 0.0,
                timeout: int = 150) -> pd.DataFrame:
    CHUNK_DIR = DATA_DIR / "_gm_chunks" / name
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FP = DATA_DIR / "_gm_state.json"
    state = json.load(open(STATE_FP, encoding="utf-8")) if STATE_FP.exists() else {}
    done = set(state.get(name, []))
    batches = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    fields = "symbol,eob,open,high,low,close,volume" + (",amount" if with_amount else "")
    failed: list[list[str]] = []
    t0 = time.time()
    for i, b in enumerate(batches):
        if i in done:
            continue
        df, err = None, "no_attempt"
        for attempt in range(3):
            df, err = _history_timeout(gma, [to_gm(c) for c in b], freq, start, end,
                                       fields, timeout=timeout)
            if err is None:
                break
            print(f"[warn] {name} {i}/{len(batches)} attempt{attempt}: {err[:140]}",
                  flush=True)
            time.sleep(2 ** attempt)
        if df is not None:
            df = clean(df, with_amount)
            if not df.empty:
                df.to_parquet(CHUNK_DIR / f"chunk_{i:04d}.parquet", index=False)
        else:
            failed.append(b)
        done.add(i)
        state[name] = sorted(done)
        json.dump(state, open(STATE_FP, "w", encoding="utf-8"))
        if (i + 1) % 5 == 0 or i == len(batches) - 1:
            print(f"[{name}] {i+1}/{len(batches)} rows_this={0 if df is None else len(df)} "
                  f"elapsed={time.time()-t0:.0f}s", flush=True)
        if sleep_between > 0 and i != len(batches) - 1:
            time.sleep(sleep_between)
    if failed:
        (DATA_DIR / "_gm_failed.json").write_text(
            json.dumps([c for b in failed for c in b], ensure_ascii=False), encoding="utf-8")
        print(f"[{name}] FAILED batches={len(failed)} symbols={sum(len(b) for b in failed)}",
              flush=True)
    frames = [pd.read_parquet(fp) for fp in sorted(CHUNK_DIR.glob("chunk_*.parquet"))]
    out = pd.concat(frames, ignore_index=True).sort_values(
        ["symbol", "dt"], kind="mergesort").reset_index(drop=True)
    return out


def main() -> None:
    from core.market_data.gm_token import load_token
    import gm.api as gma

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    codes = universe_codes()
    print(f"[universe] SH/SZ codes={len(codes)}", flush=True)
    gma.set_token(load_token())

    t0 = time.time()
    daily = fetch_phase(gma, "daily", codes, "1d",
                        "2025-03-31 09:00:00", "2026-10-09 16:00:00", 500, False)
    daily.to_parquet(DATA_DIR / "daily_raw_full.parquet", index=False)
    print(f"[daily] {len(daily)} 行, {daily['symbol'].nunique()} 只, "
          f"{daily['date'].min()}~{daily['date'].max()} ({time.time()-t0:.0f}s)", flush=True)

    min30 = fetch_phase(gma, "min", codes, "1800s",
                        "2026-04-13 09:00:00", "2026-10-09 16:00:00", 60, True,
                        sleep_between=12.0, timeout=150)
    min30.to_parquet(DATA_DIR / "min30_raw_full.parquet", index=False)
    print(f"[min30] {len(min30)} 行, {min30['symbol'].nunique()} 只, "
          f"{min30['date'].min()}~{min30['date'].max()} ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
