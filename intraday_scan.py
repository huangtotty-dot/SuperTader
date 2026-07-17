# -*- coding: utf-8 -*-
"""
盘中实时扫描 - 每天11点和14点自动执行
逻辑复用V16.0的策略，用于盘中捕捉交易信号
"""
import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Tuple, List, Dict

import numpy as np
import pandas as pd

# 设置控制台编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 导入系统报警模块V17.3
try:
    from system_alert_v17_3 import SystemAlert, trigger_alert
    SYSTEM_ALERT_AVAILABLE = True
except:
    SYSTEM_ALERT_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

for d in [LOG_DIR, CACHE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

log = logging.getLogger("三度猎手_盘中")
log.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

today_str = datetime.now().strftime('%Y-%m-%d')
file_handler = logging.FileHandler(os.path.join(LOG_DIR, f"intraday_{today_str}.log"), encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(file_handler)

# 初始化系统报警
if SYSTEM_ALERT_AVAILABLE:
    system_alert = SystemAlert(enabled=True)

def load_config() -> Dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                log.debug(f"✓ 配置加载成功")
                return config
    except Exception as e:
        log.debug(f"⚠️  配置加载失败: {str(e)}")
    return {"feishu": {"enabled": False}}

def send_to_feishu(config: Dict, signals: List[Dict], scan_time: str, total: int, success_count: int):
    if not config.get("feishu", {}).get("enabled"):
        return

    webhook_url = config.get("feishu", {}).get("webhook_url", "").strip()
    if not webhook_url or webhook_url.endswith("YOUR_WEBHOOK_KEY"):
        return

    try:
        at_all = config.get("feishu", {}).get("at_all_on_signal", True)
        use_strong = config.get("feishu", {}).get("use_strong_notification", True)

        if not signals:
            msg_text = f"⏰ 盘中扫描时间：{scan_time}\n📊 数据统计：成功获取 {success_count}/{total} 只股票\n\n暂未发现交易信号"
            payload = {"msg_type": "text", "content": {"text": msg_text}}
        else:
            at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
            # 增强通知效果：使用更强的标题和emoji
            title = "🚨🚨🚨 【盘中】交易信号提醒 - 请立即查看 🚨🚨🚨" if use_strong else "📢 【盘中】交易信号提醒"

            content_list = [
                [{"tag": "text", "text": f"{at_text}\n\n" if at_all else ""}],
                [{"tag": "text", "text": f"{title}\n"}],
                [{"tag": "text", "text": f"⏰ 扫描时间：{scan_time}\n📊 数据统计：成功获取 {success_count}/{total} 只股票\n🎯 捕获信号：{len(signals)} 个\n"}]
            ]

            # 按优先级排序并添加信号
            for s in signals:
                # 为不同类型的信号添加不同的emoji强调
                signal_emoji = {
                    "🎯 突破回踩": "🔴",
                    "⭐ 箱体突破": "🟠",
                    "👑 突破先手": "🟡",
                    "🚀 A区初显": "🟢",
                    "🔥 B区起航": "🔵",
                    "💎 底部缩量": "🟣",
                    "⚖️ B区潜伏": "⚪"
                }.get(s['type'], "")

                content_list.append([{"tag": "text", "text": f"\n{signal_emoji} [{s['type']}] {s['name']} ({s['code']})\n现价：{s['price']:.2f} | 板块：{s['sector']}\n{s['reason']}"}])

            payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": content_list}}}}

        req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get('code') == 0:
                log.debug(f"✓ 飞书推送成功 ({len(signals)} 个信号)")
    except Exception as e:
        log.debug(f"⚠️  飞书推送异常: {str(e)[:40]}")

def get_cache_file(code: str, target_date: str) -> str:
    return os.path.join(CACHE_DIR, f"{code}_{target_date}.csv")

def save_cache(df: pd.DataFrame, code: str, target_date: str):
    try:
        cache_file = get_cache_file(code, target_date)
        df.to_csv(cache_file, index=False, encoding='utf-8')
    except Exception as e:
        log.debug(f"    ⚠️  缓存保存失败: {str(e)[:40]}")

def load_cache(code: str, target_date: str) -> pd.DataFrame:
    try:
        cache_file = get_cache_file(code, target_date)
        if os.path.exists(cache_file):
            df = pd.read_csv(cache_file)
            if not df.empty:
                return df
    except:
        pass
    return pd.DataFrame()

def fetch_from_tencent_kline_final(code: str, target_date: str, start_date: str) -> pd.DataFrame:
    try:
        log.debug(f"    [腾讯财经K线] 尝试下载...")
        market = "sh" if code.startswith(('6', '5', '9')) else "sz"
        symbol = f"{market}{code}"
        url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,320,qfq"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.qq.com/'
        }

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read().decode('utf-8', errors='ignore')
            data = json.loads(content)

            if data.get('code') != 0 or not data.get('data'):
                return pd.DataFrame()

            stock_data = data['data'].get(symbol)
            if not stock_data:
                return pd.DataFrame()

            kline_data = stock_data.get('day') or stock_data.get('qfqday')
            if not kline_data:
                return pd.DataFrame()

            target_date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
            start_date_str = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"

            data_list = []
            for item in kline_data:
                try:
                    if isinstance(item, list) and len(item) >= 6:
                        date_str = item[0]
                        if len(date_str) != 10 or date_str.count('-') != 2:
                            continue
                        if date_str < start_date_str or date_str > target_date_str:
                            continue
                        data_list.append({
                            'date': date_str,
                            'open': float(item[1]),
                            'close': float(item[2]),
                            'high': float(item[3]),
                            'low': float(item[4]),
                            'volume': float(item[5])
                        })
                except (ValueError, IndexError, TypeError):
                    continue

            if not data_list:
                return pd.DataFrame()

            df = pd.DataFrame(data_list)
            df = df.sort_values('date').reset_index(drop=True)

            # 修复：获取完整的历史数据用于策略检查
            # 不需要过滤到特定日期，因为策略需要足够的历史数据（至少65条）
            # 只要确保数据包含目标日期之前的数据即可

            # 处理目标日期无数据的情况（未来日期或非交易日）
            last_date = df.iloc[-1]['date']
            if last_date != target_date_str:
                target_data = df[df['date'] == target_date_str]
                if not target_data.empty:
                    df = target_data.reset_index(drop=True)
                else:
                    log.debug(f"    ⚠️  目标日期 {target_date_str} 无数据，使用最后可用日期 {last_date}")

            log.debug(f"    ✓ 腾讯财经K线成功 ({len(df)} 条)")
            return df

    except Exception as e:
        log.debug(f"    ✗ 异常: {str(e)[:40]}")
        return pd.DataFrame()

def fetch_data(code: str, target_date: str) -> pd.DataFrame:
    td_obj = datetime.strptime(target_date, "%Y%m%d")
    start_date = (td_obj - timedelta(days=250)).strftime("%Y%m%d")
    log.debug(f"[数据获取] {code}")

    df = load_cache(code, target_date)
    if not df.empty:
        return df

    df = fetch_from_tencent_kline_final(code, target_date, start_date)
    if not df.empty:
        save_cache(df, code, target_date)
        return df

    log.debug(f"✗ {code} 数据获取失败")
    return pd.DataFrame()

def detect_box_pattern(df: pd.DataFrame) -> Tuple[float, float, int, float]:
    if len(df) < 20:
        return 0, 0, 0, 0

    best_box = None
    best_score = 0

    for period in [20, 30, 40, 50]:
        if len(df) < period:
            continue

        recent = df.iloc[-period:]
        box_high = recent['high'].max()
        box_low = recent['low'].min()
        box_width = box_high - box_low
        box_width_ratio = box_width / box_low

        box_days = sum(1 for i in range(len(recent))
                      if recent.iloc[i]['high'] <= box_high
                      and recent.iloc[i]['low'] >= box_low)

        box_quality = box_width_ratio * (box_days / period)

        if box_quality > best_score and box_width_ratio > 0.12 and box_days >= 12:
            best_score = box_quality
            best_box = (box_low, box_high, box_days, box_width_ratio)

    if best_box:
        return best_box
    return 0, 0, 0, 0

def check_strategies(df: pd.DataFrame) -> Tuple[str, str]:
    try:
        if len(df) < 65: return None, ""

        df['ma10'] = df['close'].rolling(10).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma30'] = df['close'].rolling(30).mean()
        df['ma60'] = df['close'].rolling(60).mean()

        df['vol_ma20_past'] = df['volume'].shift(1).rolling(20).mean()
        df['vol_ratio'] = df['volume'] / df['vol_ma20_past'].replace(0, 1)

        df['is_up'] = df['close'] > df['open']
        df['up_vol'] = np.where(df['is_up'], df['volume'], 0)
        df['down_vol'] = np.where(~df['is_up'], df['volume'], 0)
        df['power_ratio'] = df['up_vol'].rolling(20).sum() / (df['down_vol'].rolling(20).sum() + 1)

        today = df.iloc[-1]
        yest = df.iloc[-2]
        price = today['close']
        pct_change = (price - yest['close']) / yest['close']
        is_yang = today['close'] > today['open']

        past_15_close_max = df['close'].iloc[-16:-1].max()
        is_breaking_platform = price > past_15_close_max
        is_buyable = (0.02 < pct_change < 0.065)

        if is_breaking_platform and is_buyable and is_yang and (today['vol_ratio'] > 1.25) and (today['power_ratio'] > 1.15):
            return "👑 突破先手", f"温和突破近15日平台！放量({today['vol_ratio']:.1f}倍)，资金底座扎实(阳线动能{today['power_ratio']:.2f})，抓主升起涨第一天。"

        ma60_is_down = today['ma60'] < df.iloc[-5]['ma60']
        if ma60_is_down and (price < today['ma60'] * 0.90): return None, ""

        base_clustered = abs(today['ma20'] - today['ma30']) / today['ma30'] < 0.05
        ma10_turn_up = today['ma10'] > yest['ma10']
        mild_ignition = (0.01 < pct_change < 0.055) and (today['vol_ratio'] > 1.2)

        if base_clustered and ma10_turn_up and mild_ignition and is_yang and (price > today['ma20']) and (today['power_ratio'] > 1.05):
            return "🚀 A区初显", f"均线底座打牢，今日温和放量({today['vol_ratio']:.1f}倍)初次点火，涨幅{pct_change*100:.1f}%，最佳的左侧转右侧潜伏点。"

        ma30_is_up = today['ma30'] > df.iloc[-5]['ma30']
        near_ma30 = abs(price - today['ma30']) / today['ma30'] < 0.04
        near_ma20 = abs(price - today['ma20']) / today['ma20'] < 0.04

        if ma30_is_up and (near_ma30 or near_ma20):
            recent_shrink = df.iloc[-3]['vol_ratio'] < 0.7 or yest['vol_ratio'] < 0.7
            if recent_shrink and is_yang and (pct_change > 0.01) and (today['vol_ratio'] > 1.2):
                return "🔥 B区起航", f"洗盘动作结束，今日温和放量({today['vol_ratio']:.1f}倍)反包，进攻线半空重新归位仰头。"

            if today['vol_ratio'] < 0.45 and (not is_yang):
                support = "MA30" if near_ma30 else "MA20"
                return "⚖️ B区潜伏", f"中期趋势向上，回踩 {support} 极度缩量({today['vol_ratio']:.2f}倍)，抛压枯竭，密切关注明后天反转。"

        near_ma20_loose = abs(price - today['ma20']) / today['ma20'] < 0.02
        near_ma30_loose = abs(price - today['ma30']) / today['ma30'] < 0.08
        shrinking_volume = today['vol_ratio'] < 0.8
        ma30_uptrend = today['ma30'] > df.iloc[-10]['ma30']
        power_strong = today['power_ratio'] > 1.2

        if (near_ma20_loose or near_ma30_loose) and shrinking_volume and ma30_uptrend and power_strong and (not is_yang):
            return "💎 底部缩量", f"底部区域缩量阴线，MA30上升趋势，资金底座扎实(动能{today['power_ratio']:.2f})，抛压枯竭，蓄势待发。"

        box_low, box_high, box_days, box_width_ratio = detect_box_pattern(df)

        if box_low > 0 and box_high > 0:
            is_breaking_box_up = price > box_high * 0.98
            has_box_history = box_width_ratio > 0.12
            has_box_consolidation = box_days >= 12

            if is_breaking_box_up and has_box_history and has_box_consolidation and is_yang and (today['vol_ratio'] > 0.9) and (today['power_ratio'] > 1.1):
                space_potential = (box_width_ratio) * 100
                return "⭐ 箱体突破", f"长期箱体({box_low:.2f}-{box_high:.2f})突破！{box_days}天震荡后放量突破，后续空间{space_potential:.1f}%，第一优先级买点。"

        if box_low > 0 and box_high > 0:
            recent_10 = df.iloc[-10:]
            breakout_high = recent_10['high'].max()
            is_had_breakout = breakout_high > box_high * 0.99

            if is_had_breakout:
                is_pullback_to_box = price > box_low * 0.98 and price < box_high * 1.02
                ma_good = today['ma10'] > today['ma20'] > today['ma30']
                power_good = today['power_ratio'] > 1.0
                recent_yang = sum(1 for i in range(len(recent_10)) if recent_10.iloc[i]['close'] > recent_10.iloc[i]['open']) >= 2
                is_shrinking = today['vol_ratio'] < 1.0

                if is_pullback_to_box and ma_good and power_good and recent_yang and is_shrinking:
                    breakout_indices = df[df['high'] > box_high * 0.99].index
                    if len(breakout_indices) > 0:
                        days_since_breakout = len(df) - 1 - breakout_indices[-1]
                        return "🎯 突破回踩", f"突破箱体({box_low:.2f}-{box_high:.2f})后回踩，{days_since_breakout}天前突破，均线排列良好，资金底座扎实(动能{today['power_ratio']:.2f})，缩量回踩，二次上涨机会。"

        return None, ""
    except Exception as e:
        log.debug(f"策略检查异常: {str(e)}")
        return None, ""

def is_trading_time() -> bool:
    """检查是否在交易时间内"""
    now = datetime.now()
    hour = now.hour
    minute = now.minute

    # 11:00 或 14:00 附近（允许前后5分钟）
    is_11am = (10 <= hour <= 11) and (55 <= minute or hour == 10)
    is_2pm = (13 <= hour <= 14) and (55 <= minute or hour == 13)

    return is_11am or is_2pm

def is_trading_day() -> bool:
    """检查是否是交易日"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False

    holidays = ["20250101", "20250102", "20250103", "20250429", "20250430", "20250501", "20250502", "20250503", "20250818"]
    today_str = now.strftime("%Y%m%d")
    if today_str in holidays:
        return False

    return True

def run_intraday_scan():
    """运行盘中扫描"""
    if not is_trading_day():
        log.info("⚠️  今天不是交易日，跳过扫描")
        return

    now = datetime.now()
    scan_time = now.strftime("%Y-%m-%d %H:%M:%S")
    target_date = now.strftime("%Y%m%d")

    print("\n" + "="*70)
    print(" 🐎 三度操盘·实战量化机 V16.0 - 盘中实时扫描")
    print(f" ⏰ 扫描时间：{scan_time}")
    print(" 📊 逻辑复用V16.0策略，捕捉盘中交易机会")
    print("="*70 + "\n")

    log.info(f"【盘中扫描启动】时间: {scan_time}")

    config = load_config()

    feishu_enabled = config.get("feishu", {}).get("enabled", False)
    print(f"[配置] 飞书推送: {'✓ 已启用' if feishu_enabled else '✗ 已禁用'}\n")
    log.info(f"飞书推送: {'✓ 已启用' if feishu_enabled else '✗ 已禁用'}")

    print(f"🔍 目标锚定：获取截止至 [{target_date}] 的数据进行盘中扫描...")
    print(f"📝 详细日志: {os.path.join(LOG_DIR, f'intraday_{today_str}.log')}\n")

    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        pool = json.load(f)

    signals = []
    total = len(pool)
    success_count = 0
    failed_count = 0

    for idx, (code, info) in enumerate(pool.items(), 1):
        print(f"\r⏳ 扫描进度: [{idx}/{total}] 正在把脉: {info['name']} ({code}) ...", end="", flush=True)
        try:
            df = fetch_data(code, target_date)

            if df.empty:
                failed_count += 1
                time.sleep(0.02)
                continue

            success_count += 1

            sig_type, reason = check_strategies(df)
            if sig_type:
                signals.append({
                    "name": info["name"], "code": code, "sector": info["sector"],
                    "price": df.iloc[-1]['close'], "type": sig_type, "reason": reason
                })
                log.info(f"✓ 捕获信号: [{sig_type}] {info['name']} ({code})")

            time.sleep(0.02)

        except Exception as e:
            failed_count += 1
            log.warning(f"处理 {code} 异常: {str(e)}")
            time.sleep(0.02)

    print("\n\n✅ 盘中扫描完成！正在生成作战简报...\n")
    print(f"📊 数据统计: 成功获取 {success_count}/{total} 只股票数据 (失败 {failed_count} 只)")
    log.info(f"数据统计: 成功 {success_count}/{total}，失败 {failed_count}")

    if success_count == 0:
        print(f"\n❌ 严重错误：无法获取任何数据！\n")
        log.error("严重错误：无法获取任何数据")
        return

    if not signals:
        msg = f"⏰ 盘中扫描时间：{scan_time}\n\n在 {total} 只标的中，暂未发现交易信号。"
        print(msg)
        log.info(f"本轮扫描未发现信号 (扫描 {total} 只标的)")
        send_to_feishu(config, [], scan_time, total, success_count)
        return

    priority_order = {"🎯 突破回踩": 0, "⭐ 箱体突破": 1, "👑 突破先手": 2, "🚀 A区初显": 3, "🔥 B区起航": 4, "💎 底部缩量": 5, "⚖️ B区潜伏": 6}
    signals.sort(key=lambda x: priority_order.get(x['type'], 99))

    summary = f"⏰ 盘中扫描时间：{scan_time} | 共捕获 {len(signals)} 个高价值信号\n\n"
    for s in signals:
        line = f"[{s['type']}] {s['name']} ({s['code']} - {s['sector']}) 现价:{s['price']:.2f}\n🎯 {s['reason']}\n"
        print(line)
        summary += line + "\n"

    log.info(f"【盘中扫描完成】捕获 {len(signals)} 个信号")

    # 触发系统报警
    if SYSTEM_ALERT_AVAILABLE and len(signals) > 0:
        try:
            trigger_alert("urgent", "buy")
            log.info("✓ 系统报警已触发")
        except Exception as e:
            log.warning(f"系统报警触发失败: {str(e)}")

    print("✅ 盘中扫描完成！")
    send_to_feishu(config, signals, scan_time, total, success_count)

if __name__ == "__main__":
    run_intraday_scan()
