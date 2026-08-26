# -*- coding: utf-8 -*-
"""
auction_collector.py — 集合竞价个股快照采集器（数据层，非策略参数；方案 doc/集合竞价信息方案_20260806.md P0）

两模式：
  --slot HH:MM                    竞价时段实时快照（09:20/09:22/09:25 由外部调度触发）
                                  数据源：腾讯 qt.gtimg.cn 实时快照（竞价时段 [3]=虚拟匹配价、[6]=竞价累计量，
                                  字段行为待 2026-08-06 竞价时段实测确认，见方案 §数据审计）
  --backfill --date YYYY-MM-DD    盘后回填 9:25 替代口径：开盘价（腾讯快照"今开"）+ 竞价撮合量
                                  （腾讯分钟接口 09:30 首根量/额）+ 昨日总量（腾讯 qfq 日线）算占比

落盘：t_io/preopen/auction_YYYY-MM-DD.json
  { date, codes, snapshots: { "09:20": {ts, source, rows:{code:{...}}}, ... }, gaps: [...] }

本机网络口径：东财/akshare 在复盘环境 SSL 不可达；腾讯接口直连可用（NO_PROXY 防御）。
"""
import argparse, json, os, sys, time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]  # C17-1 修复(2026-08-18): 自解析到项目根（t_io/preopen/ → 根），替代旧目录硬编码 E:\06_T
PREOPEN_DIR = BASE / "t_io" / "preopen"
HOLDINGS_FP = BASE / "t_io" / "state" / "holdings.json"

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

UA = {"User-Agent": "Mozilla/5.0"}

# 大盘指数竞价采集（方案 §6 Phase1：sh000001 上证 / sh000688 科创50 / sz399001 深证成指）
AUCTION_INDEX_CODES = ["sh000001", "sh000688", "sz399001"]


def fetch_index_snapshot():
    """腾讯 qt.gtimg.cn 指数竞价快照 → {code: {name, auction_price, pre_close, gap_pct}}
    竞价时段字段[3]=虚拟匹配价，字段[4]=昨收；与 fetch_qt_snapshot 同源同口径。"""
    q = ",".join(AUCTION_INDEX_CODES)
    req = urllib.request.Request(f"http://qt.gtimg.cn/q={q}", headers=UA)
    txt = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
    out = {}
    for part in txt.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, payload = part.partition("=")
        code = key.strip().lstrip("v_").lower()
        f = payload.strip().strip('"').split("~")
        if len(f) < 5:
            continue

        def _f(i):
            try:
                return float(f[i])
            except Exception:
                return None

        price, pc = _f(3), _f(4)
        out[code] = {
            "name": (f[1] if len(f) > 1 and f[1] else code),
            "auction_price": price,
            "pre_close": pc,
            "gap_pct": round((price - pc) / pc * 100, 2) if price and pc else None,
        }
    return out


def mkt_code(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def fetch_qt_snapshot(codes):
    """腾讯 qt.gtimg.cn 批量实时快照 → {code: {name, price, pre_close, open, vol_hand, ts_raw}}"""
    q = ",".join(mkt_code(c) for c in codes)
    req = urllib.request.Request(f"http://qt.gtimg.cn/q={q}", headers=UA)
    txt = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
    out = {}
    for part in txt.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, payload = part.partition("=")
        code = key.strip().lstrip("v_").upper()[2:]
        f = payload.strip().strip('"').split("~")
        if len(f) < 39:
            continue
        def _f(i):
            try:
                return float(f[i])
            except Exception:
                return None
        out[code] = {
            "name": f[1],
            "price": _f(3),          # 竞价时段=虚拟匹配价（待实测）；连续竞价=现价
            "pre_close": _f(4),
            "open": _f(5) or None,   # 今开（9:25 撮合后有效）
            "vol_hand": _f(6),       # 竞价时段=竞价累计量（待实测）；盘后=全天总量
            "amount_wan": _f(37),    # 成交额（万）
            "ts_raw": f[30] if len(f) > 30 else "",
        }
    return out


def fetch_minute_first_bar(code):
    """腾讯分钟接口 09:30 首根（含 9:25 竞价撮合量）→ (vol_hand, amount_yuan)"""
    url = f"http://web.ifzq.gtimg.cn/appstock/app/minute/query?code={mkt_code(code)}"
    try:
        req = urllib.request.Request(url, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        rows = d["data"][mkt_code(code)]["data"]
        if not rows:
            return None, None
        p = rows[0].split()
        return float(p[2]), float(p[3])
    except Exception:
        return None, None


def fetch_yday_vol(code):
    """腾讯 qfq 日线昨日总成交量（手）"""
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt_code(code)},day,,,10,qfq"
    try:
        req = urllib.request.Request(url, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        node = d["data"][mkt_code(code)]
        rows = node.get("qfqday") or node.get("day") or []
        if len(rows) >= 2:
            return float(rows[-2][5])
    except Exception:
        pass
    return None


def load_pool():
    h = json.loads(HOLDINGS_FP.read_text(encoding="utf-8"))
    pool = {}
    for c, v in h.items():
        clean = c.split("_")[0]          # 双账户条目（如 000988_B）归并到正代码
        if clean not in pool:
            pool[clean] = v.get("name", clean)
    return pool


def load_auction_file(date):
    fp = PREOPEN_DIR / f"auction_{date}.json"
    if fp.exists():
        data = json.loads(fp.read_text(encoding="utf-8"))
        data["codes"] = list(load_pool().keys())   # 双账户条目归并（去 000988_B 类）
        return data, fp
    return {"date": date, "codes": list(load_pool().keys()), "snapshots": {}, "gaps": []}, fp


def _pre_close_for(date, code, snap_row):
    """pre_close 口径：当日=腾讯快照昨收；历史日=t_io/state/holdings_{date}.json 的 pre_close（=前一交易日收盘）。"""
    if snap_row and snap_row.get("pre_close"):
        return snap_row["pre_close"]
    fp = BASE / "t_io" / "state" / f"holdings_{date}.json"
    if fp.exists():
        try:
            h = json.loads(fp.read_text(encoding="utf-8"))
            row = h.get(code) or {}
            pc = float(row.get("pre_close", 0) or 0)
            if pc > 0:
                return pc
        except Exception:
            pass
    return None


def save(fp, data):
    PREOPEN_DIR.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def do_slot(date, slot):
    pool = load_pool()
    snap = fetch_qt_snapshot(list(pool.keys()))
    data, fp = load_auction_file(date)
    rows = {}
    for code, s in snap.items():
        pc = s["pre_close"] or 0
        price = s["price"]
        pct = round((price - pc) / pc * 100, 2) if pc and price else None
        rows[code] = {
            "name": s["name"] or pool.get(code, code),
            "auction_price": price, "pre_close": pc, "pct_vs_preclose": pct,
            "auction_vol_hand": s["vol_hand"], "amount_wan": s["amount_wan"],
            "src_ts": s["ts_raw"],
        }
    # 大盘指数竞价快照（供 auction_analyzer 指数分析）
    index_rows = {}
    try:
        index_rows = fetch_index_snapshot()
    except Exception as e:
        print(f"[slot {slot}] index snapshot failed: {e}")
    data["snapshots"][slot] = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "source": "qt.gtimg.cn realtime", "rows": rows,
                               "index_rows": index_rows}
    save(fp, data)
    print(f"[slot {slot}] {len(rows)} codes, {len(index_rows)} indexes -> {fp}")


def _local_minute_bars(code, date):
    """本地分钟快照（t_io/minute_snapshots/YYYY/MM/{code}_{date}.json）bars → [(hhmm,open,close,high,low,volume,amount),...]
    09:30 首根 OHLC=真实开盘价、量/额含 9:25 竞价撮合（已与腾讯今开/分钟接口交叉验证一致）。"""
    y, m = date[:4], date[5:7]
    fp = BASE / "t_io" / "minute_snapshots" / y / m / f"{code}_{date}.json"
    if not fp.exists():
        return []
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return []
    bars = []
    for b in d.get("bars") or []:
        try:
            bars.append((b["time"][11:16], float(b["open"]), float(b["close"]), float(b["high"]),
                         float(b["low"]), float(b["volume"]), float(b["amount"])))
        except Exception:
            continue
    return bars


def do_backfill(date):
    """盘后回填 9:25 替代口径。
    历史日：minute_snapshots 09:30 首根（open=开盘价，量/额≈竞价撮合）+ state/holdings 快照 pre_close。
    当日盘后：腾讯快照"今开" + 分钟接口 09:30 首根。
    昨日总量：腾讯 qfq 日线倒数第 2 根。"""
    pool = load_pool()
    today = datetime.now().strftime("%Y-%m-%d")
    snap = fetch_qt_snapshot(list(pool.keys())) if date == today else {}
    data, fp = load_auction_file(date)
    rows = {}
    for code, name in pool.items():
        open_px = vol = amt = None
        src = ""
        if date == today:
            s = snap.get(code, {})
            open_px = s.get("open")
            vol, amt = fetch_minute_first_bar(code)
            src = "qt快照今开 + 分钟09:30首根"
            if vol is None:  # 网络分钟接口静默失败 → 本地分钟快照兜底（08-06 实盘触发）
                bars = _local_minute_bars(code, date)
                if bars:
                    vol, amt = bars[0][5], bars[0][6]
                    src = "qt快照今开 + 本地分钟快照09:30首根(网络接口失败兜底)"
        else:
            bars = _local_minute_bars(code, date)
            if bars:
                b0 = bars[0]
                open_px = b0[1]                      # 09:30 首根 open = 9:25 撮合开盘价
                vol, amt = b0[5], b0[6]              # 首根量/额 ≈ 竞价撮合量/额
                src = "本地分钟快照: 09:30首根 open=开盘价, 量/额≈竞价撮合"
        pc = _pre_close_for(date, code, snap.get(code))
        yvol = fetch_yday_vol(code)
        rows[code] = {
            "name": name,
            "open_approx": open_px, "pre_close": pc,
            "pct_vs_preclose": round((open_px - pc) / pc * 100, 2) if pc and open_px else None,
            "auction_vol_hand_approx": vol,
            "auction_amount_approx": amt,
            "yday_total_vol_hand": yvol,
            "auction_vol_vs_yday": round(vol / yvol, 4) if vol and yvol else None,
        }
        time.sleep(0.2)
    data["snapshots"]["09:25"] = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"backfill: {src or '无数据源'}",
        "note": "9:20/9:22 轨迹无落盘，采集器上线后次日补齐",
        "rows": rows}
    for slot in ("09:20", "09:22"):
        if slot not in data["snapshots"] and slot not in data["gaps"]:
            data["gaps"].append(slot)
    save(fp, data)
    print(f"[backfill {date}] {len(rows)} codes -> {fp}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--slot", choices=["09:20", "09:22", "09:25"], help="竞价时段实时快照")
    p.add_argument("--backfill", action="store_true", help="盘后回填 9:25 替代口径")
    a = p.parse_args()
    if a.backfill:
        do_backfill(a.date)
    elif a.slot:
        do_slot(a.date, a.slot)
    else:
        p.print_help()


if __name__ == "__main__":
    sys.exit(main())
