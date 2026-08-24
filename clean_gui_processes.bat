@echo off
REM 清理残留的 WebView 进程（GUI 崩溃恢复脚本）
REM 使用方法: 双击运行或在命令行执行

echo 清理旧的 WebView 进程...
taskkill /F /IM WebViewHost.exe 2>nul
taskkill /F /IM msedgewebview2.exe 2>nul
taskkill /F /IM chrome.exe 2>nul

timeout /t 1 /nobreak

echo.
echo 清理完成，现在可以启动 GUI:
echo   python t_gui.py
echo.
pause
