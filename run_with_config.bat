@echo off
REM 启动策略配置菜单并执行扫描
REM 这个脚本会先显示策略配置菜单，让你选择要启用的策略，然后开始执行扫描

chcp 65001 > nul
cd /d %~dp0

echo 启动选股系统...
python selection_v17.10.py

pause
