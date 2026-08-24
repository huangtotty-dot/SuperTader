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


class MarketDataFetcher:
    """行情数据统一获取器 —— 强制网络查询，不使用本地缓存"""

    MAX_CODES_PER_REQUEST = 200
    HISTORICAL_WORKERS = 20  # 并发数，平衡速度和稳定性（2026-08-15: 10→20 加快首跑）
    HISTORICAL_RETRIES = 2

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
        symbols = [self._normalize_qt_symbol(c) for c in codes if self._is_a_share_code(c)]
        if not symbols:
            print(f"  [WARN] 过滤后无有效A股代码 (原始 {len(codes)} 只)")
            return pd.DataFrame()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com/",
        }

        snapshot_map: Dict[str, Dict] = {}

        for idx, chunk in enumerate(self._chunk_list(symbols, self.MAX_CODES_PER_REQUEST)):
            url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
            text = ""
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as response:
                    text = response.read().decode("utf-8", errors="replace")
            except Exception as e:
                print(f"  [WARN] 腾讯快照 urllib 失败: {type(e).__name__}: {str(e)[:120]}")
                try:
                    result = subprocess.run(
                        ["curl", "-k", "-s", url],
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=30,
                    )
                    text = result.stdout or ""
                    if result.returncode != 0:
                        stderr = (result.stderr or "").strip().replace("\n", " ")
                        print(f"  [WARN] 腾讯快照 curl 失败: rc={result.returncode} stderr={stderr[:120]}")
                except Exception as curl_error:
                    print(f"  [WARN] 腾讯快照抓取失败: {type(curl_error).__name__}: {str(curl_error)[:120]}")
                    continue

            if not text.strip():
                print(f"  [WARN] 腾讯快照第 {idx+1} 批返回为空 (URL len={len(url)})")
                continue

            batch_count = 0
            for line in text.splitlines():
                code, data = self._parse_qt_snapshot_line(line)
                if not code or not data:
                    continue
                if data.get("成交额", 0) <= 0 and data.get("现价", 0) <= 0:
                    continue
                snapshot_map[code] = data
                batch_count += 1

        if snapshot_map:
            print(f"  [OK] 腾讯快照批量加载完成: {len(snapshot_map)} 只")
        else:
            print(f"  [WARN] 腾讯快照批量加载为空 (总计 {len(symbols)} 只, 分 {len(list(self._chunk_list(symbols, self.MAX_CODES_PER_REQUEST)))} 批)")

        return self._snapshot_map_to_df(snapshot_map)

    @staticmethod
    def _normalize_qt_symbol(code: str) -> str:
        code = str(code).strip()
        market = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return f"{market}{code}"

    @staticmethod
    def _is_a_share_code(code: str) -> bool:
        code = str(code).strip()
        return len(code) == 6 and code.isdigit() and code.startswith(
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

    def _fetch_historical_tencent(self, codes: List[str], date_str: str) -> tuple:
        """
        获取历史K线数据，返回 (DataFrame, 失败代码列表)
        """
        target_date_str = date_str
        results = []
        failed = []

        def fetch_one(code: str):
            for attempt in range(1, self.HISTORICAL_RETRIES + 1):
                try:
                    market = "sh" if code.startswith(("6", "5", "9")) else "sz"
                    symbol = f"{market}{code}"
                    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,1000,qfq"
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Referer': 'https://finance.qq.com/'
                    }
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=10) as response:
                        content = response.read().decode('utf-8', errors='ignore')
                        data = json.loads(content)

                    if data.get('code') != 0 or not data.get('data'):
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
