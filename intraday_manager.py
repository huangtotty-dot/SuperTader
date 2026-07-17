# -*- coding: utf-8 -*-
"""
盘中模式管理工具 - 启动、停止、监控定时任务
"""
import os
import sys
import json
import subprocess
from datetime import datetime

# 设置控制台编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INTRADAY_CONFIG = os.path.join(BASE_DIR, ".intraday_config.json")

def print_menu():
    """打印菜单"""
    print("\n" + "="*70)
    print("三度操盘·盘中模式管理工具")
    print("="*70)
    print("\n请选择操作：\n")
    print("  1. 查看盘中模式状态")
    print("  2. 启动实时监控面板")
    print("  3. 查看今日扫描日志")
    print("  4. 查看定时任务列表")
    print("  5. 删除定时任务")
    print("  6. 返回主菜单")
    print("\n" + "="*70)

def view_status():
    """查看状态"""
    print("\n【盘中模式状态】\n")

    if not os.path.exists(INTRADAY_CONFIG):
        print("❌ 盘中模式未启用")
        return

    try:
        with open(INTRADAY_CONFIG, 'r', encoding='utf-8') as f:
            config = json.load(f)

        print("✓ 盘中模式已启用")
        print(f"  • 状态: {'启用' if config.get('enabled') else '禁用'}")
        print(f"  • 扫描时间: {', '.join(config.get('scan_times', []))}")
        print(f"  • 启用时间: {config.get('created_at', 'N/A')[:19]}")
        print(f"  • 执行脚本: {config.get('script', 'N/A')}")

        # 检查任务是否存在
        print("\n【定时任务检查】\n")
        result = subprocess.run(
            'tasklist /FI "IMAGENAME eq python.exe" /V',
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )

        if 'python.exe' in result.stdout:
            print("✓ Python 进程正在运行")
        else:
            print("⚠️  Python 进程未运行（可能是非交易时间）")

    except Exception as e:
        print(f"❌ 读取配置失败: {str(e)}")

def start_monitor():
    """启动监控面板"""
    print("\n正在启动实时监控面板...\n")
    script = os.path.join(BASE_DIR, "intraday_monitor.py")
    subprocess.run([sys.executable, script])

def view_logs():
    """查看日志"""
    print("\n【今日扫描日志】\n")

    log_dir = os.path.join(BASE_DIR, "logs")
    if not os.path.exists(log_dir):
        print("❌ 日志目录不存在")
        return

    today = datetime.now().strftime('%Y-%m-%d')
    found = False

    for filename in sorted(os.listdir(log_dir), reverse=True):
        if filename.startswith('intraday_') and today in filename:
            found = True
            filepath = os.path.join(log_dir, filename)

            print(f"📄 {filename}\n")
            print("-" * 70)

            try:
                # 使用 errors='ignore' 忽略编码错误
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    # 显示最后 50 行
                    lines = content.split('\n')
                    for line in lines[-50:]:
                        if line.strip():
                            print(line[:100])  # 限制每行长度
            except Exception as e:
                print(f"❌ 读取日志失败: {str(e)[:50]}")

            print("-" * 70 + "\n")

    if not found:
        print("⚠️  今日暂无扫描日志")

def view_tasks():
    """查看定时任务"""
    print("\n【定时任务列表】\n")

    try:
        result = subprocess.run(
            'tasklist /FI "IMAGENAME eq python.exe" /V',
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )

        if 'python.exe' in result.stdout:
            print("✓ 检测到 Python 进程：\n")
            print(result.stdout)
        else:
            print("⚠️  未检测到 Python 进程运行")

        # 显示 Windows 任务计划中的三度猎手任务
        print("\n【Windows 任务计划中的任务】\n")
        result = subprocess.run(
            'tasklist /FI "IMAGENAME eq schtasks.exe" /V',
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )

        print("提示：打开 Windows 任务计划程序查看详细信息")
        print("  • 搜索 '三度猎手' 查看已创建的任务")
        print("  • 查看任务的执行历史和状态")

    except Exception as e:
        print(f"❌ 获取任务列表失败: {str(e)}")

def delete_tasks():
    """删除定时任务"""
    print("\n【删除定时任务】\n")

    if not os.path.exists(INTRADAY_CONFIG):
        print("❌ 盘中模式未启用，无需删除")
        return

    print("⚠️  确认删除定时任务？(y/n): ", end="")
    choice = input().strip().lower()

    if choice != 'y':
        print("已取消")
        return

    try:
        # 删除 11:00 的任务
        task_name_11 = "三度猎手_盘中扫描_11点"
        result = subprocess.run(
            f'schtasks /delete /tn "{task_name_11}" /f',
            shell=True,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print(f"✓ 已删除任务: {task_name_11}")
        else:
            print(f"⚠️  删除任务失败: {task_name_11}")

        # 删除 14:00 的任务
        task_name_14 = "三度猎手_盘中扫描_14点"
        result = subprocess.run(
            f'schtasks /delete /tn "{task_name_14}" /f',
            shell=True,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print(f"✓ 已删除任务: {task_name_14}")
        else:
            print(f"⚠️  删除任务失败: {task_name_14}")

        # 删除配置文件
        if os.path.exists(INTRADAY_CONFIG):
            os.remove(INTRADAY_CONFIG)
            print(f"✓ 已删除配置文件")

        print("\n✅ 盘中模式已完全卸载")

    except Exception as e:
        print(f"❌ 删除任务失败: {str(e)}")

def main():
    """主菜单循环"""
    while True:
        print_menu()
        choice = input("请输入选项 (1-6): ").strip()

        if choice == "1":
            view_status()
        elif choice == "2":
            start_monitor()
        elif choice == "3":
            view_logs()
        elif choice == "4":
            view_tasks()
        elif choice == "5":
            delete_tasks()
        elif choice == "6":
            print("\n👋 返回主菜单\n")
            break
        else:
            print("\n❌ 无效选项，请重新选择")

        input("\n按 Enter 键继续...")

if __name__ == "__main__":
    main()
