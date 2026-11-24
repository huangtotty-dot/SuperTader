# -*- coding: utf-8 -*-
"""全市场涨停飞书快报（2026-11-24，全离线）。

owner 口径：①范围=**全市场**；②**回封也推**；③10s 一拍，同拍多只合并一张卡；
④网络失败不推进快照（下拍重试）；⑤同日 09:31 后重启静默接管，不重推整池。

运行：python -m pytest tests/phase3/test_limitup_alert_watch.py -q
"""
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.limitup_alert_watch as W  # noqa: E402


def _item(code, name="", tag="FIRST_LIMIT", again=False, rate=10.0):
    return {"code": code, "name": name or code, "tag": tag, "again": again, "rate": rate}


class _Env:
    def __init__(self, monkeypatch, tmp_path):
        self.sent = []
        self.pool = []
        self.fail = False
        monkeypatch.setattr(W, "_STATE_FP", str(tmp_path / "limitup_state.json"))
        monkeypatch.setattr(W, "_poll_pool", lambda d: None if self.fail else list(self.pool))
        monkeypatch.setattr(W, "send_feishu_payload", self._send)

    def _send(self, card, **kw):
        self.sent.append(card)
        return True


def test_first_seal_pushes_and_no_duplicate(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    now = datetime(2026, 11, 24, 9, 25, 5)   # 早盘首拍（≤09:31 ⇒ 首个状态正常推送）
    env.pool = [_item("600467", "好当家", tag="FIRST_LIMIT")]
    got = W._tick(now)
    assert [x["code"] for x in got] == ["600467"] and len(env.sent) == 1
    assert W._tick(now) == [] and len(env.sent) == 1, "一直封着不重复推"


def test_incremental_and_reseal_pushed(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    now = datetime(2026, 11, 24, 9, 25, 5)   # 早盘首拍（≤09:31 ⇒ 首个状态正常推送）
    env.pool = [_item("600467"), _item("603324", tag="LIMIT_BACK")]
    W._tick(now)
    env.pool = [_item("600467"), _item("603324", tag="LIMIT_BACK"), _item("000001")]
    got = W._tick(now)
    assert [x["code"] for x in got] == ["000001"], "新增一只只推增量"
    env.pool = [_item("600467"), _item("000001")]           # 603324 开板
    assert W._tick(now) == []
    env.pool = [_item("600467"), _item("000001"), _item("603324", again=True)]
    got = W._tick(now)                                      # 回封 ⇒ 必须再推
    assert [x["code"] for x in got] == ["603324"]
    card_txt = json.dumps(env.sent[-1], ensure_ascii=False)
    assert "回封" in card_txt and "603324" in card_txt


def test_network_failure_keeps_state_and_retries(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    now = datetime(2026, 11, 24, 9, 25, 5)   # 早盘首拍（≤09:31 ⇒ 首个状态正常推送）
    env.pool = [_item("600467")]
    W._tick(now)
    env.pool = [_item("600467"), _item("000001")]
    env.fail = True
    assert W._tick(now) == [], "网络失败 ⇒ 无动作"
    env.fail = False
    got = W._tick(now)
    assert [x["code"] for x in got] == ["000001"], "恢复后补推"


def test_send_failure_does_not_advance(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    now = datetime(2026, 11, 24, 9, 25, 5)   # 早盘首拍（≤09:31 ⇒ 首个状态正常推送）
    env.pool = [_item("600467")]
    monkeypatch.setattr(W, "send_feishu_payload", lambda card, **kw: False)
    assert [x["code"] for x in W._tick(now)] == ["600467"]
    assert [x["code"] for x in W._tick(now)] == ["600467"], "发送失败不推进 ⇒ 下拍重试"


def test_window_gate(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.pool = [_item("600467")]
    assert W._tick(datetime(2026, 11, 24, 8, 50)) == [], "开盘前不推"
    assert W._tick(datetime(2026, 11, 24, 15, 30)) == [], "收盘后不推"
    assert W._tick(datetime(2026, 11, 21, 10, 0)) == [], "周六不推"


def test_silent_takeover_after_0931(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    env.pool = [_item("600467"), _item("000001")]
    assert W._tick(datetime(2026, 11, 24, 11, 0)) == [], "盘中重启：首拍静默接管"
    assert env.sent == []
    env.pool.append(_item("002770"))
    got = W._tick(datetime(2026, 11, 24, 11, 0, 10))
    assert [x["code"] for x in got] == ["002770"], "接管后只推新的"


def test_card_lists_code_name_tag(monkeypatch):
    monkeypatch.setattr("core.concept_tags.label_of",
                        lambda c: {"board": "大农业", "concepts": ["渔业"]})
    card = W._build_card([_item("600467", "好当家"), _item("301209", "联合化学", rate=20.0)],
                         datetime(2026, 11, 24, 10, 0, 5))
    txt = json.dumps(card, ensure_ascii=False)
    assert "好当家（600467）" in txt and "首板" in txt and "大农业" in txt
    assert "联合化学（301209）" in txt and "20cm" in txt
