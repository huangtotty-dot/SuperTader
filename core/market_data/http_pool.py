# -*- coding: utf-8 -*-
"""stdlib 的 keep-alive HTTP 连接池（按主机分桶，全仓库共享）。

**为什么需要它（2026-10-16 起陆续把各数据源的裸 `urlopen` 换成复用连接）**：
`urllib.request.urlopen` 每次调用都新建 DNS + TCP + TLS，本机实测**建连本身 ≈290ms**
（同一 URL 连打 12 次：裸 urlopen 中位 350.9ms、复用连接 61.4ms ⇒ 5.7×）。
而快照/分钟线/日线兜底都是**逐只**调的 ⇒ 一屏 50 只就是 50 次握手 ≈14.5s，
全压在后台线程上，是 GUI 卡死的直接来源（2026-10-16 卡死栈里实测 5 个线程同时卡在
`socket.create_connection`）。

**为什么用 stdlib 而不是 requests**：本模块会被 gm 策略进程里的 `core.market_data.facade`
加载（盘前预筛走它），不能给策略进程引入新依赖；且 raw `http.client` 天然不走环境代理，
等价于既有的 `_clear_proxy()`。

**必须按主机分桶**：keep-alive 连接绑死在建连时的 host 上，混用会把 A 主机的请求发到
B 主机（实测 fqkline 发到 `ifzq` 上返 501 ⇒ 静默拿空）。`host_pool(host)` 按 host 各管各的。
"""
import http.client as _hc
import threading

_DEFAULT_MAX_IDLE = 8

_POOLS = {}
_POOLS_LOCK = threading.Lock()


def host_pool(host, max_idle=None):
    """取/建某主机的连接池记录 `{pool, lock, max}`。`max` 只在**首次**建桶时生效。"""
    with _POOLS_LOCK:
        rec = _POOLS.get(host)
        if rec is None:
            rec = {"pool": [], "lock": threading.Lock(),
                   "max": _DEFAULT_MAX_IDLE if max_idle is None else max_idle}
            _POOLS[host] = rec
        return rec


def conn_take(host, pool, lock, timeout):
    """取一条空闲 keep-alive 连接；池空则新建。"""
    with lock:
        while pool:
            c = pool.pop()
            try:
                # 池里混着不同超时的来源（2s 单只 / 3s 批量 / 4s K线）⇒ 按本次调用重设
                if c.sock is not None:
                    c.sock.settimeout(timeout)
                return c
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
    return _hc.HTTPSConnection(host, timeout=timeout)


def conn_put(pool, lock, conn, max_idle):
    """把**已完整读完**响应体的连接放回池；池满则关掉（不无限攒 fd）。"""
    with lock:
        if len(pool) < max_idle:
            pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


def pooled_get_raw(host, path, timeout, headers):
    """GET `https://{host}{path}` → **bytes**。**按主机复用连接**，失败抛异常。

    与 `pooled_get` 同一条重试纪律，只是不解码 —— 给需要自己判
    `\\x1f\\x8b`（gzip 魔数）或自己选编码的调用点用（东财 F10 就会回 gzip）。
    """
    rec = host_pool(host)
    pool, lock, max_idle = rec["pool"], rec["lock"], rec["max"]
    last = None
    for attempt in (0, 1):
        conn = (conn_take(host, pool, lock, timeout) if attempt == 0
                else _hc.HTTPSConnection(host, timeout=timeout))
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.will_close:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                conn_put(pool, lock, conn, max_idle)
            return data
        except Exception as e:
            last = e
            try:
                conn.close()
            except Exception:
                pass
    raise last


def pooled_get(host, path, timeout, headers, encoding="gbk", errors="replace"):
    """GET `https://{host}{path}` → 文本。**按主机复用连接**，失败抛异常。

    失败自动**换新连接重试一次**：keep-alive 连接被对端静默关掉后，下一次请求会抛
    `RemoteDisconnected`/`BadStatusLine` —— 这是 keep-alive 的固有代价，不是故障，
    不能让它把"本来能拿到的数据"变成拿不到。重试仍失败才抛给调用方
    （调用点的语义都是"失败即当作没数据"，与改前一致）。

    `encoding`/`errors` 与各调用点原先的 `.decode(...)` 逐点对齐（腾讯 gbk、东财/新浪 utf-8）。
    """
    return pooled_get_raw(host, path, timeout, headers).decode(encoding, errors=errors)
