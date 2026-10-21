# -*- coding: utf-8 -*-
"""`t_gui.py` 的 `__main__` 段不得引用未定义的名字（2026-10-21）。

事故：`164d565` 在 `__main__` 里加了线程级 CPU 归因的启动段，写的是
`PARAMS.get("thread_watch_sec", ...)` 和 `log.info/warning(...)` —— 而 t_gui.py **既没有
模块级 `PARAMS`（配置在 `config.PARAMS`）也没有 `log`**。于是：
    try 里 NameError → 被 `except` 接住 → `except` 里 `log.warning` 再抛 NameError
    → 进程在 import 阶段就退出 → **窗口根本不出现**，
而 `main.py` 拉起子进程时 `stdout/stderr` 都是 `DEVNULL`（`main._launch_gui`）⇒ 连报错都看不见。
owner 的表现就是「运行 python main.py 之后无法打开 GUI」。

`import t_gui` 是绿的**掩盖不了它**（崩在 `__main__`，只有真跑才触发）⇒ 这条静态检查补上。
"""
import ast
import builtins
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = (_ROOT / "t_gui.py").read_text(encoding="utf-8")


def _module_level_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Try, ast.If, ast.For, ast.While, ast.With)):
            # 顶层 try/if 里的定义（如 try: import config except: config=None）也算
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add((a.asname or a.name).split(".")[0])
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
    return names


def _main_block(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) in (
                "__name__ == '__main__'", '__name__ == "__main__"'):
            return node
    return None


def _free_names(node, known):
    """`node` 子树里引用的、局部作用域没有、`known` 也没有的名字（去掉内建）。"""
    missing = set()
    scopes = []

    def visit(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            local = {a.arg for a in getattr(n.args, "args", [])}
            local |= {a.arg for a in getattr(n.args, "kwonlyargs", [])}
            if isinstance(n, ast.Lambda):
                body = [n.body]
            else:
                local |= {x.id for x in ast.walk(n) if isinstance(x, ast.Name)
                          and isinstance(x.ctx, ast.Store)}
                local.add(n.name)
                body = n.body
            scopes.append(local)
            for stmt in body:
                visit(stmt)
            scopes.pop()
            return
        if isinstance(n, ast.ExceptHandler):
            if getattr(n, "name", None):
                scopes.append({n.name})
                for child in ast.iter_child_nodes(n):
                    visit(child)
                scopes.pop()
                return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if not any(n.id in s for s in scopes) and n.id not in known \
                    and not hasattr(builtins, n.id):
                missing.add((n.id, n.lineno))
            return
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            return
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return missing


def test_main_block_has_no_undefined_names():
    tree = ast.parse(_SRC)
    block = _main_block(tree)
    assert block is not None, "t_gui.py 里找不到 __main__ 段"
    known = _module_level_names(tree) | {"__name__", "__file__", "__doc__"}
    missing = _free_names(block, known)
    assert not missing, (
        "t_gui.py 的 __main__ 段引用了模块里不存在的名字（会 NameError 直接退出、"
        f"且 main.py 把子进程输出丢了看不见）：{sorted(missing)}")


def test_specific_landmines_gone():
    """点名两个具体地雷（本文件从来没有这两个全局）。"""
    tree = ast.parse(_SRC)
    block = _main_block(tree)
    ids = {n.id for n in ast.walk(block) if isinstance(n, ast.Name)}
    assert "PARAMS" not in ids, "配置一律走 `config.PARAMS`（`import config as _cfg` 后取）"
    assert "log" not in ids, "t_gui.py 没有模块级 logger，日志用 print(..., flush=True)"
