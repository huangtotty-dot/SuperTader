# -*- coding: utf-8 -*-
"""进程级腾讯快照总线 QuoteBus（P2-13, 2026-10-23）。

## 为什么要有它（审计实锤）

腾讯快照曾有 **5 个独立轮询者**各自建 TencentProvider、各自打 qt.gtimg.cn：
`t_gui._load_quotes_build`（10s）、`main.py` 交易主循环（15s + VWAP 60s 节流）、
`hunter_ma5_alert` 实时 watcher（20s，~172 只）、`stock_hunter hunter_poll`（30min）、
`t_gui.load_quotes_for`（按需 ≤400 只）。同一只持仓票一分钟被拉 6+ 次，
全仓库无令牌桶/熔断 —— "腾讯限流整日断供"有前科。

GUI 与 main.py 是**不同进程** ⇒ 共享必须走文件层，不是进程内缓存。

## 语义（API 契约，GUI 侧照此接入，不许偏离）

`get_quotes(codes, max_age_sec=8.0)` → `{code: quote_dict}`：

1. 读共享档 `t_io/state/quote_bus.json`（`{code: {"ts": epoch, "q": quote_dict}}`）；
   新鲜（age < max_age_sec）直接命中；
2. 过期/缺失的码：拿跨进程文件锁（`t_io/state/quote_bus.lock`，msvcrt.locking，
   非 Windows 退 O_EXCL）后**再读一次**（别的进程可能刚刷新过），仍缺的才经
   `TencentProvider.snapshot_auction_many`（内部已分块）补齐，merge 写回共享档
   （tmp + os.replace 原子写）；
3. **永不抛异常**：取数失败时过期数据照样返回（stale 兜底），实在没有就缺该 code。

`quote_dict` 口径 = `TencentProvider.snapshot_auction_many` 的每只返回值，
key 为裸 6 位数字码（输入的 `sh600001` / `600001_SH` 等会先归一）。

## 其它硬约束

- 共享档容量上限 `_MAX_ENTRIES=2000`，超出按 ts 最旧淘汰；文件损坏丢弃重建（fail-open）；
- 进程内 1s memo（`_MEMO`）防同一进程高频连打文件层；
- 本轮**不实现令牌桶**（下次迭代）；`_STATS` + `bus_stats()` 先留观测口。
- ⚠️ 模块顶层**不许** import tencent_provider / facade（它们拖 pandas 链，
  见 `core/market_data/__init__.py` 的冷启动教训）——取数路径函数内惰性 import。
"""
import json
import logging
import os
import time as _time

log = logging.getLogger("quote_bus")

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STATE_DIR = os.path.join(_BASE, "t_io", "state")
_BUS_FP = os.path.join(_STATE_DIR, "quote_bus.json")
_LOCK_FP = os.path.join(_STATE_DIR, "quote_bus.lock")

_MAX_ENTRIES = 2000          # 共享档容量上限；超出按 ts 最旧淘汰
_MEMO_TTL_SEC = 1.0          # 进程内 memo：1s 内不重读共享档
_LOCK_TIMEOUT_SEC = 8.0      # 拿锁超时上限；超时 fail-open（宁可重复取数，不许卡死轮询）

_STATS = {"hit": 0, "miss": 0, "fetch": 0}
_MEMO = {"ts": 0.0, "store": None}   # 进程内 1s memo（共享档整份快照）

try:
    import msvcrt                       # Windows 跨进程文件锁
except ImportError:                     # pragma: no cover - 非 Windows
    msvcrt = None


def bus_stats() -> dict:
    """观测口（本轮不接令牌桶，先留计数）：{"hit","miss","fetch"} 的拷贝。"""
    return dict(_STATS)


def _norm(code) -> str:
    """归一到裸码（'sh600001' / '600001_SH' / '600001.SH' → '600001'）；无法归一原样返回。"""
    s = str(code or "").strip().split("_")[0].split(".")[0]
    if len(s) > 2 and s[:2].isalpha():
        s = s[2:]
    return s


def _load_store() -> dict:
    """读共享档；缺失/损坏 ⇒ {}（fail-open，损坏档在下一次 merge 写回时被重建）。"""
    try:
        with open(_BUS_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_store(store: dict) -> None:
    """原子写回（tmp + os.replace，按 pid 分 tmp 名避免并发互踩）；超容量按 ts 最旧淘汰。"""
    try:
        if len(store) > _MAX_ENTRIES:
            victims = sorted(store, key=lambda c: float((store.get(c) or {}).get("ts") or 0))
            for c in victims[: len(store) - _MAX_ENTRIES]:
                store.pop(c, None)
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _BUS_FP + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, _BUS_FP)
    except Exception as e:
        log.debug(f"[QuoteBus] 共享档写回失败（本轮丢弃，下轮重试）: {type(e).__name__}: {str(e)[:100]}")


class _bus_lock:
    """跨进程文件锁（with 用）。Windows=msvcrt.locking 锁 1 字节；非 Windows=O_EXCL 创建。

    **fail-open**：超时/异常拿不到锁也放行 —— 后果顶多是一次重复取数，
    而拿不到锁就卡死 20s 轮询才是不可接受的。
    """

    def __init__(self):
        self._fh = None
        self._excl_fd = None

    def __enter__(self):
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            deadline = _time.time() + _LOCK_TIMEOUT_SEC
            if msvcrt is not None:
                self._fh = open(_LOCK_FP, "a+b")
                while True:
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if _time.time() >= deadline:
                            log.debug("[QuoteBus] 拿锁超时（fail-open 放行）")
                            break
                        _time.sleep(0.05)
            else:                                   # pragma: no cover - 非 Windows
                excl = _LOCK_FP + ".excl"
                while True:
                    try:
                        self._excl_fd = os.open(excl, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                        break
                    except FileExistsError:
                        if _time.time() >= deadline:
                            break
                        _time.sleep(0.05)
        except Exception as e:
            log.debug(f"[QuoteBus] 锁初始化异常（fail-open 放行）: {type(e).__name__}: {str(e)[:80]}")
        return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    self._fh.close()
                except Exception:
                    pass
            if self._excl_fd is not None:           # pragma: no cover - 非 Windows
                try:
                    os.close(self._excl_fd)
                except Exception:
                    pass
                try:
                    os.remove(_LOCK_FP + ".excl")
                except Exception:
                    pass
        except Exception:
            pass
        return False


def _read_store() -> dict:
    """共享档 + 进程内 1s memo（防同一进程高频连打文件层）。"""
    now = _time.time()
    if _MEMO["store"] is not None and now - float(_MEMO["ts"] or 0) < _MEMO_TTL_SEC:
        return _MEMO["store"]
    store = _load_store()
    _MEMO["ts"], _MEMO["store"] = now, store
    return store


def _fetch(codes: list) -> dict:
    """经 TencentProvider.snapshot_auction_many 补齐（其内部已分块）。异常 ⇒ {}（不抛）。

    惰性 import：模块顶层拉 tencent_provider 会连带 pandas 链（见 __init__.py 教训）。
    """
    _STATS["fetch"] += len(codes)                   # 记**网络请求企图**（无论成败）
    try:
        from core.market_data.tencent_provider import TencentProvider
        return TencentProvider().snapshot_auction_many(list(codes)) or {}
    except Exception as e:
        log.warning(f"[QuoteBus] 腾讯取数失败（stale 兜底）: {type(e).__name__}: {str(e)[:120]}")
        return {}


def get_quotes(codes, max_age_sec: float = 8.0) -> dict:
    """共享快照总线读。语义与口径见模块 docstring（P2-13 API 契约）。**永不抛异常**。"""
    try:
        max_age = float(max_age_sec if max_age_sec is not None else 8.0)
    except Exception:
        max_age = 8.0
    try:
        want = []
        for c in (codes or []):
            b = _norm(c)
            if b and b not in want:
                want.append(b)
        if not want:
            return {}
        now = _time.time()
        store = _read_store()
        out, todo = {}, []
        for c in want:
            ent = store.get(c) or {}
            q = ent.get("q")
            if isinstance(q, dict) and q and now - float(ent.get("ts") or 0) < max_age:
                out[c] = q
                _STATS["hit"] += 1
            else:
                todo.append(c)
                _STATS["miss"] += 1
        if todo:
            with _bus_lock():
                # 拿锁后**再读一次**共享档（绕过 memo）：别的进程可能刚刷新过
                store2 = _load_store()
                still = []
                for c in todo:
                    ent = store2.get(c) or {}
                    q = ent.get("q")
                    if isinstance(q, dict) and q and now - float(ent.get("ts") or 0) < max_age:
                        out[c] = q
                    else:
                        still.append(c)
                if still:
                    fetched = _fetch(still)
                    ts_now = _time.time()
                    for c, q in (fetched or {}).items():
                        b = _norm(c)
                        if b and isinstance(q, dict) and q:
                            store2[b] = {"ts": ts_now, "q": q}
                            out.setdefault(b, q)
                    _save_store(store2)
                    _MEMO["ts"], _MEMO["store"] = _time.time(), store2
                # stale 兜底：取数失败/部分失败时，过期数据照样返回
                for c in todo:
                    if c not in out:
                        q = (store2.get(c) or {}).get("q")
                        if isinstance(q, dict) and q:
                            out[c] = q
        return out
    except Exception as e:
        log.warning(f"[QuoteBus] get_quotes 异常（不抛，尽力 stale 兜底）: {type(e).__name__}: {str(e)[:120]}")
        try:                                        # 最后兜底：共享档里有什么给什么（含过期）
            store = _load_store()
            out = {}
            for c in (codes or []):
                q = (store.get(_norm(c)) or {}).get("q")
                if isinstance(q, dict) and q:
                    out[_norm(c)] = q
            return out
        except Exception:
            return {}
