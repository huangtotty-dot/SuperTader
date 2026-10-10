# -*- coding: utf-8 -*-
"""全仓 `send_feishu_payload` **调用契约**测试（2026-10-10 补）。

背景：`config.send_feishu_payload` 的必填参数是 `(payload, success_log, error_prefix)`。
有两个盘中告警（`core/hunter_ma5_alert.py`、`core/trend30_alert.py`）只传了 `payload`，
每次都抛 TypeError，又被各自的裸 `except Exception` 吞掉 —— 结果两条告警
**自上线起从未推送成功过一次**，且不留任何日志/去重痕迹，owner 只看到「今天没通知」。

这个测试把契约钉死：用 `inspect.Signature.bind` 把每个调用点实参**真正绑一遍**，
少传/多传/名字写错都会在这里炸出来，而不是等到某天发现告警悄悄失效。

纯静态分析，不联网、不发任何消息。

运行：python tests/phase3/test_feishu_call_contract.py
"""
import ast
import inspect
import io
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "tmp", ".archive", "t_io"}
_TARGET = "send_feishu_payload"


def _iter_calls():
    """遍历仓库里的 .py（跳过测试自身/归档/数据目录），产出 (path, lineno, Call)。"""
    for root, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".py") or f.startswith("test_"):
                continue
            p = os.path.join(root, f)
            try:
                with io.open(p, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else "")
                if name == _TARGET:
                    yield p, node.lineno, node


class TestFeishuCallContract(unittest.TestCase):
    def test_每个调用点都能绑定到真实签名(self):
        sig = inspect.signature(config.send_feishu_payload)
        bad = []
        total = 0
        for p, lineno, node in _iter_calls():
            total += 1
            args = [None] * len(node.args)
            kwargs = {}
            star = False
            for kw in node.keywords:
                if kw.arg is None:            # **kwargs 展开，静态看不出来 → 跳过不判
                    star = True
                    continue
                kwargs[kw.arg] = None
            if star or any(isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*args, **kwargs)
            except TypeError as e:
                bad.append(f"{os.path.relpath(p, _ROOT)}:{lineno} → {e}")
        self.assertGreater(total, 0, "一个 send_feishu_payload 调用点都没扫到，测试本身失效了")
        self.assertEqual(
            bad, [],
            "以下调用点的实参与 config.send_feishu_payload 的签名不匹配"
            "（少传参数会被 except 吞掉，告警会静默失效）:\n  " + "\n  ".join(bad))

    def test_必填参数就是这三个(self):
        """签名一旦改动，本测试会提醒同步检查所有调用点。"""
        sig = inspect.signature(config.send_feishu_payload)
        required = [n for n, p in sig.parameters.items()
                    if p.default is inspect.Parameter.empty]
        self.assertEqual(required, ["payload", "success_log", "error_prefix"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
