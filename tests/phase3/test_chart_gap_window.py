# -*- coding: utf-8 -*-
"""缺口色块的**回看窗口按自然日算**（2026-10-22 owner：同一缺口 10 分钟线看得到、5 分钟线看不到）。

根因：`buildGapAreas` 原来写死「最近 250 根」。5 分钟线 250 根只有 **5 个交易日**，
10 分钟线 250 根有 **10 个交易日** ⇒ 同一个真实缺口在细周期上早被扫出窗口。
现在改为传 `dates`，从最后一根往前 `lookbackDays` 个**自然日**开始扫。

本测试用 node 直接跑 app.js 里那两个真函数（不是复刻），喂同一天的 5 分钟/10 分钟序列，
断言两者都能看到同一处跳空。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端函数")


def _extract():
    """截出 `_ymdMinusDays` + `buildGapAreas` 两个**真函数**（它们之间到下一个顶层 function）。"""
    i = _JS.index("function _ymdMinusDays")
    j = _JS.index("function renderStockChart", i)
    return _JS[i:j]


def _run_gaps(ohlc, dates, days=15, tmpdir=None):
    # ⚠️ 不能 `node -e <code>`：截出来的函数体 + 序列 JSON 会超过 Windows 命令行长度上限
    #    （实测 WinError 206）。写临时文件再 `node <file>`。
    code = _extract() + f"""
const out = buildGapAreas({json.dumps(ohlc)}, {json.dumps(dates)}, {days}, 0.003, 24);
console.log(JSON.stringify(out.map(x => x[0].xAxis)));
"""
    import tempfile, os
    fd, fp = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run(["node", fp], capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.unlink(fp)
        except Exception:
            pass
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _series(n_days=20, bars_per_day=48, jump_on_day=18, gap_pct=0.02):
    """造一段跨 n_days 的分钟序列，在第 jump_on_day 天开盘处留一个上跳缺口。"""
    dates, ohlc = [], []
    price = 10.0
    for d in range(n_days):
        day = f"2026-10-{d + 1:02d}"
        for b in range(bars_per_day):
            hh = 9 + (30 + b * (5 if bars_per_day == 48 else 10)) // 60
            mm = (30 + b * (5 if bars_per_day == 48 else 10)) % 60
            dates.append(f"{day} {hh:02d}:{mm:02d}")
            if d == jump_on_day and b == 0:      # 当日第一根：跳空高开
                o = price * (1 + gap_pct); c = o; l = o; h = o * 1.001
            else:
                o = price; c = price * 1.001; l = price * 0.999; h = price * 1.002
            ohlc.append([round(o, 3), round(c, 3), round(l, 3), round(h, 3)])
            price = c
    return ohlc, dates


class TestGapWindow:
    def test_same_gap_visible_on_fine_and_coarse(self):
        # 缺口落在第 18 天；5 分钟一天 48 根 ⇒ 最后 2 天只有 ~96 根，旧口径(250 根)够到；
        # 把缺口放到更早的第 10 天更贴近现场：5 分钟 250 根够不到、10 分钟够得到。
        o5, d5 = _series(n_days=20, bars_per_day=48, jump_on_day=10)
        o10, d10 = _series(n_days=20, bars_per_day=24, jump_on_day=10)
        i5 = _run_gaps(o5, d5)
        i10 = _run_gaps(o10, d10)
        assert i5, "5 分钟线上必须能看到这个缺口（按自然日回看）"
        assert i10, "10 分钟线上必须能看到这个缺口"
        assert d5[i5[0]].startswith("2026-10-11"), d5[i5[0]]
        assert d10[i10[0]].startswith("2026-10-11"), d10[i10[0]]

    def test_old_bar_window_would_miss_it(self):
        """没传 dates 时退回旧口径（250 根）—— 用来证明"旧行为确实会漏"。"""
        o5, d5 = _series(n_days=20, bars_per_day=48, jump_on_day=10)
        old = _run_gaps(o5, None, 15)          # dates=None → 250 根
        new = _run_gaps(o5, d5, 15)
        assert not any(d5[i].startswith("2026-10-11") for i in old), "250 根窗口本应错过第 10 天的缺口"
        assert any(d5[i].startswith("2026-10-11") for i in new)

    def test_window_is_days_not_bars(self):
        src = _extract()
        assert "_ymdMinusDays" in src and "lookbackDays" in src
        assert "String(dates[i]).slice(0, 10) >= cut" in src

    def test_call_sites_pass_dates(self):
        assert "buildGapAreas(period.ohlc, period.dates, 15)" in _JS
        assert "buildGapAreas(s.ohlc, s.dates, 15, 0.003, 8)" in _JS
