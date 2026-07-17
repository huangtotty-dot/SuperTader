# -*- coding: utf-8 -*-
"""
盘中模式监控面板 - 实时查看定时任务状态
"""
import os
import sys
import json
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# 设置控制台编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
INTRADAY_CONFIG = os.path.join(BASE_DIR, ".intraday_config.json")

def get_task_status():
    """获取 Windows 定时任务状态"""
    try:
        result = subprocess.run(
            'tasklist /FI "IMAGENAME eq python.exe" /V',
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        return result.stdout
    except:
        return "无法获取任务状态"

def get_latest_logs():
    """获取最新的扫描日志"""
    if not os.path.exists(LOG_DIR):
        return []

    logs = []
    today = datetime.now().strftime('%Y-%m-%d')

    # 查找今天的日志文件
    for filename in os.listdir(LOG_DIR):
        if filename.startswith('intraday_') and today in filename:
            filepath = os.path.join(LOG_DIR, filename)
            try:
                # 使用 errors='ignore' 忽略编码错误
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    logs.append({
                        'filename': filename,
                        'content': content,
                        'size': len(content),
                        'mtime': os.path.getmtime(filepath)
                    })
            except Exception as e:
                # 忽略读取错误，继续处理其他文件
                pass

    return sorted(logs, key=lambda x: x['mtime'], reverse=True)

def get_next_execution_time():
    """计算下次执行时间"""
    now = datetime.now()

    # 11:00 的执行时间
    time_11 = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if now < time_11:
        next_11 = time_11
    else:
        next_11 = time_11 + timedelta(days=1)

    # 14:00 的执行时间
    time_14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
    if now < time_14:
        next_14 = time_14
    else:
        next_14 = time_14 + timedelta(days=1)

    # 返回最近的执行时间
    next_time = min(next_11, next_14)
    time_diff = next_time - now

    hours = time_diff.seconds // 3600
    minutes = (time_diff.seconds % 3600) // 60

    return next_time.strftime("%Y-%m-%d %H:%M:%S"), f"{hours}小时{minutes}分钟"

def print_header():
    """打印标题"""
    print("\n" + "="*80)
    print("🐎 三度操盘·盘中模式监控面板")
    print("="*80 + "\n")

def print_status():
    """打印状态信息"""
    print_header()

    # 配置状态
    print("【配置状态】")
    if os.path.exists(INTRADAY_CONFIG):
        try:
            with open(INTRADAY_CONFIG, 'r', encoding='utf-8') as f:
                config = json.load(f)
            print(f"✓ 盘中模式已启用")
            print(f"  • 扫描时间: {', '.join(config.get('scan_times', []))}")
            print(f"  • 配置时间: {config.get('created_at', 'N/A')[:19]}")
        except Exception as e:
            print(f"⚠️  配置读取失败: {str(e)[:40]}")
    else:
        print("✗ 盘中模式未启用")

    print()

    # 定时任务状态
    print("【定时任务状态】")
    try:
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
            print("✗ Python 进程未运行")
    except:
        print("⚠️  无法获取进程状态")

    print()

    # 下次执行时间
    print("【下次执行时间】")
    next_time, time_diff = get_next_execution_time()
    print(f"⏰ {next_time} (还需 {time_diff})")

    print()

    # 最新日志
    print("【最新扫描日志】")
    logs = get_latest_logs()

    if not logs:
        print("暂无今日日志")
    else:
        for log in logs[:3]:  # 显示最新的3个日志
            print(f"\n📄 {log['filename']}")
            print(f"   大小: {log['size']} 字节")

            # 显示日志的最后几行，处理编码问题
            try:
                lines = log['content'].split('\n')
                last_lines = [l for l in lines if l.strip()][-5:]

                for line in last_lines:
                    # 截断长行并处理特殊字符
                    display_line = line[:70] if len(line) > 70 else line
                    if '【' in display_line or '✓' in display_line or '✗' in display_line or '⏰' in display_line:
                        print(f"   {display_line}")
            except Exception as e:
                print(f"   ⚠️  日志显示失败: {str(e)[:40]}")

    print()

    # 日志文件位置
    print("【日志文件位置】")
    print(f"📁 {LOG_DIR}")

    print()

    # 操作提示
    print("【操作提示】")
    print("1. 查看完整日志: 打开 logs 目录")
    print("2. 查看任务计划: 打开 Windows 任务计划程序")
    print("3. 删除任务: 运行 launcher.py 并选择相应选项")
    print("4. 实时监控: 每 30 秒自动刷新一次")

    print()
    print("="*80)

def monitor_loop():
    """监控循环"""
    print_status()

    while True:
        try:
            time.sleep(30)  # 每 30 秒刷新一次
            os.system('cls' if sys.platform == 'win32' else 'clear')
            print_status()
        except KeyboardInterrupt:
            print("\n\n👋 监控已停止\n")
            break
        except Exception as e:
            print(f"\n❌ 错误: {str(e)}\n")
            time.sleep(5)

def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1] == '--once':
        # 单次显示
        print_status()
    else:
        # 持续监控
        monitor_loop()

if __name__ == "__main__":
    main()
