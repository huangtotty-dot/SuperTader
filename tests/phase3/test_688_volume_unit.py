# -*- coding: utf-8 -*-
"""科创板成交量「股当手」（2026-10-22 owner：688543 日线成交额 10-20 冲到 671 亿）。

根因链：腾讯 `f[6]` 对**科创板是「股」**、其余是「手」（差 100 倍）；补 forming bar 的三条路径
（`facade._maybe_append_forming`、`facade._append_forming_many`、hunter 的盘中 patch）把原始值
直接写进 `volume` ⇒ 下游 `_ensure_amount`（`... × volume × 100`，按手算）成交额放大 100 倍。

本测试盯三件事：
  ① 三条 forming bar 路径都要把 688 的量归一到手；
  ② `daily_cache` 见到 688 档里出现 ~100 倍的量要判为坏档、逼上层重取；
  ③ 修复脚本的**滚动**判据（连坏两天也不能互相污染）。
"""
import json
import statistics
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core.market_data import tencent_provider as tp   # noqa: E402


def _rows(vols):
    return [{"date": f"2026-10-{i + 1:02d}", "open": 1, "close": 1, "high": 1, "low": 1,
             "volume": float(v)} for i, v in enumerate(vols)]


class TestDetector:
    def test_normal_688_passes(self):
        assert tp.has_volume_unit_error(_rows([100000] * 12 + [150000]), "688543") is False

    def test_100x_688_flagged(self):
        assert tp.has_volume_unit_error(_rows([140000] * 12 + [14099000]), "688543") is True

    def test_non_688_ignored(self):
        """主板/创业板的 f[6] 本来就是手，不该误报。"""
        assert tp.has_volume_unit_error(_rows([140000] * 12 + [14099000]), "600519") is False

    def test_extreme_but_not_100x_not_flagged(self):
        """20 倍放量（真实可能发生）不判坏 —— 判据收紧到 70~140 倍。"""
        assert tp.has_volume_unit_error(_rows([100000] * 12 + [2000000]), "688543") is False


class TestFormingPathsNormalize:
    def test_facade_batch_path(self):
        src = (_ROOT / "core/market_data/facade.py").read_text(encoding="utf-8")
        i = src.index("def _append_forming_batch")
        seg = src[i:i + 2400]
        assert "vol_hand" in seg and 'startswith(("688", "689"))' in seg, \
            "批量 forming bar 路径必须把 688 的量 ÷100"

    def test_facade_single_path(self):
        src = (_ROOT / "core/market_data/facade.py").read_text(encoding="utf-8")
        i = src.index("def _maybe_append_forming")
        seg = src[i:i + 2600]
        assert 'startswith(("688", "689"))' in seg

    def test_hunter_path(self):
        src = (_ROOT / "stock_hunter/modules/market_data.py").read_text(encoding="utf-8")
        i = src.index("_svol = float(_snap.get")
        seg = src[i:i + 900]
        assert 'startswith(("688", "689"))' in seg, "猎手盘中 patch 也要归一"


class TestGuardWiredIntoCache:
    def test_daily_cache_rejects_bad_volume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tp, "_DAILY_CACHE_DIR", str(tmp_path))
        rec = {"date": "2026-10-22", "saved_at": "2026-10-22 21:00:00",
               "rows": _rows([140000] * 12 + [14099000])}
        (tmp_path / "688543.json").write_text(json.dumps(rec), encoding="utf-8")
        assert tp.TencentProvider().daily_cache("688543") is None, "坏档必须判 None 逼重取"


class TestRepairRolling:
    def test_consecutive_bad_days(self):
        """连坏两天（688296 实测 10-20、10-21）—— 必须用**已修好的**前 10 根做基准。"""
        rows = _rows([50000] * 10 + [4955675, 3363918, 131295])
        vols, changed = [], []
        for r in rows:
            v = float(r["volume"])
            if len(vols) >= 10:
                ref = statistics.median(vols[-10:])
                if ref > 0 and v > 0 and 50 <= v / ref <= 250:
                    v = round(v / 100.0)
                    changed.append(v)
            vols.append(v)
        assert len(changed) == 2, "两天都要修"
        assert changed[0] == pytest.approx(49557, abs=2)
        assert changed[1] == pytest.approx(33639, abs=2)
        assert vols[-1] == 131295, "正常那根不动"
