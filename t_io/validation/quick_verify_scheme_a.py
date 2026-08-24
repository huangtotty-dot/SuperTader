#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quick_verify_scheme_a.py — 方案A 快速验证脚本

验证内容：
  1. config.py 的参数是否正确
  2. 588170 @ 0.996 信号是否被推送
  3. main.py 中是否实现了 stock_override 逻辑
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
import re

BASE = Path(__file__).resolve().parent.parent.parent


def check_config():
    """检查 config.py 参数配置."""
    print("\n【检查1】config.py 参数配置")
    print("=" * 80)

    config_file = BASE / "config.py"
    with open(config_file, encoding="utf-8") as f:
        content = f.read()

    # 检查 fail_closed
    if 'fail_closed": False' in content:
        print("✅ fail_closed=False 已配置")
    else:
        print("❌ fail_closed=False 未配置（可能是 True）")

    # 检查 INDEX_RESONANCE_STOCK_OVERRIDE
    if "INDEX_RESONANCE_STOCK_OVERRIDE" in content:
        print("✅ INDEX_RESONANCE_STOCK_OVERRIDE 已定义")

        # 查找具体配置
        start = content.find("INDEX_RESONANCE_STOCK_OVERRIDE = {")
        if start > 0:
            end = content.find("}", start) + 1
            override_config = content[start:end]

            if '"588170"' in override_config and "False" in override_config:
                print("  ✅ 588170 禁用门控")
            else:
                print("  ❌ 588170 未禁用")

            if '"300153"' in override_config and "False" in override_config:
                print("  ✅ 300153 禁用门控")
            else:
                print("  ⏳ 300153 未禁用或已移除")
    else:
        print("❌ INDEX_RESONANCE_STOCK_OVERRIDE 未定义")


def check_main_py():
    """检查 main.py 中是否实现了 stock_override 逻辑."""
    print("\n【检查2】main.py 中的 stock_override 逻辑")
    print("=" * 80)

    main_file = BASE / "main.py"
    if not main_file.exists():
        print("❌ main.py 不存在")
        return

    with open(main_file, encoding="utf-8") as f:
        content = f.read()

    # 查找关键代码段
    keywords = [
        "INDEX_RESONANCE_STOCK_OVERRIDE",
        "stock_override",
        "stock_config",
        "enabled.*False",
    ]

    found = False
    for keyword in keywords:
        if keyword in content or re.search(keyword, content):
            found = True
            print(f"✅ 发现关键词: {keyword}")

    if not found:
        print("⚠️ 未发现 stock_override 相关代码")
        print("   可能原因：")
        print("   1. 代码还未实现")
        print("   2. 实现位置在其他文件（index_resonance.py）")
        print("   3. 变量名不同")


def check_588170_signal():
    """查看 588170 @ 0.996 的信号记录."""
    print("\n【检查3】588170 @ 0.996 信号验证")
    print("=" * 80)

    trace_file = BASE / "t_io" / "traces" / "decision_trace_2026-08-24.jsonl"
    if not trace_file.exists():
        print(f"❌ trace 文件不存在: {trace_file}")
        return

    import json
    signals_0996 = []

    with open(trace_file, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                code = entry.get("code")
                price = entry.get("price")
                time = entry.get("time")
                decision = entry.get("decision")

                if code == "588170" and price is not None:
                    if 0.994 <= price <= 0.998:  # 接近 0.996
                        signals_0996.append({
                            "time": time,
                            "price": price,
                            "decision": decision,
                            "reason": entry.get("reason", "")[:50],
                        })
            except Exception:
                pass

    if signals_0996:
        print(f"✅ 找到 {len(signals_0996)} 条相关信号:")
        for sig in signals_0996[:5]:
            print(f"   {sig['time']} {sig['decision']:<10} 价格:{sig['price']:.4f}")
    else:
        print("⏳ 未找到 0.994-0.998 价格区间的信号（可能价格精度不同）")

    # 统计所有588170的信号
    all_588170 = []
    with open(trace_file, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("code") == "588170":
                    all_588170.append(entry)
            except Exception:
                pass

    print(f"\n  588170 总信号数: {len(all_588170)}")
    if all_588170:
        print(f"  最早信号: {all_588170[0].get('time')} @ {all_588170[0].get('price'):.4f}")
        print(f"  最晚信号: {all_588170[-1].get('time')} @ {all_588170[-1].get('price'):.4f}")


def check_shadow_signals():
    """查看是否存在 shadow_signals 日志（记录拦截信息）."""
    print("\n【检查4】shadow_signals 日志")
    print("=" * 80)

    trace_dir = BASE / "t_io" / "traces"
    shadow_files = list(trace_dir.glob("shadow_signals_*.jsonl"))

    if shadow_files:
        print(f"✅ 发现 {len(shadow_files)} 个 shadow_signals 文件")

        # 查看最新的
        latest = sorted(shadow_files)[-1]
        print(f"   最新文件: {latest.name}")

        # 检查是否包含588170的拦截记录
        import json
        blocked_588170 = 0
        with open(latest, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("code") == "588170" and not entry.get("allow"):
                        blocked_588170 += 1
                except Exception:
                    pass

        if blocked_588170 > 0:
            print(f"   ✅ 588170 被拦截 {blocked_588170} 次")
        else:
            print(f"   ⏳ 588170 未发现被拦截的记录")
    else:
        print("⏳ 未发现 shadow_signals 文件")
        print("   拦截信息可能在其他位置记录（如系统日志）")


def check_index_resonance():
    """查看 index_resonance.py 中的实现."""
    print("\n【检查5】index_resonance.py 实现")
    print("=" * 80)

    ir_file = BASE / "index_resonance.py"
    if not ir_file.exists():
        print(f"❌ index_resonance.py 不存在")
        return

    with open(ir_file, encoding="utf-8") as f:
        content = f.read()

    # 检查是否有 stock_override 逻辑
    if "stock_override" in content or "INDEX_RESONANCE_STOCK_OVERRIDE" in content:
        print("✅ index_resonance.py 中包含 stock_override 逻辑")
    else:
        print("⏳ index_resonance.py 中未发现 stock_override 逻辑")

    # 检查 fail_closed 的处理
    if "fail_closed" in content:
        print("✅ index_resonance.py 中处理了 fail_closed 参数")
    else:
        print("⏳ index_resonance.py 中未显式处理 fail_closed")


def main():
    """主入口."""
    print("\n" + "=" * 80)
    print("【方案A 快速验证工具】")
    print("=" * 80)

    check_config()
    check_main_py()
    check_index_resonance()
    check_588170_signal()
    check_shadow_signals()

    print("\n" + "=" * 80)
    print("【验证总结】")
    print("=" * 80)
    print("""
    ✅ = 已验证/已实现
    ⏳ = 需要进一步检查
    ❌ = 未找到/需要实现

    下一步：
    1. 若有 ⏳，需要手动检查代码逻辑
    2. 若有 ❌，可能需要修复或补充实现
    3. 对照 SCHEME_A_REVIEW_20260824.md 的检查清单
    """)


if __name__ == "__main__":
    main()
