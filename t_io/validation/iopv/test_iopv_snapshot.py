# -*- coding: utf-8 -*-
"""test_iopv_snapshot.py — iopv_snapshot 生产化全链路离线验证（W5, 2026-09-15）。

mock gm.api 注入 sys.modules，IOPV_OUT_DIR 重定向到临时目录（绝不污染 t_io/state/iopv/）。
覆盖：采集→落盘→溢价告警判定→飞书节流→token 自愈（鉴权失败重提取+指数退避+上限告警不死）。
managed python 直接运行：python t_io/validation/iopv/test_iopv_snapshot.py
"""
import sys, os, json, types, tempfile, importlib

sys.stdout.reconfigure(encoding='utf-8')

_TMP = tempfile.mkdtemp(prefix='iopv_test_')
os.environ['IOPV_OUT_DIR'] = _TMP
os.environ['IOPV_CODES'] = '588170,513130'
os.environ.pop('IOPV_DRY_RUN', None)
os.environ.pop('GM_TOKEN', None)

# ── fake gm.api（import iopv_snapshot 前注入；模块级不 import gm，main() 内才 import） ──
_fake_gm = types.ModuleType('gm')
_fake_gm_api = types.ModuleType('gm.api')
_fake_gm.api = _fake_gm_api
sys.modules['gm'] = _fake_gm
sys.modules['gm.api'] = _fake_gm_api

sys.path.insert(0, r'E:\superTrader\scripts')
import iopv_snapshot as m  # noqa: E402

RESULTS = []


def case(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((name, True))
            print('PASS  %s' % name)
        except Exception as e:
            RESULTS.append((name, False))
            print('FAIL  %s  %r' % (name, e))
        return fn
    return deco


def _tick(price, iopv):
    return {'price': price, 'iopv': iopv, 'open': price, 'high': price, 'low': price,
            'quotes': [{'bid_p': price, 'bid_v': 100, 'ask_p': price, 'ask_v': 100}],
            'cum_volume': 1000, 'cum_amount': 999.0, 'created_at': '2026-09-15 10:00:00'}


# ═══════════ 纯函数 ═══════════

@case('F01 token 正则解析（--token= 32~64 位 hex）')
def _():
    cmd = r'"C:\gm\gmterm-serv.exe" --token=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --port=7001'
    assert m._parse_token(cmd) == 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4'
    assert m._parse_token('gmterm-serv.exe --token=zzz') is None
    assert m._parse_token('') is None


@case('F02 鉴权错误识别')
def _():
    assert m.is_auth_error(Exception('error code: 1013, info: token无效'))
    assert m.is_auth_error(Exception('Permission denied'))
    assert not m.is_auth_error(Exception('connection reset by peer'))


@case('F03 告警档位：warn≥0.5% / critical≥1.0%（含折价取绝对值）')
def _():
    assert m.alert_level(0.49) == 'ok'
    assert m.alert_level(0.5) == 'warn'
    assert m.alert_level(1.0) == 'critical'
    assert m.alert_level(-1.2) == 'critical'
    assert m.alert_level(None) == 'unknown'


@case('F04 IOPV_CODES 解析（588170→SHSE，159xxx→SZSE，全代码透传）')
def _():
    assert m._parse_codes('588170,513130') == ['SHSE.588170', 'SHSE.513130']
    assert m._parse_codes('159915') == ['SZSE.159915']
    assert m._parse_codes('shse.510300') == ['SHSE.510300']


# ═══════════ 采集→落盘→告警 全链路（--once） ═══════════

@case('C01 --once 全链路：双标的采集→jsonl 落盘→premium/alert 字段正确')
def _():
    _fake_gm_api.current = lambda symbols: [_tick(1.008, 1.0)]          # +0.8% → warn
    _fake_gm_api.set_token = lambda t: None
    pushed = []
    m._send_feishu = lambda **kw: pushed.append(kw) or True
    m._FEISHU_OK = True
    old_argv = sys.argv
    sys.argv = ['iopv_snapshot.py', '--once']
    try:
        rc = m.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    day_files = [f for f in os.listdir(_TMP) if f.endswith('.jsonl')]
    assert len(day_files) == 2, '应为双标的各一份 jsonl: %s' % day_files
    rec = json.loads(open(os.path.join(_TMP, [f for f in day_files if '588170' in f][0]),
                          encoding='utf-8').readline())
    for k in ('ts', 'code', 'symbol', 'price', 'iopv', 'premium_pct', 'alert'):
        assert k in rec, '缺字段 %s' % k
    assert rec['code'] == '588170' and abs(rec['premium_pct'] - 0.8) < 1e-6
    assert rec['alert'] == 'warn'
    assert pushed, 'warn 应触发飞书推送'


@case('C02 溢价≥1.0% → critical 推送')
def _():
    m._last_alert_ts.clear()
    _fake_gm_api.current = lambda symbols: [_tick(1.012, 1.0)]          # +1.2%
    rec = m.snap_with_heal(_fake_gm_api, 'SHSE.588170')
    assert rec['alert'] == 'critical'
    pushed = []
    m._send_feishu = lambda **kw: pushed.append(kw) or True
    m.push_alert('t', 'c', level=rec['alert'])
    assert pushed and pushed[0]['trigger_urgent_alarm_after_success'] is True


@case('C03 飞书推送 10 分钟节流')
def _():
    m._last_alert_ts.clear()
    pushed = []
    m._send_feishu = lambda **kw: pushed.append(kw) or True
    m.push_alert('同一告警', 'c', level='warn')
    m.push_alert('同一告警', 'c', level='warn')   # 应被节流
    assert len(pushed) == 1, '节流失效: %d' % len(pushed)


@case('C04 飞书推送异常→只记日志不崩溃')
def _():
    m._last_alert_ts.clear()
    def _boom(**kw):
        raise RuntimeError('network down')
    m._send_feishu = _boom
    assert m.push_alert('x', 'y', level='warn') is False   # 不抛异常即通过


# ═══════════ token 自愈 ═══════════

@case('T01 鉴权失败→重新提取 token 重试成功（指数退避可恢复）')
def _():
    calls = {'n': 0, 'set_token': []}
    def _current(symbols):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise Exception('error code: 1013, token已失效')
        return [_tick(1.0, 1.0)]
    _fake_gm_api.current = _current
    _fake_gm_api.set_token = lambda t: calls['set_token'].append(t)
    m.load_token = lambda: 'f' * 40           # 模拟重新提取到新 token
    old_sleep = m.time.sleep
    m.time.sleep = lambda s: None             # 退避不等真时间
    try:
        rec = m.snap_with_heal(_fake_gm_api, 'SHSE.588170')
    finally:
        m.time.sleep = old_sleep
    assert rec is not None and calls['n'] == 3
    assert len(calls['set_token']) == 2, '每次鉴权失败都应重提取注入: %s' % calls


@case('T02 连续 5 次鉴权失败→critical 告警但返回 None（不死）')
def _():
    _fake_gm_api.current = (_ for _ in ()).throw  # placeholder, replaced below
    def _always_fail(symbols):
        raise Exception('401 unauthorized: bad token')
    _fake_gm_api.current = _always_fail
    _fake_gm_api.set_token = lambda t: None
    m._last_alert_ts.clear()
    pushed = []
    m._send_feishu = lambda **kw: pushed.append(kw) or True
    old_sleep = m.time.sleep
    m.time.sleep = lambda s: None
    try:
        rec = m.snap_with_heal(_fake_gm_api, 'SHSE.588170')
    finally:
        m.time.sleep = old_sleep
    assert rec is None
    assert pushed and pushed[0]['trigger_urgent_alarm_after_success'] is True


@case('T03 非鉴权异常→不重试直接记日志返回 None')
def _():
    calls = {'n': 0}
    def _conn_fail(symbols):
        calls['n'] += 1
        raise Exception('connection reset by peer')
    _fake_gm_api.current = _conn_fail
    rec = m.snap_with_heal(_fake_gm_api, 'SHSE.588170')
    assert rec is None and calls['n'] == 1, '非鉴权错误不应触发退避重试'


# ═══════════ 时段自律 ═══════════

@case('H01 交易时段闸：09:25-15:05')
def _():
    from datetime import datetime as dt
    assert m.in_session(dt(2026, 9, 15, 9, 25)) is True
    assert m.in_session(dt(2026, 9, 15, 15, 4)) is True
    assert m.in_session(dt(2026, 9, 15, 9, 24)) is False
    assert m.in_session(dt(2026, 9, 15, 15, 5)) is False


print('\n══════════ 结果 ══════════')
ok = sum(1 for _, p in RESULTS if p)
print('通过 %d/%d  临时目录: %s' % (ok, len(RESULTS), _TMP))
sys.exit(0 if ok == len(RESULTS) else 1)
