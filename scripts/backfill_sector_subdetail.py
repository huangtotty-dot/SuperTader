# -*- coding: utf-8 -*-
"""给最近 N 个交易日的 `daily_summary.json` 补 **细分明细**（二级板块拆解）。

背景（owner 2026-11-08）：「板块星轨图里，大板块（如电力）排名上升时，要能看到各个
二级板块的贡献度」。拆解由 `data_loader.load_concept_summary()` 计算（挂在 DataFrame.attrs），
2026-11-08 起猎手每跑一次就会随当日汇总落盘；**历史日没有这个字段**，本脚本补。

做法（**不改其它字段**）：
  1. 备份 daily_summary.json；
  2. 逐日 `api._load_hunter_impl(date)`（与 `scripts/backfill_daily_summary.py` 同一条路径），
     它会按**当前池子**重算并落盘（含新的细分明细）；
  3. 只把落盘结果里的 `细分明细` 抠出来；
  4. **还原备份**，再把这些 `细分明细` 合并进原文件 ⇒ 排名/平均分/涨停数等历史数值一动不动。

⚠️ 两天内的增量对比（今日 vs 昨日）用的都是**按今天池子重算**的拆解，与历史汇总里的
排名可能有轻微口径差（池子后来加了题材）；星轨图的历史本来就是按当前池子重跑的（见
doc/research/2026-10-25_板块面板重跑.md）。

运行：`python scripts/backfill_sector_subdetail.py [N]`（默认 10 天）
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import gui  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
SUMMARY = BASE / "stock_hunter" / "history" / "daily_summary.json"
BACKUP = BASE / "backup"


def trading_dates(n: int) -> list[str]:
    idx = json.loads((BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json")
                     .read_text(encoding="utf-8")).get("rows", [])
    return [str(r["date"])[:10] for r in idx][-n:]


def main() -> int:
    dates = trading_dates(N)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BACKUP.mkdir(exist_ok=True)
    bak = BACKUP / f"daily_summary_before_subdetail_{stamp}.json"
    shutil.copy2(SUMMARY, bak)
    print(f"备份 {bak.name}；回填 {len(dates)} 天：{dates[0]} ~ {dates[-1]}")

    api = gui.Api()
    details: dict[str, dict] = {}
    for d in dates:
        t = time.time()
        try:
            api._load_hunter_impl(d)          # 会把当天汇总（含细分明细）落盘
            key = d.replace("-", "")
            # ⚠️ 2026-11-08：`_load_hunter_impl` 返回的 summary_rows 会**摘掉**细分明细
            #    （不随猎手大 payload 下发），所以这里从**刚写好的文件**里读。
            saved = json.loads(SUMMARY.read_text(encoding="utf-8")).get(key) or []
            details[key] = {str(x.get("板块")): (x.get("细分明细") or [])
                            for x in saved if isinstance(x, dict) and x.get("板块")}
            n_sub = sum(len(v) for v in details[key].values())
            print(f"  {d} {time.time()-t:5.1f}s rows={len(saved)} 细分条目={n_sub}", flush=True)
        except Exception as e:
            print(f"  {d} ERR {type(e).__name__}: {e}", flush=True)

    # 还原原文件，只合并 细分明细
    shutil.copy2(bak, SUMMARY)
    d = json.loads(SUMMARY.read_text(encoding="utf-8"))
    hit = miss = 0
    for day, board_map in details.items():
        for row in (d.get(day) or []):
            if not isinstance(row, dict):
                continue
            nm = str(row.get("板块") or "")
            sub = board_map.get(nm)
            if sub:
                row["细分明细"] = sub
                hit += 1
            else:
                miss += 1
    tmp = SUMMARY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SUMMARY)
    print(f"合并完成：{hit} 个(板块,日) 写入细分明细，{miss} 个无拆解（当天没跑到/新板块）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
