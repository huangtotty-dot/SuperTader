# -*- coding: utf-8 -*-
"""W33 G2: forward_tracker.py — 前瞻收益回填工具（每日 Review 运行，幂等）

读取每日 Review 报告的两张滚动前瞻表（加仓/建仓），按日线收盘价回填未完结行的浮盈% 与判定。
判定规则（W33 日志数据层方案 §3）:
  加仓: 次日浮盈 >+0.5% 优 / ±0.5% 内 平 / <−0.5% 劣
  建仓: 3 日内跌破触发日最低点 → 伪信号；否则 5 日浮盈 >0 → 成功（其余暂置平）

用法: python forward_tracker.py [--date 2026-08-13] [--report 报告路径]
  --report 缺省 = doc/每日复盘/{date}_复盘.md；文件不存在则退回模板 每日Review.md。
数据源: position_builder.fetch_daily_kline（本地缓存，无需网络）。
"""
import argparse
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
sys.path.insert(0, str(BASE))

from position_builder import fetch_daily_kline  # noqa: E402


def _num(v):
    try:
        return float(str(v).replace("%", "").replace(" ", ""))
    except Exception:
        return None


def _kline(code):
    try:
        df = fetch_daily_kline(code)
        if df is None or df.empty or "date" not in df.columns:
            return None
        df = df.copy()
        df["date"] = df["date"].astype(str)
        return df.sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def _close_at(df, date_str, offset):
    try:
        idx = df.index[df["date"] == date_str]
        if idx.empty:
            return None
        j = idx[0] + offset
        return float(df["close"].iloc[j]) if j < len(df) else None
    except Exception:
        return None


def _low_at(df, date_str):
    try:
        idx = df.index[df["date"] == date_str]
        return float(df["low"].iloc[idx[0]]) if not idx.empty else None
    except Exception:
        return None


def _pct_str(px, base):
    if px is None or not base:
        return None
    return round((px - base) / base * 100, 2)


def _parse_tables(lines):
    """识别两张前瞻表，返回 [{kind, header_idx, rows:[(lineno, cells)]}]（跳过分隔行/例行）。"""
    tables = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        kind = None
        if line.startswith("|") and "加仓价" in line and "次日浮盈" in line:
            kind = "add"
        elif line.startswith("|") and "确认价" in line and "3日浮盈" in line:
            kind = "build"
        if kind:
            header = [c.strip() for c in line.strip("|").split("|")]
            rows = []
            j = i + 1
            while j < len(lines):
                rl = lines[j].strip()
                if not rl.startswith("|"):
                    break
                cells = [c.strip() for c in rl.strip("|").split("|")]
                if all(set(c) <= set("-: ") for c in cells) or "例" in cells:
                    j += 1
                    continue
                rows.append((j, cells))
                j += 1
            tables.append({"kind": kind, "header": header, "rows": rows})
            i = j
        else:
            i += 1
    return tables


def _fill_row(kind, header, cells):
    """就地回填一行，返回 True 若有改动。列按表头名定位。"""
    idx = {h: i for i, h in enumerate(header)}
    changed = False

    if kind == "add":
        date, code = cells[idx["日期"]], cells[idx["代码"]]
        base = _num(cells[idx["加仓价"]])
        if not date or not code or base is None:
            return False
        df = _kline(code)
        if df is None:
            return False
        c1 = _close_at(df, date, 1)
        c3 = _close_at(df, date, 3)
        for col, off in (("次日浮盈%", 1), ("3日浮盈%", 3)):
            cell = cells[idx[col]]
            if cell in ("", "-"):
                px = c1 if off == 1 else c3
                v = _pct_str(px, base)
                if v is not None:
                    cells[idx[col]] = f"{v}"
                    changed = True
        # 判定（次日浮盈优先；有次日价才可判）
        if c1 is not None:
            ret = _pct_str(c1, base)
            cells[idx["判定"]] = "优" if ret > 0.5 else ("劣" if ret < -0.5 else "平")
            changed = True
        return changed

    # build 表
    date, code = cells[idx["建仓日"]], cells[idx["代码"]]
    base = _num(cells[idx["确认价"]])
    if not date or not code or base is None:
        return False
    df = _kline(code)
    if df is None:
        return False
    c3 = _close_at(df, date, 3)
    c5 = _close_at(df, date, 5)
    for col, off in (("3日浮盈%", 3), ("5日浮盈%", 5)):
        cell = cells[idx[col]]
        if cell in ("", "-"):
            px = c3 if off == 3 else c5
            v = _pct_str(px, base)
            if v is not None:
                cells[idx[col]] = f"{v}"
                changed = True
    # 判定：3日内跌破触发日低点 → 伪信号；否则 5日浮盈>0 → 成功；其余平
    trig_low = _low_at(df, date)
    lows = [_close_at_none(df, date, k) for k in range(1, 4)]
    if trig_low is not None and c5 is not None:
        broke = lows and any(l is not None and l < trig_low for l in lows)
        if broke:
            cells[idx["判定"]] = "伪信号"
        elif _pct_str(c5, base) is not None and _pct_str(c5, base) > 0:
            cells[idx["判定"]] = "成功"
        else:
            cells[idx["判定"]] = "平"
        changed = True
    return changed


def _close_at_none(df, date_str, offset):
    """t+offset 日最低价；不足交易日返回 None。"""
    try:
        idx = df.index[df["date"] == date_str]
        if idx.empty:
            return None
        j = idx[0] + offset
        return float(df["low"].iloc[j]) if j < len(df) else None
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（缺省=今天）")
    p.add_argument("--report", default=None, help="报告 md 路径（缺省=doc/每日复盘/{date}_复盘.md，回退模板）")
    args = p.parse_args()

    from datetime import datetime
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    if args.report:
        fp = Path(args.report)
    else:
        fp = BASE / f"doc/每日复盘/{date}_复盘.md"
        if not fp.exists():
            print(f"[WARN] {fp.name} 不存在，退回模板 每日Review.md")
            fp = BASE / "doc/每日复盘/每日Review.md"
    if not fp.exists():
        print(f"[ERROR] 报告不存在: {fp}")
        return 1

    lines = open(fp, encoding="utf-8").read().splitlines()
    tables = _parse_tables(lines)
    if not tables:
        print("[INFO] 报告中未找到前瞻表（加仓:含'加仓价'+'次日浮盈%' / 建仓:含'确认价'+'3日浮盈%'）")
        return 0

    n_changed = 0
    for t in tables:
        for lineno, cells in t["rows"]:
            if _fill_row(t["kind"], t["header"], cells):
                lines[lineno] = "|" + "|".join(cells) + "|"
                n_changed += 1

    if n_changed:
        open(fp, "w", encoding="utf-8").write("\n".join(lines))
        print(f"[OK] 回填 {n_changed} 行 → {fp}")
    else:
        print(f"[OK] 无未完结行（幂等，{fp}）")

    # 汇总
    add_rows = [c for t in tables if t["kind"] == "add" for _, c in t["rows"]]
    bu_rows = [c for t in tables if t["kind"] == "build" for _, c in t["rows"]]
    if add_rows:
        from collections import Counter
        cnt = Counter(c[-1] for c in add_rows if c[-1] in ("优", "平", "劣"))
        print(f"加仓前瞻: {len(add_rows)} 笔 → 优{cnt.get('优',0)}/平{cnt.get('平',0)}/劣{cnt.get('劣',0)}（{fp.name}）")
    if bu_rows:
        from collections import Counter
        cnt = Counter(c[-1] for c in bu_rows if c[-1] in ("成功", "伪信号"))
        print(f"建仓前瞻: {len(bu_rows)} 笔 → 成功{cnt.get('成功',0)}/伪信号{cnt.get('伪信号',0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
