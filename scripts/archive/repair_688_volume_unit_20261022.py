# -*- coding: utf-8 -*-
"""修复科创板日线缓存里「股被当手」的成交量（2026-10-22）。

## 现象
owner 截图：国科军工 688543 日线**成交额**窗口里 10-20 那根冲到 **671 亿**（其余日子 3~8 亿）。
根因：盘中补 forming bar 的两条路径把腾讯快照的 `f[6]` 直接写进了 `volume`，而
**科创板(688/689) 的 f[6] 是「股」、其余是「手」**（memory: qt_quote_volume_unit_by_board）
⇒ 那根成交量 100 倍 ⇒ 下游 `_ensure_amount` 按「手」算成交额 ⇒ 100 倍。

全池扫描：**367 根**受影响，其中 **360 根集中在 2026-10-20**（那天整池 688 都被写坏）。

## 修法
按时间顺序逐根走，用**已修好的前 10 根**的中位量做基准；某根量是基准的 50~250 倍
⇒ 判定为「股当手」，÷100（四舍五入）。
⚠️ 必须是**滚动、用已修值**：像 688296 连着两天（10-20、10-21）都坏，用原始窗口会互相污染。

顺带删掉受影响票的 `chart_payload/*.json` —— payload 里存的是**建图时算好的 amount**，
不删的话图表还会回放旧的 671 亿。

用法：``python scripts/repair_688_volume_unit_20261022.py [--apply]``
"""
import json
import os
import shutil
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parents[1]
DAILY = BASE / "t_io" / "cache" / "daily_kline"
PAYLOAD = BASE / "t_io" / "cache" / "chart_payload"
BAK = BASE / "t_io" / "cache" / "daily_kline_bak_20261022_volunit"


def main():
    apply = "--apply" in sys.argv
    fixed = []
    payload_hit = set()
    for fp in sorted(DAILY.glob("*.json")):
        c = fp.stem
        if not (c.isdigit() and c.startswith(("688", "689"))):
            continue
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = rec.get("rows") or []
        if len(rows) < 12:
            continue
        vols, changed = [], []
        for r in rows:
            v = float(r.get("volume") or 0)
            if len(vols) >= 10:
                ref = statistics.median(vols[-10:])
                if ref > 0 and v > 0 and 50 <= (v / ref) <= 250:
                    v = round(v / 100.0, 0)
                    changed.append((str(r.get("date"))[:10], v))
            vols.append(v)
            r["volume"] = v
        if changed:
            fixed.append((c, len(changed)))
            if apply:
                BAK.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, BAK / fp.name)
                fp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
                payload_hit.add(c)

    print(f"受影响股票 {len(fixed)} 只、修正 {sum(n for _, n in fixed)} 根")
    for c, n in fixed[:10]:
        print(f"  {c}: {n} 根")

    if apply and payload_hit:
        removed = 0
        for c in payload_hit:
            p = PAYLOAD / f"{c}.json"
            if p.exists():
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
        print(f"已删 payload {removed} 份（让它们按修好的日线缓存重建）")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
    else:
        print(f"备份目录: {BAK}")


if __name__ == "__main__":
    main()
