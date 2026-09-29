# -*- coding: utf-8 -*-
"""观测式买侧滑点测量（方案A，owner 2026-09-29 拍板）。

替代独立账户探针（预注册 doc/experiment/2026-09-24 修订案：国盛定制版终端
多 strategy_id 共享一个仿真资金账户，隔离不成立，探针废弃）。
改为从**生产引擎真实委托**挖掘 09:31 买侧滑点：

  样本 = 事件桥当日 09:30~09:40 窗口内的 BUY 委托×成交对（任何 order_type）
  滑点 = fill_vwap / 当日 09:31 首根 60s bar 的 open − 1（与预注册口径一致）
  参考系另记：vs 引擎委托参考价（order.price）、个股 gap、order_type

判读闸（沿用预注册）：OGR 条件子集（gap ≤ −1%）样本 ≥20 后，
  中位滑点 ≤ 0.35pp ⇒ A 态放行 OGR live；> 0.35pp ⇒ 优势不成立。

落盘：t_io/validation/slippage_obs/obs.jsonl（逐样本追加，幂等去重）

用法（**必须用 Python311**，gm SDK 只装在那里）：
  "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" scripts/slippage_obs.py [YYYY-MM-DD]
"""
import json
import os
import sys
from datetime import datetime

ROOT = r"E:\superTrader"
sys.path.insert(0, os.path.join(ROOT, "execution", "auto", "_gm"))
sys.stdout.reconfigure(encoding="utf-8")

WINDOW = ("09:30:00", "09:40:00")     # 观测窗口（对齐/OGRL4 都在 09:31 下单）
GATE_PP = 0.35                        # 预注册判读闸（pp）
MIN_SAMPLES = 20
OUT_DIR = os.path.join(ROOT, "t_io", "validation", "slippage_obs")
OBS_FP = os.path.join(OUT_DIR, "obs.jsonl")


def _load_events(date: str):
    fp = os.path.join(ROOT, "t_io", "bridge", f"events_{date.replace('-', '')}.jsonl")
    orders, fills = [], []
    if not os.path.exists(fp):
        return orders, fills
    for line in open(fp, encoding="utf-8"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = (e.get("time") or "")[11:19]
        if not (WINDOW[0] <= t <= WINDOW[1]):
            continue
        if e.get("event") == "order" and e.get("side") == "BUY":
            orders.append(e)
        elif e.get("event") == "fill" and e.get("side") == "BUY":
            fills.append(e)
    return orders, fills


def _bar_open_0931(code: str, date: str):
    """当日 09:31 首根 60s bar 的 open + 前收（掘金口径，前复权一致）。"""
    from gm.api import history_n, ADJUST_PREV
    sym = ("SHSE." if code[0] in "56" else "SZSE.") + code
    bars = history_n(symbol=sym, frequency="60s", count=40,
                     end_time=f"{date} 10:30:00",
                     fields="eob,open", adjust=ADJUST_PREV, df=True)
    o31 = 0.0
    if bars is not None and len(bars):
        for _, b in bars.iterrows():
            eob = str(b["eob"])
            if eob.endswith("09:31:00") or " 09:31" in eob:
                o31 = float(b["open"])
                break
        if o31 <= 0 and len(bars):          # 兜底：当日第一根
            o31 = float(bars.iloc[0]["open"])
    pc = 0.0
    his = history_n(symbol=sym, frequency="1d", count=3, end_time=f"{date} 09:00:00",
                    fields="eob,close", adjust=ADJUST_PREV, df=True)
    if his is not None and len(his):
        pc = float(his.iloc[-1]["close"])
    return o31, pc


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    orders, fills = _load_events(date)
    print(f"[obs] {date} 窗口 {WINDOW[0]}~{WINDOW[1]}：委托 {len(orders)} / 成交 {len(fills)}")
    if not fills:
        print("[obs] 无样本"); return

    from gm.api import set_token
    from utils.gm_token import load_token
    set_token(load_token())

    os.makedirs(OUT_DIR, exist_ok=True)
    done = set()
    if os.path.exists(OBS_FP):
        for line in open(OBS_FP, encoding="utf-8"):
            try:
                r = json.loads(line)
                done.add((r.get("date"), r.get("code"), r.get("qty"), r.get("fill_time")))
            except Exception:
                pass

    new = 0
    with open(OBS_FP, "a", encoding="utf-8") as f:
        for fl in fills:
            code, qty = fl.get("code"), fl.get("qty")
            ftime = fl.get("time", "")
            key = (date, code, qty, ftime)
            if key in done:
                continue
            # 委托配对：同 code+qty 最近一笔窗口内 BUY 委托
            od = next((o for o in reversed(orders)
                       if o.get("code") == code and o.get("qty") == qty), None)
            o31, pc = _bar_open_0931(code, date)
            fill_px = float(fl.get("fill_vwap") or fl.get("price") or 0)
            if o31 <= 0 or fill_px <= 0:
                print(f"[obs] {code} 缺价(o31={o31}, fill={fill_px})，跳过")
                continue
            gap = (o31 / pc - 1) if pc > 0 else None
            rec = {
                "date": date, "code": code, "qty": qty,
                "fill_time": ftime, "order_time": (od or {}).get("time"),
                "order_type": (od or {}).get("order_type"),
                "ref_px": (od or {}).get("price"),
                "fill_px": round(fill_px, 4),
                "open_0931": round(o31, 4), "prev_close": pc,
                "gap": round(gap, 6) if gap is not None else None,
                "slip_open_pp": round((fill_px / o31 - 1) * 100, 4),
                "slip_ref_pp": round((fill_px / float((od or {}).get("price") or 0) - 1) * 100, 4)
                                 if od and (od.get("price") or 0) > 0 else None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done.add(key)
            new += 1
            print(f"[obs] {code} x{qty} fill={fill_px:.4f} open0931={o31:.4f} "
                  f"滑点={rec['slip_open_pp']:+.3f}pp gap={rec['gap']}")

    # 汇总判读
    rows = [json.loads(l) for l in open(OBS_FP, encoding="utf-8") if l.strip()]
    all_slip = sorted(r["slip_open_pp"] for r in rows)
    ogr = sorted(r["slip_open_pp"] for r in rows if (r.get("gap") or 0) <= -0.01)
    def med(a): return a[len(a)//2] if len(a) % 2 else (a[len(a)//2-1]+a[len(a)//2])/2
    print(f"\n[obs] 累计样本 {len(rows)}（本日新增 {new}）")
    if all_slip:
        print(f"[obs] 全部买腿：中位滑点 {med(all_slip):+.3f}pp（闸 {GATE_PP}pp）")
    if ogr:
        ok = med(ogr) <= GATE_PP
        print(f"[obs] OGR 条件子集（gap≤-1%）n={len(ogr)}：中位 {med(ogr):+.3f}pp "
              f"→ {'A态可放行' if ok and len(ogr) >= MIN_SAMPLES else '继续积累' if len(ogr) < MIN_SAMPLES else '**超闸，优势不成立**'}")
    else:
        print("[obs] 暂无 gap≤-1% 的 OGR 条件样本")


if __name__ == "__main__":
    main()
