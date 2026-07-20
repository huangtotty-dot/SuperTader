# -*- coding: utf-8 -*-
"""
support_resistance.py — 支撑位/压力位（简化版）

只做三件事：
1. 中枢 PP = (昨高 + 昨低 + 昨收) / 3
2. 第一支撑 S1 = 2*PP - 昨高
3. 第一压力 R1 = 2*PP - 昨低
4. 判断：现价在 PP 上方还是下方
"""

from typing import Dict, Any, List, Optional
import json
import os


def calc_pivot_levels(code: str, holding: dict, daily_ctx: dict) -> Dict[str, Any]:
    name = holding.get("name", code)
    price = float(holding.get("pre_close", 0) or daily_ctx.get("daily_prev_close", 0) or 0)

    y_high = float(daily_ctx.get("daily_prev_high", 0) or 0)
    y_low = float(daily_ctx.get("daily_prev_low", 0) or 0)
    y_close = float(daily_ctx.get("daily_prev_close_real", 0) or 0)

    if y_high <= 0 or y_low <= 0 or y_close <= 0:
        return {"code": code, "name": name, "ref_price": price}

    pp = round((y_high + y_low + y_close) / 3, 2)
    r1 = round(2 * pp - y_low, 2)
    s1 = round(2 * pp - y_high, 2)

    if price >= pp:
        bias = "偏多"
    else:
        bias = "偏空"

    return {
        "code": code,
        "name": name,
        "ref_price": round(price, 2),
        "PP": pp,
        "R1": r1,
        "S1": s1,
        "bias": bias,
    }


def format_pivot_text(all_levels: List[Dict[str, Any]], max_stocks: int = 8) -> str:
    lines = []
    for item in all_levels[:max_stocks]:
        if not item.get("PP"):
            continue
        name = item["name"]
        code = item["code"]
        price = item["ref_price"]
        pp = item["PP"]
        r1 = item["R1"]
        s1 = item["S1"]
        bias = item["bias"]
        lines.append(f"{name} {code}")
        lines.append(f"中枢{pp}  压力{r1}  支撑{s1}  {bias}")
    return "\n".join(lines)


def calc_for_holdings(holdings: Dict[str, dict]) -> List[Dict[str, Any]]:
    results = []
    for code, holding in holdings.items():
        try:
            daily_ctx = get_daily_context(code, holding)
            levels = calc_pivot_levels(code, holding, daily_ctx)
            if levels.get("PP"):
                results.append(levels)
        except Exception as e:
            log.debug(f"⚠️  {code} 支撑压力计算失败: {str(e)[:80]}")
            continue
    return results


# ==================== 盘后复盘日志 ====================

PIVOT_LOG_FILE = os.path.join(T_IO_DIR, "pivot_audit.jsonl")


def pivot_audit(now: Optional[datetime] = None) -> str:
    """
    盘后审计：对比 pivot 预测的 S1/R1 与实际走势。
    写入 pivot_audit.jsonl，返回一段可打日志的摘要。

    判定标准（S1/R1 触碰阈值 = 价位 ±0.2%）：
    - S1 有效：最低价接近或跌破 S1，但收盘 > S1 → 支撑确认
    - S1 无效：最低价跌破 S1 且收盘 < S1 → 支撑失效
    - R1 有效：最高价接近或突破 R1，但收盘 < R1 → 压力确认
    - R1 无效：最高价突破 R1 且收盘 > R1 → 压力失效
    """
    now = now or _now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.time() < dtime(13, 0):
        return ""

    holdings = load_holdings()
    records = []
    summary_parts = []

    for code, holding in holdings.items():
        try:
            daily_ctx = get_daily_context(code, holding)
            levels = calc_pivot_levels(code, holding, daily_ctx)
            if not levels.get("PP"):
                continue

            pp = levels["PP"]
            r1 = levels["R1"]
            s1 = levels["S1"]

            # 从分钟线获取今日实际高、低、收
            df = fetch_minute_bar(code, is_etf=holding.get("type") == "etf")
            if df.empty:
                continue
            today_df = df[df["date"] == pd.to_datetime(today).date()] if "date" in df.columns else df
            if today_df.empty:
                today_df = df
            day_high = float(today_df["high"].max())
            day_low = float(today_df["low"].min())
            day_close = float(today_df.iloc[-1]["close"])

            # ---- S1 判定 ----
            s1_verdict = "未触及"
            touched_s1 = day_low <= s1 * 1.002
            if touched_s1:
                if day_close > s1:
                    s1_verdict = "支撑确认"
                else:
                    s1_verdict = "支撑失效"

            # ---- R1 判定 ----
            r1_verdict = "未触及"
            touched_r1 = day_high >= r1 * 0.998
            if touched_r1:
                if day_close < r1:
                    r1_verdict = "压力确认"
                else:
                    r1_verdict = "压力失效"

            records.append({
                "date": today,
                "code": code,
                "name": holding.get("name", code),
                "ref_price": round(ref_price, 2),
                "PP": pp,
                "R1": r1,
                "S1": s1,
                "day_high": round(day_high, 2),
                "day_low": round(day_low, 2),
                "day_close": round(day_close, 2),
                "s1_verdict": s1_verdict,
                "r1_verdict": r1_verdict,
                "close_vs_pp": "之上" if day_close >= pp else "之下",
            })

            if s1_verdict != "未触及" or r1_verdict != "未触及":
                summary_parts.append(
                    f"{holding.get('name', code)}({code}) "
                    f"S1:{s1_verdict} R1:{r1_verdict}"
                )

        except Exception as e:
            log.debug(f"⚠️  {code} pivot 审计异常: {str(e)[:80]}")
            continue

    if not records:
        return ""

    # 写入 JSONL
    try:
        os.makedirs(os.path.dirname(PIVOT_LOG_FILE), exist_ok=True)
        with open(PIVOT_LOG_FILE, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"⚠️  pivot_audit 写入失败: {str(e)[:80]}")

    if summary_parts:
        summary = f"📊 Pivot支撑/压力复盘 | {' | '.join(summary_parts)}"
        log.info(summary)
        return summary
    return ""


def read_pivot_audit(days: int = 5) -> str:
    """读取最近 N 天的 pivot 审计日志，返回可读摘要"""
    if not os.path.exists(PIVOT_LOG_FILE):
        return "暂无 pivot 审计数据"

    from datetime import timedelta
    cutoff = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    records = []
    try:
        with open(PIVOT_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("date", "") >= cutoff:
                        records.append(rec)
                except Exception:
                    continue
    except Exception:
        return "读取失败"

    if not records:
        return f"近 {days} 天无 pivot 审计数据"

    lines = []
    for rec in records:
        name = rec.get("name", "")
        code = rec.get("code", "")
        date = rec.get("date", "")[5:]  # MM-DD
        pp = rec.get("PP", 0)
        r1 = rec.get("R1", 0)
        s1 = rec.get("S1", 0)
        dh = rec.get("day_high", 0)
        dl = rec.get("day_low", 0)
        dc = rec.get("day_close", 0)
        sv = rec.get("s1_verdict", "")
        rv = rec.get("r1_verdict", "")

        lines.append(
            f"{date} {name}({code})  "
            f"PP={pp} R1={r1} S1={s1}  "
            f"高={dh} 低={dl} 收={dc}  "
            f"S1:{sv}  R1:{rv}"
        )

    return "\n".join(lines)
