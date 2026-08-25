# GUI 加载卡死/直接崩溃问题诊断与解决

## 症状
1. **永久加载** — 打开 GUI 后显示"加载中..."状态，永不响应
2. **直接崩溃** — 打开后立即关闭窗口（通常伴随 Chromium 错误日志）

## 根本原因
1. **WebView 进程残留冲突** — 多个旧的 WebViewHost.exe/msedgewebview2.exe 进程占用资源
2. **pywebview 桥接失败** — `window.pywebview.api` 未初始化
3. **后端进程无响应** — t_gui.py API 调用超时

## 快速解决方案

### 方案 1️⃣：一键清理 + 重启（最常用）
**Windows:**
```bash
# 运行清理脚本
clean_gui_processes.bat

# 或手动执行
python t_gui.py
```

**命令行:**
```bash
# 直接清理 + 启动
python -c "import os; os.system('taskkill /F /IM WebViewHost.exe 2>nul'); os.system('taskkill /F /IM msedgewebview2.exe 2>nul')" && python t_gui.py
```

### 方案 2️⃣：完全重启（彻底清理）
```bash
# 杀死所有 Python 进程
taskkill /F /IM python.exe

# 清理 WebView
python -c "import os; os.system('taskkill /F /IM WebViewHost.exe 2>nul'); os.system('taskkill /F /IM msedgewebview2.exe 2>nul')"

# 重新启动
python t_gui.py
```

### 方案 3️⃣：调试模式查看详细信息
```bash
python t_gui.py --debug
```
在打开的窗口中：
- 按 `F12` 打开开发者工具
- 查看 **Console** 标签页的错误信息
- 查看 **Network** 标签页检查 API 请求

## 已修复的改进 ✅

| 项目 | 修改 |
|------|------|
| 超时检测 | app.js:3338 — API 调用 10 秒超时 |
| 错误提示 | app.js:3375-3380 — 后端连接失败显示具体错误 |
| 防御性检查 | app.js:3252-3259 — 检查 DOM 元素存在性 |
| 元素延迟初始化 | app.js:3208-3211 — 将 dateSelect/refreshBtn 延迟到 init() |
| 消息安全 | app.js:41-45 — statusEl() 添加元素存在检查 |

## 如果问题仍未解决

1. **检查进程状态**
   ```bash
   tasklist | find "python"
   tasklist | find "WebView"
   ```

2. **查看最近日志**
   ```bash
   tail -100 t_io/logs/t_trader_sys_2026-08-24.log
   ```

3. **确认工作目录**
   - 必须在 `E:\superTrader` 目录运行
   - `web/` 文件夹必须存在且完整

4. **测试后端 API**
   ```bash
   python test_gui_startup.py
   ```

5. **重装依赖**
   ```bash
   pip install --upgrade pywebview
   ```

## 相关文件
- `clean_gui_processes.bat` — 一键清理脚本（Windows）
- `test_gui_startup.py` — 后端 API 快速测试
- `GUI_LOADING_FIX.md` — 本诊断文档

