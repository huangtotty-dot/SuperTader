# -*- coding: utf-8 -*-
"""src/holdings_repo.py — 持仓真源网关（2026-10-04 手动/自动双文件拆分）

背景：原单一 `t_io/state/holdings.json`（手动+自动混存，`pool` 标记归属）按 owner 决策
拆成两份独立真源：

  · `t_io/state/holdings_manual.json` —— 手动盘（实盘）真源，`pool ∈ {manual, both}`
  · `t_io/state/holdings_auto.json`   —— 自动盘（掘金仿真）真源，`pool ∈ {auto, both}`

`both` 标的在两份各留一条。跨副本字段归属（守卫强制）：
  · 身份 `name/gm_symbol/type`、行情 `pre_close`、目标底仓 `base` —— 两份必须一致；
    身份/pre_close 更新写全所有副本，base 为自动侧所有但镜像进手动侧。
  · `qty` / `cost` —— **分侧所有**，两份允许不同（唯一可分化字段）。

本模块仅依赖标准库（json/os），可被 superTrader 手动链与 goldminer 自动链共同 import。
所有写入口经本模块（原子 tmp+os.replace + 强制审计 t_io/logs/holdings_write_audit_*.jsonl）。

过渡回退（迁移完成前）：两份文件缺失但旧 `holdings.json` 尚在时，读侧按 `pool` 从旧文件
派生；写侧在首次写入时把旧文件对应侧落成新文件。迁移落盘后删除旧文件，回退自然失效。
"""
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE = os.path.join(_ROOT, "t_io", "state")
MANUAL_FILE = os.path.join(_STATE, "holdings_manual.json")
AUTO_FILE = os.path.join(_STATE, "holdings_auto.json")
_LEGACY_FILE = os.path.join(_STATE, "holdings.json")
_AUDIT_DIR = os.path.join(_ROOT, "t_io", "logs")

# 跨副本必须一致的字段（身份 + 行情 + 目标底仓）；qty/cost 不在此列（分侧所有）
_SHARED_FIELDS = ("name", "gm_symbol", "type", "pre_close", "base")
_MANUAL_POOLS = ("manual", "both")
_AUTO_POOLS = ("auto", "both")


def _audit_holdings_write(code, action, reason, before, after, actor="system"):
    """holdings 写入审计（P0-6）：追加 t_io/logs/holdings_write_audit_{date}.jsonl。
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


def _read_raw(path) -> dict:
    """读整份文件（含 _ 元数据 key）。缺失/损坏返回 {}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _entries(raw: dict) -> dict:
    return {c: h for c, h in (raw or {}).items() if _is_entry(c, h)}


def _legacy_side(pools) -> dict:
    """从旧 holdings.json 派生某一侧（含 _ 元数据）。无旧文件返回 {}。"""
    legacy = _read_raw(_LEGACY_FILE)
    if not legacy:
        return {}
    out = {}
    for c, h in legacy.items():
        if str(c).startswith("_"):
            out[c] = h
            continue
        if not isinstance(h, dict):
            continue
        if str(h.get("pool") or "manual") in pools:
            out[c] = h
    return out


def _read_side(path, pools) -> dict:
    """读某一侧：优先新文件；缺失回退旧 holdings.json 派生。返回仅条目（去 _）。"""
    raw = _read_raw(path)
    if raw:
        return _entries(raw)
    return _entries(_legacy_side(pools))


def _raw_side_or_seed(path, pools) -> dict:
    """写侧用：读整份（含 _）；缺失则从旧文件派生作为初始内容（迁移前首写种子）。"""
    raw = _read_raw(path)
    if raw:
        return raw
    return _legacy_side(pools)


def _atomic_write(path, raw: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _tmp = path + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(_tmp, path)


# ── 读 ────────────────────────────────────────────────────────────
def load_manual() -> dict:
    """手动盘全量条目（pool ∈ {manual, both}）。"""
    return _read_side(MANUAL_FILE, _MANUAL_POOLS)


def load_auto() -> dict:
    """自动盘全量条目（含未持有候选，pool ∈ {auto, both}）。"""
    return _read_side(AUTO_FILE, _AUTO_POOLS)


def load_union() -> dict:
    """手动∪自动合并视图（内存，不落盘）。both 以自动副本为基、覆写手动 qty/cost。"""
    out = {}
    for c, h in load_manual().items():
        out[c] = dict(h)
    for c, h in load_auto().items():
        if c in out:
            merged = dict(h)                 # 自动副本为基（含 base/身份/行情）
            merged["qty"] = out[c].get("qty")  # 覆写为手动侧 qty/cost（实盘口径）
            merged["cost"] = out[c].get("cost")
            out[c] = merged
        else:
            out[c] = dict(h)
    return out


def load_held_manual() -> dict:
    """手动侧实际持有（qty > 0）。"""
    return {c: h for c, h in load_manual().items() if int(h.get("qty") or 0) > 0}


def load_held_auto() -> dict:
    """自动侧实际持有（qty > 0）。"""
    return {c: h for c, h in load_auto().items() if int(h.get("qty") or 0) > 0}


def load_auto_pool() -> dict:
    """auto 池身份（code → {name, gm_symbol}），pool ∈ {auto, both}。"""
    return {c: {"name": h.get("name", c), "gm_symbol": h.get("gm_symbol", "")}
            for c, h in load_auto().items() if str(h.get("pool") or "") in _AUTO_POOLS}


def get_entry(code, side=None):
    """取单条。side ∈ {None=>union, "manual", "auto"}。"""
    if side == "manual":
        return load_manual().get(code)
    if side == "auto":
        return load_auto().get(code)
    return load_union().get(code)


# ── watchlist pool 同步 ────────────────────────────────────────────
def sync_watchlist_pool(code, pool="auto"):
    """T-4(2026-09-02): 同步 watchlist_buy.json 该码 pool —— watchlist 侧镜像持仓池归属。

    凡持仓置 pool∈{auto,both}，watchlist 侧缺省 manual 会触发 P3-2 池分管冲突，故写入口统一兜底。
    值不同即纠正（自愈），防已被写坏的 both 标的永远修不回来。
    """
    try:
        _wl = os.path.join(_ROOT, "t_io", "state", "watchlist_buy.json")
        if not os.path.exists(_wl):
            return
        with open(_wl, "r", encoding="utf-8") as f:
            wl = json.load(f)
        stocks = wl.get("stocks", {})
        if isinstance(stocks, dict) and isinstance(stocks.get(code), dict):
            if str(stocks[code].get("pool") or "manual") != str(pool):
                stocks[code]["pool"] = pool
                _tmp = _wl + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    json.dump(wl, f, ensure_ascii=False, indent=2)
                os.replace(_tmp, _wl)
    except Exception:
        pass


def _sync_pool_for(code, entry):
    """条目 pool∈{auto,both} 时同步 watchlist。"""
    _pool = str((entry or {}).get("pool") or "")
    if _pool in _AUTO_POOLS:
        sync_watchlist_pool(code, _pool)


# ── 写 ────────────────────────────────────────────────────────────
def _apply_shared(dst_entry: dict, src_entry: dict) -> bool:
    """把 src 的共享字段写入 dst 条目（仅当值不同）。返回是否改动。"""
    changed = False
    for k in _SHARED_FIELDS:
        if k in src_entry and dst_entry.get(k) != src_entry.get(k):
            dst_entry[k] = src_entry[k]
            changed = True
    return changed


def save_manual(patch: dict, *, actor="system", reason="merge") -> None:
    """把条目合并进手动文件；共享字段（身份/pre_close/base）传播到自动副本（若同码存在）。"""
    patch = patch or {}
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    before = {c: dict(man_raw.get(c) or {}) for c in patch}
    auto_changed = False
    for code, entry in patch.items():
        man_raw[code] = dict(entry or {})
        if code in auto_raw and isinstance(auto_raw[code], dict):
            if _apply_shared(auto_raw[code], man_raw[code]):
                auto_changed = True
    _atomic_write(MANUAL_FILE, man_raw)
    if auto_changed:
        _atomic_write(AUTO_FILE, auto_raw)
    for code in patch:
        _audit_holdings_write(code, "save_manual", reason, before.get(code), man_raw.get(code), actor)
        _sync_pool_for(code, man_raw.get(code))


def save_auto(patch: dict, *, actor="system", reason="merge") -> None:
    """把条目合并进自动文件；共享字段（含 base 镜像）传播到手动副本（若同码存在）。"""
    patch = patch or {}
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    before = {c: dict(auto_raw.get(c) or {}) for c in patch}
    man_changed = False
    for code, entry in patch.items():
        auto_raw[code] = dict(entry or {})
        if code in man_raw and isinstance(man_raw[code], dict):
            if _apply_shared(man_raw[code], auto_raw[code]):
                man_changed = True
    _atomic_write(AUTO_FILE, auto_raw)
    if man_changed:
        _atomic_write(MANUAL_FILE, man_raw)
    for code in patch:
        _audit_holdings_write(code, "save_auto", reason, before.get(code), auto_raw.get(code), actor)
        _sync_pool_for(code, auto_raw.get(code))


def save_shared(code, fields: dict, *, actor="system", reason="shared") -> None:
    """把共享字段写进该码存在的所有副本（手动/自动）。"""
    fields = fields or {}
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    man_changed = auto_changed = False
    if code in man_raw and isinstance(man_raw[code], dict):
        for k, v in fields.items():
            if man_raw[code].get(k) != v:
                man_raw[code][k] = v
                man_changed = True
    if code in auto_raw and isinstance(auto_raw[code], dict):
        for k, v in fields.items():
            if auto_raw[code].get(k) != v:
                auto_raw[code][k] = v
                auto_changed = True
    if man_changed:
        _atomic_write(MANUAL_FILE, man_raw)
    if auto_changed:
        _atomic_write(AUTO_FILE, auto_raw)
    if man_changed or auto_changed:
        _audit_holdings_write(code, "save_shared", reason, None, fields, actor)


def save_pre_close(prices: dict, *, actor="main", reason="eod_pre_close") -> int:
    """EOD 批量写 pre_close 到该码存在的所有副本。返回写入票数。"""
    prices = prices or {}
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    man_changed = auto_changed = False
    n = 0
    for code, px in prices.items():
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        touched = False
        if code in man_raw and isinstance(man_raw[code], dict) and man_raw[code].get("pre_close") != px:
            man_raw[code]["pre_close"] = px
            man_changed = True
            touched = True
        if code in auto_raw and isinstance(auto_raw[code], dict) and auto_raw[code].get("pre_close") != px:
            auto_raw[code]["pre_close"] = px
            auto_changed = True
            touched = True
        if touched:
            n += 1
            _audit_holdings_write(code, "save_pre_close", reason, None, {"pre_close": px}, actor)
    if man_changed:
        _atomic_write(MANUAL_FILE, man_raw)
    if auto_changed:
        _atomic_write(AUTO_FILE, auto_raw)
    return n


def set_pool(code, pool: str, *, actor="system", reason="set_pool") -> dict:
    """设定成员关系：manual=仅手动文件；auto=仅自动文件；both=两份都在。返回新条目。"""
    pool = str(pool or "").strip()
    if pool not in ("manual", "auto", "both"):
        raise ValueError(f"pool 非法: {pool!r}（须 manual/auto/both）")
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    src = man_raw.get(code) if isinstance(man_raw.get(code), dict) else auto_raw.get(code)
    if not isinstance(src, dict):
        raise KeyError(f"{code} 不在任何持仓文件，无法 set_pool")
    entry = dict(src)
    entry["pool"] = pool
    man_changed = auto_changed = False
    if pool in ("manual", "both"):
        if man_raw.get(code) != entry:
            man_raw[code] = dict(entry)
            man_changed = True
    else:  # auto
        if code in man_raw:
            del man_raw[code]
            man_changed = True
    if pool in ("auto", "both"):
        if auto_raw.get(code) != entry:
            auto_raw[code] = dict(entry)
            auto_changed = True
    else:  # manual
        if code in auto_raw:
            del auto_raw[code]
            auto_changed = True
    if man_changed:
        _atomic_write(MANUAL_FILE, man_raw)
    if auto_changed:
        _atomic_write(AUTO_FILE, auto_raw)
    _audit_holdings_write(code, "set_pool", reason, None, {"pool": pool}, actor)
    _sync_pool_for(code, entry)
    return entry


def upsert_auto_entry(code, *, name, gm_symbol, type, base=None,
                      actor="system", reason="upsert") -> dict:
    """新增/更新 auto 池标的（pool=auto）。已存在则保留既有 qty/cost/base（仅设身份/pool）。
    若同码在手动文件存在，移出（pool=auto 为自动侧专有）。返回该条目。"""
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    # 既有值从对侧/union 兜底（旧单文件语义：改 pool 保留 qty/cost/base）
    cur = auto_raw.get(code)
    if not isinstance(cur, dict):
        cur = get_entry(code) or {}
        cur = dict(cur) if isinstance(cur, dict) else {}
    entry = dict(cur)
    entry.update({
        "name": name or cur.get("name", code),
        "gm_symbol": gm_symbol or cur.get("gm_symbol", ""),
        "type": type or cur.get("type", "stock"),
        "pool": "auto",
        "qty": int(cur.get("qty") or 0),
        "base": int(base if base is not None else (cur.get("base") or 0)),
        "cost": float(cur.get("cost") or 0),
        "pre_close": float(cur.get("pre_close") or 0),
    })
    auto_raw[code] = entry
    _atomic_write(AUTO_FILE, auto_raw)
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    if code in man_raw:
        del man_raw[code]
        _atomic_write(MANUAL_FILE, man_raw)
    _audit_holdings_write(code, "upsert_auto", reason, cur, entry, actor)
    sync_watchlist_pool(code, "auto")
    return entry


def upsert_manual_entry(code, *, name, gm_symbol, type,
                        actor="system", reason="upsert") -> dict:
    """新增/更新手动侧标的（pool=manual）。已存在则保留既有 qty/cost/base。"""
    man_raw = _raw_side_or_seed(MANUAL_FILE, _MANUAL_POOLS)
    cur = man_raw.get(code)
    if not isinstance(cur, dict):
        cur = get_entry(code) or {}
        cur = dict(cur) if isinstance(cur, dict) else {}
    entry = dict(cur)
    entry.update({
        "name": name or cur.get("name", code),
        "gm_symbol": gm_symbol or cur.get("gm_symbol", ""),
        "type": type or cur.get("type", "stock"),
        "pool": "manual",
        "qty": int(cur.get("qty") or 0),
        "base": int(cur.get("base") or 0),
        "cost": float(cur.get("cost") or 0),
        "pre_close": float(cur.get("pre_close") or 0),
    })
    man_raw[code] = entry
    _atomic_write(MANUAL_FILE, man_raw)
    auto_raw = _raw_side_or_seed(AUTO_FILE, _AUTO_POOLS)
    if code in auto_raw:
        del auto_raw[code]
        _atomic_write(AUTO_FILE, auto_raw)
    _audit_holdings_write(code, "upsert_manual", reason, cur, entry, actor)
    return entry


def delete_entry(code, *, side=None, actor="system", reason="delete") -> bool:
    """删除条目。side None=两份都删；"manual"/"auto"=仅删该侧（both 时另侧降为单侧归属）。"""
    man_raw = _read_raw(MANUAL_FILE) or _legacy_side(_MANUAL_POOLS)
    auto_raw = _read_raw(AUTO_FILE) or _legacy_side(_AUTO_POOLS)
    existed = False
    man_changed = auto_changed = False
    if side in (None, "manual") and code in man_raw:
        del man_raw[code]
        man_changed = existed = True
    if side in (None, "auto") and code in auto_raw:
        del auto_raw[code]
        auto_changed = existed = True
    # 单侧删除后，另一侧若原为 both → 降为单侧 pool
    if side == "auto" and code in man_raw and isinstance(man_raw[code], dict) \
            and str(man_raw[code].get("pool")) == "both":
        man_raw[code]["pool"] = "manual"
        man_changed = True
    if side == "manual" and code in auto_raw and isinstance(auto_raw[code], dict) \
            and str(auto_raw[code].get("pool")) == "both":
        auto_raw[code]["pool"] = "auto"
        auto_changed = True
    if not existed:
        return False
    if man_changed:
        _atomic_write(MANUAL_FILE, man_raw)
    if auto_changed:
        _atomic_write(AUTO_FILE, auto_raw)
    _audit_holdings_write(code, "delete", reason, None, None, actor)
    return True


if __name__ == "__main__":
    m, a = load_manual(), load_auto()
    print(f"manual={len(m)} auto={len(a)} union={len(load_union())}")
    print("manual:", sorted(m))
    print("auto:", sorted(a))
