# -*- coding: utf-8 -*-
"""电力运营「发电方式」标签（owner 2026-11-08）。

owner：「电力板块中的电力运营，请给每支股一个标签：火电？核电？水电？还是储能相关？」

实现：
  · 数据 = watchlist 的 `jiuyan_power_type`（tag_power_type_20261108.py 逐只判定），
    同时把类型写进 `jiuyan_concept_reason['电力运营']`（`（发电运营·水电）`）；
  · 猎手 `_load_hunter_impl` 把该字段注入个股行（`power_type`）；
  · 前端 `powerTypeBadge()` 在个股名称后显示彩色小徽章。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

_WL = json.loads((_ROOT / "stock_hunter" / "watchlist_jiuyan.json").read_text(encoding="utf-8"))
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_PY = (_ROOT / "gui.py").read_text(encoding="utf-8")


def _pairs(e):
    out = []
    for k in range(1, 10):
        c = str(e.get(f"jiuyan_category{k}") or "").strip()
        if c:
            out.append((c, str(e.get(f"jiuyan_concept{k}") or "").strip()))
    if out:
        return out
    cats = [x.strip() for x in str(e.get("jiuyan_category") or "").split("|") if x.strip()]
    cons = [x.strip() for x in str(e.get("jiuyan_concept") or "").split("|") if x.strip()]
    return [(c, cons[i] if i < len(cons) else "") for i, c in enumerate(cats)]


def _power_ops():
    return {c: e for c, e in _WL.items()
            if isinstance(e, dict) and any(cc == "电力" and cn == "电力运营" for cc, cn in _pairs(e))}


def test_每只电力运营股都有发电方式标签():
    members = _power_ops()
    assert len(members) >= 50
    missing = [c for c, e in members.items() if not str(e.get("jiuyan_power_type") or "").strip()]
    assert not missing, f"未打标：{missing}"


def test_标签取值在允许集合内():
    allowed = {"水电", "火电", "核电", "风光", "光伏", "热电联产", "供热", "电网供电", "综合能源", "待定"}
    bad = {c: e.get("jiuyan_power_type") for c, e in _power_ops().items()
           if e.get("jiuyan_power_type") not in allowed}
    assert not bad, f"非法类型：{bad}"


def test_理由里带上类型():
    for c, e in list(_power_ops().items())[:10]:
        txt = str((e.get("jiuyan_concept_reason") or {}).get("电力运营") or "")
        assert "发电运营·" in txt, f"{c} 理由缺类型：{txt}"


def test_后端把类型注入猎手个股行():
    assert 'jiuyan_power_type' in _PY
    assert '_s["power_type"] = _t' in _PY


def test_前端渲染发电方式徽章():
    assert "function powerTypeBadge(t)" in _JS
    assert "POWER_TYPE_COLORS" in _JS
    assert "${powerTypeBadge(s.power_type)}" in _JS
