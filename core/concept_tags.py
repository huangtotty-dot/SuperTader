# -*- coding: utf-8 -*-
"""概念标签的**展示用**读取（2026-11-02 owner：「飞书突破推送要注明来自概念哪个板块」）。

主板块选法与 `gui._concept_board()` / `oversold_gap_confirm.prescreen.sym2board_map()`
**逐码一致**（同一个 MERGES、同样剔除「北交所/央企」这类机构标签，取 `category{1..9}`
第一个有值的）。这里是**第三份实现**，不是疏漏：

  · `gui` 不能 import 策略包（`execution/auto/` 只在一个部署里存在）；
  · `main.py` 不能 import `gui`（GUI 是它拉起的**子进程**，模块级副作用重）。

两份既有副本由 `tests/phase3/test_market_beacon.py::TestBoardMapSingleSource` 守卫；
本模块由 `tests/phase3/test_price_alert_concept_label.py` 逐码比对 —— **改任一侧都要同步三侧**。

⚠️ 只做展示，不参与任何判定/排序/过滤。读不到 watchlist ⇒ 返回空标签，推送照发。
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
_WL_FP = BASE / "stock_hunter" / "watchlist_concept.json"

# 与 gui._concept_board() / prescreen.MERGES 同步（创新药/中药 归入 医药）
MERGES = {"创新药": "医药", "中药": "医药"}
# 机构/市场标签，不是题材板块
NON_BOARD_LABELS = {"北交所", "央企"}

_CACHE = {"key": None, "map": {}}


def _concepts(info: dict) -> list:
    """合并 `concept1..9`（有编号就不再看旧字段）+ 旧 `concept`，去重保序。

    与 `gui._concepts` 同口径：值可以是 list/tuple，也可以是 `|` 分隔的字符串。
    """
    parts = []

    def _collect(v):
        if isinstance(v, (list, tuple)):
            for x in v:
                if x and str(x).strip():
                    parts.append(str(x).strip())
        elif v and str(v).strip():
            parts.append(str(v).strip())

    for i in range(1, 10):
        _collect(info.get(f"concept{i}"))
    if not parts:
        _collect(info.get("concept"))
    seen, out = set(), []
    for p in parts:
        for c in p.split("|"):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _build() -> dict:
    """{6位码: {"board": 主板块, "concepts": [概念...]}}。文件变（mtime_ns+size）才重算。"""
    try:
        st = _WL_FP.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _CACHE["key"] == key:
        return _CACHE["map"]
    out = {}
    try:
        data = json.loads(_WL_FP.read_text(encoding="utf-8"))
        for code, info in (data.items() if isinstance(data, dict) else []):
            if not str(code).isdigit() or not isinstance(info, dict):
                continue
            cats = []
            for i in range(1, 10):
                c = str(info.get(f"category{i}", "") or "").strip()
                if c:
                    cats.append(MERGES.get(c, c))
            if not cats:
                c0 = str(info.get("category", "") or "")
                cats = [MERGES.get(x.strip(), x.strip()) for x in c0.split("|") if x.strip()]
            cats = [c for c in cats if c not in NON_BOARD_LABELS]
            out[str(code)] = {"board": cats[0] if cats else "",
                              "concepts": _concepts(info)}
    except Exception:
        return _CACHE["map"]      # 读坏：沿用上一次（首读失败就是空）
    _CACHE.update({"key": key, "map": out})
    return out


def board_of(code) -> str:
    """主板块名（与猎手/风向标「板块」列同口径）；无标签返回空串。"""
    return (_build().get(str(code).split("_")[0]) or {}).get("board", "")


def concepts_of(code) -> list:
    """该股全部概念板块（去重保序）；无标签返回 []。"""
    return list((_build().get(str(code).split("_")[0]) or {}).get("concepts") or [])


def label_of(code, max_concepts: int = 3) -> dict:
    """{board, concepts} —— concepts 截断到 max_concepts（飞书一行放不下太多）。"""
    ent = _build().get(str(code).split("_")[0]) or {}
    cs = list(ent.get("concepts") or [])
    if max_concepts and max_concepts > 0:
        cs = cs[:int(max_concepts)]
    return {"board": ent.get("board", ""), "concepts": cs}
