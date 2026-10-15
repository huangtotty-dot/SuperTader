# 账本目录被误写 → 已修复（2026-10-15 08:16-08:22）

## 发生了什么

2026-10-14 **19:11**，一次**离线端到端测试**（`tests/phase3/osc_cycle_driver.py`，两日纸面演练）
把它的合成数据写进了**生产账本目录**（本目录），覆盖/新建了：

| 文件 | 被写成 |
|---|---|
| `positions.json` | 合成票 600002 2600股 / 600003 10400股（**真实持仓 605198/688003/301373 被抹掉**） |
| `fills_2026-10-14.json` | 合成数据（真实当日 4 笔买单留痕被覆盖） |
| `exec_report_2026-10-14.json` | 同上 |
| `sells_close_2026-10-14.json` | 同上 |
| `day_state_2026-10-14.json` · `day_state_2026-10-15.json` | 新建（合成） |
| `fills/sells_morning/sells_close_2026-10-15.json` | 新建（合成，**今天的**） |

## 根因

测试驱动的设计是**沙箱**的（`GM._OSC_BOOK_DIR` / `GM.OSC_RT.book_dir` 都指向临时目录），
但当时工作区里有一版**未提交的**改动：`gm_main.init()` 会调 `_restore_live_bindings()`，
在 MODE_LIVE 下把 book_dir **重算回生产路径**。驱动先设沙箱、再 `init(实盘 ctx)` ⇒
**沙箱设置被覆盖**，随后整场演练写进生产。
⇒ 该版本已于同日**全部撤销**（工作区回到已提交状态），此路径不再存在。

## 🔴 最危险的一条（已消除）

`day_state_2026-10-15.json` 里带着 **`buy_done: true`**。真策略今天 10:00 启动时
`load_day_state()` 会读到它、判定"首轮已执行"⇒ **今天买侧一笔都不下**（且只在日志里留一行）。
该文件已删除。

## 修复动作

1. **删除**全部 4 个伪造的 10-15 文件（`day_state` / `fills` / `sells_morning` / `sells_close`）
   ＋ `exec_report_2026-10-15.json` ＋ 合成的 `day_state_2026-10-14.json`。**未重建这些——它们本就不该存在。**
2. **还原 `positions.json`** 为真实持仓（与 `t_io/bridge/heartbeat.json` 的仿真账户持仓**逐项吻合**）：
   `605198 1800@62.043` · `688003 1700@65.662` · `301373 300@108.81`
3. **重建 3 个 10-14 留痕**（`fills` / `exec_report` / `sells_close`），来源见下，并在文件里带
   `_rebuilt` 标记说明是重建、不是原始产物。

## 重建的数据来源（都是**未被污染**的）

| 来源 | 提供了什么 |
|---|---|
| `execution/auto/logs/strategy_20261014.log`（策略控制台 tee） | 当日全部执行摘要：候选 276 只、买侧 `candidates=4/buys_placed=3/deferred=1/notional=230803`、卖侧 `sells_planned=3/sells_placed=0`、逐笔 `[WS] BUY …` 行 |
| `t_io/bridge/events_20261014.jsonl` | 3 笔 order 事件（600097/000823/301373）+ 1 笔 fill（301373）+ 2 笔 15:00 拒单（status=12） |
| `t_io/bridge/heartbeat.json`（10-14 14:58 快照） | 收盘时仿真账户的真实持仓与现金 |
| 本会话污染前读到的 `fills_2026-10-14.json` 原文 | `buys[]` 四条逐字 |

## 顺带确认（当日真实结果）

- 10-14 **没有任何卖出**（早盘 `sells_placed=0`、收盘 `sells_placed=0`）—— 与心跳里持仓未变一致。
- 301373 是当日**唯一成交**（300股@109.6967）；600097/000823 全天封死涨停，15:00 以 status=12 过期。

## 遗留

- `sells_morning_2026-10-14.json` 与 `*_2026-10-13.json` **未被污染**，保持原样。
- 被覆盖的原始文件已另存到 `/tmp/osc_book_bogus/`（仅供追查，**不要**拿来当记录用）。

---

## 附录 B：days_held 手工补记（2026-10-15 15:45，管道堵塞事故）

## 发生了什么

2026-10-15 **13:00** 策略进程静默挂死：py-spy 栈实锤主线程阻塞在
`gm_bridge/ops_guard.py` 的 `_TeeStream.write` —— 策略 stdout 是通向掘金终端的管道，
**终端进程（22516）午后死亡**，父进程链断裂（终端22516已死 → cmd 17040 孤儿 → 策略30000），
Windows 管道写满 64KB 后 write 永久阻塞。挂死进程已 kill。

后果：**14:50 卖侧三件套整日缺席**（SL5只深止损 + 满5日到期平仓 + days_held 推进），
心跳停在 11:29，`sells_close_2026-10-15.json` 未生成。

## 补记动作（positions.json）

按"如果今天正常跑"的轨迹（`executor._advance_days_held`：last_check_date≠当日 且
entry_date≠当日 ⇒ +1）手工补记，留痕字段 `days_held_backfill_2026_10_15`：

| 票 | days_held | last_check_date | 依据 |
|---|---|---|---|
| 605198 | 1 → **2** | 2026-10-14 → **2026-10-15** | entry 10-12，到期轨迹 10-16→3、10-19→4、10-20→5 |
| 688003 | 1 → **2** | 2026-10-14 → **2026-10-15** | 同上 |
| 301373 | 0 → **1** | 2026-10-14 → **2026-10-15** | 10-15 10:00 早盘冲高卖已按 days_held==0 判定过（`sells_morning_2026-10-15.json`: hold，未触发 +3%），补 1 后明早起 not_next_day，与正常轨迹一致 |

**未补生成** `sells_close_2026-10-15.json`：它是"当日真实跑过卖侧"的凭证，
手工伪造会让 watchdog/复盘误判；缺失本身就是事故留痕的一部分。

## 修复链路（同日施工）

1. `ops_guard._TeeStream` 根治：文件日志先行 + 控制台异步有界队列（commit e7ebf2c）；
2. `scripts/osc_close_watchdog.py`：14:50 卖侧独立补跑看门狗（commit 5c0b29b）——
   明晨起建议挂 Windows 计划任务 14:52 每日拉起（策略正常 ⇒ 秒退零副作用）；
3. 本附录：days_held 补记留痕。
