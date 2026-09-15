# -*- coding: utf-8 -*-
"""iopv_snapshot.py — 场内 ETF IOPV / 溢价率盘中采集器（生产常驻版）。

实验员_E4 草案（2026-09-15）→ IOPV施工员_W5 生产化（2026-09-15 盘后，owner 拍板常驻）。

背景：A10 与 2-B' 实验实测 gm SDK 的 IOPV 可得性——
  · current() 实时快照带 iopv 字段（588170 实测 iopv=0.9218 vs price=0.922）；
  · history_n(1d) 请求 iopv 字段被静默丢弃 → 无历史 IOPV 序列；
结论：历史溢价率不可得，只能盘中自行采集增量落盘。

生产化要点（W5）：
  1. token 自愈：GM_TOKEN 环境变量优先，否则从运行中的 gmterm-serv.exe 命令行
     实时提取（token 随终端重启轮换，不做静态缓存）；采集遇鉴权失败自动重新提取，
     指数退避重试，上限 5 次后飞书告警但进程不死。
  2. 交易时段自律：工作日 09:25-15:05 每 30s 采集；时段外休眠；15:05 后正常退出(0)；
     周末直接退出(0)（节假日不苛求，由守护进程的时段闸兜底）。
  3. 落盘 jsonl 单行追加；字段含 ts/code/symbol/price/iopv/premium_pct 等。
  4. 告警：|溢价| ≥0.5% warn、≥1.0% critical，复用 gm_bridge/feishu.py 推送
     （10 分钟节流）；推送失败只记日志不崩溃。
  5. 环境变量：IOPV_OUT_DIR（输出重定向，测试用）、IOPV_CODES（默认 588170，
     逗号分隔可扩展 513130/513180）、IOPV_DRY_RUN=1（只打印不落盘不发飞书）。

运行方式（需用户 Python，gm SDK 只装在那里）：
  C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\python.exe scripts\\iopv_snapshot.py
  ...\\python.exe scripts\\iopv_snapshot.py --once          # 采集一条后退出（联调用）
  ...\\python.exe scripts\\iopv_snapshot.py --once --dry-run # 只打印不落盘

落盘：{IOPV_OUT_DIR}/iopv_<sec_id>_<YYYY-MM-DD>.jsonl（默认 t_io/state/iopv/），每行一条 JSON：
    ts          本地采集时间 ISO 秒级（+08:00）
    code        证券代码，如 588170
    symbol      gm 代码，如 SHSE.588170
    price       最新价（元）
    iopv        实时参考净值（元），交易所每 15s 刷新
    premium_pct 溢价率 % = (price / iopv - 1) * 100；iopv 缺失/为 0 时为 null
    open/high/low, bid_p/bid_v, ask_p/ask_v, cum_volume, cum_amount
    tick_time   gm 快照时间 created_at
    alert       ok / warn(≥0.5%) / critical(≥1.0%)

进程守护：由 scripts/auto_process_guardian.py 看护（交易时段内未运行则拉起）。
"""
import sys, os, re, json, time, argparse, datetime, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.environ.get('IOPV_OUT_DIR') or os.path.join(ROOT, 't_io', 'state', 'iopv')


def _parse_codes(env_val):
    """IOPV_CODES='588170,513130' → ['SHSE.588170','SHSE.513130']（沪市 5 开头；深市 15 开头）。"""
    syms = []
    for c in (env_val or '588170').split(','):
        c = c.strip()
        if not c:
            continue
        if '.' in c:                      # 已是 gm 全代码
            syms.append(c.upper())
        elif c.startswith(('15', '16', '12')):
            syms.append('SZSE.' + c)
        else:
            syms.append('SHSE.' + c)
    return syms


SYMBOLS = _parse_codes(os.environ.get('IOPV_CODES'))
INTERVAL_SEC = 30
SESSION_START = (9, 25)
SESSION_END = (15, 5)               # 15:05 后退出（含 15:05 整点不再采）
PREMIUM_WARN_PCT = 0.5
PREMIUM_CRIT_PCT = 1.0
ALERT_THROTTLE_SEC = 600            # 飞书告警 10 分钟节流
AUTH_RETRY_MAX = 5                  # 鉴权失败重新提取 token 重试上限
AUTH_BACKOFF_BASE_SEC = 5           # 指数退避基数：5/10/20/40/80s

_DRY_RUN = os.environ.get('IOPV_DRY_RUN', '') == '1'

# ═══════════════ token 自愈（gmterm-serv.exe 命令行实时提取，不静态缓存） ═══════════════
_TERM_PROCS = ('gmterm-serv.exe', 'gsgm3.exe')   # gsgm3.exe=新终端 Electron 壳，防御改名
_TOKEN_RE = re.compile(r'--token=([0-9a-fA-F]{32,64})')


def _parse_token(text):
    m = _TOKEN_RE.search(text or '')
    return m.group(1) if m else None


def extract_token_from_terminal():
    """从运行中的掘金终端进程命令行提取会话 token。psutil→wmic→PowerShell CIM 回退链。"""
    for proc in _TERM_PROCS:
        t = _token_from_proc(proc)
        if t:
            return t
    return None


def _resolve_exe(name, fallback):
    """PATH 查找 + 绝对路径兜底（守护/采集进程的 PATH 可能不含 PowerShell/wbem 目录）。"""
    import shutil
    p = shutil.which(name)
    if p:
        return p
    return fallback if os.path.exists(fallback) else name


_WMIC = _resolve_exe('wmic', r'C:\Windows\System32\wbem\wmic.exe')
_POWERSHELL = _resolve_exe('powershell', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')


def _token_from_proc(proc):
    try:
        import psutil
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                if p.info['name'] and p.info['name'].lower() == proc.lower() and p.info['cmdline']:
                    t = _parse_token(' '.join(p.info['cmdline']))
                    if t:
                        return t
            except Exception:
                continue
    except ImportError:
        pass
    try:
        out = subprocess.run(
            [_WMIC, 'process', 'where', "name='%s'" % proc, 'get', 'commandline', '/format:list'],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            t = _parse_token(out.stdout)
            if t:
                return t
    except Exception:
        pass
    try:
        out = subprocess.run(
            [_POWERSHELL, '-NoProfile', '-Command',
             'Get-CimInstance Win32_Process -Filter "name=\'%s\'" | '
             'Select-Object -ExpandProperty CommandLine' % proc],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            t = _parse_token(out.stdout)
            if t:
                return t
    except Exception:
        pass
    return None


def load_token():
    """GM_TOKEN 环境变量优先，否则实时提取终端进程 token（每次调用都重新发现，无缓存）。"""
    return os.environ.get('GM_TOKEN') or extract_token_from_terminal()


_AUTH_ERR_KEYS = ('token', 'auth', '鉴权', '认证', '登录', 'permission', 'denied',
                  'unauthorized', '401', '1001', '1002', '1013')


def is_auth_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in _AUTH_ERR_KEYS)


# ═══════════════ 飞书告警（复用 gm_bridge.feishu，只 import 不改；失败只记日志） ═══════════════
_GM_BRIDGE_DIR = os.path.join(ROOT, 'execution', 'auto', '_gm', 'gm_bridge')
if _GM_BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _GM_BRIDGE_DIR)
_send_feishu = None
try:
    from feishu import send_feishu_payload as _send_feishu  # noqa: E501
    _FEISHU_OK = True
except Exception as _e:
    _FEISHU_OK = False
    _FEISHU_ERR = str(_e)

_last_alert_ts = {}


def _log(msg):
    print('[%s] %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def push_alert(title, content, level='warn'):
    """10 分钟节流；DRY_RUN/飞书不可用/推送异常 → 仅日志，绝不抛异常。"""
    key = '%s|%s' % (title, level)
    now_ts = time.time()
    if now_ts - _last_alert_ts.get(key, 0) < ALERT_THROTTLE_SEC:
        _log('[alert_throttled] %s' % title)
        return False
    _last_alert_ts[key] = now_ts
    _log('[ALERT:%s] %s — %s' % (level, title, content[:200]))
    if _DRY_RUN or not _FEISHU_OK:
        return False
    try:
        template = {'warn': 'orange', 'critical': 'red'}.get(level, 'blue')
        card = {
            'msg_type': 'interactive',
            'card': {
                'config': {'wide_screen_mode': True},
                'header': {'title': {'tag': 'plain_text', 'content': title},
                           'template': template},
                'elements': [{'tag': 'markdown', 'content': content}],
            },
        }
        return bool(_send_feishu(payload=card, success_log='', error_prefix='iopv',
                                 trigger_urgent_alarm_after_success=(level == 'critical')))
    except Exception as e:
        _log('[alert_failed] %s' % e)
        return False


# ═══════════════ 采集 ═══════════════

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
        'code': symbol.split('.')[-1],
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
    return os.path.join(OUT_DIR, 'iopv_%s_%s.jsonl' % (sec_id, day))


def snap_with_heal(gma, symbol):
    """采集一条；鉴权失败 → 重新提取 token + 指数退避重试，上限 5 次后告警返回 None（不死）。"""
    for attempt in range(AUTH_RETRY_MAX + 1):
        try:
            return snap_one(gma, symbol)
        except Exception as e:
            if not is_auth_error(e):
                _log('%s 采集失败(非鉴权): %r' % (symbol, e))
                return None
            if attempt >= AUTH_RETRY_MAX:
                push_alert('IOPV采集器：token 自愈失败',
                           '%s 连续 %d 次鉴权失败，重新提取 token 无效。\n最后错误: %s\n'
                           '采集器继续运行，下轮自动重试。请检查掘金终端状态。'
                           % (symbol, AUTH_RETRY_MAX, str(e)[:200]),
                           level='critical')
                return None
            wait = AUTH_BACKOFF_BASE_SEC * (2 ** attempt)
            _log('%s 鉴权失败(#%d)，%ds 后重新提取 token 重试: %r' % (symbol, attempt + 1, wait, e))
            time.sleep(wait)
            new_token = load_token()
            if new_token:
                try:
                    gma.set_token(new_token)
                    _log('token 已重新提取并注入（末4位 ...%s）' % new_token[-4:])
                except Exception as e2:
                    _log('set_token 异常: %r' % e2)
            else:
                _log('token 重新提取失败（终端未运行？），下轮重试')
    return None


# ═══════════════ 主流程 ═══════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='采集一条后退出（联调用）')
    ap.add_argument('--dry-run', action='store_true', help='只打印不落盘（同 IOPV_DRY_RUN=1）')
    args = ap.parse_args()
    dry_run = _DRY_RUN or args.dry_run

    import gm.api as gma
    token = load_token()
    if not token:
        # 启动即无 token：告警但继续（终端可能稍后启动，靠采集轮的自愈兜底）
        push_alert('IOPV采集器：启动无 token',
                   'GM_TOKEN 未设置且未从 gmterm-serv.exe 提取到 token，采集将依赖轮次内自愈。',
                   level='warn')
    else:
        gma.set_token(token)
        _log('token 注入完成（末4位 ...%s）' % token[-4:])

    if not dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)
    _log('iopv_snapshot 启动 pid=%d symbols=%s out=%s dry_run=%s feishu=%s'
         % (os.getpid(), SYMBOLS, OUT_DIR, dry_run, 'ok' if _FEISHU_OK else 'unavailable'))

    while True:
        now = datetime.datetime.now()
        if not args.once:
            # 周末不干活：直接退出(0)（守护进程时段闸不会拉起；手动启动也安全退出）
            if now.weekday() >= 5:
                _log('周末，退出(0)')
                return 0
            # 15:05 后正常退出(0)，当日采集结束
            if (now.hour, now.minute) >= SESSION_END:
                _log('已过 %02d:%02d，当日采集结束，退出(0)' % SESSION_END)
                return 0
        if in_session(now) or args.once:
            for sym in SYMBOLS:
                rec = snap_with_heal(gma, sym)
                if rec is None:
                    continue
                line = json.dumps(rec, ensure_ascii=False)
                if dry_run:
                    print(line, flush=True)
                else:
                    try:
                        with open(out_path(sym), 'a', encoding='utf-8') as f:
                            f.write(line + '\n')
                    except Exception as e:
                        _log('%s 落盘失败: %r' % (sym, e))
                if rec['alert'] in ('warn', 'critical'):
                    push_alert('IOPV溢价%s：%s %.2f%%'
                               % ('告警' if rec['alert'] == 'warn' else '严重告警',
                                  rec['code'], rec['premium_pct']),
                               '标的: %s\n最新价: %s  IOPV: %s\n溢价率: %.2f%%（阈值 warn≥%.1f%% / critical≥%.1f%%）\n时间: %s'
                               % (rec['symbol'], rec['price'], rec['iopv'], rec['premium_pct'],
                                  PREMIUM_WARN_PCT, PREMIUM_CRIT_PCT, rec['ts']),
                               level=rec['alert'])
        if args.once:
            break
        time.sleep(INTERVAL_SEC)
    return 0


if __name__ == '__main__':
    sys.exit(main())
