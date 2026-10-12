# -*- coding: utf-8 -*-
"""产物新鲜度巡检（2026-10-12，owner：P1）

**为什么单独有这么一个东西**
2026-10-12 owner 说「选股猎手轮询报异常效果不是很好」，深挖后结论是：`core.hunter_poll` 校验的是
**"我能不能重跑猎手管线"**，而 owner 真正的风险是**"我正在看的数据还新不新"** —— 后者那个监控
**一个都看不到**。有据可查的同类事故：
  · 30min 本地缓存曾**陈旧 3 周**（末根 2026-09-18）
  · 6 个 `chart_payload` 末根是**盘前假当日根**（O=H=L=C 且 volume=0）
  · `daily_kline` / `tushare_mins` 是否被盘后预下载刷新，此前**没有任何巡检**
本模块就盯这一件事：**owner 真正消费的产物，末根数据日期是不是够新**。

**零网络**：只读磁盘缓存，比日期。新鲜度判据复用 `core.chart_cache`（"交易日近似"：
末行数据日期距今自然日数 ≤ `MAX_DISPLAY_GAP_DAYS`，可跨周末/长假命中）。

**只读、不写生产数据**：唯一写的是巡检自己的历史（`t_io/state/freshness_patrol_history.jsonl`）
与当日标记（`..._YYYY-MM-DD.json`，用于跨重启幂等）。

**防噪**（今日教训：没校准阈值的报警 = 死报警）：
卡里**始终列出全部数字**（信息给足），但 ⚠️ 判定只看**新出现**的陈旧 ——
与上一轮相比从"新鲜"变"陈旧"的才打断；持续陈旧的标"持续 N 天"，不每轮刷屏。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from core import chart_cache as _cc

log = logging.getLogger("freshness_patrol")

_BASE = _cc._BASE
_STATE_DIR = _BASE / "t_io" / "state"
_HISTORY_FP = _STATE_DIR / "freshness_patrol_history.jsonl"
_MARK_FP_FMT = "freshness_patrol_{date}.json"

_DAILY_DIR = _cc._DAILY_CACHE_DIR
_PAYLOAD_DIR = _cc._PAYLOAD_DIR
_MINUTE_DIR = _cc._MINUTE_CACHE_DIR

FRESH_GAP_DAYS = _cc.MAX_DISPLAY_GAP_DAYS       # 12：超此即"陈旧"（与展示口径同一把尺）
# 超过此天数算**长期停更**（退市/长期停牌），单列不报警。
# ⚠️ 为什么必须分桶：daily_kline 里有一批退市股（实测 000618 末根 2006-02-10、300104 2020-07-20、
# 000511 2018-07-17…），它们**永远**是"陈旧"的。若不隔离，卡片头条永远是这些僵尸代码 ⇒
# owner 会学会忽略这条告警（正是今天刚下的结论：没校准的报警 = 死报警）。
_LONG_DEAD_DAYS = 90
_SAMPLE_DAILY = 400                             # daily_kline 有 5000+ 文件 ⇒ 采样（固定步长，可复现）
_SAMPLE_MINUTE = 200


def _now(now=None) -> datetime:
    return now or datetime.now()


def _safe_json(fp):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _stats(name, entries, threshold=FRESH_GAP_DAYS):
    """entries: [(key, gap, extra)] → 汇总。gap 很大表示停更/读不出。

    分两桶：`stale` = 超阈值但仍在"还能救"的窗口内（可行动）；
    `dormant` = 超过 `_LONG_DEAD_DAYS`（退市/长期停牌，永远陈旧，不进告警）。
    """
    stale = [(k, g, e) for k, g, e in entries if threshold < g <= _LONG_DEAD_DAYS]
    dormant = [k for k, g, _ in entries if g > _LONG_DEAD_DAYS]
    # ⚠️ 中位/最差只在"非休眠"样本上算：退市股那 7500d 会把整条曲线带偏
    alive = sorted(g for _, g, _ in entries if g <= _LONG_DEAD_DAYS)
    return {
        "name": name,
        "total": len(entries),
        "stale": len(stale),
        "dormant": len(dormant),
        "ratio": round(len(stale) / len(entries), 4) if entries else 0.0,
        "worst_gap": (alive[-1] if alive else None),
        "median_gap": (alive[len(alive) // 2] if alive else None),
        "examples": [f"{k} gap={g}d{e}" for k, g, e in
                     sorted(stale, key=lambda x: -x[1])[:4]],
        "threshold": threshold,
    }


def _sample(files, cap):
    """固定步长采样（可复现，不用随机——便于跨日比较同一批文件）。"""
    if len(files) <= cap:
        return list(files)
    step = len(files) / float(cap)
    return [files[int(i * step)] for i in range(cap)]


def survey_daily_kline(now=None, sample=_SAMPLE_DAILY) -> list:
    """日线缓存：个股 `{code}.json` 与指数 `index_{code}.json` 分开统计。"""
    now = _now(now)
    try:
        allf = sorted(p for p in _DAILY_DIR.glob("*.json"))
    except Exception:
        return []
    indiv = [p for p in allf if not p.name.startswith("index_")]
    index = [p for p in allf if p.name.startswith("index_")]
    out = []
    for label, files, cap in (("daily_kline", indiv, sample),
                              ("daily_kline_index", index, max(50, len(index)))):
        entries = []
        for fp in _sample(files, cap):
            rec = _safe_json(fp)
            rows = (rec or {}).get("rows") or []
            g = _cc.natural_gap_days(_cc.last_row_date(rows), now) if rows else 9999
            entries.append((fp.stem, g, "" if rows else " 无可读行"))
        st = _stats(label, entries)
        st["files_total"] = len(files)
        st["sampled"] = len(entries)
        out.append(st)
    return out


def survey_payload(now=None) -> dict:
    """图表 payload：末根日线日期 + **假当日根**（盘前快照造出来的 O=H=L=C 且 volume=0）。

    假当日根是本仓咬过两次的坑（600362「已站上5日线却报破线」）⇒ 巡检要能提前发现存量坏件。
    """
    now = _now(now)
    entries, fakes = [], []
    try:
        files = sorted(_PAYLOAD_DIR.glob("*.json"))
    except Exception:
        files = []
    for fp in files:
        rec = _safe_json(fp) or {}
        g = _cc.natural_gap_days(rec.get("last_daily_date"), now)
        entries.append((fp.stem, g, ""))
        if _is_fake_forming(rec):
            fakes.append(fp.stem)
    st = _stats("chart_payload", entries)
    st["fake_forming"] = len(fakes)
    st["fake_examples"] = fakes[:6]
    return st


def _is_fake_forming(rec: dict) -> bool:
    """末根与写入同日、且 volume<=0 且 high==low ⇒ 盘前快照造出的假当日根。

    ⚠️ 一字板 volume>0 必须放行（判据不依赖 volume 字段是否存在）。
    """
    rows = (rec or {}).get("daily_rows") or []
    if not rows:
        return False
    last = rows[-1] or {}
    saved = str(rec.get("saved_at") or "")[:10]
    if not saved or str(last.get("date") or "")[:10] != saved:
        return False
    try:
        vol = float(last.get("volume") or 0)
        hi, lo = float(last.get("high")), float(last.get("low"))
    except Exception:
        return False
    return vol <= 0 and hi == lo


def survey_minutes(now=None, sample=_SAMPLE_MINUTE) -> dict:
    """分钟线缓存（5/30min 等）：末行**时间**距今自然日数。"""
    now = _now(now)
    try:
        files = sorted(p for p in _MINUTE_DIR.glob("*.json"))
    except Exception:
        files = []
    entries = []
    for fp in _sample(files, sample):
        rec = _safe_json(fp) or {}
        rows = rec.get("rows") if isinstance(rec, dict) else None
        rows = rows or (rec if isinstance(rec, list) else [])
        last = ""
        if rows:
            last = str((rows[-1] or {}).get("time") or (rows[-1] or {}).get("date") or "")
        entries.append((fp.stem, _cc.natural_gap_days(last, now), ""))
    st = _stats("minutes", entries)
    st["files_total"] = len(files)
    st["sampled"] = len(entries)
    return st


def survey(now=None) -> dict:
    """一次完整巡检（**零网络**）。返回 {"ts","date","artifacts":[...],"newly_stale":[...]}"""
    now = _now(now)
    arts = survey_daily_kline(now) + [survey_payload(now), survey_minutes(now)]
    arts = [a for a in arts if a]
    prev = _prev_record()
    prev_stale = set((prev or {}).get("stale_names") or [])
    newly = []
    for a in arts:
        a["was_stale"] = a["name"] in prev_stale
        if a["stale"] > 0 and not a["was_stale"]:
            newly.append(a["name"])
        a["verdict"] = ("陈旧" if a["stale"] > 0 else "新鲜")
    return {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "artifacts": arts,
        "stale_names": [a["name"] for a in arts if a["stale"] > 0],
        "newly_stale": newly,
    }


def _prev_record() -> dict:
    """上一条历史记录（用于区分"新出现"与"持续"）。"""
    try:
        last = None
        with open(_HISTORY_FP, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last else {}
    except Exception:
        return {}


def format_card(rep: dict) -> dict:
    """飞书卡：**数字全给**，⚠️ 只留给"新出现"的陈旧（防每轮刷屏）。"""
    newly = rep.get("newly_stale") or []
    head = ("⚠️ " if newly else "✅ ") + f"产物新鲜度巡检 · {rep['ts'][5:16]}"
    lines = [head]
    for a in rep["artifacts"]:
        extra = f" 假当日根{a['fake_forming']}" if a.get("fake_forming") else ""
        dorm = f" 休眠{a['dormant']}" if a.get("dormant") else ""
        if a["stale"]:
            mark = "🆕" if a["name"] in newly else "持续"
            lines.append(f"· {a['name']} 陈旧 {a['stale']}/{a['total']}"
                         f"（{a['ratio'] * 100:.1f}%，最差 {a['worst_gap']}d）{mark}{extra}{dorm}")
            # 新出现的才给样例（可行动）；持续的只报数，免得刷屏
            if a["name"] in newly and a.get("examples"):
                lines.append("   " + " / ".join(a["examples"][:2]))
        else:
            lines.append(f"· {a['name']} 新鲜 {a['total']} 只"
                         f"（最差 {a['worst_gap']}d）{extra}{dorm}")
    if newly:
        lines.append(f"❌ 新出现陈旧: {', '.join(newly)}（上一轮还是新鲜的）")
    lines.append(f"· 零网络巡检；「持续」=上轮也陈旧；休眠=停更>{_LONG_DEAD_DAYS}d(疑退市，不计告警)")
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def push(rep: dict) -> bool:
    card = format_card(rep)
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(card,
                                 success_log=f"产物新鲜度巡检: {rep.get('date')}",
                                 error_prefix="产物新鲜度巡检推送")
    except Exception as e:
        log.warning(f"⚠️ 新鲜度巡检推送抛异常: {type(e).__name__}: {str(e)[:180]}")
        return False
    if not ok:
        log.warning("⚠️ 新鲜度巡检推送返回失败")
    return bool(ok)


def _append_history(rep: dict, pushed: bool) -> None:
    """落历史（供误报率复盘）：把每轮的原始数字都留下，别只留结论。"""
    try:
        os.makedirs(str(_STATE_DIR), exist_ok=True)
        rec = {**rep, "pushed": bool(pushed)}
        with open(_HISTORY_FP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"新鲜度巡检历史落盘失败: {type(e).__name__}: {e}")


def run_once(now=None, force: bool = False, dry_run: bool = False) -> dict:
    """巡检 → 推飞书 → 落历史。**不抛**。

    幂等：当日标记文件存在即跳过（跨进程/跨重启都成立）。`force=True` 绕过（手动/测试用），
    `dry_run=True` 只算不推不落盘。
    """
    now = _now(now)
    today = now.strftime("%Y-%m-%d")
    mark = _STATE_DIR / _MARK_FP_FMT.format(date=today)
    # ⚠️ `dry_run` 不走幂等闸：它是"只算不推"的入口，当日已跑过也要能再算一遍
    #    （否则当日标记一落，dry_run 就只回 {"skipped"} —— 单测踩到过）。
    if not dry_run and not force and mark.exists():
        return {"skipped": "already_ran_today", "date": today}
    try:
        rep = survey(now)
    except Exception as e:
        log.warning(f"⚠️ 新鲜度巡检自身失败: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}
    if dry_run:
        return {**rep, "pushed": None, "dry_run": True}
    pushed = push(rep)
    _append_history(rep, pushed)
    if pushed:
        try:
            os.makedirs(str(_STATE_DIR), exist_ok=True)
            with open(mark, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return {**rep, "pushed": pushed}
