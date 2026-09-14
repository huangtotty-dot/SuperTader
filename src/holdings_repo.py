# -*- coding: utf-8 -*-
"""src/holdings_repo.py — 持仓单一真源 loader（2026-08-30）

把散落三处的持仓信息（手动链 holdings.json / 自动链 auto_pool.py / MIRROR_HOLDINGS / 回测 HOLDINGS）
合并成单一真源文件 t_io/state/holdings.json（全量 18 只：3 持有 + 17 auto 候选，未持有 qty=0），
所有消费方从这里派生身份/持仓/目标底仓，杜绝"更新漏改一处导致三套逻辑漂移"。

本模块仅依赖标准库（json/os），可被 superTrader 手动链与 goldminer 自动链共同 import。

字段 schema（每只 6 位 code）：
  name / gm_symbol / type / account / pool(manual|auto|both)
  qty / base / t_qty / cost / pre_close      —— 持仓字段，非持有=0
  mirror_qty / mirror_cost                   —— 自动目标底仓，无=0
"""
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_FILE = os.path.join(_ROOT, "t_io", "state", "holdings.json")
_AUDIT_DIR = os.path.join(_ROOT, "t_io", "logs")


def _audit_holdings_write(code, action, reason, before, after, actor="system"):
    """holdings 写入审计（P0-6，2026-09-01）：追加 t_io/logs/holdings_write_audit_{date}.jsonl。
    记录 actor/action/reason/code/before/after/changed_fields/pid/ts。失败不静默（写 stderr）。"""
    try:
        os.makedirs(_AUDIT_DIR, exist_ok=True)
        _d = datetime.now()
        _before = before if isinstance(before, dict) else {}
        _after = after if isinstance(after, dict) else {}
        rec = {
            "ts": _d.strftime("%Y-%m-%d %H:%M:%S"),
            "date": _d.strftime("%Y-%m-%d"),
            "actor": actor, "action": action, "reason": reason,
            "code": code,
            "before": {k: _before.get(k) for k in _before},
            "after": {k: _after.get(k) for k in _after},
            "changed_fields": sorted(k for k in set(list(_before.keys()) + list(_after.keys()))
                                     if _before.get(k) != _after.get(k)),
            "pid": os.getpid(),
        }
        path = os.path.join(_AUDIT_DIR, f"holdings_write_audit_{rec['date']}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as _e:
        try:
            print(f"[holdings-audit] 审计写失败: {_e}", file=sys.stderr, flush=True)
        except Exception:
            pass


def _is_entry(code, h):
    """排除 _ 前缀的元数据 key 与非 dict 条目。"""
    return isinstance(h, dict) and not str(code).startswith("_")


def load_full() -> dict:
    """读全量 18 只（含未持有 auto 候选）。容错返回 {}。"""
    if not os.path.exists(HOLDINGS_FILE):
        return {}
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {c: h for c, h in data.items() if _is_entry(c, h)}


def load_held() -> dict:
    """仅持有（qty>0 or base>0 or t_qty>0）—— 手动链视图，行为与扩容前（3 只）一致。"""
    return {c: h for c, h in load_full().items()
            if h.get("qty") or h.get("base") or h.get("t_qty")}


def load_auto_pool() -> dict:
    """auto 池身份（code → {name, gm_symbol}），pool ∈ {auto, both}。"""
    return {c: {"name": h.get("name", c), "gm_symbol": h.get("gm_symbol", "")}
            for c, h in load_full().items() if str(h.get("pool") or "") in ("auto", "both")}


def load_mirror_holdings() -> dict:
    """自动目标底仓（code → {qty, cost}），mirror_qty > 0。

    cost 取 mirror_cost（参考成本，非真实持仓 cost）——515180=1.451 是参考价，
    与持仓 cost 语义不同，须分离，否则回测 -8% 硬止损会用错成本。"""
    return {c: {"qty": int(h.get("mirror_qty") or 0),
                "cost": float(h.get("mirror_cost") or 0)}
            for c, h in load_full().items() if int(h.get("mirror_qty") or 0) > 0}


def sync_watchlist_pool(code, pool="auto"):
    """T-4(2026-09-02): 同步 watchlist_buy.json 该码 pool——凡 holdings 置 pool∈{auto,both}，
    watchlist 侧缺省 manual 会触发 P3-2 池分管冲突；此函数统一兜底（幂等，仅 manual→auto）。"""
    try:
        _wl = os.path.join(_ROOT, "t_io", "state", "watchlist_buy.json")
        if not os.path.exists(_wl):
            return
        with open(_wl, "r", encoding="utf-8") as f:
            wl = json.load(f)
        stocks = wl.get("stocks", {})
        if isinstance(stocks, dict) and isinstance(stocks.get(code), dict):
            if str(stocks[code].get("pool") or "manual") == "manual":
                stocks[code]["pool"] = pool
                _tmp = _wl + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    json.dump(wl, f, ensure_ascii=False, indent=2)
                os.replace(_tmp, _wl)
    except Exception:
        pass


def save_held_merged(held: dict, actor: str = "system", reason: str = "merge") -> None:
    """把持有 dict 合并回全量文件（保留未持有的 auto 候选），原子写回。写入强制审计（P0-6）。
    T-4(2026-09-02): 写入口自动同步——凡 pool∈{auto,both} 的标的同步 watchlist pool 置 auto（防 P3-2 冲突）。"""
    full = load_full()
    _before = dict(full)
    full.update(held or {})
    _tmp = HOLDINGS_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)
    os.replace(_tmp, HOLDINGS_FILE)
    for code in (held or {}):
        _audit_holdings_write(code, "merge", reason, _before.get(code), full.get(code), actor)
        _h = full.get(code) or {}
        if str(_h.get("pool") or "") in ("auto", "both"):
            sync_watchlist_pool(code, "auto")


def upsert_auto_entry(code, *, name, gm_symbol, type, mirror_qty, mirror_cost=0.0,
                      actor: str = "system", reason: str = "upsert") -> dict:
    """新增/更新 auto 池标的（pool=auto, 未持仓）→ 原子写回。写入强制审计（P0-6）。

    已存在则保留其既有 qty/cost/base/t_qty 持仓字段不归零（仅设 pool/mirror_qty/mirror_cost）。
    返回该条目。"""
    full = load_full()
    cur = full.get(code) or {}
    entry = dict(cur)
    entry.update({
        "name": name or cur.get("name", code),
        "gm_symbol": gm_symbol or cur.get("gm_symbol", ""),
        "type": type or cur.get("type", "stock"),
        "account": cur.get("account", ""),
        "pool": "auto",
        "qty": int(cur.get("qty") or 0),
        "base": int(cur.get("base") or 0),
        "t_qty": int(cur.get("t_qty") or 0),
        "cost": float(cur.get("cost") or 0),
        "pre_close": float(cur.get("pre_close") or 0),
        "mirror_qty": int(mirror_qty or 0),
        "mirror_cost": float(mirror_cost or 0),
    })
    full[code] = entry
    _tmp = HOLDINGS_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)
    os.replace(_tmp, HOLDINGS_FILE)
    _audit_holdings_write(code, "upsert", reason, cur, entry, actor)
    sync_watchlist_pool(code, "auto")  # T-4(2026-09-02): 加入 auto 池同步 watchlist（防 P3-2 冲突）
    return entry


def delete_entry(code, actor: str = "system", reason: str = "delete") -> bool:
    """从持仓真源删除条目（原子写）。返回是否存在并删除（False=不存在）。写入强制审计（P0-6）。"""
    full = load_full()
    if code not in full:
        return False
    _before = dict(full[code])
    del full[code]
    _tmp = HOLDINGS_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)
    os.replace(_tmp, HOLDINGS_FILE)
    _audit_holdings_write(code, "delete", reason, _before, None, actor)
    return True
