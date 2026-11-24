# -*- coding: utf-8 -*-
"""指数「市值前 10」名单（core.index_leaders，2026-11-24，全离线）。

owner 口径：每个指数取总市值最大的 10 只，方格显示涨幅。
  · 宽基（上证/深证/创业板/科创50）= 对应板块 clist 按 f20 降序前 10；
  · 中证500/1000 = 先取新浪成分表再按总市值排前 10；
  · 磁盘按自然日缓存，跨日后台单飞重建；js_api 线程只读缓存。

铁律：全部离线（网络函数一律 monkeypatch），不读真实 t_io 缓存。

运行：python -m pytest tests/phase3/test_index_leaders.py -q
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.index_leaders as IL  # noqa: E402


_SINA_HTML = """
<table>
 <tr><td><a href="/corp/go.php/vII_NewestComponent/indexid/000001.phtml">上证指数</a>
     <a href="/corp/go.php/vII_NewestComponent/indexid/399001.phtml">深证成指</a></td></tr>
 <tr><td><a href="http://finance.sina.com.cn/realstock/company/sh688981/nc.shtml">中芯国际</a></td></tr>
 <tr><td><a href="http://finance.sina.com.cn/realstock/company/sh688825/nc.shtml">长鑫科技</a></td></tr>
 <tr><td><a href="http://finance.sina.com.cn/realstock/company/sh688981/nc.shtml">中芯国际</a></td></tr>
 <tr><td><a href="http://finance.sina.com.cn/realstock/company/sh000300/nc.shtml">沪深300成份</a></td></tr>
</table>
"""


class TestParse:
    def test_剔除指数自身链接与重复(self):
        rows = IL.parse_sina_components(_SINA_HTML)
        codes = [c for _m, c, _n in rows]
        assert codes == ["688981", "688825"], f"应只剩两只真实成分，实际 {codes}"

    def test_剔除名称像指数的行(self):
        html = ('<a href="/x/sh000300/">沪深300成份</a>'
                '<a href="/x/sz399001/">深证成指</a>'
                '<a href="/x/sz300750/">宁德时代</a>')
        rows = IL.parse_sina_components(html)
        assert [c for _m, c, _n in rows] == ["300750"]


class _FakeGma:
    """假 GM：成分表带 market_value_total（亿元）+ 名称表。"""

    def __init__(self, rows):
        self.rows = rows

    def stk_get_index_constituents(self, sym):
        import pandas as pd
        return pd.DataFrame([
            {"index": sym, "symbol": f"SHSE.{c}", "trade_date": "2026-11-24",
             "market_value_total": cap, "market_value_circ": cap, "weight": 0.0}
            for c, cap in self.rows])

    def get_instruments(self, syms, fields=None):
        return [{"symbol": s, "sec_name": "名" + s.split(".")[-1]} for s in syms]


class TestBuild:
    def test_gm主源_取市值前10(self, monkeypatch):
        # GM 给 12 只且乱序 ⇒ 必须自己按市值降序截前 10
        rows = [(f"6{i:05d}", float(i)) for i in range(12)]
        monkeypatch.setattr(IL, "_gma", lambda: _FakeGma(rows[::-1]))
        monkeypatch.setattr(IL, "_gm_top", lambda sym, n=IL._N: IL._norm(
            [{"code": c, "name": "名" + c, "mktcap": cap * 1e8} for c, cap in rows[::-1]], n))
        items = IL._build()
        for name in IL._ORDER:
            assert name in items, f"缺 {name}"
            assert len(items[name]) == IL._N
            caps = [r["mktcap"] for r in items[name]]
            assert caps == sorted(caps, reverse=True), "必须按市值降序"
            assert items[name][0]["code"] == "600011", "市值最大的应排第一"

    def test_gm取数_解析符号与名称(self, monkeypatch):
        rows = [("688981", 10411.9), ("688256", 5752.8), ("688041", 3811.1)]
        monkeypatch.setattr(IL, "_gma", lambda: _FakeGma(rows))
        got = IL._gm_top("SHSE.000688", n=2)
        assert [r["code"] for r in got] == ["688981", "688256"]
        assert got[0]["name"] == "名688981"
        assert abs(got[0]["mktcap"] - 10411.9e8) < 1, "亿元 → 元"

    def test_gm挂掉_走兜底(self, monkeypatch):
        def boom(sym, n=IL._N):
            raise RuntimeError("gm down")
        monkeypatch.setattr(IL, "_gm_top", boom)
        monkeypatch.setattr(IL, "_em_top_multi", lambda fss: [
            {"code": "601398", "name": "工商银行", "mktcap": 3.2e12}])
        monkeypatch.setattr(IL, "_csi_top", lambda idx, n=IL._N: [
            {"code": "688146" + idx[-1], "name": "C" + idx, "mktcap": 9e10}])
        items = IL._build()
        assert items["上证指数"][0]["code"] == "601398"
        assert items["中证500"][0]["code"].startswith("688146")


class TestCache:
    def _patch_disk(self, monkeypatch, tmp_path, payload):
        fp = tmp_path / "index_leaders.json"
        if payload is not None:
            fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(IL, "_CACHE_FP", str(fp))
        monkeypatch.setattr(IL, "_state", {"data": None, "ts": 0.0, "running": False})
        return fp

    def test_无缓存_返回pending并踢后台(self, monkeypatch, tmp_path):
        self._patch_disk(monkeypatch, tmp_path, None)
        kicked = []
        monkeypatch.setattr(IL, "_kick_build", lambda: kicked.append(1))
        r = IL.get_cached(86400)
        assert r["pending"] is True and r["items"] == {}
        assert kicked == [1], "无档必须踢一次后台重建"

    def test_当日缓存_立返且不重建(self, monkeypatch, tmp_path):
        today = IL._today()
        items = {name: [{"code": "600000", "name": "占位", "mktcap": 1e12}]
                 for name in IL._ORDER}                 # 完整档（六指数齐全）才算"当日缓存"
        self._patch_disk(monkeypatch, tmp_path,
                         {"date": today, "schema": IL._SCHEMA,
                          "built_at": "10:00:00", "items": items})
        kicked = []
        monkeypatch.setattr(IL, "_kick_build", lambda: kicked.append(1))
        r = IL.get_cached(86400)
        assert r["pending"] is False
        assert list(r["items"].keys()) == list(IL._ORDER), "顺序按 _ORDER 固定"
        assert kicked == [], "当日档不该重建"

    def test_跨日缓存_先回旧档再后台重建(self, monkeypatch, tmp_path):
        items = {"科创50": [{"code": "688825", "name": "长鑫科技", "mktcap": 2.4e12}]}
        self._patch_disk(monkeypatch, tmp_path,
                         {"date": "19990101", "schema": IL._SCHEMA,
                          "built_at": "10:00:00", "items": items})
        kicked = []
        monkeypatch.setattr(IL, "_kick_build", lambda: kicked.append(1))
        r = IL.get_cached(86400)
        assert r["pending"] is False, "有旧档 ⇒ 不显示 pending"
        assert r["items"], "旧档照常下发（SWR）"
        assert kicked == [1], "跨日必须踢后台重建"

    def test_旧schema档判废_显示准备中(self, monkeypatch, tmp_path):
        """旧档名字可能来自 gbk 误读（乱码）⇒ 不可信：当无缓存 + 后台重建。"""
        items = {"科创50": [{"code": "688981", "name": "涓�鑺�鍥介檯", "mktcap": 1e12}]}
        self._patch_disk(monkeypatch, tmp_path,
                         {"date": IL._today(), "built_at": "10:00:00", "items": items})
        kicked = []
        monkeypatch.setattr(IL, "_kick_build", lambda: kicked.append(1))
        r = IL.get_cached(86400)
        assert r["pending"] is True and r["items"] == {}
        assert kicked == [1]
