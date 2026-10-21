# -*- coding: utf-8 -*-
"""日线缓存里的「盘中半成品根」——识别、隔离、自愈（2026-10-21）。

owner：「润建股份(002929) 10 月 20 日是涨停的，K 线图上却显示跌了」。
实测：
  · 缓存里 10-20 收 **46.10 / -0.65%**（10 月 20 日上午的快照根）；
  · 10-20 真实收盘 **51.04（涨停）** —— 由 10-21 的开盘 53.12 与实时价 56.14(+9.99%) 反推；
  · 全池 5520 只里 **36 只**同症状，全部写于 10-21 09:31~09:33 那一批。

两条防线（数据只可能来自"盘中的自己拍自己"）：
  ① **涨跌停跳空判据**：A 股单日涨跌幅有硬上限（主板 10 / 创业板·科创板 20 / 北交所 30），
     前复权序列不会制造超限跳空 ⇒ `open[t]/close[t-1]-1` 超限 = 档里有半成品根 ⇒ 该档判不可信，
     上层重取（真数据按日期覆盖合并）。这条对**已存在的**坏档也生效 ⇒ 自愈。
  ② **forming 标记**：`facade.daily()` 用实时快照补的那根记进缓存 `forming` 列表；
     merge 时「被真数据确认过」才清标记，**没被确认的旧快照根直接丢弃**（宁可缺一根也不留错值），
     `daily_cache()` 见到历史日上还挂着标记 ⇒ 判 stale 强制重取。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import core.chart_cache as cc                      # noqa: E402
from core.market_data import tencent_provider as tp  # noqa: E402

_TODAY = datetime.now().strftime("%Y-%m-%d")
_YDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _rows(*pairs):
    """[(date, open, close), …] → 缓存 rows（high/low 取 o/c 的包络，够判跳空用）。"""
    return [{"date": d, "open": o, "close": c, "high": max(o, c), "low": min(o, c),
             "volume": 1000.0} for d, o, c in pairs]


class TestGapDetector:
    def test_normal_days_pass(self):
        rows = _rows(("2026-10-16", 10.0, 10.5), ("2026-10-19", 10.5, 10.2),
                     ("2026-10-20", 10.2, 10.9))
        assert tp.has_impossible_gap(rows, "600900") is False

    def test_limit_up_gap_is_fine(self):
        """主板 +10% 的隔夜跳空是合法的（涨停开盘），不能误判。"""
        rows = _rows(("2026-10-19", 10.0, 10.0), ("2026-10-20", 11.0, 11.0))
        assert tp.has_impossible_gap(rows, "600900") is False

    def test_15pct_gap_on_main_board_flagged(self):
        """002929 现场：前收 46.10（错的快照根）→ 次日开 53.12 = +15.2%。"""
        rows = _rows(("2026-10-20", 46.40, 46.10), ("2026-10-21", 53.12, 56.14))
        assert tp.has_impossible_gap(rows, "002929") is True

    def test_stale_history_not_flagged(self):
        """退市/长期停牌票的远古序列（000556 停在 2002 年）不参与判定，免得每次读都白重取。"""
        rows = _rows(("2002-04-19", 4.0, 4.0), ("2002-04-26", 1.5, 1.5))
        assert tp.has_impossible_gap(rows, "000556") is False

    def test_board_limits(self):
        assert tp._limit_pct("688018") == 20.0
        assert tp._limit_pct("300750") == 20.0
        assert tp._limit_pct("830799") == 30.0
        assert tp._limit_pct("920045") == 30.0, "北交所新代码段 920xxx（codec 认定 BJ）"
        assert tp._limit_pct("600900") == 10.0
        # 科创板 15% 跳空合法
        assert tp.has_impossible_gap(
            _rows(("2026-10-20", 100.0, 100.0), ("2026-10-21", 115.0, 115.0)), "688018") is False
        # 科创板 25% 跳空非法
        assert tp.has_impossible_gap(
            _rows(("2026-10-20", 100.0, 100.0), ("2026-10-21", 125.0, 125.0)), "688018") is True

    def test_only_tail_is_checked(self):
        """很久以前的除权跳空不该让整档永久不可用（只看最近 n+1 根）。"""
        rows = _rows(("2026-09-01", 10.0, 10.0), ("2026-09-02", 20.0, 20.0),
                     ("2026-10-17", 20.0, 20.0), ("2026-10-20", 20.0, 20.0),
                     ("2026-10-21", 20.5, 21.0))
        assert tp.has_impossible_gap(rows, "600900") is False

    def test_never_raises_on_garbage(self):
        assert tp.has_impossible_gap([], "600900") is False
        assert tp.has_impossible_gap([{"date": "x"}], "600900") is False


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "_DAILY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "daily_fp", lambda code: tmp_path / f"{str(code).split('_')[0]}.json")
    return tmp_path


def _write(dirpath, code, rows, forming=None, saved_at=None):
    rec = {"date": _TODAY,
           "saved_at": saved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "rows": rows}
    if forming is not None:
        rec["forming"] = forming
    (dirpath / f"{code}.json").write_text(json.dumps(rec), encoding="utf-8")


class TestReadersRejectPoison:
    def test_daily_cache_rejects_gap(self, cache_dir):
        _write(cache_dir, "002929", _rows(("2026-10-20", 46.40, 46.10),
                                          ("2026-10-21", 53.12, 56.14)))
        assert tp.TencentProvider().daily_cache("002929") is None

    def test_display_read_rejects_gap(self, cache_dir):
        _write(cache_dir, "002929", _rows(("2026-10-20", 46.40, 46.10),
                                          ("2026-10-21", 53.12, 56.14)))
        assert cc.load_daily_display("002929") is None

    def test_healthy_cache_still_reads(self, cache_dir):
        _write(cache_dir, "600900", _rows(("2026-10-20", 10.0, 10.2),
                                          ("2026-10-21", 10.2, 10.4)))
        df = cc.load_daily_display("600900")
        assert df is not None and len(df) == 2

    def test_past_forming_marker_forces_refetch(self, cache_dir):
        """历史日上还挂着"快照根"标记（从未被真数据确认）⇒ 整档不可信。"""
        _write(cache_dir, "600900", _rows(("2026-10-20", 10.0, 10.2),
                                          ("2026-10-21", 10.2, 10.4)),
               forming=[_YDAY])
        assert tp.TencentProvider().daily_cache("600900") is None

    def test_today_forming_marker_is_fine(self, cache_dir):
        """当天那根是快照根很正常（盘中），不该因此拒绝整档。"""
        _write(cache_dir, "600900", _rows(("2026-10-20", 10.0, 10.2),
                                          ("2026-10-21", 10.2, 10.4)),
               forming=[_TODAY])
        assert tp.TencentProvider().daily_cache("600900") is not None


class TestMergeDropsUnconfirmedForming:
    def _df(self, *pairs):
        import pandas as pd
        return pd.DataFrame([{"date": d, "open": o, "close": c, "high": max(o, c),
                              "low": min(o, c), "volume": 1000.0} for d, o, c in pairs])

    def test_unconfirmed_forming_row_dropped(self, cache_dir):
        """旧档把 _YDAY 标成快照根，而新数据里**没有这一天**（源缺口）⇒ 丢弃，不许复制下去。"""
        _write(cache_dir, "600900",
               _rows(("2026-10-19", 10.0, 10.0), (_YDAY, 10.0, 10.1), (_TODAY, 10.1, 10.2)),
               forming=[_YDAY])
        tp.merge_daily_cache("600900", self._df((_TODAY, 10.3, 10.5)))
        rec = json.loads((cache_dir / "600900.json").read_text(encoding="utf-8"))
        dates = [r["date"] for r in rec["rows"]]
        assert _YDAY not in dates, "未确认的快照根必须丢弃（宁可缺一根，也不留错值）"
        assert _TODAY in dates and str(rec["forming"]) == "[]"

    def test_confirmed_forming_row_kept_and_unmarked(self, cache_dir):
        _write(cache_dir, "600900",
               _rows(("2026-10-19", 10.0, 10.0), (_YDAY, 10.0, 10.1)),
               forming=[_YDAY])
        tp.merge_daily_cache("600900", self._df((_YDAY, 10.0, 12.0)))   # 真数据确认了那天
        rec = json.loads((cache_dir / "600900.json").read_text(encoding="utf-8"))
        row = next(r for r in rec["rows"] if r["date"] == _YDAY)
        assert row["close"] == 12.0, "真数据必须覆盖快照值"
        assert rec["forming"] == [], "被确认过就不再是 forming"

    def test_forming_date_is_recorded(self, cache_dir):
        tp.save_daily_cache("600900", self._df(("2026-10-19", 10.0, 10.0), (_TODAY, 10.0, 10.3)),
                            forming_date=_TODAY)
        rec = json.loads((cache_dir / "600900.json").read_text(encoding="utf-8"))
        assert rec["forming"] == [_TODAY]
