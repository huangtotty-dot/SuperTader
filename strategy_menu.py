# -*- coding: utf-8 -*-
"""
策略配置交互式菜单
提供UI界面让用户选择启用/禁用各个策略
"""

import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class StrategyConfig:
    """策略配置管理器"""

    STRATEGIES = [
        ("BREAKTHROUGH", "👑 突破先手", "温和突破近15日平台，放量且资金底座扎实"),
        ("MA_CLUSTER", "🧲 均线粘合", "短中期均线粘合，价格贴近均线中心"),
        ("A_AREA", "🚀 A区初显", "均线底座打牢，温和放量初次点火"),
        ("B_AREA", "🔥 B区起航", "洗盘结束，温和放量反包"),
        ("BOX_BOTTOM", "💎 底部缩量", "底部区域缩量阴线，MA30上升趋势"),
        ("BOX_TOP", "⭐ 箱体突破", "箱顶突破确认，整理后放量上破"),
        ("BOX_INTERNAL", "💥 箱内加速", "箱体内加速，放量且动能充足"),
        ("HISTORY_BREAK", "🚀 历史突破", "突破90日历史高点并站稳"),
        ("MILD_TREND", "⚪ 温和抬升", "温和放量上行，站上短中期均线"),
        ("MA_NEAR", "🧭 均线邻近", "价格靠近MA60或MA150"),
    ]

    CONFIG_FILE = os.path.join(BASE_DIR, "strategy_config.json")

    def __init__(self):
        self.config = self.load_config()

    def load_config(self):
        """从文件加载配置，如果不存在则使用默认值"""
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {code: True for code, _, _ in self.STRATEGIES}

    def save_config(self):
        """保存配置到文件"""
        with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def show_menu(self):
        """显示交互式菜单"""
        while True:
            os.system('cls' if os.name == 'nt' else 'clear')
            print("=" * 75)
            print("📊 策略配置菜单 - 选择要启用的策略".center(75))
            print("=" * 75)

            enabled_count = sum(1 for code in self.config.values() if code)
            print(f"  已启用: {enabled_count} 个策略，已禁用: {len(self.STRATEGIES) - enabled_count} 个")
            print("=" * 75)
            print()

            # 显示所有策略及其状态
            for idx, (code, emoji_name, desc) in enumerate(self.STRATEGIES, 1):
                status = "✓" if self.config.get(code, True) else "✗"
                checkbox = "[✓]" if self.config.get(code, True) else "[✗]"
                print(f"  {idx:2d}. {checkbox} {emoji_name}")
                print(f"       {desc}")
                print()

            print("=" * 75)
            print("  操作说明:")
            print("  • 输入数字 (1-10):      切换该策略 [✓]→[✗] 或 [✗]→[✓]")
            print("  • 输入多个 (如 1 3 5):  同时切换多个策略")
            print("  • 输入 'a' 或 '*':      启用所有策略")
            print("  • 输入 'd' 或 '-':      禁用所有策略")
            print("  • 输入 's' 或 'c':      保存配置并开始执行")
            print("  • 输入 'q' 或 'e':      退出不保存")
            print("=" * 75)

            choice = input("\n请输入选择 (如: 1 3 5 / a / d / s / q): ").strip().lower()

            if choice in ('q', 'e', 'exit'):
                print("↩️  退出配置，不保存任何更改。")
                return

            if choice in ('s', 'c', 'confirm'):
                self.save_config()
                print("\n✓ 配置已保存！\n")
                print("📋 启用的策略:")
                enabled_strategies = []
                for code, emoji_name, _ in self.STRATEGIES:
                    if self.config.get(code, True):
                        print(f"  ✓ {emoji_name}")
                        enabled_strategies.append(emoji_name)

                if not enabled_strategies:
                    print("  ⚠️  未启用任何策略！")

                print("\n按 Enter 键开始执行...")
                input()
                return

            if choice in ('a', '*'):
                self.config = {code: True for code, _, _ in self.STRATEGIES}
                print("✓ 已启用所有策略")
                input("按 Enter 继续...")
                continue

            if choice in ('d', '-'):
                self.config = {code: False for code, _, _ in self.STRATEGIES}
                print("✓ 已禁用所有策略")
                input("按 Enter 继续...")
                continue

            # 处理数字输入（支持多个）
            numbers = choice.split()
            if numbers and all(n.isdigit() for n in numbers):
                valid_nums = [int(n) for n in numbers if 1 <= int(n) <= len(self.STRATEGIES)]

                if valid_nums:
                    # 切换选中的策略
                    for num in valid_nums:
                        idx = num - 1
                        code = self.STRATEGIES[idx][0]
                        self.config[code] = not self.config.get(code, True)

                    toggled = [self.STRATEGIES[num-1][1] for num in valid_nums]
                    print(f"\n✓ 已切换 {len(valid_nums)} 个策略:")
                    for emoji_name in toggled:
                        status = "启用" if self.config.get(self.STRATEGIES[[i for i, (_, name, _) in enumerate(self.STRATEGIES) if name == emoji_name][0]][0], True) else "禁用"
                        print(f"  • {emoji_name} → {status}")
                    input("按 Enter 继续...")
                    continue

            print("❌ 无效输入，请重试")
            input("按 Enter 继续...")

    def get_env_vars(self):
        """返回配置对应的环境变量字典"""
        env_vars = {}
        for code, _, _ in self.STRATEGIES:
            env_name = f"ENABLE_STRATEGY_{code}"
            env_vars[env_name] = "1" if self.config.get(code, True) else "0"
        return env_vars

    def apply_config(self):
        """应用配置到环境变量"""
        for env_name, value in self.get_env_vars().items():
            os.environ[env_name] = value


def show_strategy_config_menu():
    """显示策略配置菜单的主入口"""
    config = StrategyConfig()

    print("\n")
    print("*" * 75)
    print("*" + "策略配置".center(73) + "*")
    print("*" * 75)
    print("  1. 自由选择策略（进入详细菜单）")
    print("  2. 使用上次保存的配置")
    print("  3. 启用所有策略")
    print("  4. 禁用所有策略")
    print("*" * 75)

    choice = input("请选择 (1-4): ").strip()

    if choice == "1":
        config.show_menu()
    elif choice == "2":
        print("✓ 使用上次保存的配置\n")
        config.load_config()
    elif choice == "3":
        print("✓ 启用所有策略\n")
        config.config = {code: True for code, _, _ in config.STRATEGIES}
        config.save_config()
    elif choice == "4":
        print("✓ 禁用所有策略\n")
        config.config = {code: False for code, _, _ in config.STRATEGIES}
        config.save_config()
    else:
        print("无效选择，使用上次配置\n")
        config.load_config()

    config.apply_config()
