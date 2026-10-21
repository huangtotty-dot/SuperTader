# -*- coding: utf-8 -*-
"""TencentProvider — 腾讯数据源（合并实施方案 P1-1，降级兜底）。
从现有内联逻辑原样搬迁（fetch_daily_kline / _fetch_index_daily / fetch_minute_bar /
_live_quote_forming），不改变行为，只加契约断言与 attrs["source"] 标记。
缓存格式不变式：t_io/cache/daily_kline/*.json 保持 {date, saved_at, rows:[...]}。
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DAILY_CACHE_DIR = os.path.join(_BASE, "t_io", "cache", "daily_kline")
_MINUTE_CACHE_DIR = os.path.join(_BASE, "t_io", "cache")

_DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]

def _tx_symbol(code: str) -> str:
    """6 位码 → 腾讯符号（sh/sz/bj）。市场判定统一走 codec（本模块此前内联
    `code[0] in "56"`，正是 codec docstring 明令禁止的写法，且完全漏掉北交所）。

    2026-10-17：**已带前缀的代码原样返回**。`codec.to_tx` 只吃裸 6 位码，
    传 `sh000001` 会拼成 `szsh000001`（K线弹窗的指数码就是这种形式）。
    """
    c = str(code).split("_")[0].strip()
    _lo = c.lower()
    if _lo[:2] in ("sh", "sz", "bj") and _lo[2:].isdigit():
        return _lo
    from .codec import to_tx
    return to_tx(c)


def _limit_pct(code) -> float:
    """单日涨跌幅上限（%）：主板 10 / 创业板·科创板 20 / 北交所 30。ST 是 5，按 10 判（保守）。

    ⚠️ 北交所的判定**必须走 codec**：除 4xxxxx/8xxxxx 外还有 **920xxx**（新代码段）。
    2026-10-21 手写 `c[0] in ("4","8")` 漏了 920 ⇒ 把北交所 -16% 的正常波动判成"不可能跳空"，
    全池扫出 3 个假阳性。
    """
    c = str(code).split("_")[0]
    try:
        from .codec import market_of
        if market_of(c) == "BJ":
            return 30.0
    except Exception:
        pass
    if c.startswith(("688", "300", "301")):
        return 20.0
    return 10.0


#: 跳空判据的容差（百分点）：留出四舍五入 / 长期停牌复牌 / 复权小数误差
_GAP_TOL = 0.8


def has_impossible_gap(rows, code, n: int = 3) -> bool:
    """最近 n 根日线里是否有**超过涨跌停的隔夜跳空** ⇒ 该档不可信。永不抛。

    为什么这是判据：A 股单日涨跌幅有硬上限（主板 10% / 创业板·科创板 20% / 北交所 30%），
    而前复权序列会把历史价按除权因子**一并缩放** ⇒ 正常数据不可能出现超过上限的隔夜跳空
    （`open[t] / close[t-1] - 1`）。

    2026-10-21 实证（owner 报「润建股份 10-20 是涨停的，图上却显示跌」）：
    002929 的 10-20 根是**盘中快照**（收 46.10），而 10-20 真实收盘 51.04（涨停）；
    10-21 开 53.12 相对 46.10 是 +15.2% —— 图上一根"跌 0.65%"的阴线，实际是涨停。
    全池扫描：5520 只有 **36 只**命中（都写于 10-21 09:31~09:33 那一批），
    详见 `tmp/` 的排查脚本与 commit。
    """
    try:
        if not rows or len(rows) < 2:
            return False
        # 只看**近期**档：退市/长期停牌票的历史序列（如 000556 的 2002 年数据）不参与判定，
        # 否则它们的旧除权跳空会被永久当成"坏档"，每次读都白重取一次。
        try:
            _last = str(rows[-1].get("date") or "")[:10]
            if _last and _last < (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d"):
                return False
        except Exception:
            pass
        tail = rows[-(n + 1):]
        _lim = _limit_pct(code) + _GAP_TOL
        for i in range(1, len(tail)):
            try:
                pc = float(tail[i - 1]["close"])
                o = float(tail[i]["open"])
            except (KeyError, TypeError, ValueError):
                continue
            if pc <= 0 or o <= 0:
                continue
            if abs(o / pc - 1.0) * 100.0 > _lim:
                return True
        return False
    except Exception:
        return False


def _vol_div(sym) -> float:
    """腾讯按**板块**给成交量不同单位：科创板(688)是**股**，其余是**手**。
    返回归一到「手」所需的除数（688 → 100，其余 → 1）。

    实测 2026-10-20 688008：qt 快照 vol_hand = 24,597,313、m5 十分钟两根 ≈1.46M（都是**股**），
    而 GM/日线缓存是 245,973（**手**）—— 正好 100 倍。不归一的后果：
    科创板的量能/成交额在整个 K 线图上大 100 倍（owner 报「澜起科技量能显然不对」，
    成交额一度画出 2000 亿的量级）。

    ⚠️ 入参可以是 `sh688008` 这种带前缀的符号，也可以是裸 6 位码。
    """
    s = str(sym)
    c = s[-6:] if s[-6:].isdigit() else s
    return 100.0 if c.startswith("688") else 1.0


def _read_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


_IDX_COLS_ = ["date", "open", "high", "low", "close", "volume", "amount"]


def _idx_ensure_cols(df, source):
    """确保指数日线含全部列（旧缓存缺 amount 时退化为 volume，口径自洽）。"""
    for c in _IDX_COLS_:
        if c not in df.columns:
            df[c] = df["volume"] if c == "amount" else 0.0
    df = df[_IDX_COLS_]
    df.attrs["source"] = source
    return df


def save_daily_cache(code: str, df, days: int = 800, forming_date: str = None):
    """写个股日线缓存（不变式 {date, saved_at, rows:[{date,open,close,high,low,volume}]}）。
    gm 结果也写缓存（审核 P1 阻断6：gm 路径不能每次直拉）。

    ⚠️ **不许截短既有历史**（2026-10-12 加）：本函数是**整体覆盖**，调用方只拉了短窗口
    （如 1 个月）时会把长历史**静默**写没。本仓已被这一类咬过两次：
      · `t_io/cache/daily_kline/index_*.json`（见 memory「指数日线缓存会被静默截短」）
      · **600362 个股：801 根 → 21 根** ⇒ 图表 payload 存了 21 根 ⇒ 持仓体检的
        `if n < 30: continue` 把该股**静默丢掉**（owner 报「持仓体检少了江西铜业」）
    既有缓存更长时改走 `merge_daily_cache`（按日期并集，不缩短）—— 语义上严格更安全。
    """
    if df is None or df.empty:
        return
    code = str(code).split("_")[0]
    try:
        _fp0 = os.path.join(_DAILY_CACHE_DIR, f"{code}.json")
        if os.path.exists(_fp0):
            _old = (_read_json(_fp0) or {}).get("rows") or []
            if len(_old) > len(df):
                # 新数据更短 ⇒ 并集而不是覆盖
                merge_daily_cache(code, df, forming_date=forming_date)
                return
    except Exception:
        pass
    recs = []
    for r in df.itertuples():
        recs.append({"date": r.date, "open": float(r.open), "close": float(r.close),
                     "high": float(r.high), "low": float(r.low), "volume": float(r.volume)})
    try:
        os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_DAILY_CACHE_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "forming": [str(forming_date)] if forming_date else [],
                                "rows": recs}, ensure_ascii=False))
    except Exception:
        pass


def merge_daily_cache(code: str, df, cap: int = 800, forming_date: str = None):
    """把 df 的日线**并入**共享缓存（按日期去重合并，不缩短既有历史）。

    与 `save_daily_cache` 的区别——后者是**整体覆盖**：若调用方只拉了 150 根而缓存里
    有 800 根，覆盖会把长历史**截短**（这正是 t_io/cache/daily_kline/index_*.json
    踩过的坑，见 memory「指数日线缓存会被静默截短」）。本函数用于「只想补上当日实时 bar、
    不想动历史」的调用方（如猎手拉完 150 天后回写当日价）。

    ⚠️ 只在**当日**运行里调用：历史日回写会把当天的新数据挤掉。
    """
    if df is None or df.empty:
        return
    code = str(code).split("_")[0]
    fp = os.path.join(_DAILY_CACHE_DIR, f"{code}.json")
    try:
        _old = _read_json(fp) if os.path.exists(fp) else {}
        old_rows = (_old.get("rows") or [])
        old_forming = {str(x) for x in (_old.get("forming") or [])}
        in_dates = {str(r.date) for r in df.itertuples() if str(r.date)}
        by_date = {}
        for r in old_rows:
            d = str(r.get("date") or "")
            if not d:
                continue
            # 2026-10-21：**未被真数据确认的"盘中快照根"不许被复制到未来**。
            # 事故（002929）：10-20 盘中的快照根（收 46.10）在后续 merge 里被当成 10-20 的
            # 历史一路带下去，而 10-20 真实是涨停 51.04 ⇒ K 线图上那根显示"跌 0.65%"。
            # 判据：旧档把它标成 forming，而这次的真数据里**没有这一天** ⇒ 宁可缺一根也不留错值。
            if d in old_forming and d not in in_dates:
                continue
            by_date[d] = r
        for r in df.itertuples():
            d = str(r.date)
            if not d:
                continue
            by_date[d] = {"date": d, "open": float(r.open), "close": float(r.close),
                          "high": float(r.high), "low": float(r.low), "volume": float(r.volume)}
        rows = [by_date[k] for k in sorted(by_date)]
        if cap and len(rows) > cap:
            rows = rows[-cap:]
        # 旧标记一律作废：没被确认的已被丢弃（上面 continue），被确认的已用真数据覆盖。
        # 本次若又追加了"当日快照根"，由调用方通过 forming_date 重新标上。
        forming = {str(forming_date)} if forming_date else set()
        os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "forming": sorted(forming),
                                "rows": rows}, ensure_ascii=False))
    except Exception:
        pass


def save_index_daily_cache(index: str, df):
    """写指数日线缓存（含 amount 列）。"""
    if df is None or df.empty:
        return
    idx = str(index).lower()
    recs = [{"date": r.date, "open": float(r.open), "close": float(r.close),
             "high": float(r.high), "low": float(r.low), "volume": float(r.volume),
             "amount": float(r.amount)} for r in df.itertuples()]
    try:
        os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_DAILY_CACHE_DIR, f"index_{idx}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"rows": recs}, ensure_ascii=False))
    except Exception:
        pass


def _clear_proxy():
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "*"


# 单只快照的 socket 超时。2026-10-12 由 8s 收紧到 2s：
# `facade.daily()` 盘中会对**每只**额外调一次本函数补当日 bar，实测中位仅 0.232s，
# 但 3/40 会**打满**旧超时值（8.25/8.25/8.22s，正是 8s+开销）⇒ 整条 daily() 被腾讯这一步拖住
# （40 只耗时分解：GM 合计 2.4s、本步 34.0s，**93%** 在这）。打满=请求本就在挂，
# 收紧只是**更快失败**（失败路径同样是"不补当日 bar"，语义不变），不会丢原本能拿到的数据。
_QT_SNAPSHOT_TIMEOUT = 2

# 批量实时快照（`snapshot_auction` / `index_auction`，qt.gtimg.cn）的 socket 超时。
# 2026-10-12 由 8s 收紧到 3s：同一现象已实测两次 —— **批量**请求也会偶发挂满 ~8.2s
# （实测 n=200 那次耗时 8.33s，但数据其实回来了；n=50/400 只要 0.29/0.56s）
# ⇒ 8s 不是"等待合法慢响应"，而是把"偶发挂死"放大成"阻塞 8 秒"。
# 影响面：`load_quotes`（10s 轮询）+ 猎手展开取实时价 + 指数板，全都吃这条尾巴；
# 前端在飞上限只有 6 ⇒ 一次 8.5s 就够把整块界面拖慢（实测 gui_slow_calls.log:
# `load_quotes_for 8639ms / 8496ms`）。成功路径 0.3s，3s 有 10 倍余量。
_QT_AUCTION_TIMEOUT = 3

# 单次批量快照的 symbol 上限。⚠️ qt.gtimg.cn 的 URL 一长就**静默返回空 body**（不是报错）——
# 实测悬崖：n=800（URL≈12000 字符）正常返 800 条；**n=1000（≈15000 字符）返回 0 条**，耗时 0.2s。
# 200 与 `sector_rotation.SNAPSHOT_BATCH` / `hunter MAX_CODES_PER_REQUEST` 同口径。
_SNAPSHOT_CHUNK = 200


# ── qt.gtimg.cn 连接复用（2026-10-16）────────────────────────────────────────
# **为什么必须做**：本模块原先每个请求都走 `urllib.request.urlopen`，它**每次都新建**
# DNS + TCP + TLS。本机实测**建连本身 ≈290ms**（同一 URL 连打 12 次：裸 urlopen 中位
# 350.9ms，复用同一连接中位 61.4ms ⇒ **5.7×**）。而 `snapshot()` 是**逐只**调的
# （`t_gui._live_quote_forming` → `snapshot([code])`）⇒ 一屏 50 只票 = **50 次握手 ≈14.5s**，
# 全部落在后台线程上。2026-10-16 的卡死线程栈里**实测 5 个线程同时卡在
# `socket.create_connection`**（另有 1 个在读 TLS），这就是"打开 GUI 卡顿"的直接来源。
#
# **为什么用 stdlib 而不是 requests**：本模块会被 **gm 策略进程**里的
# `core.market_data.facade` 加载（盘前预筛走它），不能给策略进程引入新依赖；
# 且 raw `http.client` 天然不走环境代理，等价于既有的 `_clear_proxy()`。
_QT_HOST = "qt.gtimg.cn"
_QT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/",
               "Connection": "keep-alive"}
_QT_POOL_MAX = 8                 # 空闲连接上限（并发峰值只有个位数，8 足够且不堆积 fd）

# 2026-10-17：分钟 K 线（`ifzq.gtimg.cn`）走同一套 keep-alive。盘中 K 线弹窗每 3s 打一次，
# 每次新建 TLS 实测 ≈290ms（见上方 qt 的测量）；复用后 ≈60ms。
_IFZQ_HOST = "ifzq.gtimg.cn"
_WEBIFZQ_HOST = "web.ifzq.gtimg.cn"      # fqkline（日线）只在这个主机可用，ifzq 返 501
_IFZQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/",
                 "Connection": "keep-alive"}
_IFZQ_POOL_MAX = 4

# ⚠️ 连接池**必须按主机分桶**：keep-alive 连接绑死在建连时的 host 上，混用会把
# `web.ifzq` 的请求发到 `ifzq`（实测 fqkline 在 ifzq 上返 501 ⇒ 静默拿空）。
# 池实现 2026-10-20 起统一到 `core/market_data/http_pool.py`（全仓库共享），这里只留薄封装，
# 保留 `_QT_POOL` 等既有名字（测试与 tmp 探针直接引用）。
from . import http_pool as _hp


def _host_pool(host):
    """取/建某主机的连接池记录 {pool, lock, max}。qt=8 路、其余=4 路。"""
    return _hp.host_pool(host, _QT_POOL_MAX if host == _QT_HOST else _IFZQ_POOL_MAX)


# 兼容既有名字（测试直接 clear 这个列表）
_QT_POOL = _host_pool(_QT_HOST)["pool"]
_QT_POOL_LOCK = _host_pool(_QT_HOST)["lock"]
_IFZQ_POOL = _host_pool(_IFZQ_HOST)["pool"]
_IFZQ_POOL_LOCK = _host_pool(_IFZQ_HOST)["lock"]


def _conn_take(host, pool, lock, timeout):
    return _hp.conn_take(host, pool, lock, timeout)


def _conn_put(pool, lock, conn, max_idle):
    _hp.conn_put(pool, lock, conn, max_idle)


def _pooled_get(host, path, timeout, headers):
    """GET `https://{host}{path}` → 文本（gbk 解码）。**按主机复用连接**，失败抛异常。"""
    _host_pool(host)   # 确保按正确 max 分桶（qt=8 / 其余=4）后再委托
    return _hp.pooled_get(host, path, timeout, headers, encoding="gbk", errors="replace")


def _qt_conn_take(timeout):
    return _conn_take(_QT_HOST, _QT_POOL, _QT_POOL_LOCK, timeout)


def _qt_conn_put(conn):
    _conn_put(_QT_POOL, _QT_POOL_LOCK, conn, _QT_POOL_MAX)


def _qt_get(path, timeout):
    return _pooled_get(_QT_HOST, path, timeout, _QT_HEADERS)


def _ifzq_get(path, timeout=4.0):
    """GET `https://ifzq.gtimg.cn{path}`（分钟 K 线专用），复用连接。"""
    return _pooled_get(_IFZQ_HOST, path, timeout, _IFZQ_HEADERS)


def _qt_snapshot_raw(symbol: str):
    """腾讯 qt.gtimg.cn 实时快照 → fields 列表；失败返回 None。"""
    _clear_proxy()
    raw = _qt_get(f"/q={symbol}", _QT_SNAPSHOT_TIMEOUT)
    if "~" not in raw:
        return None
    f = raw.split('"')[1].split("~")
    return f


# ── 分钟 K 线 / 裸日线（2026-10-17，盘中实时 K 线专用）───────────────────────
_MINUTE_KLINE_COLS = ["time", "open", "high", "low", "close", "volume"]
_MKLINE_FREQS = ("m1", "m5", "m15", "m30", "m60")     # 腾讯无 m10（10min 由 m5 合成）

# 单次最多 640 根；>640 会被**静默降回 320**（实测 n=1000/2000 → 320 根）。
_MINUTE_KLINE_MAX = 640


def minute_kline(code: str, freq: str = "m5", count: int = _MINUTE_KLINE_MAX,
                 timeout: float = 4.0) -> pd.DataFrame:
    """腾讯**分钟 K 线**（含未收盘的当前根，盘中随行情更新）→ 规范帧。

    为什么需要它（2026-10-17）：tushare `stk_mins` **盘中只回到上一交易日收盘**
    （实测 2026-10-14 14:13 取回来仍止于 10-13 15:00，见 `position_structure_feed`
    的冷却注释）⇒ 分钟 K 线在盘中是**死的**。本源是 K 线弹窗盘中实时的唯一数据源。

    实测口径（周末回放，个股/指数/ETF 均可，北交所返回空）：
      · 字段 `[时间, 开, 收, 高, 低, 量]`，**时间是 K 线右端**（10:05 的 5min 覆盖 10:00~10:05）
      · m5 每日首根 09:35（**不含 09:30 竞价棒**）、末根 15:00；m30/m60 桶边界与 tushare 一致
      · 量为**手**（与日线缓存/tushare 分钟同口径）
      · 价格为**不复权**（与 tushare 分钟一致；日线用的是前复权，除权窗口内会不同）

    失败 / 空 → 空 DataFrame（调用方回退 tushare 或磁盘）。
    """
    if freq not in _MKLINE_FREQS:
        return pd.DataFrame(columns=_MINUTE_KLINE_COLS)
    sym = _tx_symbol(code)
    if not sym:
        return pd.DataFrame(columns=_MINUTE_KLINE_COLS)
    n = max(1, min(int(count or _MINUTE_KLINE_MAX), _MINUTE_KLINE_MAX))
    try:
        raw = _ifzq_get(f"/appstock/app/kline/mkline?param={sym},{freq},,{n}", timeout)
        data = json.loads(raw)
        node = ((data or {}).get("data") or {}).get(sym) or {}
        rows = node.get(freq) or []
        if not isinstance(rows, list):
            return pd.DataFrame(columns=_MINUTE_KLINE_COLS)
    except Exception:
        return pd.DataFrame(columns=_MINUTE_KLINE_COLS)

    out = []
    for r in rows:
        try:
            t = str(r[0])
            if len(t) < 12 or not t[:12].isdigit():
                continue
            out.append({
                "time": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:00",
                "open": float(r[1]), "high": float(r[3]), "low": float(r[4]),
                "close": float(r[2]), "volume": float(r[5] or 0),
            })
        except (TypeError, ValueError, IndexError):
            continue
    if not out:
        return pd.DataFrame(columns=_MINUTE_KLINE_COLS)
    df = pd.DataFrame(out)[_MINUTE_KLINE_COLS]
    # 2026-10-20：科创板(688)腾讯按**股**返回成交量，其余按**手** ⇒ 统一归一到手。
    # 实测 688008 当日：qt 快照 vol_hand=24,597,313（股）而日线缓存是 245,973（手），
    # 正好 100 倍；m5 十分钟两根合计 ≈1.46M 也印证是股。
    # 不归一的后果：K 线量能窗口上科创板比别的票大 100 倍（owner 报「澜起科技量能显然不对」）。
    _vd = _vol_div(sym)          # ⚠️ sym 带市场前缀（sh688008），_vol_div 内部已兼容
    if _vd != 1.0:
        df["volume"] = df["volume"] / _vd
    df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    df.attrs["source"] = "tencent_minute"
    return df


def daily_kline_raw(code: str, days: int = 5, qfq: bool = True,
                    timeout: float = 4.0) -> pd.DataFrame:
    """腾讯**前复权日线**原样取数（不读写磁盘缓存），最后一根=当日 forming bar（盘中）。

    与 `TencentProvider.daily()` 的区别：后者受磁盘缓存与 `date==today` 闸影响，盘中拿到的
    可能是上一次调用时的快照。本函数**只服务 K 线弹窗的实时补丁**（每 3~10s 一次），
    要求每次都是**此刻**的当日根。

    返回 columns=[date, open, high, low, close, volume]；失败/空 → 空 DataFrame。
    ⚠️ 用 `web.ifzq.gtimg.cn`：`ifzq.gtimg.cn` 的 fqkline 实测返回 **HTTP 501**。
    """
    sym = _tx_symbol(code)
    if not sym:
        return pd.DataFrame(columns=_DAILY_COLS)
    suffix = ",qfq" if qfq else ","
    path = f"/appstock/app/fqkline/get?param={sym},day,,,{int(days)}{suffix}"
    try:
        raw = _pooled_get(_WEBIFZQ_HOST, path, timeout, _IFZQ_HEADERS)
        data = json.loads(raw)
        node = ((data or {}).get("data") or {}).get(sym) or {}
        kline = node.get("day") or node.get("qfqday") or []
        _vd = _vol_div(sym)          # 688 的成交量腾讯按股返回 ⇒ 归一到手
        rows = [{"date": str(i[0])[:10], "open": float(i[1]), "close": float(i[2]),
                 "high": float(i[3]), "low": float(i[4]), "volume": float(i[5] or 0) / _vd}
                for i in kline if len(i) >= 6]
    except Exception:
        return pd.DataFrame(columns=_DAILY_COLS)
    if not rows:
        return pd.DataFrame(columns=_DAILY_COLS)
    df = pd.DataFrame(rows)[_DAILY_COLS]
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    df.attrs["source"] = "tencent"
    return df


# ── 标的搜索（名称/代码 → 标的；2026-10-18，手动盘「K线速查」）────────────────
_SEARCH_HOST = "smartbox.gtimg.cn"
_SEARCH_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/",
                   "Connection": "keep-alive"}
_SEARCH_POOL_MAX = 2


def search_instruments(query: str, limit: int = 12, timeout: float = 4.0) -> list:
    """腾讯 smartbox 按**名称/拼音/代码**搜索标的 → [{market, code, name, type}]。

    实测（2026-10-18）返回 `v_hint="sh~600362~江西铜业~jxty~GP-A^hk~00358~..."`：
      · `type`：`GP-A`=A股、`GP`=港美股、`ZS`=指数、`ETF`/`LOF`=场内基金、`KJ`=场外基金
      · 无结果 → `v_hint="N"`
      · 名字是 `\\uXXXX` 转义，必须 `json.loads` 反转义，否则界面上是乱码
    失败/空 → []（调用方再退回本地表）。**不做过滤**：是否可画 K 线由调用方决定。
    """
    q = str(query or "").strip()
    if not q:
        return []
    try:
        path = f"/s3/?v=2&q={urllib.parse.quote(q)}&t=all&c=1"
        raw = _pooled_get(_SEARCH_HOST, path, timeout, _SEARCH_HEADERS)
        body = raw.split("=", 1)[1].strip() if "=" in raw else ""
        if not body or body in ('"N"', "N"):
            return []
        text = json.loads(body)          # 反转义 \uXXXX
    except Exception:
        return []
    out = []
    for item in str(text).split("^"):
        parts = item.split("~")
        if len(parts) < 5 or not parts[1]:
            continue
        out.append({"market": parts[0].lower(), "code": parts[1], "name": parts[2],
                    "pinyin": parts[3], "type": parts[4]})
        if len(out) >= limit:
            break
    return out


def qt_vwap(quote: dict):
    """由 qt.gtimg.cn 快照字典算**当日 VWAP**（元/股，2 位）—— 口径 = 成交额 ÷ 成交量。

    入参 = `snapshot_auction` / `snapshot_auction_many` 的**单只**值
    （用到 `vol_hand`(f[6]) / `amount_wan`(f[37]) / `low`(f[34]) / `high`(f[33])）。

    ⚠️⚠️ **成交量单位按板块不同，差 100 倍**（2026-10-16 实测 7 只全命中）：

    | 板块 | `f[6]` 单位 | 公式 |
    |---|---|---|
    | 主板 / 创业板 / ETF | **手** | `成交额 ÷ (f[6] × 100)` |
    | **科创板 `688xxx`** | **股** | `成交额 ÷ f[6]` |

    实测：`600362` 手口径 39.438（akshare 39.44 ✅）、`515120` 0.609（0.61 ✅）、
    `688008` **股**口径 169.229（akshare 169.23 ✅ 分毫吻合）、`688111` 230.289（230.29 ✅）。

    **判据刻意不用代码前缀**（前缀表迟早漏板块），改用**不变量**：VWAP 是当日成交价的
    加权平均 ⇒ **必落在当日 [low, high] 内**；两个候选差 100 倍 ⇒ 至多一个在区间内。
    `low`/`high` 不可用时退回"手"口径（常见情形，且这是下面分支的默认）。

    返回 `None` = **算不出来**（量或额非正 / 两个候选都越界）⇒ **调用方不得写缓存**，
    与旧 akshare 实现 `mask=(vol>0)&(amt>0)` 同语义：**宁缺勿假**。
    """
    try:
        vh = float(quote.get("vol_hand") or 0)
        aw = float(quote.get("amount_wan") or 0)
        if not (vh > 0 and aw > 0):
            return None
        amount = aw * 10000.0            # 万元 → 元
        lots = amount / (vh * 100.0)     # f[6] 当"手"
        shares = amount / vh             # f[6] 当"股"
        lo = float(quote.get("low") or 0)
        hi = float(quote.get("high") or 0)
        if lo > 0 and hi > 0:
            # 两个候选相差 100 倍 ⇒ 同时落进当日区间的概率可忽略；真出现时取"手"（多数板块）
            if lo <= lots <= hi:
                return round(lots, 2)
            if lo <= shares <= hi:
                return round(shares, 2)
            return None
        return round(lots, 2)
    except Exception:
        return None


class TencentProvider:
    source = "tencent"

    # ---------- 日线 ----------
    def daily_cache(self, code: str):
        """读个股日线缓存（每日 + 盘中 15 分钟新鲜度）。命中返回 df(source=cache)，否则 None。
        供 facade gm 路径 cache-first 快读（审核残留建议，削减 10× 负载）。"""
        code = str(code).split("_")[0]
        cache_fp = os.path.join(_DAILY_CACHE_DIR, f"{code}.json")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        try:
            os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        except Exception:
            return None
        if not os.path.exists(cache_fp):
            return None
        try:
            cached = _read_json(cache_fp)
            if not (cached.get("date") == _today and cached.get("rows")):
                return None
            rows = cached["rows"]
            _last_date = str(rows[-1].get("date", "")) if rows else ""
            _saved_at = cached.get("saved_at")
            try:
                _ts = datetime.strptime(_saved_at, "%Y-%m-%d %H:%M:%S") if _saved_at \
                    else datetime.fromtimestamp(os.path.getmtime(cache_fp))
            except Exception:
                _ts = datetime.fromtimestamp(os.path.getmtime(cache_fp))
            _stale = ((_last_date < _today and _now.strftime("%H:%M") >= "09:15")
                      or (_last_date == _today and (_now - _ts).total_seconds() > 15 * 60))
            if _stale:
                return None
            # 2026-10-21：**历史日上还挂着"盘中快照"标记** ⇒ 那根从未被真数据确认过
            # （见 `save_daily_cache(forming_date=…)`）⇒ 整档不可信，逼上层重新取数。
            # 取数成功后该标记会随真数据一起被清掉（merge 里 `old_forming ∩ incoming`）。
            if any(str(d) < _today for d in (cached.get("forming") or [])):
                return None
            # 2026-10-21：**不可能存在的隔夜跳空** ⇒ 档里有半成品根（002929 事故），重取。
            if has_impossible_gap(rows, code):
                return None
            df = pd.DataFrame(rows)[_DAILY_COLS]
            df.attrs["source"] = "cache"
            return df
        except Exception:
            return None

    def daily(self, code: str, days: int = 800, min_rows: int = 0) -> pd.DataFrame:
        """腾讯日线（前复权 qfq），带本地缓存（t_io/cache/daily_kline/{code}.json）。

        `min_rows`（2026-10-20）：缓存行数不足时**强制走网络**拉长历史。
        修复「日线 K 线只显示一部分」——扫描类短窗口（_RC_BARS=30）会给没有旧缓存的票
        写入 30~40 根，而 `daily_cache` 只按新鲜度判命中 ⇒ 图表永远只画那一小段。
        """
        cached = self.daily_cache(code)
        if cached is not None and (not min_rows or len(cached) >= int(min_rows)):
            return cached
        code = str(code).split("_")[0]
        symbol = _tx_symbol(code)
        # P0 修复(2026-08-31): daily() 写缓存/回退旧缓存用了 cache_fp/_now/_today 但未定义 →
        # gm 降级腾讯路径抛 NameError → 手动盘盘后重跑"无输出"。补齐定义（与 daily_cache 同路径/时间口径）。
        cache_fp = os.path.join(_DAILY_CACHE_DIR, f"{code}.json")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        _clear_proxy()  # 审核 #7: daily 路径代理清除恢复
        for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
            try:
                url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Referer": "https://finance.qq.com/"})
                raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
                data = json.loads(raw)
                kline = data.get("data", {}).get(symbol, {}).get("day") or \
                        data.get("data", {}).get(symbol, {}).get("qfqday") or []
                _vd = _vol_div(symbol)   # 688 的成交量腾讯按股返回 ⇒ 归一到手
                rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                         "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]) / _vd}
                        for i in kline if len(i) >= 6]
                if not rows:
                    continue
                df = pd.DataFrame(rows)[_DAILY_COLS]
                df.attrs["source"] = "tencent"
                # 2026-10-21：**走 `save_daily_cache`，不在这里裸写**。
                # 裸写没有任何"不许截短"的闸：调用方只要传了短窗口（如 `daily(code, 30)`）
                # 且在缓存未命中时走到这里，就会把 800 行历史静默截成 30 行 —— 与本仓
                # 「指数日线缓存会被静默截短」同一类事故（个股侧也已咬过：600362 801→21 根）。
                # `save_daily_cache` 在新数据更短时自动改走 `merge_daily_cache`（按日期并集）。
                save_daily_cache(code, df)
                return df
            except Exception:
                continue

        # 全部主机失败 → 回退旧缓存 + 补当日 forming bar（防破均线误判，08-28 教训）
        if cache_fp and os.path.exists(cache_fp):
            try:
                cached = _read_json(cache_fp)
                if cached.get("rows"):
                    rows = cached["rows"]
                    live = self.snapshot([code]).get(code)
                    if live and str(live.get("ts_date")) == _today and live.get("price"):
                        # F-6(2026-09-04): 防重复日期行——缓存末行已是 today 则不追加（实测 347 行含两行 09-04）
                        if not rows or str(rows[-1].get("date", "")) != _today:
                            rows = rows + [{"date": _today, "open": live["open"], "close": live["price"],
                                            "high": live["high"], "low": live["low"],
                                            "volume": live["volume"]}]
                    df = pd.DataFrame(rows)[_DAILY_COLS]
                    df.attrs["source"] = "cache"
                    return df
            except Exception as _e:
                # F-6(2026-09-04): 兜底异常不再裸吞——打日志可观测（588170 抖动即空特征的排查源）
                print(f"[WARN] tencent.daily 缓存兜底失败({code}): {type(_e).__name__}: {str(_e)[:80]}", flush=True)
        return pd.DataFrame(columns=_DAILY_COLS)

    # ---------- 指数日线 ----------
    _IDX_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        """指数日线（腾讯 qfq，OHLCV），缓存 t_io/cache/daily_kline/index_{index}.json。
        end_date 给定时按该日截止（回测/历史 regime 用）。"""
        idx = str(index).lower()
        cache_fp = os.path.join(_DAILY_CACHE_DIR, f"index_{idx}.json")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        _end = end_date or _today
        cached_rows = None
        if end_date is None and os.path.exists(cache_fp):
            try:
                d = _read_json(cache_fp)
                if d.get("rows"):
                    cached_rows = d["rows"]
                    cache_date = str(cached_rows[-1].get("date", ""))
                    need_refresh = (_now.weekday() < 5 and _now.strftime("%H:%M") >= "09:15"
                                    and cache_date < _today)
                    if cached_rows and not need_refresh:
                        return _idx_ensure_cols(pd.DataFrame(cached_rows), "cache")
            except Exception:
                pass
        symbol = idx.replace("sh", "sh").replace("sz", "sz")
        _clear_proxy()  # 审核 #7: index_daily 路径代理清除恢复
        for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
            try:
                url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Referer": "https://finance.qq.com/"})
                raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                data = json.loads(raw)
                kline = data.get("data", {}).get(symbol, {}).get("day") or \
                        data.get("data", {}).get(symbol, {}).get("qfqday") or []
                _vd = _vol_div(symbol)   # 688 归一到手（指数恒为 1，不影响）
                rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                         "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]) / _vd,
                         "amount": float(i[6]) if len(i) >= 7 and str(i[6]).replace(".", "").isdigit() else float(i[5])}
                        for i in kline if len(i) >= 6]
                if end_date:  # 审核 #8: 历史查询按 end_date 截止（此前静默退化）
                    rows = [r for r in rows if r["date"] <= end_date]
                if not rows:
                    continue
                if end_date is None:
                    os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
                    with open(cache_fp, "w", encoding="utf-8") as f:
                        f.write(json.dumps({"rows": rows}, ensure_ascii=False))
                return _idx_ensure_cols(pd.DataFrame(rows), "tencent")
            except Exception:
                continue
        if cached_rows:
            return _idx_ensure_cols(pd.DataFrame(cached_rows), "cache")
        return pd.DataFrame(columns=self._IDX_COLS)

    # ---------- 分钟线 ----------
    def minute_cache(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        """只读分钟 CSV 缓存。命中返回 df(source=cache)；超龄或缺失返回空 df。"""
        code = str(code).split("_")[0]
        cache_fp = os.path.join(_MINUTE_CACHE_DIR, f"minute_{code}_{date}.csv")
        if not os.path.exists(cache_fp):
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
        if ttl_seconds is not None:
            try:
                if (datetime.now().timestamp() - os.path.getmtime(cache_fp)) > ttl_seconds:
                    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
            except Exception:
                pass
        try:
            df = pd.read_csv(cache_fp)
            if df.empty or "time" not in df.columns:
                return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
            t = df["time"].astype(str).str.strip()
            mask = ~t.str.match(r"^\d{4}-\d{2}-\d{2}")
            if mask.any():
                t = t.mask(mask, date + " " + t)
            df["time"] = pd.to_datetime(t, errors="coerce")  # datetime64（与 gm/腾讯输出一致）
            _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
            df = df.reindex(columns=[c for c in _cols if c in df.columns])
            df.attrs["source"] = "cache"
            return df
        except Exception:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])

    def save_minute_cache(self, code: str, date: str, df: pd.DataFrame):
        try:
            code = str(code).split("_")[0]
            os.makedirs(_MINUTE_CACHE_DIR, exist_ok=True)
            df.to_csv(os.path.join(_MINUTE_CACHE_DIR, f"minute_{code}_{date}.csv"), index=False, encoding="utf-8")
        except Exception:
            pass

    def minute(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        """当日 1 分钟线（腾讯 minute 接口），CSV 缓存（t_io/cache/minute_{code}_{date}.csv）。
        ttl_seconds 给定时缓存超龄视为 miss（盘中避免返回陈旧分钟数据）。"""
        code = str(code).split("_")[0]
        # 缓存命中（TTL 超龄视为 miss）→ 直接返回
        _cached = self.minute_cache(code, date, ttl_seconds)
        if not _cached.empty:
            return _cached
        symbol = _tx_symbol(code)
        last_error = None
        _clear_proxy()  # 审核 #7: minute 路径代理清除恢复
        for _ in range(3):
            try:
                url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://finance.qq.com/"})
                content = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                if not content.strip() or "<html" in content.lower():
                    last_error = "empty_or_html"
                    continue
                data = json.loads(content)
                if data.get("code") != 0 or not data.get("data"):
                    last_error = "api_empty"
                    continue
                minute_data = data.get("data", {}).get(symbol)
                if not minute_data:
                    last_error = "symbol_missing"
                    continue
                # P1 审核阻断3：校验响应实际日期，周末/节假日会把上一交易日伪造成当日 → 不符即空
                _pack = minute_data.get("data")
                resp_date = (str(_pack.get("date") or "") if isinstance(_pack, dict)
                             else str(minute_data.get("date") or ""))
                if resp_date and len(resp_date) >= 8:
                    rd = f"{resp_date[:4]}-{resp_date[4:6]}-{resp_date[6:8]}"
                    if rd != str(date):
                        last_error = f"resp_date_mismatch({rd} != {date})"
                        continue
                rows = minute_data.get("data") or minute_data.get("day") or []
                if isinstance(rows, dict):
                    rows = rows.get("data") or []   # 实际列表在 data.data[symbol].data.data 下
                bars = []
                for row in rows:
                    parts = row.split() if isinstance(row, str) else \
                        ([str(x) for x in row] if isinstance(row, list) else None)
                    if not parts:
                        continue
                    if len(parts) >= 6:
                        tm, o, c, h, l, v = (parts[0], float(parts[1]), float(parts[2]),
                                             float(parts[3]), float(parts[4]), float(parts[5]))
                        amt = float(parts[6]) if len(parts) > 6 else 0.0
                    elif len(parts) >= 4:
                        # 腾讯基础分钟接口仅 time close vol amount → OHLC 同价派生（与原实现一致）
                        tm, c, v, amt = (parts[0], float(parts[1]), float(parts[2]), float(parts[3]))
                        o = h = l = c
                    else:
                        continue
                    tm = str(tm).strip()
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        # time 用 datetime64：signal_engine 按时间切片依赖 datetime 比较与算术
                        bars.append({"time": pd.to_datetime(f"{date} {tm[:2]}:{tm[2:]}:00"),
                                     "open": o, "high": h, "low": l, "close": c,
                                     "volume": v, "amount": amt})
                if bars:
                    df = pd.DataFrame(bars)
                    self.save_minute_cache(code, date, df)
                    df.attrs["source"] = "tencent"
                    return df
                last_error = "no_bars"
            except Exception as e:
                last_error = str(e)[:60]
        _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
        return pd.DataFrame(columns=_cols)

    # ---------- 指数分时（累计量差分还原） ----------
    def index_minute(self, index: str = "sh000001") -> pd.DataFrame:
        """指数当日分时（腾讯 minute/query，rows=[HHMM price cum_vol(手) cum_amount(元)]）。
        累计量差分还原为每分钟量；gm 无此数据形态，指数分钟保留腾讯（P1-2 #6 记录在案）。
        返回 {time(datetime64), open, high, low, close, volume(手), amount(元)}。"""
        code = str(index).strip()
        _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
        try:
            _clear_proxy()
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"})
            content = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
            data = json.loads(content)
            node = data.get("data", {}).get(code) or {}
            pack = node.get("data") or {}
            if isinstance(pack, list):
                rows, day_str = pack, ""
            else:
                rows = pack.get("data") or []
                day_str = str(pack.get("date") or "")
            if not rows:
                return pd.DataFrame(columns=_cols)
            if not day_str:
                day_str = datetime.now().strftime("%Y%m%d")
            day_fmt = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:8]}"
            parsed = []
            prev_v, prev_a = 0.0, 0.0
            for row in rows:
                parts = row.split() if isinstance(row, str) else [str(x) for x in row]
                if len(parts) < 4:
                    continue
                hm = str(parts[0]).strip().zfill(4)
                price = float(parts[1])
                cum_v, cum_a = float(parts[2]), float(parts[3])
                # 审核 #6: 差分还原 → 股（×100 手→股），与 akshare 主通道 vwap 口径一致（Σamount/Σvolume=元/股）
                v = max(cum_v - prev_v, 0.0) * 100.0
                a = max(cum_a - prev_a, 0.0)
                prev_v, prev_a = cum_v, cum_a
                parsed.append({"time": pd.to_datetime(f"{day_fmt} {hm[:2]}:{hm[2:]}:00"),
                               "open": price, "high": price, "low": price, "close": price,
                               "volume": v, "amount": a})
            if not parsed:
                return pd.DataFrame(columns=_cols)
            df = pd.DataFrame(parsed)
            df.attrs["source"] = "tencent"
            return df.sort_values("time").reset_index(drop=True)
        except Exception:
            return pd.DataFrame(columns=_cols)

    # ---------- 实时快照 ----------
    def snapshot(self, codes: list) -> dict:
        """腾讯实时快照 → {code: {price, open, high, low, volume(手), ts_date}}。"""
        out = {}
        if not codes:
            return out
        for code in codes:
            base = str(code).split("_")[0]
            symbol = _tx_symbol(base)
            try:
                f = _qt_snapshot_raw(symbol)
                if not f or len(f) < 35:
                    continue
                price = float(f[3])
                if price <= 0:
                    continue
                ts = f[30] if len(f) > 30 else ""
                _tsd = ts[:8] if len(ts) >= 8 and ts[:8].isdigit() else None
                ts_date = (f"{_tsd[:4]}-{_tsd[4:6]}-{_tsd[6:8]}" if _tsd else None)
                def _f(i, d=0.0):
                    try:
                        return float(f[i]) if f[i] else d
                    except (ValueError, IndexError):
                        return d
                out[base] = {"price": price, "open": _f(5), "high": _f(33), "low": _f(34),
                             "volume": _f(6), "ts_date": ts_date}
            except Exception:
                continue
        return out

    # ---------- 竞价快照（唯一例外：gm 无虚拟匹配价，保留腾讯） ----------
    def snapshot_auction(self, codes: list) -> dict:
        """竞价/实时快照（qt.gtimg.cn 批量，字段[3]=虚拟匹配价/现价）。
        gm 无虚拟匹配价概念，竞价专用保留腾讯（合并实施方案 P1-2 #9）。
        返回 {code: {name, price, pre_close, open, high, low, vol_hand, amount_wan, ts_raw, ts_date}}。"""
        out = {}
        if not codes:
            return out
        syms = []
        for c in codes:
            b = str(c).split("_")[0]
            syms.append(_tx_symbol(b))
        try:
            _clear_proxy()
            q = ",".join(syms)
            txt = _qt_get(f"/q={q}", _QT_AUCTION_TIMEOUT)
            for part in txt.strip().split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                key, _, payload = part.partition("=")
                code = key.strip().lstrip("v_").upper()[2:]
                f = payload.strip().strip('"').split("~")
                if len(f) < 38:
                    continue
                def _f(i, d=0.0):
                    try:
                        return float(f[i]) if f[i] else d
                    except (ValueError, IndexError):
                        return d
                ts = f[30] if len(f) > 30 else ""
                _tsd = ts[:8] if len(ts) >= 8 and ts[:8].isdigit() else None
                ts_date = (f"{_tsd[:4]}-{_tsd[4:6]}-{_tsd[6:8]}" if _tsd else None)
                out[code] = {"name": f[1] if len(f) > 1 else code,
                             "price": _f(3), "pre_close": _f(4), "open": _f(5) or None,
                             "high": _f(33), "low": _f(34), "vol_hand": _f(6),
                             "amount_wan": _f(37), "pct": _f(32),
                             "turnover": _f(38), "amplitude": _f(43),
                             "limit_up": _f(47), "limit_down": _f(48), "vol_ratio": _f(49),
                             "ts_raw": ts, "ts_date": ts_date}
        except Exception:
            pass
        return out

    def snapshot_auction_many(self, codes: list, chunk_size: int = None) -> dict:
        """**分块**批量实时快照（2026-10-12）。为什么必须有：

        `snapshot_auction` 把全部 symbol 拼进一个 URL，**URL 一长就被静默拒绝** ——
        返回空 body，函数里 `except: pass` ⇒ 表现为**空 dict**，调用方完全看不出出错。
        实测悬崖：n=800（≈12000 字符）正常，**n=1000（≈15000 字符）返回 0 条**。

        而受伤的是**猎手盘中 live patch**：它传的是**整池**（1200~1500 只）⇒ 常年 patch 到 **0 只**，
        扫描的价/涨跌幅只能取日线历史，而日线盘中不含当日 bar ⇒ **整块面板退回上一交易日**
        （实测：300821 显示 10-09 的 +20.01%，而当天只有 +4.6%）。
        `facade._append_forming_batch` 按 900 只/批，也贴着同一悬崖。

        本函数分块调用并合并。**失败可见**：把空块数记在 `self.last_chunk_failures`，
        由调用方打印（不用 logging —— 本仓实测 `core.market_data.*` 的 log 到不了主日志）。
        """
        out = {}
        codes = [c for c in dict.fromkeys(codes or []) if c]
        self.last_chunk_failures = 0
        if not codes:
            return out
        n = int(chunk_size or _SNAPSHOT_CHUNK)
        for i in range(0, len(codes), n):
            chunk = codes[i:i + n]
            try:
                got = self.snapshot_auction(chunk) or {}
            except Exception:
                got = {}
            if not got:
                self.last_chunk_failures += 1      # 空块 ⇒ 这一批彻底没拿到
            out.update(got)
        return out

    def index_auction(self, codes: list) -> dict:
        """指数竞价快照（qt.gtimg.cn 批量，字段[3]=虚拟匹配价，[4]=昨收）。
        返回 {code: {name, auction_price, pre_close, gap_pct}}。"""
        out = {}
        if not codes:
            return out
        try:
            _clear_proxy()
            q = ",".join(codes)
            txt = _qt_get(f"/q={q}", _QT_AUCTION_TIMEOUT)
            for part in txt.strip().split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                key, _, payload = part.partition("=")
                code = key.strip().lstrip("v_").lower()
                f = payload.strip().strip('"').split("~")
                if len(f) < 5:
                    continue
                def _f(i):
                    try:
                        return float(f[i])
                    except Exception:
                        return None
                price, pc = _f(3), _f(4)
                out[code] = {"name": f[1] if len(f) > 1 and f[1] else code,
                             "auction_price": price, "pre_close": pc,
                             "gap_pct": round((price - pc) / pc * 100, 2) if price and pc else None}
        except Exception:
            pass
        return out
