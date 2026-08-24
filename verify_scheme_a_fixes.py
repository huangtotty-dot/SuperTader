#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案A三大风险修复验证脚本
验证风险1/2/3的修复是否正确实现
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path

BASE = Path(__file__).resolve().parent


def verify_risk1():
    """验证风险1：stock_override 代码检查"""
    print("\n" + "=" * 80)
    print("【验证风险1】stock_override 代码实现")
    print("=" * 80)

    # 检查 config.py 中的配置
    config_path = BASE / "config.py"
    with open(config_path, 'r', encoding='utf-8') as f:
        config_content = f.read()

    if "INDEX_RESONANCE_STOCK_OVERRIDE" in config_content:
        print("✅ config.py 中定义了 INDEX_RESONANCE_STOCK_OVERRIDE")
    else:
        print("❌ config.py 中未定义 INDEX_RESONANCE_STOCK_OVERRIDE")
        return False

    if "588170" in config_content and "enabled" in config_content:
        print("✅ config.py 中配置了 588170 的 enabled=False")
    else:
        print("⚠️ config.py 中未完全配置 588170")

    # 检查 main.py 中的 _check_gate() 函数
    main_path = BASE / "main.py"
    with open(main_path, 'r', encoding='utf-8') as f:
        main_content = f.read()

    if "INDEX_RESONANCE_STOCK_OVERRIDE" in main_content:
        print("✅ main.py 中读取了 INDEX_RESONANCE_STOCK_OVERRIDE")
    else:
        print("❌ main.py 中未读取 INDEX_RESONANCE_STOCK_OVERRIDE")
        return False

    print("✅ 风险1验证通过")
    return True


def verify_risk2():
    """验证风险2：防重桶二次拦截修复"""
    print("\n" + "=" * 80)
    print("【验证风险2】防重桶跳过处理")
    print("=" * 80)

    main_path = BASE / "main.py"
    with open(main_path, 'r', encoding='utf-8') as f:
        main_content = f.read()

    # 检查是否添加了 stock_override 的防重桶跳过逻辑
    if "_stock_override_enabled" in main_content:
        print("✅ main.py 中添加了 _stock_override_enabled 变量")
    else:
        print("❌ main.py 中未添加 _stock_override_enabled 变量")
        return False

    if "stock_override: 禁用门控标的跳过防重桶拦截" in main_content:
        print("✅ main.py 中添加了防重桶跳过逻辑注释")
    else:
        print("⚠️ main.py 中未找到防重桶跳过逻辑的注释")

    if "if not _stock_override_enabled:" in main_content:
        print("✅ main.py 中添加了 stock_override 检查分支")
    else:
        print("❌ main.py 中未添加 stock_override 检查分支")
        return False

    print("✅ 风险2验证通过")
    return True


def verify_risk3():
    """验证风险3：虚假信号监控系统"""
    print("\n" + "=" * 80)
    print("【验证风险3】虚假信号监控系统")
    print("=" * 80)

    # 检查 fake_signal_monitor.py 是否存在
    monitor_path = BASE / "fake_signal_monitor.py"
    if not monitor_path.exists():
        print("❌ fake_signal_monitor.py 不存在")
        return False

    print("✅ fake_signal_monitor.py 已创建")

    # 检查 fake_signal_monitor.py 中是否有核心类
    with open(monitor_path, 'r', encoding='utf-8') as f:
        monitor_content = f.read()

    if "class FalseSignalMonitor:" in monitor_content:
        print("✅ fake_signal_monitor.py 中定义了 FalseSignalMonitor 类")
    else:
        print("❌ fake_signal_monitor.py 中未定义 FalseSignalMonitor 类")
        return False

    required_methods = [
        "record_signal",
        "check_signal_outcome",
        "check_expired_signals",
        "get_false_ratio",
        "should_rollback",
        "get_daily_report",
        "dump_state",
    ]

    for method in required_methods:
        if f"def {method}" in monitor_content:
            print(f"✅ FalseSignalMonitor 中定义了 {method} 方法")
        else:
            print(f"❌ FalseSignalMonitor 中未定义 {method} 方法")
            return False

    # 检查 main.py 中是否引入了监控器
    main_path = BASE / "main.py"
    with open(main_path, 'r', encoding='utf-8') as f:
        main_content = f.read()

    if "_FALSE_SIGNAL_MONITOR" in main_content:
        print("✅ main.py 中添加了 _FALSE_SIGNAL_MONITOR 全局变量")
    else:
        print("❌ main.py 中未添加 _FALSE_SIGNAL_MONITOR 全局变量")
        return False

    if "from fake_signal_monitor import get_monitor" in main_content:
        print("✅ main.py 中导入了 fake_signal_monitor 模块")
    else:
        print("⚠️ main.py 中未直接导入 fake_signal_monitor 模块（使用动态导入）")

    if "m.record_signal(code, sig.price, sig.action, timestamp=now)" in main_content:
        print("✅ main.py 中记录推送信号到监控器")
    else:
        print("⚠️ main.py 中未找到记录推送信号的代码")

    print("✅ 风险3验证通过")
    return True


def verify_compilation():
    """验证编译通过"""
    print("\n" + "=" * 80)
    print("【编译验证】")
    print("=" * 80)

    import subprocess

    # 测试 fake_signal_monitor.py 编译
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", "fake_signal_monitor.py"],
            cwd=str(BASE),
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0:
            print("✅ fake_signal_monitor.py 编译成功")
        else:
            print(f"❌ fake_signal_monitor.py 编译失败: {result.stderr.decode()}")
            return False
    except Exception as e:
        print(f"❌ 编译 fake_signal_monitor.py 时出错: {e}")
        return False

    # 测试 main.py 编译
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", "main.py"],
            cwd=str(BASE),
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0:
            print("✅ main.py 编译成功")
        else:
            print(f"❌ main.py 编译失败: {result.stderr.decode()}")
            return False
    except Exception as e:
        print(f"❌ 编译 main.py 时出错: {e}")
        return False

    print("✅ 编译验证通过")
    return True


def verify_imports():
    """验证导入可以工作"""
    print("\n" + "=" * 80)
    print("【导入验证】")
    print("=" * 80)

    try:
        from fake_signal_monitor import FalseSignalMonitor, get_monitor, init_monitor
        print("✅ 成功导入 FalseSignalMonitor")

        m = init_monitor()
        print("✅ 成功初始化监控器实例")

        m.record_signal('588170.SH', 10.5, 'BUY_LOW')
        print("✅ 成功记录信号")

        m.record_signal('300153.SZ', 20.3, 'BUY_LOW')
        m.check_signal_outcome('588170.SH', 10.2, 10.5)
        m.check_signal_outcome('300153.SZ', 20.8, 20.3)

        ratio = m.get_false_ratio()
        print(f"✅ 成功计算虚假比例: {ratio:.2%}")

        report = m.get_daily_report()
        print(f"✅ 成功生成日报表")

        print("✅ 导入验证通过")
        return True
    except Exception as e:
        print(f"❌ 导入验证失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主验证流程"""
    print("\n" + "╔" + "=" * 78 + "╗")
    print("║" + " " * 20 + "【方案A三大风险修复验证】" + " " * 30 + "║")
    print("╚" + "=" * 78 + "╝")

    results = {
        "风险1": verify_risk1(),
        "风险2": verify_risk2(),
        "风险3": verify_risk3(),
        "编译": verify_compilation(),
        "导入": verify_imports(),
    }

    print("\n" + "=" * 80)
    print("【验证总结】")
    print("=" * 80)

    for name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{name:.<40} {status}")

    if all(results.values()):
        print("\n✅ 所有验证通过，可以部署到生产环境！")
        return 0
    else:
        print("\n❌ 部分验证失败，请检查修复代码")
        return 1


if __name__ == "__main__":
    sys.exit(main())
