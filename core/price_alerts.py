# -*- coding: utf-8 -*-
"""价格突破提醒 · **画框版**（2026-10-28 owner）。

在 K 线图上按住拖一个矩形：**上沿向上突破**报警、**下沿向下跌破**也报警。
（原「右键点一个价位」的写法已废弃，改成画框 —— 一个框同时管上下两个方向。）

分工（三个进程共用这一个文件，所以读写都必须是原子的）：
  · GUI（t_gui.py）：画框/删除（`add_box`/`remove`）、读事件响铃（`load`）；
  · main.py 的 10s watcher：读 armed 框 → 拉快照 → 命中就 `mark_triggered` + 推飞书；
  · 两边都**只**通过本模块落盘（tmp + os.replace），绝不留半截 JSON。

结构：
{
  "version": 3,
  "items": {"600900": [{"id","code","kind":"box","upper","lower","x0","x1","created",
                        "up_at","up_price","down_at","down_price","last_px"}]},
  "events": [{"id","code","name","side":"up"|"down","level","price","ts"}]
}

口径（**只认穿越 + 可重复**）：上一拍 < 上沿 ≤ 这一拍 ⇒ 上破；上一拍 > 下沿 ≥ 这一拍 ⇒ 下破。
画框时把当时的现价存进 `last_px`，所以"框整个画在现价上方/下方"不会一画上去就误报。

**一个框一天可以报很多次**（owner 2026-10-28：「价格有可能全天波动 一会在价格上面一会在
价格下面 需要连续报 不要只报一次」）。旧实现 `up_at`/`down_at` 落定后该边永久退休，
穿过一次就哑了。现在改成：每穿一次记一次（`up_n`/`down_n` 累计，`up_at`/`down_at` 只留
**最近一次**），并且同一侧要**价格先回到框内**（留 `REARM_PCT` 余量）才允许再报 ——
贴边抖动（30.89/30.91 来回）不刷屏，真实来回（30.95→30.60→31.10）一次不漏。
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
STORE = BASE / "t_io" / "state" / "price_alerts.json"
LOCK = threading.Lock()
KEEP_EVENTS = 200

# 同一侧"回框重武装"的余量：价格要先退回框内这么深，才允许这一侧再报。
# 0.2% 是 A 股 1 分钟噪声的常见量级；低价标的（ETF 0.65 元）按百分比只有 0.0013，
# 低于最小变动价，故兜底 REARM_MIN 一手（ETF 跳 0.001 ⇒ 0.01 = 10 跳）。
REARM_PCT = 0.002
REARM_MIN = 0.01


def _empty() -> dict:
    return {"version": 3, "items": {}, "events": []}


def load() -> dict:
    """读库。坏文件/缺文件 → 空库（fail-open，绝不让画框把调用方拖垮）。"""
    try:
        d = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), dict):
            d.setdefault("events", [])
            d["version"] = 3
            # v2 旧框没有 up_n/down_n（那时每边只报一次）⇒ 按 up_at/down_at 补成 1，
            # 否则图上会显示"已触发过但 0 次"
            for items in (d.get("items") or {}).values():
                for x in (items or []):
                    if not isinstance(x, dict) or x.get("kind") != "box":
                        continue
                    if x.get("up_n") is None:
                        x["up_n"] = 1 if x.get("up_at") else 0
                    if x.get("down_n") is None:
                        x["down_n"] = 1 if x.get("down_at") else 0
            return d
    except Exception:
        pass
    return _empty()


def save(d: dict) -> None:
    """tmp + 原子替换：GUI 与 main.py 是两个进程，**绝不能写一半**。"""
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STORE)
    except Exception:
        pass


def add_box(code: str, upper: float, lower: float,
            x0: str | None = None, x1: str | None = None,
            ref_px: float | None = None) -> list:
    """加一个画框（同上下沿、两边都未触发时幂等）。返回该票的 items。

    `ref_px` = 画框那一刻的现价；存进 `last_px` 供「只认穿越」用。
    """
    c = str(code).split("_")[0].strip()
    try:
        up = round(float(upper), 3)
        dn = round(float(lower), 3)
    except Exception:
        return []
    if not c or up <= 0 or dn <= 0:
        return []
    if up < dn:
        up, dn = dn, up
    if up == dn:
        return []
    with LOCK:
        d = load()
        items = d["items"].setdefault(c, [])
        # 同上下沿就算同一个框（**不再豁免已触发过的**：现在会一直报，重复画只会变成两个
        # 一模一样、各自报一次的框 ⇒ 每条提醒都翻倍）
        dup = any(x.get("kind") == "box"
                  and abs(float(x.get("upper") or 0) - up) < 1e-9
                  and abs(float(x.get("lower") or 0) - dn) < 1e-9 for x in items)
        if not dup:
            items.append({
                "id": f"{c}-{int(_time.time() * 1000)}",
                "code": c, "kind": "box",
                "upper": up, "lower": dn,
                "x0": (str(x0)[:10] if x0 else None),
                "x1": (str(x1)[:10] if x1 else None),
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                # 最近一次（只用于图上的标签/颜色）+ 累计次数
                "up_at": None, "up_price": None, "up_n": 0,
                "down_at": None, "down_price": None, "down_n": 0,
                # 该侧当前是否可以再报（价格回到框内后自动置 True）
                "up_armed": True, "down_armed": True,
                "last_px": (round(float(ref_px), 3) if ref_px not in (None, "") else None),
            })
            save(d)
        return list(items)


def update_box(code: str, alert_id: str, upper: float, lower: float,
               ref_px: float | None = None) -> list:
    """改某个框的上下沿（2026-10-28 owner：改完要同步到多窗口）。

    ***这是「修改」而不是「再加一个」***：同一个 id 原地改价，并把两边触发状态**重置**
    （新价位 = 新提醒）；`last_px` 一起更新为改框时的现价，继续按「只认穿越」判。
    """
    c = str(code).split("_")[0].strip()
    try:
        up = round(float(upper), 3)
        dn = round(float(lower), 3)
    except Exception:
        return []
    if not c or up <= 0 or dn <= 0:
        return []
    if up < dn:
        up, dn = dn, up
    if up == dn:
        return []
    with LOCK:
        d = load()
        hit = False
        for x in (d["items"].get(c) or []):
            if x.get("id") != alert_id:
                continue
            x.update({
                "kind": "box", "upper": up, "lower": dn,
                "up_at": None, "up_price": None, "up_n": 0, "up_armed": True,
                "down_at": None, "down_price": None, "down_n": 0, "down_armed": True,
                "last_px": (round(float(ref_px), 3) if ref_px not in (None, "") else None),
                "edited": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            hit = True
            break
        if hit:
            save(d)
        return list(d["items"].get(c) or [])


def remove(code: str, alert_id: str | None = None) -> list:
    """删一个框（给 alert_id）或清空该票全部（alert_id 为空）。返回剩余 items。"""
    c = str(code).split("_")[0].strip()
    with LOCK:
        d = load()
        if not alert_id:
            d["items"].pop(c, None)
        else:
            left = [x for x in (d["items"].get(c) or []) if x.get("id") != alert_id]
            if left:
                d["items"][c] = left
            else:
                d["items"].pop(c, None)
        save(d)
        return list(d["items"].get(c) or [])


def armed() -> list:
    """所有还有效的框 [{code, item}]。

    ⚠️ **不再退休**（owner 2026-10-28）。旧实现"两边都触发过"就移出扫描，
    而"全天波动、一会儿上一会儿下"恰恰要求它一直在。
    """
    d = load()
    out = []
    for c, items in (d.get("items") or {}).items():
        for x in (items or []):
            if x.get("kind") != "box":
                continue
            try:
                if float(x.get("upper") or 0) > 0 and float(x.get("lower") or 0) > 0:
                    out.append({"code": c, "item": x})
            except Exception:
                continue
    return out


def _rearm_gap(edge: float) -> float:
    return max(abs(float(edge)) * REARM_PCT, REARM_MIN)


def _fire(d: dict, item: dict, code: str, side: str, level: float,
          price: float, name: str, now: datetime) -> dict:
    """记一次触发并追加事件。**id 带累计序号** —— GUI 靠 id 去重响铃，
    序号如果固定成 `#up`，第二次穿越就会被前端当成旧事件、静默吞掉。"""
    n = int(item.get(f"{side}_n") or 0) + 1
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    item[f"{side}_n"] = n
    item[f"{side}_at"] = ts
    item[f"{side}_price"] = round(float(price), 3)
    ev = {"id": f"{item.get('id')}#{side}#{n}", "code": code, "name": name or code,
          "side": side, "level": round(float(level), 3),
          "price": round(float(price), 3), "ts": ts, "n": n}
    evs = list(d.get("events") or [])
    evs.append(ev)
    d["events"] = evs[-KEEP_EVENTS:]
    return ev


def evaluate(code: str, alert_id: str, px: float, name: str = "") -> list:
    """一拍的全套判定（**原子**：记价 → 回框重武装 → 判穿越 → 落事件 → 存盘）。

    返回这条框本拍**新产生**的事件列表（0~1 条；上下沿不可能同拍都成立，因为 up>lower）。

    规则：
      · 穿越：上一拍 < 上沿 ≤ 这一拍 ⇒ 上破；上一拍 > 下沿 ≥ 这一拍 ⇒ 下破；
      · 同一侧报过之后要**价格回到框内**（`_rearm_gap` 余量）才重新可报 —— 既满足
        owner「来回穿越要连续报」，又不会被贴边抖动每 10s 刷一次；
      · `prev is None`（第一次观测）只记价不判：画框时已写入现价，不该一画上就报。
    """
    c = str(code).split("_")[0].strip()
    try:
        v = round(float(px), 3)
    except Exception:
        return []
    if not c or v <= 0:
        return []
    now = datetime.now()
    with LOCK:
        d = load()
        item = None
        for x in (d["items"].get(c) or []):
            if x.get("id") == alert_id:
                item = x
                break
        if item is None:
            return []
        try:
            up = float(item.get("upper") or 0)
            dn = float(item.get("lower") or 0)
        except Exception:
            return []
        if not (up > 0 and dn > 0 and up > dn):
            return []
        pv = item.get("last_px")
        try:
            prev = float(pv) if pv not in (None, "") else None
        except Exception:
            prev = None

        changed = (pv != v)
        item["last_px"] = v

        up_armed = was_up = bool(item.get("up_armed", True))
        dn_armed = was_dn = bool(item.get("down_armed", True))
        if not up_armed and v <= up - _rearm_gap(up):
            up_armed = True                     # 价格退回框内 ⇒ 上沿重新可报
        if not dn_armed and v >= dn + _rearm_gap(dn):
            dn_armed = True                     # 价格退回框内 ⇒ 下沿重新可报

        evs = []
        if prev is not None:
            if up_armed and prev < up <= v:
                evs.append(_fire(d, item, c, "up", up, v, name, now))
                up_armed = False
            if dn_armed and prev > dn >= v:
                evs.append(_fire(d, item, c, "down", dn, v, name, now))
                dn_armed = False

        item["up_armed"] = up_armed
        item["down_armed"] = dn_armed
        if changed or evs or up_armed != was_up or dn_armed != was_dn:
            save(d)
        return evs
