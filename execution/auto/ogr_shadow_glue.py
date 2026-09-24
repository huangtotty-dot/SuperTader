# -*- coding: utf-8 -*-
"""开盘低开反转 · L3 影子层胶水（2026-09-22）。

**只记日志、绝不下单、绝不写持仓。** 目的是在真实 bar 流上观察 20 个交易日，
回答三件离线答不了的事：

  1. **第一根 60s bar 的 `open` 是否等于集合竞价价**（决定入场价口径）；
  2. 真实 bar 流下 `mkt_gap` / 触发集合是否稳定（离线用的是 30min 面板）；
  3. `holdings.json::pre_close` 是否可靠地等于上一交易日收盘（gap 的分母）。

## 纪律
- 不 import `gm.api`、不调 `order_volume`、不碰 `holdings.json`（只读）。
- 一切异常 **fail-safe**：影子层出错绝不影响主循环（调用方 try/except + 本模块内 try）。
- 决策逻辑**不在本文件**：一律委托 `core/open_gap_reversal.py`（纯函数，L1/L2 已验）。

日志：`t_io/logs/ogr_shadow_{date}.jsonl`（沿用 `t_io/logs/` 的既有约定）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

# ── 仓库根（execution/auto/ → 上两级），支持 SUPERTRADER_ROOT 重定向（测试用）──
_ROOT = os.environ.get(
    "SUPERTRADER_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_DEFAULT_LOG_DIR = os.path.join(_ROOT, "t_io", "logs")

AUTO_POOLS = ("auto", "both")     # 与 config/auto_pool.py 的口径一致


# ── 决策核按文件路径加载（core 不加入 sys.path，避免与 _gm/config 等同名包互撞）──
_OGR_MOD = None
try:
    import importlib.util as _ilu
    _ogr_path = os.path.join(_ROOT, "core", "open_gap_reversal.py")
    _ogr_spec = _ilu.spec_from_file_location("open_gap_reversal", _ogr_path)
    _OGR_MOD = _ilu.module_from_spec(_ogr_spec)
    _ogr_spec.loader.exec_module(_OGR_MOD)
except Exception as _e:                                  # pragma: no cover
    print(f"[OGR] 决策核加载失败 → 影子层关闭: {_e}")
    _OGR_MOD = None


def available() -> bool:
    return _OGR_MOD is not None


# ══════════════════════════════════════════════════════════════════════
# 快照组装（全部防御式：bar 可能是对象也可能是 dict）
# ══════════════════════════════════════════════════════════════════════
def _attr(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] is not None:
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return default


def _code_of(gm_symbol: str) -> str | None:
    """'SHSE.600481' → '600481'。"""
    s = str(gm_symbol or "")
    if "." in s:
        s = s.split(".")[-1]
    s = s.strip()
    return s if len(s) == 6 and s.isdigit() else None


code_of = _code_of          # 公开别名（gm_main 逐票累积时要用）


def snapshot_from_bars(bars) -> dict:
    """{code: {'gm_symbol','bar_open','bar_close','eob'}}。无有效开盘价的票不入选。"""
    out = {}
    for b in (bars or []):
        gs = _attr(b, "symbol")
        code = _code_of(gs)
        if not code:
            continue
        op = _attr(b, "open")
        cl = _attr(b, "close")
        eob = _attr(b, "eob", "bob")
        try:
            op = float(op)
            cl = float(cl) if cl is not None else None
        except (TypeError, ValueError):
            continue
        if not (op > 0):
            continue
        out[code] = {"gm_symbol": str(gs), "bar_open": op, "bar_close": cl,
                     "eob": str(eob) if eob is not None else None}
    return out


def pool_prev_close(holdings_map: dict, pools=AUTO_POOLS) -> dict:
    """{code: pre_close}，只取 auto 侧且有正 pre_close 的票（holdings.json 只读）。"""
    out = {}
    for code, rec in (holdings_map or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("pool", "")) not in pools:
            continue
        try:
            pc = float(rec.get("pre_close") or 0)
        except (TypeError, ValueError):
            continue
        if pc > 0:
            out[str(code)] = pc
    return out


def read_holdings(path: str | None = None) -> dict:
    """**只读** holdings.json（唯一持仓真源；本模块绝不写）。

    不依赖引擎的 `latest_pre_close`（那个在逐票循环里才填），故调用点不受循环顺序影响。
    """
    try:
        p = path or os.path.join(_ROOT, "t_io", "state", "holdings.json")
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════════════════════
def append_log(rec: dict, log_dir: str | None = None) -> None:
    """追加一行到 t_io/logs/ogr_shadow_{date}.jsonl。fail-open 静默。"""
    try:
        d = log_dir or _DEFAULT_LOG_DIR
        os.makedirs(d, exist_ok=True)
        date = str(rec.get("date") or datetime.now().strftime("%Y-%m-%d"))
        r = dict(rec)
        r.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(os.path.join(d, f"ogr_shadow_{date}.jsonl"),
                  "a", encoding="utf-8") as fp:
            fp.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 主入口（供 gm_main 在开盘后「每日一次」调用）
# ══════════════════════════════════════════════════════════════════════
def evaluate_maps(open_px: dict, prev_close: dict, now: datetime,
                  codes=None, median_codes=None) -> dict | None:
    """**核心决策**：直接给 `{code: 开盘价}` 与 `{code: 前收}` 两张 map。

    `codes`        —— **关注名单**（默认 = 两张 map 的交集），只对它出 decision。
    `median_codes` —— **取 mkt_gap 中位数的名单**（默认 = 关注名单）。⚠️ 必须分开传：
      `open_px/prev_close` 给「代理池 + 交易池」的并集（交易池的 gap 仍要算），而中位数**只取
      代理池**。若用交易池自己的中位就是**自指**：2026-09-24 实测与预注册的 981 面板中位在
      31 天里 9 天符号相反、腿集交集仅 36/141 ⇒ 跑的是另一条规则。

    L3 影子（从 bars 组快照）与 L4 实单（从逐票累积的 `_ogr_opens` + 昨收快照）
    都走这里 —— 后者是必须的： **gm 回测的 `on_bar` 是逐票回调**（一次只有 1 根 bar），
    无法在单次调用里看到全池，故开盘价必须跨回调累积。
    """
    if _OGR_MOD is None:
        return None
    try:
        common = sorted(set(open_px) & set(prev_close))
        if len(common) < _OGR_MOD.MIN_POOL:
            return None
        _watch = (common if codes is None
                  else [c for c in codes if c in open_px and c in prev_close])
        _mgp = None
        if median_codes is not None:
            _mgp = [c for c in median_codes if c in open_px and c in prev_close]
            if len(_mgp) < _OGR_MOD.MIN_POOL:
                return None                      # 代理池太薄 ⇒ fail-closed（不猜）
        r = _OGR_MOD.evaluate({c: prev_close[c] for c in common},
                              {c: open_px[c] for c in common},
                              codes=_watch, median_codes=_mgp)
        rows = []
        for d in r["decisions"]:
            rows.append({
                "code": d["code"],
                "prev_close": round(float(prev_close[d["code"]]), 4),
                "bar_open": round(float(open_px[d["code"]]), 4),
                "gap": None if d["gap"] is None else round(d["gap"], 6),
                "rel": None if d["rel"] is None else round(d["rel"], 6),
                "decision": d["decision"], "reason": d["reason"],
            })
        return {
            "date": now.strftime("%Y-%m-%d"),
            "bar_time": now.strftime("%H:%M:%S"),
            "pool_n": r["pool_n"],
            "mkt_gap": None if r["mkt_gap"] is None else round(r["mkt_gap"], 6),
            "tradable": r["tradable"],
            "n_tradable": len(r["tradable"]),
            "rows": rows,
        }
    except Exception:
        return None


def decide(bars, holdings_map: dict, now: datetime,
           prev_close_map: dict | None = None) -> dict | None:
    """**纯决策**：组装池快照 → 调决策核 → 返回判定记录（**不落日志、不下单**）。

    L3 影子与 L4 实单共用本函数；差异只在调用方是否下单。

    `prev_close_map`（code → 前一交易日收盘）：**回测必须传** —— `holdings.json::pre_close`
    是"当前"值（superTrader 14:59 写入），拿它算历史某日的 gap 会得到完全错误的信号。
    live 亦应优先传（bar_cache 末根 = 前一交易日收盘，见 gm_main._ogr_prev_close_map）。
    """
    if _OGR_MOD is None:
        return None
    try:
        snap = snapshot_from_bars(bars)
        pc_map = dict(prev_close_map or {})
        if not pc_map:
            pc_map = pool_prev_close(holdings_map)
        common = sorted(set(snap) & set(pc_map))
        if len(common) < _OGR_MOD.MIN_POOL:
            return None

        prev_close = {c: pc_map[c] for c in common}
        open_px = {c: snap[c]["bar_open"] for c in common}
        r = _OGR_MOD.evaluate(prev_close, open_px, codes=common)

        rows = []
        for d in r["decisions"]:
            s = snap.get(d["code"], {})
            rows.append({
                "code": d["code"], "gm_symbol": s.get("gm_symbol"),
                "prev_close": round(float(pc_map[d["code"]]), 4),
                "bar_open": round(float(s.get("bar_open") or 0), 4),
                "bar_close": (round(float(s["bar_close"]), 4)
                              if s.get("bar_close") is not None else None),
                "bar_eob": s.get("eob"),
                "gap": None if d["gap"] is None else round(d["gap"], 6),
                "rel": None if d["rel"] is None else round(d["rel"], 6),
                "decision": d["decision"], "reason": d["reason"],
            })
        return {
            "date": now.strftime("%Y-%m-%d"),
            "bar_time": now.strftime("%H:%M:%S"),
            "n_bars_in_call": len(list(bars or [])),
            "pool_n": r["pool_n"],
            "mkt_gap": None if r["mkt_gap"] is None else round(r["mkt_gap"], 6),
            "tradable": r["tradable"],
            "n_tradable": len(r["tradable"]),
            "rows": rows,
        }
    except Exception:
        return None


def run_shadow(bars, holdings_map: dict, now: datetime,
               log_dir: str | None = None) -> dict | None:
    """组装池快照 → 调决策核 → 落日志。**不产生任何订单。**

    返回落盘的判定记录（便于调用方打印一行摘要）；不可用/样本不足返回 None。
    """
    if _OGR_MOD is None:
        return None
    try:
        rec = decide(bars, holdings_map, now)
        if rec is None:
            return None
        rec = {**rec, "layer": "L3_shadow", "note": "只记日志，未下单"}
        append_log(rec, log_dir)
        return rec
    except Exception as e:                                   # 影子层绝不炸主循环
        try:
            append_log({"date": now.strftime("%Y-%m-%d"), "error": f"{type(e).__name__}: {e}"[:300],
                        "layer": "L3_shadow"}, log_dir)
        except Exception:
            pass
        return None
