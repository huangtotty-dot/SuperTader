# -*- coding: utf-8 -*-
"""公司简介**后台落盘**的回归测试（2026-10-20）。

线上症状：K 线弹窗「业务简介」永远停在「⏳ 正在查询完整简介」——
根因是 `_company_profile._bg()` 里 `os.replace(...)` 抛 **NameError**（t_gui 当时没有
module 级 `import os`），又被 `except Exception: pass` 吞掉 ⇒ 从 2026-10-17 起
**缓存一次都没写进去过**，只有预取脚本写的那几十只能用。

这组测试用**桩 fetch**（不出网）盯住"后台真的把结果落盘了"：
只测「函数返回 pending」是不够的 —— 当时 pending 也返回了，坏的是**后面那一步**。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import t_gui   # noqa: E402


class TestCompanyProfileBackgroundWrite:
    def _make_api(self):
        return t_gui.Api()

    def _wait(self, fp, code, timeout=3.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                if code in d:
                    return d[code]
            except Exception:
                pass
            time.sleep(0.05)
        return None

    def test_background_fetch_is_persisted(self, tmp_path, monkeypatch):
        """抓到简介后必须**落盘**，否则每次打开都要重抓、界面永远"⏳"。"""
        monkeypatch.setattr(t_gui, "BASE", tmp_path)
        (tmp_path / "t_io" / "cache").mkdir(parents=True, exist_ok=True)
        fp = tmp_path / "t_io" / "cache" / "company_profile.json"
        fp.write_text("{}", encoding="utf-8")

        api = self._make_api()
        monkeypatch.setattr(api, "_fetch_company_profile",
                            lambda code: {"code": code, "brief": "主营某某", "brief_title": "主营业务"})
        got = api._company_profile("600000")          # 未命中 ⇒ (None, True) 并 kick 后台
        assert got == (None, True)

        saved = self._wait(fp, "600000")
        assert saved is not None, "后台抓取结果没有被写入 company_profile.json"
        assert saved["brief"] == "主营某某"

        # 落盘后第二次调用应直接命中（不再 pending）
        hit, pend = api._company_profile("600000")
        assert pend is False and hit and hit["brief"] == "主营某某"

    def test_empty_fetch_does_not_write_and_clears_inflight(self, tmp_path, monkeypatch):
        """抓取为空（风控/网络）⇒ 不写缓存，但 inflight 必须释放，否则该票再也不会重试。"""
        monkeypatch.setattr(t_gui, "BASE", tmp_path)
        (tmp_path / "t_io" / "cache").mkdir(parents=True, exist_ok=True)
        fp = tmp_path / "t_io" / "cache" / "company_profile.json"
        fp.write_text("{}", encoding="utf-8")

        api = self._make_api()
        monkeypatch.setattr(api, "_fetch_company_profile", lambda code: None)
        api._company_profile("600001")
        # 等后台线程收尾
        deadline = time.time() + 2
        while time.time() < deadline and getattr(api, "_prof_inflight", set()):
            time.sleep(0.05)
        assert "600001" not in getattr(api, "_prof_inflight", set())
        assert json.loads(fp.read_text(encoding="utf-8")) == {}

    def test_t_gui_imports_os(self):
        """直接钉死当年那个 NameError：`os` 必须是 t_gui 的模块级名字。"""
        assert hasattr(t_gui, "os"), "t_gui 缺少模块级 `import os`（`_company_profile` 落盘会抛 NameError）"
        assert hasattr(t_gui.os, "replace")
