# -*- coding: utf-8 -*-
"""
行情数据获取模块
职责：从腾讯财经获取A股实时/历史行情数据
数据源：
  1. 腾讯快照 qt.gtimg.cn — 实时行情（稳定，批量200只/请求）
  2. 腾讯K线 ifzq.gtimg.cn — 历史日线（前复权，单股请求，30天）
"""
import os
import time
import json
import subprocess
import pandas as pd
import urllib.request
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed


# 2026-08-15: 拉取进度上报（供 t_gui 选股猎手前端轮询显示进度条）
MARKET_PROGRESS = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}


def _df_to_kline_rows(df):
    """provider 日线 DataFrame → 腾讯 fqkline 6 列形态 [[date,open,close,high,low,volume]]。
    2026-10-08：ifzq 主机被 WAF 拦截后，改由 provider（GM/腾讯）供数，下游解析保持不变。"""
    try:
        if df is None or getattr(df, "empty", True):
            return []
        # 猎手评分只要 ~150 日；共享缓存有 800 根，只取尾部可省 4× 解析开销（2026-10-08）
        if len(df) > 300:
            df = df.tail(300)
        return [[str(r.date)[:10], float(r.open), float(r.close), float(r.high), float(r.low),
                 float(r.volume)] for r in df.itertuples(index=False)]
    except Exception:
        return []


class MarketDataFetcher:
    """行情数据统一获取器 —— 强制网络查询，不使用本地缓存"""

    MAX_CODES_PER_REQUEST = 200
    HISTORICAL_WORKERS = 20  # 并发数，平衡速度和稳定性（2026-08-15: 10→20 加快首跑）
    HISTORICAL_RETRIES = 2
    # 2026-08-25: 腾讯 WAF 间歇性 501 拦截不同主机（ifzq / web.ifzq 轮换），多主机兜底
    KLINE_HOSTS = ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn")

    def __init__(self, data_dir: str = None, st_codes: set = None):
        self.data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        self.last_failed = []  # 记录上次失败的股票代码
        self._st_codes = st_codes or set()  # ST 股票代码集合

    def fetch_for_date(self, codes: List[str], date_str: str) -> pd.DataFrame:
        """
        统一获取历史K线数据（150天），因为评分规则需要150日历史数据
        如果目标日期数据不存在（如今天未收盘），自动使用最近交易日

        fix 2026-08-15: 按日期缓存（market_{date}.csv）。盘后/历史日数据固定 → 复用缓存，
        避免 1300+ 只 × 单只HTTP拉1000天日线的重复开销（首次约2-5分钟，后续秒级）。
        盘中（当日 9:15-15:00）不读缓存、实时拉最新。
        """
        from datetime import datetime as _dt
        _now = _dt.now()
        _is_today = date_str == _now.strftime("%Y-%m-%d")
        _after_close = (_now.hour * 100 + _now.minute) >= 1500 or _now.weekday() >= 5
        _use_cache = (not _is_today) or _after_close  # 历史日或盘后 → 复用缓存
        cache_path = os.path.join(self.data_dir, f"market_{date_str.replace('-', '')}.csv")
        if _use_cache and os.path.exists(cache_path):
            try:
                df = pd.read_csv(cache_path, dtype={"代码": str}, encoding="utf-8-sig")
                if "代码" in df.columns:
                    df = df[df["代码"].astype(str).isin([str(c) for c in codes])]
                if len(df) > 0:
                    print(f"  [CACHE] 复用 {date_str} 历史行情缓存 ({len(df)} 只，省去全量拉取)")
                    return df
            except Exception:
                pass
        print(f"  [NET] 获取历史行情 {date_str} (腾讯K线 ifzq.gtimg.cn，{len(codes)} 只，150天)...")
        MARKET_PROGRESS.update({"running": True, "phase": "拉取日线行情", "done": 0, "total": len(codes), "msg": ""})
        try:
            df, failed = self._fetch_historical_tencent(codes, date_str)
        finally:
            # 保留拉取循环的真实 done/total（超时部分拉取时不伪 100%）
            MARKET_PROGRESS.update({"running": False, "phase": "拉取完成", "msg": "拉取完成"})
        self.last_failed = failed
        if _use_cache and not df.empty:
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                df.to_csv(cache_path, index=False, encoding="utf-8-sig")
                print(f"  [CACHE] 已缓存 {date_str} 历史行情 → {cache_path} ({len(df)} 只)")
            except Exception:
                pass
        return df

    def save_spot(self, df: pd.DataFrame, date_str: str):
        os.makedirs(self.data_dir, exist_ok=True)
        spot_path = os.path.join(self.data_dir, f"spot_{date_str.replace('-', '')}.csv")
        df.to_csv(spot_path, index=False, encoding="utf-8-sig")
        print(f"  [SAVE] 行情数据已归档: {spot_path} ({len(df)} 只)")

    def _fetch_spot_tencent(self, codes: List[str]) -> pd.DataFrame:
        """腾讯实时快照（P1-2 #10 收敛：走 tencent_provider.snapshot_auction，含筛选字段）。
        批量由 provider 内处理；保留 A股过滤与 成交额/现价 双零跳过。"""
        keep = [c for c in codes if self._is_a_share_code(c)]
        if not keep:
            print(f"  [WARN] 过滤后无有效A股代码 (原始 {len(codes)} 只)")
            return pd.DataFrame()
        from core.market_data.tencent_provider import TencentProvider
        snapshot_map: Dict[str, Dict] = {}
        for chunk in self._chunk_list(keep, self.MAX_CODES_PER_REQUEST):
            snap = TencentProvider().snapshot_auction(chunk)
            for code, d in snap.items():
                if (d.get("amount_wan") or 0) <= 0 and (d.get("price") or 0) <= 0:
                    continue
                pct = d.get("pct") or 0.0
                # 涨停列恢复 0/1 布尔旗标（审核 P1 阻断4）：按板块分档阈值，heat_tracker 按 0/1 计数
                name = str(d.get("name", ""))
                is_st = name.startswith(("*ST", "ST", "SST", "S*ST"))
                if is_st:
                    is_limit = 1 if pct >= 4.5 else 0
                elif code.startswith(("30", "68")):
                    is_limit = 1 if pct >= 19.5 else 0
                elif code.startswith(("8", "9")):
                    is_limit = 1 if pct >= 29.5 else 0
                else:
                    is_limit = 1 if pct >= 9.5 else 0
                snapshot_map[code] = {
                    "代码": code, "名称": name, "现价": d.get("price", 0.0),
                    "涨跌幅": pct, "涨停": is_limit,
                    "成交额": d.get("amount_wan", 0.0), "换手率": d.get("turnover", 0.0),
                    "振幅": d.get("amplitude", 0.0), "最高": d.get("high", 0.0),
                    "最低": d.get("low", 0.0), "今开": d.get("open", 0.0),
                    "昨收": d.get("pre_close", 0.0), "量比": d.get("vol_ratio", 1.0),
                }
        if snapshot_map:
            print(f"  [OK] 腾讯快照批量加载完成: {len(snapshot_map)} 只")
        else:
            print(f"  [WARN] 腾讯快照批量加载为空 (总计 {len(keep)} 只)")
        return self._snapshot_map_to_df(snapshot_map)


    @staticmethod
    def _normalize_qt_symbol(code: str) -> str:
        """6 位码 → 腾讯符号。统一走 codec（本模块此前内联 `startswith(("5","6","9"))`，
        与 codec docstring 明令相悖，且把北交所 920xxx 编成 sh920xxx ⇒ 永远取不到数据）。"""
        from core.market_data.codec import to_tx
        return to_tx(code)

    @staticmethod
    def _is_a_share_code(code: str) -> bool:
        """是否纳入猎手行情拉取的 A 股。2026-09-29：**纳入北交所**（4xxxxx/8xxxxx/920xxx）。

        ⚠️ 代价已知：北交所日线在当前环境四源全不通（腾讯 ifzq/ GM / 东财 / akshare，
        见 memory「突破扫描改造的环境级事实」）⇒ 这 321 只进来后会以「取数失败」形式进池，
        拉取分母与失败计数都会变大。owner 2026-09-29 明确要求纳入。
        """
        code = str(code).strip()
        if not (len(code) == 6 and code.isdigit()):
            return False
        if code.startswith(("4", "8", "920")):      # 北交所
            return True
        return code.startswith(
            ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
        )

    @staticmethod
    def _chunk_list(items: List[str], size: int) -> List[List[str]]:
        return [items[i:i + size] for i in range(0, len(items), size)]

    @staticmethod
    def _parse_qt_snapshot_line(line: str) -> tuple:
        line = str(line or "").strip()
        if not line or "=\"" not in line:
            return "", {}
        symbol = line.split("=", 1)[0].strip()
        payload = line.split("=", 1)[1].strip().strip(';').strip('"')
        fields = payload.split("~")
        if len(fields) < 8:
            return "", {}
        code = str(fields[2]).strip()
        if not code:
            return "", {}

        try:
            price = float(fields[3] or 0)
        except Exception:
            price = 0.0
        try:
            volume = float(fields[6] or 0)
        except Exception:
            volume = 0.0

        amount = 0.0
        turnover_raw = ""
        for field in fields:
            parts = str(field).strip().split("/")
            if len(parts) == 3:
                try:
                    amount = float(parts[2] or 0)
                    turnover_raw = parts[2].strip()
                    break
                except Exception:
                    continue
        if amount <= 0:
            try:
                # 腾讯快照 fields[7] 单位为元，无需再乘 10000
                amount = float(fields[7] or 0)
                turnover_raw = str(fields[7]).strip()
            except Exception:
                amount = 0.0
                turnover_raw = ""

        try:
            change_pct = float(fields[32] or 0)   # 腾讯快照 [31]=涨跌额, [32]=涨跌幅
        except Exception:
            change_pct = 0.0
        try:
            turnover_rate = float(fields[37] or 0)
        except Exception:
            turnover_rate = 0.0
        try:
            amplitude = float(fields[39] or 0)
        except Exception:
            amplitude = 0.0
        try:
            limit_up_price = float(fields[43] or 0)
        except Exception:
            limit_up_price = 0.0
        try:
            prev_close = float(fields[4] or 0)   # 腾讯快照 [4]=昨收, [5]=今开
        except Exception:
            prev_close = 0.0
        try:
            high = float(fields[33] or 0)
        except Exception:
            high = 0.0
        try:
            low = float(fields[34] or 0)
        except Exception:
            low = 0.0
        try:
            open_price = float(fields[5] or 0)   # 腾讯快照 [1]=名称, [5]=今开
        except Exception:
            open_price = 0.0

        is_limit = 0
        stock_name = str(fields[1]).strip()
        is_st = stock_name.startswith(("*ST", "ST", "SST", "S*ST"))
        if is_st:
            if change_pct >= 4.5:
                is_limit = 1
        elif code.startswith(("30", "68")):
            if change_pct >= 19.5:
                is_limit = 1
        elif code.startswith(("8", "9")):
            if change_pct >= 29.5:
                is_limit = 1
        else:
            if change_pct >= 9.5:
                is_limit = 1

        return code, {
            "symbol": symbol,
            "名称": stock_name,
            "现价": price,
            "涨跌幅": change_pct,
            "涨停": is_limit,
            "成交额": amount,
            "换手率": turnover_rate,
            "振幅": amplitude,
            "最高": high,
            "最低": low,
            "今开": open_price,
            "昨收": prev_close,
            "涨停价": limit_up_price,
            "量比": 1.0,
        }

    @staticmethod
    def _snapshot_map_to_df(snapshot_map: Dict[str, Dict]) -> pd.DataFrame:
        rows = []
        for code, data in snapshot_map.items():
            rows.append({
                "代码": code,
                "名称": data.get("名称", ""),
                "现价": data.get("现价", 0),
                "涨跌幅": data.get("涨跌幅", 0),
                "涨停": data.get("涨停", 0),
                "成交额": data.get("成交额", 0),
                "换手率": data.get("换手率", 0),
                "振幅": data.get("振幅", 0),
                "最高": data.get("最高", 0),
                "最低": data.get("最低", 0),
                "今开": data.get("今开", 0),
                "昨收": data.get("昨收", 0),
                "量比": data.get("量比", 1.0),
            })
        return pd.DataFrame(rows)

    def _fetch_kline_multi(self, symbol: str):
        """腾讯日线 fqkline 多主机兜底拉取：WAF 会间歇性 501 拦截不同主机，返回首个有效 JSON。

        2026-10-08：实测 `ifzq.gtimg.cn` / `web.ifzq.gtimg.cn` **两台都被 WAF 501**（全池逐只
        重试 ⇒ 974 只 4.7min 且 0 成功）。故改为：①先读**预取的 provider 批量结果**（GM history，
        `_bulk_kline`，见 `_fetch_historical_tencent`）②两台 ifzq ③最后兜底单只 provider。
        三者都拼成腾讯 fqkline 形态（6 列 [[date,open,close,high,low,volume]]），下游解析不变。
        """
        _c6 = symbol[-6:] if symbol[-6:].isdigit() else symbol
        _bulk = getattr(self, "_bulk_kline", None)
        if _bulk and _c6 in _bulk:
            rows = _df_to_kline_rows(_bulk[_c6])
            if rows:
                return {"code": 0, "data": {symbol: {"qfqday": rows}}}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.qq.com/'
        }
        for host in self.KLINE_HOSTS:
            try:
                url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,1000,qfq"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    content = response.read().decode('utf-8', errors='ignore')
                    data = json.loads(content)
                if data.get('code') == 0 and data.get('data'):
                    return data
            except Exception:
                continue
        # 兜底：统一 provider（GM 优先/腾讯兜底）
        try:
            from core.market_data import get_provider
            _df = get_provider().daily(_c6, 1200)
            rows = _df_to_kline_rows(_df)
            if rows:
                return {"code": 0, "data": {symbol: {"qfqday": rows}}}
        except Exception:
            pass
        return None

    def _fetch_historical_tencent(self, codes: List[str], date_str: str) -> tuple:
        """
        获取历史K线数据，返回 (DataFrame, 失败代码列表)
        """
        target_date_str = date_str
        results = []
        failed = []
        # 2026-10-08: 预批量取数。ifzq 两台都 501 ⇒ 逐只网络 974 次要 4.7min 且 0 成功。
        # 优先读**本地日线缓存**（`t_io/cache/daily_kline/`，图表/预下载已铺 5000+ 只、零网络），
        # 缺的再走 provider 批量（GM `history`，内部 900/批，days 控制在 200 以免超 GM 200k 行上限）。
        self._bulk_kline = {}
        self._bulk_from_cache = set()   # 来自本地缓存的码 → 值未变，省去回写 merge（见 fetch_one）
        _c6map = {str(c): (str(c)[-6:] if str(c)[-6:].isdigit() else str(c)) for c in codes}
        try:
            import core.chart_cache as _cc
            for _c, _c6 in _c6map.items():
                if _c6 in self._bulk_kline:
                    continue
                _df = _cc.load_daily_display(_c6)
                if _df is not None and not _df.empty:
                    self._bulk_kline[_c6] = _df
                    self._bulk_from_cache.add(_c6)
            print(f"  [BULK] 本地日线缓存命中 {len(self._bulk_kline)}/{len(codes)} 只")
        except Exception:
            pass
        _miss = [c6 for c6 in dict.fromkeys(_c6map.values()) if c6 not in self._bulk_kline]
        if _miss:
            try:
                from core.market_data import get_provider
                _got = get_provider().daily_many(_miss, days=200) or {}
                self._bulk_kline.update(_got)
                print(f"  [BULK] provider 批量补 {len(_got)}/{len(_miss)} 只")
            except Exception as _e:
                print(f"  [BULK] provider 批量失败: {str(_e)[:80]}")

        # 2026-10-08 owner：**盘中实时**——批量快照 patch/append 当日 forming bar。
        # 本地缓存/批量取数的最后一根可能是隔夜或较早的收盘价；不补的话猎手盘中并非实时。
        self._live_snap = {}
        try:
            from datetime import datetime as _dtlive
            if str(target_date_str) == _dtlive.now().strftime("%Y-%m-%d"):
                from core.market_data.tencent_provider import TencentProvider as _TP
                _sn = _TP().snapshot_auction(list(dict.fromkeys(_c6map.values()))) or {}
                for _c, _v in _sn.items():
                    if _v.get("ts_date") == str(target_date_str):
                        self._live_snap[_c] = _v
                print(f"  [LIVE] 盘中快照 patch {len(self._live_snap)}/{len(_c6map)} 只")
        except Exception as _e:
            print(f"  [LIVE] 快照失败(忽略): {str(_e)[:80]}")

        def fetch_one(code: str):
            for attempt in range(1, self.HISTORICAL_RETRIES + 1):
                try:
                    symbol = self._normalize_qt_symbol(code)
                    data = self._fetch_kline_multi(symbol)
                    if not data:
                        continue

                    stock_data = data['data'].get(symbol)
                    if not stock_data:
                        continue

                    kline_data = stock_data.get('day') or stock_data.get('qfqday')
                    if not kline_data:
                        continue

                    parsed = []
                    for item in kline_data:
                        try:
                            if isinstance(item, list) and len(item) >= 6:
                                _close = float(item[2])
                                _volume = float(item[5])
                                _high = float(item[3])
                                _low = float(item[4])
                                # 腾讯K线接口：len=6 时无 amount 字段
                                # 注意：不同板块的 volume 单位可能不同
                                # 主板/创业板：volume 单位是手（1手=100股），需 *100
                                # 科创板（688）：volume 单位是股，无需 *100
                                is_kcb = code.startswith("688")
                                volume_unit = 1.0 if is_kcb else 100.0
                                if len(item) >= 7:
                                    _amount = float(item[6])
                                else:
                                    derived_price = (_high + _low + _close) / 3.0
                                    _amount = derived_price * _volume * volume_unit
                                parsed.append({
                                    'date': item[0],
                                    'open': float(item[1]),
                                    'close': _close,
                                    'high': _high,
                                    'low': _low,
                                    'volume': _volume,
                                    'amount': _amount,
                                })
                        except (ValueError, IndexError, TypeError):
                            continue

                    if not parsed:
                        continue

                    df = pd.DataFrame(parsed).sort_values('date').reset_index(drop=True)
                    # 2026-10-08 需求②：盘中实时——用批量快照 patch 当日 bar（或补一根）
                    _c6k = code[-6:] if str(code)[-6:].isdigit() else str(code)
                    _snap = (getattr(self, "_live_snap", {}) or {}).get(_c6k)
                    if _snap:
                        _spx = float(_snap.get("price") or 0)
                        if _spx > 0:
                            _shi = float(_snap.get("high") or _spx)
                            _slo = float(_snap.get("low") or _spx)
                            _svol = float(_snap.get("vol_hand") or 0) or 0.0
                            _u = 1.0 if str(code).startswith("688") else 100.0
                            if str(df["date"].iloc[-1])[:10] == str(target_date_str):
                                _i = df.index[-1]
                                df.at[_i, "close"] = _spx
                                df.at[_i, "high"] = max(float(df.at[_i, "high"]), _shi)
                                df.at[_i, "low"] = min(float(df.at[_i, "low"]), _slo)
                                if _svol:
                                    df.at[_i, "volume"] = _svol
                                df.at[_i, "amount"] = (_shi + _slo + _spx) / 3.0 * float(df.at[_i, "volume"]) * _u
                            else:
                                df = pd.concat([df, pd.DataFrame([{
                                    "date": str(target_date_str), "open": float(_snap.get("open") or _spx),
                                    "close": _spx, "high": _shi, "low": _slo, "volume": _svol,
                                    "amount": (_shi + _slo + _spx) / 3.0 * _svol * _u,
                                }])], ignore_index=True)
                    # 2026-09-29: **当日**运行时把这次拉到的实时日线并入共享缓存。
                    # 为什么：GUI「建仓符合度」(_hunter_build_conformance) 为省资源是**零网络**
                    # 直接读 t_io/cache/daily_kline/，而那些缓存对全池多数票是陈旧的
                    # （实测 5373 只里只有 686 只有当日 bar，1116 只停在 2026-08-07）
                    # ⇒ 算出来的 GO 列是昨天的。猎手本轮已经拿到了 150 天实时线，顺手回写即可。
                    # ⚠️ 只在当日写：历史日回写会把当天新数据挤掉（用 merge 而非覆盖，且不缩历史）。
                    from datetime import datetime as _dtm
                    _c6 = code[-6:] if str(code)[-6:].isdigit() else str(code)
                    if (str(target_date_str) == _dtm.now().strftime("%Y-%m-%d")
                            and _c6 not in getattr(self, "_bulk_from_cache", set())):
                        try:
                            from core.market_data.tencent_provider import merge_daily_cache
                            merge_daily_cache(code, df)
                        except Exception:
                            pass
                    target_rows = df[df['date'] == target_date_str]
                    if target_rows.empty:
                        # 目标日期不存在，回退到目标日期之前最近的交易日
                        prev_rows = df[df['date'] < target_date_str]
                        if not prev_rows.empty:
                            target_row = prev_rows.iloc[-1]
                            target_idx = prev_rows.index[-1]
                            fallback_date = target_row['date']
                            # 如果回退日期与目标日期相差太远（超过5天），标记为失败
                            from datetime import datetime
                            try:
                                target_dt = datetime.strptime(target_date_str, '%Y-%m-%d')
                                fallback_dt = datetime.strptime(str(fallback_date), '%Y-%m-%d')
                                gap_days = (target_dt - fallback_dt).days
                                if gap_days > 30:
                                    return {"_error": code, "_msg": f"target {target_date_str} missing, fallback {fallback_date} too far ({gap_days} days)"}
                            except (ValueError, TypeError):
                                pass
                        elif len(df) >= 2:
                            # 特殊情况：目标日期在所有数据之后（如查询未来日期），使用最近已完成交易日
                            target_row = df.iloc[-2]
                            target_idx = len(df) - 2
                        elif len(df) >= 1:
                            target_row = df.iloc[-1]
                            target_idx = len(df) - 1
                        else:
                            continue
                    else:
                        target_row = target_rows.iloc[-1]
                        target_idx = target_rows.index[-1]

                    prev_idx = target_idx - 1
                    prev_close = float(df.iloc[prev_idx]['close']) if prev_idx >= 0 else 0
                    close = float(target_row['close'])
                    high = float(target_row['high'])
                    low = float(target_row['low'])
                    open_price = float(target_row['open'])

                    change_pct = 0.0
                    if prev_close > 0:
                        change_pct = round((close - prev_close) / prev_close * 100, 2)

                    amplitude = 0.0
                    if prev_close > 0:
                        amplitude = round((high - low) / prev_close * 100, 2)

                    amount = float(target_row.get('amount', 0) or 0)
                    if amount <= 0:
                        # 兜底：根据板块判断 volume 单位
                        is_kcb = code.startswith("688")
                        volume_unit = 1.0 if is_kcb else 100.0
                        volume = float(target_row['volume'])
                        amount = close * volume * volume_unit

                    is_limit = 0
                    is_st = code in self._st_codes
                    if is_st:
                        if change_pct >= 4.5:
                            is_limit = 1
                    elif code.startswith(("30", "68")):
                        if change_pct >= 19.5:
                            is_limit = 1
                    elif code.startswith(("8", "9")):
                        if change_pct >= 29.5:
                            is_limit = 1
                    else:
                        if change_pct >= 9.5:
                            is_limit = 1

                    # 派生指标
                    idx_5 = target_idx - 5
                    close_5 = float(df.iloc[idx_5]['close']) if idx_5 >= 0 else 0
                    change_5 = ((close - close_5) / close_5 * 100) if close_5 > 0 else 0

                    # 近5日最高价（不包含当日）
                    start_5 = max(0, target_idx - 5)
                    high_5 = df.iloc[start_5:target_idx]['high'].max() if target_idx > 0 else 0

                    # 近20日最高价（不包含当日）
                    start_20 = max(0, target_idx - 20)
                    high_20 = df.iloc[start_20:target_idx]['high'].max() if target_idx > 0 else 0

                    # 近10日最高价（不包含当日）
                    start_10 = max(0, target_idx - 10)
                    high_10 = df.iloc[start_10:target_idx]['high'].max() if target_idx > 0 else 0

                    # 近150日最高价（不包含当日）
                    start_150 = max(0, target_idx - 150)
                    high_150 = df.iloc[start_150:target_idx]['high'].max() if target_idx > 0 else 0

                    # 涨停价计算
                    limit_up_price = 0.0
                    if prev_close > 0:
                        if code.startswith(("30", "68")):
                            limit_up_price = round(prev_close * 1.2, 2)
                        elif code.startswith(("8", "9")):
                            limit_up_price = round(prev_close * 1.3, 2)
                        else:
                            limit_up_price = round(prev_close * 1.1, 2)

                    # 近10日是否有涨停
                    limit_10 = 0
                    for i in range(max(0, target_idx - 10), target_idx):
                        row_i = df.iloc[i]
                        close_i = float(row_i['close'])
                        prev_close_i = float(df.iloc[i-1]['close']) if i > 0 else 0
                        if prev_close_i > 0:
                            pct_i = (close_i - prev_close_i) / prev_close_i * 100
                            if pct_i >= 9.5:
                                limit_10 = 1
                                break

                    # 前日涨停判断
                    prev_limit = 0
                    if prev_idx >= 0 and prev_close > 0:
                        prev_prev_idx = prev_idx - 1
                        prev_prev_close = float(df.iloc[prev_prev_idx]['close']) if prev_prev_idx >= 0 else 0
                        if prev_prev_close > 0:
                            prev_pct = (prev_close - prev_prev_close) / prev_prev_close * 100
                            if prev_pct >= 9.5:
                                prev_limit = 1

                    # 首板涨停：当日涨停且前日非涨停
                    is_first_limit = 1 if (is_limit and not prev_limit) else 0

                    # 连板天数（从当日往前连续涨停的天数）
                    consecutive_limit = 0
                    if is_limit:
                        consecutive_limit = 1
                        for i in range(target_idx - 1, -1, -1):
                            if i <= 0:
                                break
                            row_i = df.iloc[i]
                            close_i = float(row_i['close'])
                            prev_close_i = float(df.iloc[i-1]['close'])
                            if prev_close_i > 0:
                                pct_i = (close_i - prev_close_i) / prev_close_i * 100
                                if pct_i >= 9.5:
                                    consecutive_limit += 1
                                else:
                                    break
                            else:
                                break

                    # 一字板涨停：当日涨停且今开 >= 涨停价 * 0.99
                    is_word_limit = 0
                    if is_limit and limit_up_price > 0 and open_price >= limit_up_price * 0.99:
                        is_word_limit = 1

                    # 「刚站上5日线」状态跃迁（2026-10-10）：口径**与 core/ma_reclaim.ma5_state 一致**
                    # （昨收<昨MA5 且今收>今MA5），勿另立门户。
                    # 为什么只出跃迁、不出「是否站上」：实测 2488 只 / 119,275 候选 stock-day，
                    # 布尔「站上」在候选层 96% 冗余且方向为负（未站上的票反而更好），
                    # 而跃迁是唯一 h1/h2/非重叠三重为正的口径（见 tmp/exp_hunter_ma5.py）。
                    ma5_reclaim = 0
                    try:
                        _lo = max(0, target_idx - 5)
                        _win = [float(x) for x in df.iloc[_lo:target_idx + 1]['close'].tolist()]
                        if len(_win) >= 6:                     # 需 5 根算昨 MA5 + 今日
                            _px = _win[-1]
                            _basis = _win[:-1]                 # 截至昨日
                            _prev_close_ = _basis[-1]
                            _prev_ma5 = sum(_basis[-5:]) / 5.0
                            _cur_ma5 = (sum(_basis[-4:]) + _px) / 5.0
                            if _cur_ma5 > 0 and _px > 0:
                                ma5_reclaim = 1 if (_prev_close_ < _prev_ma5 and _px > _cur_ma5) else 0
                    except Exception:
                        ma5_reclaim = 0

                    return {
                        "代码": code,
                        "名称": "",
                        "现价": close,
                        "涨跌幅": change_pct,
                        "涨停": is_limit,
                        "成交额": amount,
                        "换手率": 0.0,
                        "振幅": amplitude,
                        "最高": high,
                        "最低": low,
                        "今开": open_price,
                        "昨收": prev_close,
                        "量比": 1.0,
                        "近5日涨幅": round(change_5, 2),
                        "近5日最高": round(float(high_5), 2) if high_5 else 0,
                        "近20日最高": round(float(high_20), 2) if high_20 else 0,
                        "近10日最高": round(float(high_10), 2) if high_10 else 0,
                        "近150日最高": round(float(high_150), 2) if high_150 else 0,
                        "涨停价": limit_up_price,
                        "近10日涨停": limit_10,
                        "前日涨停": prev_limit,
                        "首板涨停": is_first_limit,
                        "连板天数": consecutive_limit,
                        "一字板涨停": is_word_limit,
                        "刚站上5日线": ma5_reclaim,
                        "数据日期": str(target_row['date']),
                    }

                except Exception as e:
                    if attempt < self.HISTORICAL_RETRIES:
                        time.sleep(2 ** attempt)
                    continue
            return {"_error": code, "_msg": "all retries failed"}

        max_workers = min(self.HISTORICAL_WORKERS, len(codes))
        # 硬限时兜底（2026-08-25）：个别 urllib 在 Windows 网络抖动时不遵守 timeout 会卡死，
        # future.result() 无超时 + as_completed 等最后一个 future → 前端进度条卡"100%"永远无结果。
        # 给整体预算，超时带部分结果返回，不让卡死拖住选股猎手。
        _FETCH_BUDGET = 420.0  # 秒（< 前端 600s 轮询守卫）
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_code = {executor.submit(fetch_one, c): c for c in codes}
            for i, future in enumerate(as_completed(future_to_code, timeout=_FETCH_BUDGET), 1):
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result is None:
                    continue
                if "_error" in result:
                    failed.append(result["_error"])
                else:
                    results.append(result)
                if i % 100 == 0:
                    print(f"     进度: {i}/{len(codes)} 只...")
                if i % 20 == 0 or i == len(codes):
                    MARKET_PROGRESS.update({"done": i, "total": len(codes), "msg": f"已拉取 {i}/{len(codes)} 只"})
                time.sleep(0.05)  # 增加延迟避免被限流
        except TimeoutError:
            # 整体预算超时：未完成 futures 的 code 记为失败，带部分结果继续
            for fut, code in future_to_code.items():
                if not fut.done():
                    failed.append(code)
            print(f"  [WARN] 拉取超时（>{_FETCH_BUDGET:.0f}s），返回部分结果 {len(results)}/{len(codes)} 只")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)  # 不等卡死线程，由 urllib 超时自愈

        if failed:
            print(f"  [WARN] {len(failed)} 只获取失败（已跳过）: {', '.join(failed[:20])}{'...' if len(failed) > 20 else ''}")
        if results:
            print(f"  [OK] 历史数据加载完成: {len(results)} 只")
        return pd.DataFrame(results), failed

    @staticmethod
    def _is_today_trading_hours(date_str: str) -> bool:
        from datetime import datetime
        return date_str == datetime.now().strftime("%Y-%m-%d")


class TencentDataFetcher:
    @staticmethod
    def fetch_batch(codes: List[str]) -> pd.DataFrame:
        fetcher = MarketDataFetcher()
        return fetcher._fetch_spot_tencent(codes)
