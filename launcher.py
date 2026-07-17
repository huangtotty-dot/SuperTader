# -*- coding: utf-8 -*-
"""
快速启动脚本 - 一键运行完整流程
"""
import os
import sys
import subprocess
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
WATCHLIST_BUILDER = os.path.join(BASE_DIR, "watchlist_builder.py")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
UNIVERSE_CACHE_FILE = os.path.join(CACHE_DIR, "a_share_pool.json")

def print_menu():
    """打印菜单"""
    print("\n" + "="*70)
    print("三度操盘·实战量化机 V17.10 - 快速启动菜单")
    print("="*70)
    print("\n请选择操作：\n")
    print("  1. 运行V17.10日线策略扫描（先做成交额回归检查，再跑全部策略）")
    print("  2. 运行V17.10周线扫描（先做成交额回归检查，再跑周线策略）")
    print("  3. 生成月度涨幅排行")
    print("  4. 运行完整流程：V17.10扫描 + stock_monitor评分（新增）")
    print("  5. 退出")
    print("\n" + "="*70)

def test_data_sources():
    """测试数据源"""
    print("\n正在测试V9.0数据源连接...\n")
    script = os.path.join(BASE_DIR, "test_v8_sources.py")
    subprocess.run([sys.executable, script])


def ensure_watchlist_ready() -> bool:
    """确保 watchlist.json 可用"""
    print("\n正在检查 watchlist.json 完整性...\n")
    script = WATCHLIST_BUILDER
    env = os.environ.copy()
    result = subprocess.run([sys.executable, script, "--ensure-complete", "--no-refresh"], env=env)
    if result.returncode == 0:
        return True

    print("\n⚠️  完整性校验失败，改用本地缓存回退...\n")
    result = subprocess.run([sys.executable, script, "--ensure-complete", "--cache-only"], env=env)
    if result.returncode == 0:
        return True

    print("\n⚠️  完整性修复失败，尝试直接修复 watchlist.json...\n")
    result = subprocess.run([sys.executable, script, "--no-refresh"], env=env)
    if result.returncode == 0:
        return True

    print("\n⚠️  本地修复失败，改用缓存回退...\n")
    result = subprocess.run([sys.executable, script, "--cache-only"], env=env)
    return result.returncode == 0

def run_amount_guard() -> bool:
    """运行成交额回归检查"""
    print("\n🧪 运行成交额回归检查...")
    script = os.path.join(BASE_DIR, "selection_v17.10.py")
    env = os.environ.copy()
    env["AMOUNT_REGRESSION_ONLY"] = "1"
    env["AMOUNT_CHECK_LOG"] = "1"
    env["AMOUNT_REGRESSION_MODE"] = "unit"
    result = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode == 0:
        print("\n✓ 成交额回归检查通过")
        return True
    print("\n⚠️  成交额回归检查未通过")
    return False


def run_scan(version="v17.7", scan_mode="daily"):
    """运行扫描"""
    if not ensure_watchlist_ready():
        print("\n❌ watchlist 修复失败，已取消扫描")
        return
    if version == "v17.10" and not run_amount_guard():
        return
    if version == "v17.10":
        script_name = "selection_v17.10.py"
        version_name = "V17.10"
    elif version == "v17.9":
        script_name = "selection_v17.9.py"
        version_name = "V17.9"
    elif version == "v17.8":
        script_name = "selection_v17.8.py"
        version_name = "V17.8"
    elif version == "v17.7":
        script_name = "selection_v17.7.py"
        version_name = "V17.7"
    elif version == "v17.6":
        script_name = "selection_v17.6.py"
        version_name = "V17.6"
    elif version == "v17.4":
        script_name = "selection_v17.4.py"
        version_name = "V17.4"
    elif version == "v17.3":
        script_name = "selection_v17.3.py"
        version_name = "V17.3"
    elif version == "v17.2":
        script_name = "selection_v17.0.py"
        version_name = "V17.2"
    else:
        script_name = "selection_v16.0.py"
        version_name = "V16.0"

    print(f"\n正在启动{version_name}扫描程序...\n")
    script = os.path.join(BASE_DIR, script_name)
    env = os.environ.copy()
    env["SCAN_MODE"] = "weekly" if scan_mode in ("weekly", "weekly_light") else scan_mode
    env["SCAN_LIGHT"] = "1" if scan_mode == "weekly_light" else "0"
    env["SCAN_TEST_MODE"] = "0"
    env.pop("SCAN_LIMIT", None)
    if scan_mode == "weekly":
        min_amount = input("请输入周线模式的成交额下限（默认1500000000，直接回车则使用默认）: ").strip()
        max_amount = input("请输入周线模式的成交额上限（默认无限制，直接回车则使用默认）: ").strip()
        env["WEEKLY_AMOUNT_MIN"] = min_amount or env.get("WEEKLY_AMOUNT_MIN", "1500000000")
        env["WEEKLY_AMOUNT_MAX"] = max_amount or env.get("WEEKLY_AMOUNT_MAX", "inf")
    else:
        env["WEEKLY_AMOUNT_MIN"] = env.get("WEEKLY_AMOUNT_MIN", "1500000000")
        env["WEEKLY_AMOUNT_MAX"] = env.get("WEEKLY_AMOUNT_MAX", "inf")
    subprocess.run([sys.executable, script], env=env)

def setup_intraday_mode():
    """设置盘中模式"""
    print("\n" + "="*70)
    print("盘中模式设置")
    print("="*70)
    print("\n盘中模式将在每个交易日的以下时间自动执行扫描：")
    print("  • 11:00 - 上午盘中扫描")
    print("  • 14:00 - 下午盘中扫描")
    print("\n逻辑复用V16.0策略，实时捕捉交易机会\n")

    choice = input("是否启动盘中模式？(y/n): ").strip().lower()
    if choice != 'y':
        print("已取消")
        return

    print("\n正在启动盘中模式...\n")
    script = os.path.join(BASE_DIR, "setup_intraday_scheduler.py")
    subprocess.run([sys.executable, script])

def manage_intraday_mode():
    """管理盘中模式"""
    print("\n正在启动盘中模式管理工具...\n")
    script = os.path.join(BASE_DIR, "intraday_manager.py")
    subprocess.run([sys.executable, script])

def verify_prices():
    """验证价格"""
    print("\n正在验证价格准确性...\n")
    target_date = input("请输入日期 (YYYYMMDD): ").strip()

    if len(target_date) != 8 or not target_date.isdigit():
        print("❌ 日期格式错误！")
        return

    script = os.path.join(BASE_DIR, "verify_prices.py")
    subprocess.run([sys.executable, script, target_date])

def test_system_alert():
    """测试系统报警"""
    print("\n" + "="*70)
    print("系统报警测试")
    print("="*70)
    print("\n请选择要测试的版本：\n")
    print("  1. V17.3 (1.5秒快速报警)")
    print("  2. V17.2 (标准报警)")
    print("  3. 返回菜单")

    choice = input("\n请输入选项 (1-3): ").strip()

    if choice == "1":
        print("\n正在测试V17.3系统报警功能...\n")
        script = os.path.join(BASE_DIR, "system_alert_v17_3.py")
    elif choice == "2":
        print("\n正在测试V17.2系统报警功能...\n")
        script = os.path.join(BASE_DIR, "system_alert.py")
    else:
        return

    subprocess.run([sys.executable, script])

def show_guide():
    """显示使用指南"""
    guide_file = os.path.join(BASE_DIR, "USAGE_GUIDE.md")

    if not os.path.exists(guide_file):
        print("\n❌ 使用指南文件不存在")
        return

    try:
        with open(guide_file, 'r', encoding='utf-8') as f:
            content = f.read()
            print("\n" + content)
    except Exception as e:
        print(f"\n❌ 读取文件失败: {str(e)}")

def clear_cache():
    """清空缓存"""
    cache_dir = os.path.join(BASE_DIR, "cache")

    if not os.path.exists(cache_dir):
        print("\n✓ 缓存目录不存在，无需清空")
        return

    try:
        count = 0
        for f in os.listdir(cache_dir):
            file_path = os.path.join(cache_dir, f)
            if os.path.isfile(file_path):
                os.remove(file_path)
                count += 1

        print(f"\n✓ 已清空 {count} 个缓存文件")
    except Exception as e:
        print(f"\n❌ 清空缓存失败: {str(e)}")


def build_watchlist():
    """生成或刷新 watchlist.json"""
    print("\n正在生成/刷新 watchlist.json...\n")
    script = os.path.join(BASE_DIR, "watchlist_builder.py")
    env = os.environ.copy()
    result = subprocess.run([sys.executable, script], env=env, encoding="utf-8", errors="replace")
    if result.returncode == 0:
        print("\n✓ watchlist.json 已刷新")
        return

    print("\n⚠️  在线刷新失败，尝试使用本地缓存修复 watchlist.json...\n")
    repair = subprocess.run([sys.executable, script, "--cache-only"], env=env)
    if repair.returncode == 0:
        print("\n✓ 已使用本地缓存修复 watchlist.json")
    else:
        print("\n❌ watchlist.json 修复失败，请先检查网络或 cache/a_share_pool.json")


def run_monthly_gain():
    """运行月度涨幅排行"""
    print("\n正在生成月度涨幅排行...\n")
    month_input = input("请输入月份 (YYYYMM 或 YYYY-MM，系统会自动换算为该月月初~月末): ").strip()
    if not month_input:
        print("已取消")
        return

    top_n_input = input("请输入 TOP N（默认50）: ").strip()
    env = os.environ.copy()
    env["SCAN_MODE"] = "monthly_gain"
    env["MONTHLY_STATS_ONLY"] = "1"
    env["MONTHLY_STATS_MONTH"] = month_input
    env["MONTHLY_GAIN_TOP_N"] = top_n_input or env.get("MONTHLY_GAIN_TOP_N", "50")
    env["SCAN_TEST_MODE"] = "0"
    env.pop("SCAN_LIMIT", None)
    subprocess.run([sys.executable, os.path.join(BASE_DIR, "selection_v17.10.py")], env=env)

def run_complete_pipeline():
    """运行完整流程：V17.10 -> stock_monitor"""
    print("\n" + "="*70)
    print("完整流程：V17.10扫描 + stock_monitor评分")
    print("="*70)
    print("\n正在执行完整流程...\n")

    # 第一步：运行 selection_v17.10.py
    print("\n【步骤1】正在运行V17.10扫描...")
    print("="*70)

    if not ensure_watchlist_ready():
        print("\n❌ watchlist 修复失败，已取消流程")
        return

    if not run_amount_guard():
        print("\n⚠️  成交额回归检查未通过，但继续执行...")

    script1 = os.path.join(BASE_DIR, "selection_v17.10.py")
    env = os.environ.copy()
    env["SCAN_MODE"] = "daily"
    env["SCAN_TEST_MODE"] = "0"
    env["PIPELINE_MODE"] = "1"  # 标记为流程模式，跳过交互式输入
    env.pop("SCAN_LIMIT", None)

    subprocess.run([sys.executable, script1], env=env)

    print("\n✅ V17.10扫描完成！\n")

    # 第二步：运行 stock_monitor.py
    print("\n【步骤2】正在运行stock_monitor评分...")
    print("="*70 + "\n")

    script2 = os.path.join(BASE_DIR, "stock_monitor.py")
    env2 = os.environ.copy()
    env2["PIPELINE_MODE"] = "1"  # 标记为流程模式

    subprocess.run([sys.executable, script2], env=env2)

    print("\n✅ stock_monitor评分完成！\n")
    print("="*70)
    print("\n✨ 完整流程执行完毕！")
    print("="*70)

def main():
    """主菜单循环"""
    while True:
        print_menu()
        try:
            choice = input("请输入选项 (1-5): ").strip()
        except EOFError:
            print("\n检测到非交互环境，退出。")
            return

        if choice == "1":
            run_scan("v17.10", "daily")
        elif choice == "2":
            run_scan("v17.10", "weekly")
        elif choice == "3":
            run_monthly_gain()
        elif choice == "4":
            run_complete_pipeline()
        elif choice == "5":
            print("\n👋 再见！\n")
            break
        else:
            print("\n❌ 无效选项，请重新选择")

        input("\n按 Enter 键继续...")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 程序已中断\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 发生错误: {str(e)}\n")
        sys.exit(1)
