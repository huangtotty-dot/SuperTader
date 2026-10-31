# -*- coding: utf-8 -*-
"""盘后**日线缓存 ↔ 通达信本地数据**校准 CLI（2026-10-30）。

核心逻辑在 `core/tdx_calibrate.py`（GUI/main 也能直接调）；本文件只做参数解析与打印。

    python scripts/ops/tdx_calibrate_daily.py                 # 只报告
    python scripts/ops/tdx_calibrate_daily.py --fix           # 追加缺失尾部（安全门见 core）
    python scripts/ops/tdx_calibrate_daily.py --fix --codes 301079,000001
    python scripts/ops/tdx_calibrate_daily.py --tdx-home D:\\new_tdx

产物：`t_io/logs/tdx_calibrate_<YYYYmmdd_HHMM>.json`；退出码 2 = 找不到通达信目录。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_BASE = Path(__file__).resolve().parents[2]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from core import tdx_calibrate as tc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="追加缺失尾部（默认只报告）")
    ap.add_argument("--codes", default="", help="只处理这些代码（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只")
    ap.add_argument("--tdx-home", default=os.environ.get("TDX_HOME") or r"C:\new_tdx")
    a = ap.parse_args()

    codes = [c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None
    rep = tc.calibrate_all(codes=codes, fix=a.fix, tdx_home=Path(a.tdx_home), limit=a.limit)
    if rep.get("missing_home"):
        print(f"⛔ 找不到通达信数据目录：{rep['tdx_home']}\\vipdoc（用 --tdx-home 指定）")
        return 2

    print(f"通达信目录：{rep['tdx_home']}\\vipdoc")
    print(f"检查 {len(rep['results'])} 只 ｜ 状态 {rep['summary']}")
    if rep["behind"]:
        ex = rep["behind"][:8]
        print("落后示例：" + ", ".join(f"{r['code']}({r.get('cache_last')}→{r.get('tdx_last')})"
                                    for r in ex))
    if rep["ratio_bad"]:
        print(f"复权比率异常（跳过不修）：{len(rep['ratio_bad'])} 只，例 "
              + ", ".join(f"{r['code']}({r['status']})" for r in rep["ratio_bad"][:8]))
    if rep["short"]:
        s0 = rep["short"][0]
        print(f"历史偏短（通达信多 {s0.get('tdx_rows', 0) - s0.get('cache_rows', 0)}+ 行）："
              f"{len(rep['short'])} 只 —— 本工具只补尾部，历史需 provider 重拉")
    if rep.get("bak_dir"):
        print(f"备份 → {rep['bak_dir']}")

    log_dir = _BASE / "t_io" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fp = log_dir / f"tdx_calibrate_{datetime.now():%Y%m%d_%H%M}.json"
    fp.write_text(json.dumps({"tdx_home": rep["tdx_home"], "fix": rep["fix"],
                              "summary": rep["summary"], "results": rep["results"]},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告 → {fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
