# -*- coding: utf-8 -*-
"""把交割单台账（`t_io/state/trade_history_ledger.json`）落回账户状态（2026-11-02 owner）。

背景：owner 发了两份券商交割单（东方 / 东莞），要求「根据交割单重新更新账户信息」。
台账由 `scripts/build_trade_ledger.py` 生成；本脚本只做**状态落地**，不改台账。

落地内容：
  · `holdings_manual.json`：每只票的 qty / cost 取台账里该 **(账户, 代码)** 的未平仓股数与成本；
    同一代码出现在两个账户 → 第二条加账户后缀（repo 既有约定，如 `000988_B`）。
  · `accounts_config.json`：
      - 各账户 `holdings` 列表按台账重建；
      - `available`（可用资金）= **当前值（视作 `--cash-asof` 收盘快照）+ 台账里该日之后的净流水**；
      - `total_capital` = 该账户持仓市值（台账 last_price）+ 可用；
      - `realized_pnl` = 按代码、跨账户合计的**有符号**已实现盈亏（旧字段 realized_loss 保留不动）。
  · 写前备份到 `backup/`；`--dry-run` 只打印。

用法：
    python scripts/ops/apply_trade_ledger.py --cash-asof 2026-10-16          # 实写
    python scripts/ops/apply_trade_ledger.py --cash-asof 2026-10-16 --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parents[2]
LEDGER = BASE / "t_io" / "state" / "trade_history_ledger.json"
HOLDINGS = BASE / "t_io" / "state" / "holdings_manual.json"
ACCOUNTS = BASE / "t_io" / "state" / "accounts_config.json"
BACKUP = BASE / "backup"

# 台账账户名 → accounts_config 账户键（交割单文件按账户归属导入）
ACC_MAP = {"东方": "账户B", "东莞": "账户A"}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _gm_symbol(code: str) -> str:
    c = str(code)
    if c.startswith(("6", "5")):
        return "SHSE." + c
    if c.startswith(("4", "8", "92")):
        return "BJSE." + c
    return "SZSE." + c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash-asof", default="2026-10-16",
                    help="当前 available 视为哪一天收盘的快照（之后流水叠加）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    L = _load(LEDGER, {})
    if not L.get("stocks"):
        print("⛔ 台账为空（先跑 scripts/build_trade_ledger.py）")
        return 2
    H = _load(HOLDINGS, {})
    A = _load(ACCOUNTS, {})

    opens = [s for s in L["stocks"] if (s.get("open_qty") or 0) > 0]
    # 每账户的可用资金：现有 available + 台账里 asof 之后的净流水
    flow = {}
    for t in (L.get("trades") or []):
        if str(t.get("date") or "") > a.cash_asof:
            acc = ACC_MAP.get(str(t.get("account") or ""))
            if acc:
                flow[acc] = flow.get(acc, 0.0) + float(t.get("net") or 0)

    accounts = A.setdefault("accounts", {})
    new_hold = {}
    acc_hold_codes = {}
    acc_mv = {}
    seen_code = {}
    for s in sorted(opens, key=lambda x: (x.get("account"), x["code"])):
        code, qty = str(s["code"]), int(s["open_qty"])
        cost = round(float(s.get("cost_basis") or 0) / qty, 3) if qty else 0.0
        acc = ACC_MAP.get(str(s.get("account") or ""))
        key = code
        if code in seen_code:                      # 同一代码在另一账户
            key = f"{code}_{(acc or 'X')[-1]}"
        seen_code[code] = True
        old = H.get(key) or H.get(code) or {}
        ent = {
            "name": s.get("name") or old.get("name") or code,
            "gm_symbol": old.get("gm_symbol") or _gm_symbol(code),
            "type": old.get("type") or ("etf" if code.startswith(("5", "1")) else "stock"),
            "pool": old.get("pool") or "manual",
            "qty": qty,
            "cost": cost,
            "base": min(int(old.get("base") or 0), qty) if old.get("base") else 0,
            "pre_close": round(float(s.get("last_price") or 0), 3) or old.get("pre_close"),
        }
        new_hold[key] = ent
        if acc:
            acc_hold_codes.setdefault(acc, []).append(code)
            acc_mv[acc] = acc_mv.get(acc, 0.0) + qty * float(s.get("last_price") or 0)

    # 已实现（按代码跨账户合计，有符号）
    realized = {}
    for s in L["stocks"]:
        realized[str(s["code"])] = round(realized.get(str(s["code"]), 0.0)
                                         + float(s.get("realized") or 0), 2)

    print(f"台账：{L.get('range')}  未平仓 {len(opens)} 只 / {len(new_hold)} 条持仓记录")
    for acc, codes in sorted(acc_hold_codes.items()):
        old_av = float((accounts.get(acc) or {}).get("available") or 0)
        new_av = round(old_av + flow.get(acc, 0.0), 2)
        mv = round(acc_mv.get(acc, 0.0), 2)
        print(f"  {acc}: 持仓 {len(codes)} 只 市值 {mv:,.2f} 可用 {old_av:,.2f}"
              f" + 流水 {flow.get(acc, 0.0):,.2f} = {new_av:,.2f} ⇒ 总资产 {mv + new_av:,.2f}")
        if not a.dry_run and acc in accounts:
            accounts[acc]["holdings"] = codes
            accounts[acc]["available"] = new_av
            accounts[acc]["total_capital"] = round(mv + new_av, 2)
    print(f"  已实现（有符号，跨账户合计）：{sum(realized.values()):,.2f}")

    if a.dry_run:
        print("(--dry-run：未写任何文件)")
        return 0

    BACKUP.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    for fp in (HOLDINGS, ACCOUNTS):
        if fp.exists():
            shutil.copy2(fp, BACKUP / f"{fp.stem}_before_ledger_{ts}{fp.suffix}")

    # holdings：整表重建（台账未平仓即全部手工持仓）
    HOLDINGS.write_text(json.dumps(new_hold, ensure_ascii=False, indent=1), encoding="utf-8")
    A["realized_pnl"] = realized
    A["_updated"] = (f"{datetime.now():%Y-%m-%d %H:%M} 按交割单台账更新"
                     f"（{L.get('range', {}).get('start')}~{L.get('range', {}).get('end')}，"
                     f"{L.get('range', {}).get('n_trades')} 笔）；可用={a.cash_asof}收盘+之后流水")
    ACCOUNTS.write_text(json.dumps(A, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已写 {HOLDINGS.name}（{len(new_hold)} 条）/ {ACCOUNTS.name}；备份在 backup/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
