@echo off
REM 每日做T优化效果检查脚本（Windows batch）
REM 在任务计划中设置：每日 15:10 执行（复盘后）
REM 命令行：cmd /c E:\superTrader\t_io\validation\daily_review\check_daily_optimization.bat

setlocal enabledelayedexpansion

REM 获取今天日期（Windows 格式 YYYY-MM-DD）
for /f "tokens=1-3 delims=/-" %%a in ('powershell -Command "[datetime]::Now.ToString('yyyy-MM-dd')"') do set TODAY=%%a-%%b-%%c

set PYTHON=python
set SCRIPT_DIR=E:\superTrader\t_io\validation\daily_review
set REVIEW_DIR=E:\superTrader\doc\每日复盘
set REVIEW_FILE=%REVIEW_DIR%\%TODAY%_复盘.md

echo [%date% %time%] 开始做T优化效果检查...
echo 日期: %TODAY%
echo 复盘文件: %REVIEW_FILE%

REM 检查复盘文件是否存在
if not exist "%REVIEW_FILE%" (
    echo [ERROR] 复盘文件不存在: %REVIEW_FILE%
    exit /b 1
)

REM 生成优化效果检查报告
echo [%date% %time%] 生成优化效果检查报告...
%PYTHON% "%SCRIPT_DIR%\check_optimization_effect.py" --date %TODAY% > "%SCRIPT_DIR%\optimization_effect_%TODAY%.txt" 2>&1

if errorlevel 1 (
    echo [ERROR] 优化效果检查失败
    exit /b 1
)

REM 追加到复盘文件
echo [%date% %time%] 将报告追加到复盘文件...
type "%SCRIPT_DIR%\optimization_effect_%TODAY%.txt" >> "%REVIEW_FILE%"

echo [%date% %time%] ✅ 完成！
echo 报告已追加到: %REVIEW_FILE%

exit /b 0
