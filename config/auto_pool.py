# -*- coding: utf-8 -*-
"""
config/auto_pool.py — 自动盘标的池（P3-2 池分管 → 2026-08-30 单文件合并）

来源：原 goldminer main.py:33-71 硬编码 17 票 STOCKS/STOCK_NAMES，P3-2 迁入本模块；
2026-08-30 起改为从单一持仓真源 t_io/state/holdings.json 派生（pool ∈ {auto, both} →
{name, gm_symbol}），与手动链/回测共用同一文件，杜绝身份清单漂移。

消费方：
  · superTrader：core/position_builder.py（manual 扫描过滤）、main.py（启动池校验）、t_gui.py（池筛选）
  · goldminer：main.py（STOCKS/STOCK_NAMES 构建 + init 池校验）——经 SUPERTRADER_ROOT 定位本文件、
    以绝对路径 importlib 加载（goldminer 自身也有 config 包，不能用 `from config.auto_pool` 直接导入）。

注意：本模块保持轻量、无副作用（不 import 顶层 config.py 那套 requests/akshare，不 import core/src），
仅依赖标准库；改名/挪位需同步 goldminer main.py 的加载路径。

2026-09-11 方案A（手动/自动持仓分离）：MIRROR 目标底仓默认**镜像手动盘 holdings.json 的 base**
（缺失回退 qty），不再读 holdings.mirror_qty（deprecated 读兼容、不再写）。
按票可在 AUTO_POOL[code] 增加可选键 **mirror_qty** 覆盖镜像默认（缺省=镜像）。
"""
import json
import os

POOL = "auto"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_FILE = os.path.join(_ROOT, "t_io", "state", "holdings.json")


def _load_auto_pool() -> dict:
    """从 holdings.json 派生 auto 池身份（pool ∈ {auto, both} → {name, gm_symbol}）。读失败返回 {}。"""
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        code: {"name": h.get("name", code), "gm_symbol": h.get("gm_symbol", "")}
        for code, h in data.items()
        if isinstance(h, dict)
        and not str(code).startswith("_")
        and str(h.get("pool") or "") in ("auto", "both")
    }


# code(内部 6 位) → {name, gm_symbol}
AUTO_POOL = _load_auto_pool()

# 2026-09-11 方案A：auto 目标底仓（MIRROR）按票覆盖。缺省=镜像手动盘 holdings.base；
# 本表显式列出的 code 用表中值（含 0=不做底仓）。本次拆分按"保原 mirror 值"登记，行为中性；
# 后续 owner 想放开镜像，删除对应行即可。600481=100 为 2026-09-11 owner 裁决（实盘=100）。
AUTO_MIRROR_OVERRIDE = {
    "588170": 70000,
    "600481": 100,      # owner 裁决：实盘 100 股
    "002451": 1800,
    "000988": 500,
    "600176": 1600,
    "603667": 500,
    "300054": 100,
    "002639": 0,        # 原为纯观察，无底仓目标
    "300153": 0,
    "002396": 300,      # 原 mirror>0（当前 qty=0，保留原目标）
    "518880": 2000,
}


def auto_pool_codes() -> list:
    """auto 池全部内部 6 位码。"""
    return list(AUTO_POOL.keys())


def is_manual(code) -> bool:
    """是否属于 manual 池（auto 池之外）。_A/_B 账户后缀剥离后判断。"""
    base = str(code).split("_")[0]
    return base not in AUTO_POOL


def validate_pool_split(watchlist_path: str) -> list:
    """交集裁决：manual 池（watchlist_buy.json 中 pool=manual/缺省 manual）与 auto 池不得重叠。
    返回冲突代码列表；空=通过。启动时调用，冲突则拒绝启动并提示。"""
    conflicts = []
    if not os.path.exists(watchlist_path):
        return conflicts  # watchlist 缺失 → 无 manual 池，无冲突
    try:
        with open(watchlist_path, "r", encoding="utf-8") as f:
            wl = json.load(f)
    except Exception:
        return conflicts  # 读取失败不阻断（外层有启动错误处理），保守放行
    for code, info in (wl.get("stocks", {}) or {}).items():
        if not isinstance(info, dict) or str(code).startswith("_"):
            continue
        base = str(code).split("_")[0]
        pool = str(info.get("pool") or "manual")
        if pool == "manual" and base in AUTO_POOL:
            conflicts.append(base)
    return sorted(set(conflicts))


if __name__ == "__main__":
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    wl = root / "t_io" / "state" / "watchlist_buy.json"
    print(f"auto_pool codes ({len(AUTO_POOL)}): {auto_pool_codes()}")
    print(f"validate_pool_split({wl}) -> conflicts: {validate_pool_split(str(wl))}")
