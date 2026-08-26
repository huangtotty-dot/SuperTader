# -*- coding: utf-8 -*-
"""
holdings_sync.py — holdings.json 收盘同步不变量（V1.1.3, 2026-08-06, 修复类非调优）

不变量：**同步只减不增 t_qty；t_qty 增加只能来自晨间截图 reconcile（人工）**。
纯底仓（t_qty=0，用户 2026-08-05 拍板 002639/603667）由此天然持久——任何 sync 不得复活它。

事故背景（2026-08-06，P0）：
- 旧 eod_sync 逻辑 `holding["t_qty"] = new_qty`（释放冻结）无条件执行，
  14:50:25 把 002639（0→200）/603667（0→100）的纯底仓标记冲掉；
- 14:50:44 扫描循环按复活的 t_qty=200 算出"建议交易100股/份"，
  14:50:45 002639 纯底仓误推 SELL_HIGH，且幻影卖出持久化进 t_io/virtual_trades.json。
- 附带修复读取口径：`holding.get("t_qty", 0) or old_qty` 把合法的 0 当假值回退，
  改为"键存在严格取值 / 键缺失才回退"。

本模块为纯函数，无重型依赖，供 main.py 收盘同步块与单测共同使用。
"""


def read_t_qty(holding: dict, fallback_qty: int) -> int:
    """t_qty 读取口径：键存在则严格取值（0 是纯底仓合法值，禁止 `or fallback` 回退）；
    键缺失才回退 fallback_qty（历史文件兼容）。None/非法值按 0 处理（宁缺毋滥，0=不交易最安全）。"""
    if "t_qty" in holding:
        try:
            v = holding["t_qty"]
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0
    return int(fallback_qty)


def sync_t_qty(old_t_qty: int, new_qty: int) -> int:
    """只减不增：new_t_qty = min(old_t_qty, new_qty)。
    - 虚拟卖出减仓：t_qty 跟随 qty 下降（与现网一致）；
    - 正T买入未卖出（qty 增加）：t_qty 不顶回，增加只能来自晨间 reconcile；
    - t_qty=0 纯底仓：恒保持 0，不得复活。"""
    return min(int(old_t_qty), int(new_qty))


def apply_eod_sync(holding: dict, unclosed_buy: int, unrebuilt: int):
    """收盘同步单股计算（纯函数，不修改传入 dict）。

    参数:
        holding: 持仓 dict（qty/base/t_qty ...）
        unclosed_buy: 正T买入未卖出股数
        unrebuilt: 反T/高抛卖出未接回股数
    返回:
        (new_qty, new_t_qty, new_base, delta, changed)
    """
    old_qty = int(holding.get("qty", 0) or 0)
    old_t_qty = read_t_qty(holding, old_qty)
    delta = int(unclosed_buy) - int(unrebuilt)
    new_qty = max(0, old_qty + delta)
    new_t_qty = sync_t_qty(old_t_qty, new_qty)
    changed = (delta != 0) or (old_t_qty != new_t_qty)
    return new_qty, new_t_qty, new_qty, delta, changed
