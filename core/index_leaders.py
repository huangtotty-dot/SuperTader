# -*- coding: utf-8 -*-
"""指数「市值前 10」名单（2026-11-24 owner：每个指数取市值最大的 10 只，方格显示涨幅）。

口径（owner 2026-11-24 拍板：「每个指数选取 10 个市值最大的股票」）：

  · 上证指数  = 沪市 A 股（**含科创板**）总市值前 10
  · 深证成指  = 深市 A 股（**含创业板**）总市值前 10
  · 创业板指  = 创业板前 10
  · 科创50    = 科创板前 10
  · 中证500 / 中证1000 = **先取成分表**（新浪"最新成分"分页）再按总市值取前 10
    ——这两个指数横跨全市场，没法用某一个板块近似。

为什么宽基用"板块前 10"而不是逐只成分：上证综指 = 全部沪市股票，深证成指 = 深市
最大的 500 只，创业板指 = 创业板最大的 100 只，科创 50 = 科创板最大的 50 只
⇒ 这些指数的前 10 大权重与对应板块的总市值前 10 一致，一次 clist 请求即可；
中证 500 / 1000 是中盘/小盘切片，只有成分表能确定。

数据源（2026-11-24 owner：「使用 gm 的数据」）：
  · **主源 = GM `stk_get_index_constituents`**：一次调用拿到**全部成分 + 总市值**
    （`market_value_total`，亿元）与权重 ⇒ 六个指数各 1 个请求，排序后取前 10；
    再 `get_instruments` 批量取 10 个名字。GM 对指数成分是**官方口径**（例如科创50
    以 GM 成分表为准，新上市未纳入的票不会混进来）。
  · **兜底**（GM 不可用/未授权）：宽基走东财 clist 按 f20 降序；中证系走新浪成分分页
    + 东财批量市值。仅首次/跨日时后台跑一次。

缓存与 SWR：结果落 `t_io/cache/index_leaders.json`（按自然日）。
  · 有缓存（哪怕昨天的）⇒ 立即返回旧档；跨日则后台单飞重建；
  · 从未成功过 ⇒ `pending=True`，前端显示"名单准备中"；
  · 任何异常都保留旧档，绝不抛到 js_api 线程。
"""
from __future__ import annotations

import json
import os
import threading as _threading
import time as _time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FP = os.path.join(_ROOT, "t_io", "cache", "index_leaders.json")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
_N = 10                       # 每个指数取市值最大的 N 只
_POLITE_SEC = 0.25            # 请求间隔（新浪分页多，别把对方打急）

# 指数 → GM 符号（主源）。顺序 = 前端方格顺序。
_GM_INDICES = (
    ("上证指数", "SHSE.000001"),
    ("深证成指", "SZSE.399001"),
    ("创业板指", "SZSE.399006"),
    ("科创50", "SHSE.000688"),
    ("中证500", "SHSE.000905"),
    ("中证1000", "SHSE.000852"),
)
# 兜底①：宽基 → 东财 clist 的 fs 过滤串（拆成单串分别请求再合并，见 _em_top_multi）
_SEGMENTS = (
    ("上证指数", ("m:1+t:2", "m:1+t:23")),   # 沪A + 科创板
    ("深证成指", ("m:0+t:6", "m:0+t:80")),   # 深A + 创业板
    ("创业板指", ("m:0+t:80",)),
    ("科创50", ("m:1+t:23",)),
)
# 兜底②：中证系走新浪成分表（indexid）
_CSI = (
    ("中证500", "000905"),
    ("中证1000", "000852"),
)
_ORDER = tuple(name for name, _ in _GM_INDICES)

_state = {"data": None, "ts": 0.0, "running": False}
_lock = _threading.Lock()


# ---------------- 数据源（可被测试替换） ----------------
def _get(host: str, path: str, timeout: float = 8.0, encoding: str = "utf-8") -> str:
    """⚠️ `encoding` **必须显式**：`pooled_get` 默认按 gbk 解码（腾讯口径），
    东财 JSON 是 UTF-8 ⇒ 用默认会把股票名解成乱码（2026-11-24 真机：方格全是"浠ｇ悊…"）。
    新浪成分页是 GBK，调用处显式传 encoding="gbk"。"""
    from core.market_data.http_pool import pooled_get
    return pooled_get(host, path, timeout,
                      {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
                      encoding=encoding)


def _em_top(fs: str, n: int = _N):
    """东财 clist 按总市值降序取前 n → [{code,name,mktcap}]；失败/空 → []。"""
    path = (f"/api/qt/clist/get?pn=1&pz={max(n, 20)}&po=1&np=1&fltt=2&invt=2"
            f"&fid=f20&fs={fs}&fields=f12,f14,f20")
    for attempt in range(4):
        try:
            d = json.loads(_get("push2.eastmoney.com", path))
            rows = ((d.get("data") or {}).get("diff") or [])
            out = []
            for x in rows:
                code = str(x.get("f12") or "")
                name = str(x.get("f14") or "").strip()
                try:
                    cap = float(x.get("f20") or 0.0)
                except Exception:
                    cap = 0.0
                if len(code) == 6 and code.isdigit() and name and cap > 0:
                    out.append({"code": code, "name": name, "mktcap": cap})
            out.sort(key=lambda r: -r["mktcap"])
            return out[:n]
        except Exception:
            _time.sleep(0.8 + attempt * 1.2)     # 1.2s / 2s / 3.2s：东财限流是常态
    return []


def parse_sina_components(html: str):
    """新浪「最新成分」页 → [(mkt, code, name)]（去重、剔除指数自身链接）。

    页面是 GBK 编码的表格，成分逐行形如 `<a href=".../sh688981/">中芯国际</a>`；
    表头/导航还会链到指数本身（上证指数/深证成指/沪深300…）⇒ 按 6 位代码 + 名称过滤。
    """
    import re
    pairs = re.findall(r'href="[^"]*?/(s[hz]\d{6})/?[^"]*"[^>]*>([^<]{1,14})</a>', html or "")
    _IDX_CODES = {"000001", "399001", "000300", "000905", "000852", "000688",
                  "399006", "899050", "000016", "000010", "399005"}
    out, seen = [], set()
    for mkt, nm in pairs:
        code = mkt[2:]
        nm = (nm or "").strip()
        if code in _IDX_CODES or not nm:
            continue
        if nm.endswith(("指数", "成指", "成份")):
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append((mkt, code, nm))
    return out


def _sina_members(idx_code: str, max_pages: int = 40):
    """新浪最新成分（分页）→ {code: name}；任一页失败就返回已取到的部分。"""
    got: dict = {}
    for page in range(1, max_pages + 1):
        if page == 1:
            path = f"/corp/go.php/vII_NewestComponent/indexid/{idx_code}.phtml"
        else:
            path = f"/corp/go.php/vII_NewestComponent/indexid/{idx_code}/page/{page}.phtml"
        try:
            raw = _get("vip.stock.finance.sina.com.cn", path, 10.0, encoding="gbk")
        except Exception:
            break
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("gbk", "replace")
        rows = parse_sina_components(raw)
        if not rows:
            break
        before = len(got)
        for _mkt, code, nm in rows:
            got.setdefault(code, nm)
        if len(got) == before:      # 该页没有新码 ⇒ 到头了
            break
        _time.sleep(_POLITE_SEC)
    return got


def _em_caps(codes, chunk: int = 60):
    """东财 ulist 批量取总市值 → {code: (name, cap)}；失败块跳过。"""
    out: dict = {}
    codes = list(codes)
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        secids = ",".join(("1." if c[:1] in ("5", "6") else "0.") + c for c in part)
        path = ("/api/qt/ulist.np/get?fltt=2&invt=2&fields=f12,f14,f20&secids=" + secids)
        for attempt in range(2):
            try:
                d = json.loads(_get("push2.eastmoney.com", path))
                for x in ((d.get("data") or {}).get("diff") or []):
                    code = str(x.get("f12") or "")
                    cap = 0.0
                    try:
                        cap = float(x.get("f20") or 0.0)
                    except Exception:
                        cap = 0.0
                    if code and cap > 0:
                        out[code] = (str(x.get("f14") or code), cap)
                break
            except Exception:
                _time.sleep(0.8 + attempt * 1.2)
        _time.sleep(_POLITE_SEC * 0.6)
    return out


def _csi_top(idx_code: str, n: int = _N):
    """中证系：成分表 → 总市值 → 前 n。"""
    members = _sina_members(idx_code)
    if not members:
        return []
    caps = _em_caps(members.keys())
    rows = []
    for code, (nm, cap) in caps.items():
        nm = nm or members.get(code) or code
        rows.append({"code": code, "name": nm, "mktcap": cap})
    rows.sort(key=lambda r: -r["mktcap"])
    return rows[:n]


def _norm(rows, n: int = _N):
    """兜底归一：去坏行 + 按市值降序 + 截前 n（数据源偶发乱序/多给，别把脏样子传下去）。"""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("code")]
    rows.sort(key=lambda r: -(r.get("mktcap") or 0.0))
    return rows[:n]


_gma_cache = {"gma": None}


def _gma():
    """GM SDK 句柄（懒加载 + 进程内缓存）。"""
    if _gma_cache["gma"] is None:
        from core.market_data.gm_provider import GmProvider
        _gma_cache["gma"] = GmProvider()._gma
    return _gma_cache["gma"]


def _gm_names(gma, gm_symbols):
    """GM 批量取名字 → {6位码: 名称}（取不到就回落代码）。"""
    out = {}
    try:
        rows = gma.get_instruments(list(gm_symbols), fields="symbol,sec_name") or []
        for r in rows:
            sym = str(r.get("symbol") or "")
            nm = str(r.get("sec_name") or "").strip()
            if sym and nm:
                out[sym.split(".")[-1]] = nm
    except Exception:
        pass
    return out


def _gm_top(index_symbol: str, n: int = _N):
    """GM 官方指数成分 → 按总市值降序取前 n。

    `stk_get_index_constituents` 一次调用就带 `market_value_total`（亿元）与权重
    ⇒ 不再逐只查市值（原东财 ulist 要按 50 只/请求切块，中证1000 就是 20 个请求）。
    """
    gma = _gma()
    df = gma.stk_get_index_constituents(index_symbol)
    if df is None or not hasattr(df, "sort_values") or len(df) == 0:
        return []
    df = df.sort_values("market_value_total", ascending=False).head(n)
    gsyms, rows = [], []
    for r in df.itertuples():
        sym = str(getattr(r, "symbol", "") or "")
        code = sym.split(".")[-1]
        if len(code) != 6 or not code.isdigit():
            continue
        try:
            cap_yi = float(getattr(r, "market_value_total", 0) or 0.0)
        except Exception:
            cap_yi = 0.0
        gsyms.append(sym)
        rows.append({"code": code, "name": code, "mktcap": cap_yi * 1e8})
    names = _gm_names(gma, gsyms) if gsyms else {}
    for r in rows:
        r["name"] = names.get(r["code"]) or r["name"]
    return _norm(rows, n)


def _em_top_multi(fss):
    """多个 fs 分片分别取 top，合并后按市值取前 n（某一片失败不影响其它片）。"""
    rows = []
    for fs in fss:
        rows.extend(_em_top(fs))
        _time.sleep(_POLITE_SEC)
    return _norm(rows)


_EM_FS = dict(_SEGMENTS)
_CSI_IDX = dict(_CSI)


def _build() -> dict:
    items = {}
    for name, gsym in _GM_INDICES:
        rows = []
        try:
            rows = _gm_top(gsym)
        except Exception:
            rows = []
        if not rows:                       # GM 不可用 ⇒ 兜底（宽基东财 / 中证新浪）
            try:
                if name in _EM_FS:
                    rows = _em_top_multi(_EM_FS[name])
                elif name in _CSI_IDX:
                    rows = _norm(_csi_top(_CSI_IDX[name]))
            except Exception:
                rows = []
        if rows:
            items[name] = rows
    return items


# ---------------- 缓存 / SWR ----------------
def _read_disk() -> dict:
    try:
        with open(_CACHE_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), dict):
            return d
    except Exception:
        pass
    return {}


def _write_disk(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FP), exist_ok=True)
        tmp = _CACHE_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FP)
    except Exception:
        pass


_SCHEMA = 2          # 2=显式编码修正后的名单（旧的 gbk 误读档不可信，直接判废）


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _kick_build() -> None:
    def _bg():
        try:
            items = _build()
            if items:
                d = {"date": _today(), "schema": _SCHEMA,
                     "built_at": datetime.now().strftime("%H:%M:%S"),
                     "items": items}
                _write_disk(d)
                with _lock:
                    _state.update({"data": d, "ts": _time.time()})
        except Exception:
            pass
        finally:
            with _lock:
                _state["running"] = False
    _threading.Thread(target=_bg, daemon=True, name="index-leaders").start()


def get_cached(ttl_sec: float = 86400.0) -> dict:
    """给 js_api 用：**只读缓存 + 后台单飞重建**，绝不在这里打网络。

    返回 {date, built_at, items: {指数名: [{code,name,mktcap}]}, pending}。
    缓存过期（跨自然日）仍有旧档 ⇒ 先回旧档 + 后台重建（pending=False）；
    完全无档 ⇒ pending=True（前端显示"名单准备中"）。
    """
    now = _time.time()
    with _lock:
        d = _state.get("data")
        fresh = d is not None and (now - float(_state.get("ts") or 0.0)) < ttl_sec
    if not fresh:
        disk = _read_disk()
        if disk:
            with _lock:
                if not _state.get("data"):
                    _state.update({"data": disk, "ts": now})
            d = disk
    if d is None:
        disk = _read_disk()
        if disk:
            d = disk
    if d is not None and int(d.get("schema") or 0) != _SCHEMA:
        d = None            # 旧 schema（乱码名字）不可信：当无缓存，等后台重建
    # 不完整（缺任一指数）也判过期：旧版本/兜底失败时可能只落了一部分名单，
    # 若只按日期判，缺的那格会**整天不出现**（2026-11-24 真机踩到：上证指数缺失）。
    _missing = bool(d) and any(k not in (d.get("items") or {}) for k, _ in _GM_INDICES)
    stale = (not d) or str(d.get("date") or "") != _today() or _missing
    if stale:
        with _lock:
            if not _state.get("running"):
                _state["running"] = True
                _kick_build()
    if not d:
        return {"date": None, "items": {}, "pending": True}
    # 按固定顺序整理（前端按这个顺序铺格子；缺的指数不补）
    items = {k: d["items"][k] for k in _ORDER if d["items"].get(k)}
    return {"date": d.get("date"), "built_at": d.get("built_at") or "",
            "items": items, "pending": False}
