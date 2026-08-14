# -*- coding: utf-8 -*-
"""C7b reconcile 校验脚本（W33 立项 08-14，周六实施）

防呆：人工晨间截图 reconcile 写回 holdings.json 前，校验 t_qty 合法性，
防止 08-12 P0「误清做T股 t_qty → can_t 全池停摆」重演。

规则（周复盘 §1.3 C7b）——对每只 qty>0 的持仓股：
  1. 纯底仓（PURE_BASE_CODES 白名单，用户 2026-08-05 拍板不做T）：豁免 t_qty 校验；
     但仍断言 t_qty 恒 0（防 08-06 P0「复活纯底仓」）。
  2. 做T股：断言 t_qty > 0 且 t_qty >= qty * 0.5，否则 FAIL（拒写 + 报警）。

用法:
  python reconcile_guard.py [holdings.json 路径]
  - 缺省校验 t_io/state/holdings_{今天}.json，回退 holdings.json
  - exit 0 = 全过；exit 1 = 有 FAIL（人工应拒绝写回并排查）

背景（08-12 P0）:
  晨间 reconcile 误把做T股 t_qty 清零 → can_t（可做T额度）判定为否 → 做T停摆。
  C7a 铁律（清单 V1.9）: t_qty = 活动T仓额度，做T股严禁清零。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")

# 纯底仓白名单（用户 2026-08-05 拍板不做T；恢复 T 需重评估 base_pct 走管线）
PURE_BASE_CODES = {"002639", "603667"}


def validate_holdings(holdings: dict, pure_base_codes=None) -> list:
    """校验 holdings 的 t_qty 合法性。

    返回 FAIL 列表 [{code, qty, t_qty, rule, msg}]；空列表 = 全过。
    t_qty 键缺失（历史文件）按 qty 回退（与 holdings_sync.read_t_qty 口径一致）。
    """
    pure = set(pure_base_codes or PURE_BASE_CODES)
    fails = []
    for code, h in holdings.items():
        if not isinstance(h, dict):
            continue
        qty = int(h.get("qty", 0) or 0)
        if qty <= 0:
            continue  # 已清仓，无 t_qty 校验
        if "t_qty" in h:
            t_qty = int(h["t_qty"]) if h["t_qty"] is not None else 0
        else:
            t_qty = qty  # 历史文件缺键，按 qty 回退

        if code in pure:
            if t_qty != 0:
                fails.append({"code": code, "qty": qty, "t_qty": t_qty,
                              "rule": "纯底仓 t_qty 恒 0",
                              "msg": f"纯底仓 {code} 被设 t_qty={t_qty}（疑似 08-06 P0 复活）"})
            continue

        # 做T股
        if t_qty <= 0:
            fails.append({"code": code, "qty": qty, "t_qty": t_qty,
                          "rule": "t_qty > 0",
                          "msg": f"做T股 {code} t_qty={t_qty} 被清零（can_t 停摆风险）"})
        elif t_qty < qty * 0.5:
            fails.append({"code": code, "qty": qty, "t_qty": t_qty,
                          "rule": "t_qty >= qty*0.5",
                          "msg": f"做T股 {code} t_qty={t_qty} < qty*0.5={qty * 0.5:.1f}（半截清零）"})
    return fails


def _default_holdings_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    p = BASE / "t_io" / "state" / f"holdings_{today}.json"
    if p.exists():
        return p
    return BASE / "holdings.json"


def main():
    ap = argparse.ArgumentParser(description="C7b reconcile t_qty 校验（写回前防呆）")
    ap.add_argument("path", nargs="?", default=None, help="holdings.json 路径（缺省=今日快照/根 holdings.json）")
    args = ap.parse_args()

    fp = Path(args.path) if args.path else _default_holdings_path()
    if not fp.exists():
        print(f"[ERROR] 持仓文件不存在: {fp}")
        return 1
    holdings = json.loads(fp.read_text(encoding="utf-8"))

    fails = validate_holdings(holdings)
    n_pos = sum(1 for h in holdings.values() if isinstance(h, dict) and int(h.get("qty", 0) or 0) > 0)
    print(f"校验文件: {fp}")
    print(f"持仓股(含 qty>0): {n_pos} 只  |  纯底仓白名单: {sorted(PURE_BASE_CODES)}")

    if not fails:
        print("[PASS] t_qty 校验全过，可安全写回 holdings")
        return 0

    print(f"[FAIL] {len(fails)} 处 t_qty 非法，拒绝写回并排查：")
    for f in fails:
        print(f"  ✗ {f['code']}: qty={f['qty']} t_qty={f['t_qty']}  [{f['rule']}]  {f['msg']}")
    print("\n⚠️  请核对晨间截图 reconcile 记录，修正 t_qty 后重跑本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
