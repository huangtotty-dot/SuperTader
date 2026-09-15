# -*- coding: utf-8 -*-
"""iopv_snapshot.py — 场内 ETF IOPV / 溢价率盘中采集（草案，评审前不常驻）。

实验员_E4，2026-09-15。背景：A10 与 2-B' 实验均已实测 gm SDK 的 IOPV 可得性——
  · current() 实时快照带 iopv 字段（588170 实测 iopv=0.9218 vs price=0.922）；
  · history_n(1d) 请求 iopv 字段被静默丢弃 → 无历史 IOPV 序列；
  · get_fundamentals(fund) / get_instrumentinfos 均无净值字段。
结论：历史溢价率不可得，只能盘中自行采集增量落盘。本脚本即采集器草案。

用途：588170（及后续 QDII 类 513130 等）做T上线前的盘中溢价率监控。
QDII/跨境 ETF 溢价风险真实存在；588170 为 A 股场内 ETF，溢价通常 <0.5%，
但半导体极端行情下仍可能出现持续溢价/折价，做T 信号应与溢价率联动过滤。

运行方式（需用户 Python，gm SDK 只装在那里）：
  C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\python.exe scripts\\iopv_snapshot.py            # 常驻采集（评审通过后才启用）
  ...\\python.exe scripts\\iopv_snapshot.py --once          # 采集一条后退出（联调用）
  ...\\python.exe scripts\\iopv_snapshot.py --once --dry-run # 只打印不落盘（验证用）

落盘：t_io/state/iopv/iopv_<sec_id>_<YYYY-MM-DD>.jsonl，每行一条 JSON：

  字段说明（一条记录）：
    ts          本地采集时间 ISO 秒级（+08:00）
    symbol      gm 代码，如 SHSE.588170
    price       最新价（元）
    iopv        实时参考净值（元），交易所每 15s 刷新；非交易时段为上一有效值
    premium_pct 溢价率 % = (price / iopv - 1) * 100；iopv 缺失/为 0 时为 null
    open/high/low  当日开/高/低
    bid_p/bid_v    买一价/量（股）；ask_p/ask_v 卖一价/量
    cum_volume  当日累计成交量（股）
    cum_amount  当日累计成交额（元）
    tick_time   gm 快照时间 created_at（行情源时间，可能滞后于 ts）
    alert       溢价率告警档：ok / warn(≥0.5%) / critical(≥1.0%)（阈值见下方常量）

采集节奏：每 30s 一次（IOPV 15s 刷新，30s 采样对溢价监控足够且控制量额）；
仅 09:15~15:15 采集；非交易时段 current() 返回 last 快照，仍记录但 alert 恒为 ok
的语义不保证，下游用 tick_time 判断新鲜度。

注意：
  · token 从 core.market_data.gm_token.load_token() 或环境变量 GM_TOKEN 读取；
    gmterm-serv.exe 重启会换 token，采集器每次启动时重新读取。
  · 本脚本只写 t_io/state/iopv/ 目录；今日（2026-09-15）只交付草案，不常驻。
"""
import sys, os, json, time, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, 't_io', 'state', 'iopv')
SYMBOLS = ['SHSE.588170']          # 后续可加 'SHSE.513130' 等
INTERVAL_SEC = 30
SESSION_START = (9, 15)
SESSION_END = (15, 15)
PREMIUM_WARN_PCT = 0.5
PREMIUM_CRIT_PCT = 1.0


def in_session(now):
    return SESSION_START <= (now.hour, now.minute) < SESSION_END


def alert_level(premium_pct):
    if premium_pct is None:
        return 'unknown'
    a = abs(premium_pct)
    if a >= PREMIUM_CRIT_PCT:
        return 'critical'
    if a >= PREMIUM_WARN_PCT:
        return 'warn'
    return 'ok'


def snap_one(gma, symbol):
    cur = gma.current(symbols=symbol)
    if not cur:
        return None
    c = cur[0]
    price = float(c.get('price') or 0)
    iopv = c.get('iopv')
    iopv = float(iopv) if iopv not in (None, '') else None
    premium = (price / iopv - 1) * 100 if (iopv and price) else None
    q0 = (c.get('quotes') or [{}])[0]
    return {
        'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'symbol': symbol,
        'price': price,
        'iopv': iopv,
        'premium_pct': round(premium, 4) if premium is not None else None,
        'open': c.get('open'), 'high': c.get('high'), 'low': c.get('low'),
        'bid_p': q0.get('bid_p'), 'bid_v': q0.get('bid_v'),
        'ask_p': q0.get('ask_p'), 'ask_v': q0.get('ask_v'),
        'cum_volume': c.get('cum_volume'), 'cum_amount': c.get('cum_amount'),
        'tick_time': str(c.get('created_at')),
        'alert': alert_level(premium),
    }


def out_path(symbol):
    sec_id = symbol.split('.')[-1]
    day = datetime.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(OUT_DIR, f'iopv_{sec_id}_{day}.jsonl')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='采集一条后退出')
    ap.add_argument('--dry-run', action='store_true', help='只打印不落盘')
    args = ap.parse_args()

    import gm.api as gma
    from core.market_data.gm_token import load_token
    token = os.environ.get('GM_TOKEN') or load_token()
    assert token, 'gm token 不可得（掘金终端未运行？）'
    gma.set_token(token)

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)

    while True:
        now = datetime.datetime.now()
        if in_session(now) or args.once:
            for sym in SYMBOLS:
                try:
                    rec = snap_one(gma, sym)
                except Exception as e:
                    print(f'[{now:%H:%M:%S}] {sym} 采集失败: {e!r}', flush=True)
                    continue
                if rec is None:
                    continue
                line = json.dumps(rec, ensure_ascii=False)
                if args.dry_run:
                    print(line, flush=True)
                else:
                    with open(out_path(sym), 'a', encoding='utf-8') as f:
                        f.write(line + '\n')
                    if rec['alert'] in ('warn', 'critical'):
                        print(f"[{now:%H:%M:%S}] {sym} 溢价告警 {rec['alert']}: "
                              f"{rec['premium_pct']}%", flush=True)
        if args.once:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == '__main__':
    main()
