# -*- coding: utf-8 -*-
"""
config/auto_pool.py — 自动盘标的池（P3-2 池分管：auto 侧候选池单一真源）

来源：原 goldminer main.py:33-71 硬编码 17 票 STOCKS/STOCK_NAMES，P3-2 迁入本模块。
标注 pool="auto"；manual 池 = watchlist_buy.json 中 pool="manual" 的标的。

消费方：
  · superTrader：core/position_builder.py（manual 扫描过滤）、main.py（启动池校验）、t_gui.py（池筛选）
  · goldminer：main.py（STOCKS/STOCK_NAMES 构建 + init 池校验）——经 SUPERTRADER_ROOT 定位本文件、
    以绝对路径 importlib 加载（goldminer 自身也有 config 包，不能用 `from config.auto_pool` 直接导入）。

注意：本模块保持轻量、无副作用（不 import 顶层 config.py 那套 requests/akshare），
仅依赖标准库；改名/挪位需同步 goldminer main.py 的加载路径。
"""
import json
import os

POOL = "auto"

# code(内部 6 位) → {name, gm_symbol}
AUTO_POOL = {
    "000988": {"name": "华工科技", "gm_symbol": "SZSE.000988"},
    "600481": {"name": "双良节能", "gm_symbol": "SHSE.600481"},
    "600176": {"name": "中国巨石", "gm_symbol": "SHSE.600176"},
    "603667": {"name": "五洲新春", "gm_symbol": "SHSE.603667"},
    "300054": {"name": "鼎龙股份", "gm_symbol": "SZSE.300054"},
    "300364": {"name": "中文在线", "gm_symbol": "SZSE.300364"},
    "002639": {"name": "雪人集团", "gm_symbol": "SZSE.002639"},
    "300153": {"name": "科泰电源", "gm_symbol": "SZSE.300153"},
    "300456": {"name": "赛微电子", "gm_symbol": "SZSE.300456"},
    "002202": {"name": "金风科技", "gm_symbol": "SZSE.002202"},
    "002536": {"name": "飞龙股份", "gm_symbol": "SZSE.002536"},
    "002176": {"name": "江特电机", "gm_symbol": "SZSE.002176"},
    "600584": {"name": "长电科技", "gm_symbol": "SHSE.600584"},
    "002261": {"name": "拓维信息", "gm_symbol": "SZSE.002261"},
    "600089": {"name": "特变电工", "gm_symbol": "SHSE.600089"},
    "002451": {"name": "摩恩电气", "gm_symbol": "SZSE.002451"},
    # WP-E4(2026-08-24 owner决策): 红利ETF 防守仓纳入做T体系
    "515180": {"name": "红利ETF", "gm_symbol": "SHSE.515180"},
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
