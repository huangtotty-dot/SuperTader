#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案A修复部署前质量检查清单
"""
import sys
import os
from pathlib import Path
import subprocess

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent


def check_file_exists(path: str, description: str):
    """检查文件是否存在"""
    if Path(path).exists():
        print(f"✅ {description}: {path}")
        return True
    else:
        print(f"❌ {description}: 文件不存在 {path}")
        return False


def check_file_modified(path: str, keywords: list, description: str):
    """检查文件是否包含关键词"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        found = 0
        for keyword in keywords:
            if keyword in content:
                found += 1

        if found == len(keywords):
            print(f"✅ {description}: 包含所有关键词({len(keywords)}个)")
            return True
        else:
            print(f"⚠️ {description}: 仅包含{found}个关键词（期望{len(keywords)}个）")
            return False
    except Exception as e:
        print(f"❌ {description}: 检查失败 {e}")
        return False


def check_syntax(filepath: str, description: str):
    """检查Python文件语法"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", filepath],
            cwd=str(BASE),
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0:
            print(f"✅ {description}: 语法检查通过")
            return True
        else:
            print(f"❌ {description}: 语法错误 {result.stderr.decode()[:100]}")
            return False
    except Exception as e:
        print(f"❌ {description}: 检查失败 {e}")
        return False


def main():
    print("\n" + "╔" + "=" * 78 + "╗")
    print("║" + " " * 20 + "【方案A修复部署前质量检查】" + " " * 26 + "║")
    print("╚" + "=" * 78 + "╝\n")

    checks = {
        "文件存在性": [],
        "代码修改检查": [],
        "语法检查": [],
    }

    # 1. 文件存在性检查
    print("【1️⃣ 文件存在性检查】")
    checks["文件存在性"].append(check_file_exists("main.py", "主程序"))
    checks["文件存在性"].append(check_file_exists("config.py", "配置文件"))
    checks["文件存在性"].append(check_file_exists("fake_signal_monitor.py", "虚假信号监控"))
    checks["文件存在性"].append(check_file_exists("verify_scheme_a_fixes.py", "验证脚本"))
    checks["文件存在性"].append(check_file_exists("SCHEME_A_FIXES_IMPLEMENTATION.md", "实现文档"))

    # 2. 代码修改检查
    print("\n【2️⃣ 代码修改检查】")
    checks["代码修改检查"].append(
        check_file_modified(
            "main.py",
            [
                "_FALSE_SIGNAL_MONITOR = None",
                "_stock_override_enabled = True",
                "stock_override: 禁用门控标的跳过防重桶拦截",
                "from fake_signal_monitor import get_monitor",
                "m.record_signal(code, sig.price, sig.action, timestamp=now)",
            ],
            "main.py 修改验证"
        )
    )

    checks["代码修改检查"].append(
        check_file_modified(
            "config.py",
            [
                "INDEX_RESONANCE_STOCK_OVERRIDE",
            ],
            "config.py 配置验证"
        )
    )

    checks["代码修改检查"].append(
        check_file_modified(
            "fake_signal_monitor.py",
            [
                "class FalseSignalMonitor:",
                "def record_signal",
                "def check_signal_outcome",
                "def check_expired_signals",
                "def should_rollback",
                "def get_daily_report",
                "def dump_state",
            ],
            "fake_signal_monitor.py 实现验证"
        )
    )

    # 3. 语法检查
    print("\n【3️⃣ 语法检查】")
    checks["语法检查"].append(check_syntax("main.py", "main.py"))
    checks["语法检查"].append(check_syntax("config.py", "config.py"))
    checks["语法检查"].append(check_syntax("fake_signal_monitor.py", "fake_signal_monitor.py"))
    checks["语法检查"].append(check_syntax("verify_scheme_a_fixes.py", "verify_scheme_a_fixes.py"))

    # 4. 总结
    print("\n" + "=" * 80)
    print("【质量检查总结】")
    print("=" * 80)

    all_pass = True
    for category, results in checks.items():
        passed = sum(1 for r in results if r)
        total = len(results)
        status = f"{passed}/{total} ✅" if all(results) else f"{passed}/{total} ❌"
        print(f"{category:.<50} {status}")
        if not all(results):
            all_pass = False

    if all_pass:
        print("\n" + "╔" + "=" * 78 + "╗")
        print("║" + " " * 20 + "✅ 所有检查通过，可以部署！" + " " * 30 + "║")
        print("╚" + "=" * 78 + "╝\n")

        print("【部署步骤】")
        print("1. 备份当前生产代码（git commit）")
        print("2. 部署 fake_signal_monitor.py 到生产环境")
        print("3. 部署修改后的 main.py 到生产环境")
        print("4. 启动监控观察期（Day 1-3）")
        print("5. 观察期间收集虚假信号数据")
        print("6. Day 3 下午做最终评估决策")
        print()

        return 0
    else:
        print("\n❌ 部分检查失败，请修复后重试")
        return 1


if __name__ == "__main__":
    sys.exit(main())
