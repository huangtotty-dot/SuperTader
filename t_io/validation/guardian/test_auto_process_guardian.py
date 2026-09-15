# -*- coding: utf-8 -*-
"""test_auto_process_guardian.py — auto_process_guardian 巡检逻辑离线测试（W5, 2026-09-15）。

说明：W3 交付 guardian 时未落测试文件（git 全历史核查无 test_*guardian*），本套件按
guardian 文档化行为补齐：11 项策略巡检回归用例 + 4 项新增 IOPV 看护用例，共 15 项。
managed python 直接运行：python t_io/validation/guardian/test_auto_process_guardian.py
所有路径/进程/飞书均 mock（GUARDIAN_DRY_RUN=1 + 临时目录），不碰生产文件与进程。
"""
import sys, os, json, tempfile, importlib

sys.stdout.reconfigure(encoding='utf-8')

_TMP = tempfile.mkdtemp(prefix='guardian_test_')
os.environ['SUPERTRADER_ROOT'] = _TMP          # 防 feishu import 触到生产配置
os.environ['GUARDIAN_BRIDGE_DIR'] = os.path.join(_TMP, 'bridge')
os.environ['GUARDIAN_LOG_DIR'] = os.path.join(_TMP, 'logs')
os.environ['GUARDIAN_DRY_RUN'] = '1'
os.makedirs(os.environ['GUARDIAN_BRIDGE_DIR'], exist_ok=True)

sys.path.insert(0, os.path.join(r'E:\superTrader', 'scripts'))
import auto_process_guardian as g  # noqa: E402

from datetime import datetime  # noqa: E402

RESULTS = []


def case(name):
    def deco(fn):
        try:
            _reset()
            fn()
            RESULTS.append((name, True, ''))
            print('PASS  %s' % name)
        except Exception as e:
            RESULTS.append((name, False, repr(e)))
            print('FAIL  %s  %r' % (name, e))
        return fn
    return deco


# ── 测试装置：每例前全量复位 guardian 状态与 mock ──
_rec = {}


def _reset():
    g._state.update({
        'consec_fail': 0, 'alert_only': False, 'pending_restart_ts': 0.0,
        'spawned_proc': None, 'last_alert': {},
        'iopv_consec_fail': 0, 'iopv_alert_only': False, 'iopv_pending_ts': 0.0,
    })
    _rec.clear()
    _rec.update({'push': [], 'spawn_strategy': [], 'spawn_iopv': [], 'kill': [],
                 'strategy_pids': [], 'iopv_pids': [111],   # 默认 IOPV 存活→策略用例不受扰
                 'hb_age': 10.0, 'now': datetime(2026, 9, 15, 10, 0), 'time': 1_000_000.0})
    g._push = lambda *a, **kw: _rec['push'].append((a, kw)) or True
    g._spawn_strategy = lambda: _rec['spawn_strategy'].append(1) or 'DRY'
    g._spawn_iopv = lambda: _rec['spawn_iopv'].append(1) or 'DRY'
    g._kill_pids = lambda pids: _rec['kill'].append(list(pids))
    g._find_strategy_pids = lambda: list(_rec['strategy_pids'])
    g._find_iopv_pids = lambda: list(_rec['iopv_pids'])
    g._heartbeat_age_sec = lambda: _rec['hb_age']
    g._now_dt = lambda: _rec['now']
    g.time.time = lambda: _rec['time']
    ks = os.path.join(os.environ['GUARDIAN_BRIDGE_DIR'], 'KILL_SWITCH')
    if os.path.exists(ks):
        os.remove(ks)


def _set_kill_switch(on):
    ks = os.path.join(os.environ['GUARDIAN_BRIDGE_DIR'], 'KILL_SWITCH')
    if on:
        open(ks, 'w').close()
    elif os.path.exists(ks):
        os.remove(ks)


# ═══════════ 策略巡检回归（既有文档化行为） ═══════════

@case('S01 KILL_SWITCH 存在→只告警不拉起')
def _():
    _set_kill_switch(True)
    _rec['hb_age'] = 9999.0
    g.tick()
    assert not _rec['spawn_strategy'], 'KILL_SWITCH 下不得拉起策略'
    assert not _rec['spawn_iopv'], 'KILL_SWITCH 下不得拉起 IOPV'
    assert any(kw.get('level') == 'red' for a, kw in _rec['push']), '应有 red 告警'


@case('S02 心跳新鲜→无动作')
def _():
    _rec['hb_age'] = 10.0
    g.tick()
    assert not _rec['spawn_strategy'] and not _rec['push']


@case('S03 心跳新鲜→只告警态/失败计数自动复位（人工恢复确认）')
def _():
    g._state['alert_only'] = True
    g._state['consec_fail'] = 2
    g.tick()
    assert g._state['alert_only'] is False and g._state['consec_fail'] == 0


@case('S04 心跳新鲜→确认重启成功（pending 清零+green 推送）')
def _():
    g._state['pending_restart_ts'] = _rec['time'] - 100
    g.tick()
    assert g._state['pending_restart_ts'] == 0.0 and g._state['consec_fail'] == 0
    assert any(kw.get('level') == 'green' for a, kw in _rec['push']), '应有 green 恢复推送'


@case('S05 非交易时段心跳停顿→不动')
def _():
    _rec['hb_age'] = 9999.0
    _rec['now'] = datetime(2026, 9, 15, 16, 0)
    g.tick()
    assert not _rec['spawn_strategy'] and not _rec['push']


@case('S06 周末心跳停顿→不动')
def _():
    _rec['hb_age'] = 9999.0
    _rec['now'] = datetime(2026, 9, 13, 10, 0)   # 周日
    g.tick()
    assert not _rec['spawn_strategy'] and not _rec['push']


@case('S07 交易时段心跳停顿+进程存活→先 kill 冻结进程再拉起')
def _():
    _rec['hb_age'] = 9999.0
    _rec['strategy_pids'] = [123, 456]
    g.tick()
    assert _rec['kill'] == [[123, 456]], '应先 taskkill 冻结进程'
    assert len(_rec['spawn_strategy']) == 1
    assert g._state['pending_restart_ts'] > 0
    assert any(kw.get('level') == 'orange' for a, kw in _rec['push'])


@case('S08 拉起宽限期内→不重复拉起')
def _():
    _rec['hb_age'] = 9999.0
    g._state['pending_restart_ts'] = _rec['time'] - 10   # 10s 前刚拉起，宽限 240s
    g.tick()
    assert not _rec['spawn_strategy']


@case('S09 宽限过心跳未恢复→记连续失败 #1 并当轮再拉起')
def _():
    _rec['hb_age'] = 9999.0
    g._state['pending_restart_ts'] = _rec['time'] - 9999
    g.tick()
    # 实际行为：记失败 #1（<上限2）后落入停顿分支当轮再拉起，pending 重新置位
    assert g._state['consec_fail'] == 1 and g._state['alert_only'] is False
    assert len(_rec['spawn_strategy']) == 1 and g._state['pending_restart_ts'] > 0


@case('S10 连续 2 次重启失败→转只告警态')
def _():
    _rec['hb_age'] = 9999.0
    g._state['consec_fail'] = 1
    g._state['pending_restart_ts'] = _rec['time'] - 9999
    g.tick()
    assert g._state['consec_fail'] == 2 and g._state['alert_only'] is True
    assert any(kw.get('level') == 'red' for a, kw in _rec['push'])


@case('S11 只告警态→不再拉起，只 red 告警')
def _():
    _rec['hb_age'] = 9999.0
    g._state['alert_only'] = True
    g.tick()
    assert not _rec['spawn_strategy']
    assert any(kw.get('level') == 'red' for a, kw in _rec['push'])


# ═══════════ IOPV 看护（W5 新增） ═══════════

@case('I01 采集时段内 IOPV 存活→不拉起且状态复位')
def _():
    g._state['iopv_consec_fail'] = 1
    g._state['iopv_alert_only'] = True
    _rec['iopv_pids'] = [999]
    g.tick()
    assert not _rec['spawn_iopv']
    assert g._state['iopv_consec_fail'] == 0 and g._state['iopv_alert_only'] is False


@case('I02 采集时段内 IOPV 未运行→拉起并置 pending')
def _():
    _rec['iopv_pids'] = []
    g.tick()
    assert len(_rec['spawn_iopv']) == 1
    assert g._state['iopv_pending_ts'] > 0
    assert any(kw.get('level') == 'orange' for a, kw in _rec['push'])


@case('I03 IOPV 拉起连败 2 次→转只告警不再拉起')
def _():
    _rec['iopv_pids'] = []
    g._state['iopv_consec_fail'] = 1
    g._state['iopv_pending_ts'] = _rec['time'] - 9999   # 宽限(90s)已过仍未存活
    g.tick()
    assert g._state['iopv_consec_fail'] == 2 and g._state['iopv_alert_only'] is True
    assert any(kw.get('level') == 'red' for a, kw in _rec['push'])
    _rec['push'].clear()
    g.tick()   # 只告警态：不再拉起
    assert not _rec['spawn_iopv']
    assert any(kw.get('level') == 'red' for a, kw in _rec['push'])


@case('I04 非采集时段 IOPV 未运行→不拉起不告警')
def _():
    _rec['iopv_pids'] = []
    _rec['now'] = datetime(2026, 9, 15, 16, 0)   # 15:05 后采集器自行退出属正常
    g.tick()
    assert not _rec['spawn_iopv']
    assert not any('IOPV' in str(a) for a, kw in _rec['push'])


@case('I05 KILL_SWITCH 存在→IOPV 只告警不拉起')
def _():
    _set_kill_switch(True)
    _rec['iopv_pids'] = []
    g.tick()
    assert not _rec['spawn_iopv']
    assert any(kw.get('level') == 'red' and 'IOPV' in str(a) for a, kw in _rec['push'])


print('\n══════════ 结果 ══════════')
ok = sum(1 for _, p, _ in RESULTS if p)
print('通过 %d/%d' % (ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
