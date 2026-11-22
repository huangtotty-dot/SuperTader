# -*- coding: utf-8 -*-
"""凡拓数创（301313）补挂「物理AI」分类（2026-11-21 owner 确认）。

依据：
  · 11-20 韭研复盘把它列在「机器人」段，涨停关键词就是「物理AI（数据采集）+机器人+AI-3D」；
  · 公司 2026 半年报口径：3D可视化 → 数字孪生 → **物理AI** 三层递进，FunSim 具身数据仿真平台、
    空间/仿真交互/无本体/本体四类数据、与北大深研院共建具身数采训练实验室。
  结论：属于物理AI 的「数据采集」环节（与风语筑同一标签）。

口径：只增不改（已有 数据要素 分类保持原样），改前自动备份。
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

from daily_recap_ingest import get_pairs, put_pairs  # noqa: E402

WL = BASE / "stock_hunter" / "watchlist_concept.json"
TAG = ("301313", "物理AI", "物理AI_数据采集")


def main():
    code, cat, con = TAG
    wl = json.loads(WL.read_text(encoding="utf-8"))
    e = wl.get(code)
    if not isinstance(e, dict):
        raise SystemExit(f"{code} 不在 watchlist")
    pairs = get_pairs(e)
    if any(c == cat for c, _ in pairs):
        print(f"已有 {cat} 分类，按只增不改跳过：{pairs}")
        return
    bak = BASE / "backup" / f"watchlist_concept_before_physics_ai_{datetime.now():%Y%m%d_%H%M%S}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WL, bak)
    put_pairs(e, pairs + [(cat, con)])
    wl["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{code} {e.get('name')}：追加 {cat}/「{con}」；现分类 {get_pairs(e)}")
    print(f"[backup] {bak}")


if __name__ == "__main__":
    main()
