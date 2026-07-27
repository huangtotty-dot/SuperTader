@echo off
REM 模拟盘每日收盘归档（15:05 后运行）——K3 复盘五件套
REM 用法：双击或计划任务每日 15:05 执行
set DAY=%date:~0,4%%date:~5,2%%date:~8,2%
set DAY=%DAY: =0%
set DEST=E:\06_T\t_io\reviews\gm_sim\%DAY%\evidence
mkdir "%DEST%" 2>nul
copy /Y "E:\06_T\t_io\gm_bridge\events_%DAY%.jsonl" "%DEST%\" 2>nul
copy /Y "E:\06_T\t_io\gm_bridge\heartbeat.json" "%DEST%\heartbeat_close.json" 2>nul
copy /Y "E:\06_T\t_io\gm_bridge\heartbeat_%DAY%.jsonl" "%DEST%\" 2>nul
copy /Y "C:\Users\Lenovo\.goldminer3\projects\e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7\gmcache\backtrace.jsonl" "%DEST%\backtrace_%DAY%.jsonl" 2>nul
echo [%DAY%] archived to %DEST%
REM 终端成交导出（材料#4）需人工从掘金终端导出后放入本目录
