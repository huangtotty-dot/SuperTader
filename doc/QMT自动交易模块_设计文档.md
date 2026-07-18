# QMT 自动交易模块 设计文档

- 模块文件：`E:\06_T\qmt_trader.py`（v0.1.0）
- 订单落盘：`E:\06_T\t_io\qmt_orders.jsonl`
- 待确认令牌：`E:\06_T\t_io\qmt_pending_liquidate.json`
- 文档日期：2026-07-18
- 状态：**dry-run 默认**（本机未装 xtquant / miniQMT，全部订单走模拟单）

---

## 1. 架构图

```
┌────────────────────────────────────────────────────────────────────┐
│                         main.py（做T主程序）                        │
│  exec 加载 module_order = [config, utils, ..., qmt_trader]         │
│  shared 命名空间提供：send_feishu_payload / _append_jsonl /         │
│  _feishu_card_header / _feishu_md_div / log / BASE_DIR             │
└──────────────┬─────────────────────────────────────────────────────┘
               │ exec(shared)            独立运行 python qmt_trader.py
               ▼                         （shared 函数缺失 → try/except
┌──────────────────────────────────┐      NameError → 打印降级，
│        qmt_trader.py             │      绝不触达飞书）
│  ┌────────────────────────────┐  │
│  │ QmtConfig / load_qmt_config│  │ ← config.json "qmt" 节
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │         QmtTrader          │  │
│  │  connect()                 │  │     ┌──────────────────────┐
│  │  query_positions()         │──┼────→│ live 路径（xtquant）  │
│  │  place_order()             │  │     │ XtQuantTrader         │
│  │  liquidate_all()           │  │     │  · order_stock_async  │
│  │  handle_systemic_risk()    │  │     │  · query_stock_       │
│  │  close_loop_check() 静态   │  │     │    positions          │
│  └───────┬────────────────────┘  │     └─────────▲────────────┘
│          │ dry_run/enabled/      │               │ 任一异常
│          │ xtquant 缺失          │     自动降级 + 飞书/日志告警
│          ▼                       │     （绝不搞崩主程序）
│  ┌────────────────────────────┐  │
│  │ dry-run 路径                │  │
│  │  · 持仓 ← holdings.json    │  │
│  │  · 模拟单 DRY-时间戳-序号   │  │
│  │  · 落盘 qmt_orders.jsonl   │  │
│  └────────────────────────────┘  │
└──────────────┬───────────────────┘
               ▼
   t_io/qmt_orders.jsonl（order / liquidate_plan / liquidate_execute 三类记录）
               ▼
        飞书卡片（红卡=清仓预警待确认 / 绿卡·橙卡=执行结果 / 橙卡=降级告警）
```

## 2. dry-run / 实盘双模说明

| 判定（按优先级） | 结果 |
|---|---|
| `config.json` 无 `"qmt"` 节，或 `qmt.enabled=false` | dry-run |
| `qmt.dry_run=true`（默认） | dry-run |
| `import xtquant` 失败（未装 miniQMT/SDK） | dry-run + 日志说明 |
| 实盘 `connect()` / 下单任一步骤抛异常 | 自动降级 dry-run + 飞书橙卡告警（每进程一次） |

- **dry-run 行为**：持仓读 `holdings.json`；下单生成 `DRY-YYYYMMDD-HHMMSS-序号` 假单号，
  记录 `ts/code/side/qty/price/est_price/est_value/order_type/reason/dry_run` 落盘 jsonl；
  飞书推送走 shared `send_feishu_payload`（独立运行时打印代替）。
- **实盘行为**：`XtQuantTrader(mini_qmt_path, session_id)` → `start()` → `connect()`（rc≠0 即抛错降级）
  → `subscribe(StockAccount(account_id))`；下单用 `order_stock_async`，市价单 `LATEST_PRICE/-1`，
  限价单 `FIX_PRICE/委托价`。
- **结构化对齐**：dry-run 与实盘共用同一份校验/落盘/卡片代码路径，装好 xtquant 后仅改
  config.json 两个开关即开箱即用，无需改代码。

## 3. API 表

| 接口 | 签名 | 说明 |
|---|---|---|
| 配置 | `load_qmt_config() -> QmtConfig` | 读 config.json `"qmt"` 节；缺失 → 全默认 dry-run |
| 构造 | `QmtTrader(config)` | 尝试 import xtquant；失败 `self.live=False` |
| 连接 | `connect() -> bool` | 实盘建连+订阅；失败自动降级，仍返回 True |
| 持仓 | `query_positions() -> list[dict]` | `{code,name,qty,available,cost,base,t_qty,account,pre_close}`；dry-run 读 holdings.json（含 `000988`/`000988_B` 两条） |
| 下单 | `place_order(code, side, qty, price=None, order_type="market", reason="") -> dict` | 校验：qty>0、买入 100 股整数倍（卖出允许零股）、单笔金额 ≤ max_order_value；返回 `{order_id,status,dry_run,...}`，status ∈ simulated/submitted/rejected/error |
| 清仓 | `liquidate_all(confirm=None, scope="t_qty", dry_run=None, reason="") -> dict` | 双重确认防误触，详见 §4 |
| 系统性风险 | `handle_systemic_risk(meta=None) -> dict` | 上游对接入口，详见 §6 |
| 闭环核对 | `QmtTrader.close_loop_check(holdings, virtual_trades) -> dict` | 静态方法；`unrebuilt = ΣSELL.qty − ΣBUY.qty`，返回 `need_rebuild` 清单（BUY_BACK/SELL_OUT） |

CLI（独立运行，全部 dry-run 安全）：

```bash
python qmt_trader.py --positions                                  # 查持仓
python qmt_trader.py --liquidate --scope t_qty --dry-run          # 生成清仓计划（不执行）
python qmt_trader.py --liquidate --scope t_qty --dry-run --confirm CONFIRM-LIQUIDATE
python qmt_trader.py --close-loop-demo                            # 闭环核对演示
```

## 4. 一键清仓流程（confirm token 时序）

```
调用方                QmtTrader                      飞书
  │  liquidate_all(confirm=None)                     │
  │─────────────────→│ 1. 日清仓次数护栏检查           │
  │                  │ 2. query_positions 生成计划     │
  │                  │    scope=t_qty → 卖量=min(t_qty,│
  │                  │    可用)；scope=all → 全清      │
  │                  │ 3. plan_hash=sha256(计划)[:16]  │
  │                  │ 4. liquidate_plan 落盘          │
  │                  │ 5. 存 pending(token+TTL 300s)   │
  │                  │ 6. 红卡预警（原因/清单/预估金额/ │
  │                  │    确认方式）+ 急促告警          │──→ 🚨 红卡
  │←─────────────────│ 返回 need_confirm + plan_hash  │
  │                                                  │
  │  （人工在 TTL 内确认，二选一）                      │
  │  liquidate_all(confirm=plan_hash)                │
  │   或 confirm="CONFIRM-LIQUIDATE"                 │
  │─────────────────→│ 7. hash 校验+TTL 校验（过期拒绝）│
  │                  │ 8. 逐腿 place_order(sell,市价)   │
  │                  │    每腿仍受单笔金额上限约束        │
  │                  │ 9. order/liquidate_execute 落盘 │
  │                  │10. 结果汇总卡（绿=全成/橙=有失败）│──→ 🏁 汇总卡
  │←─────────────────│ 返回 executed + 逐腿结果        │
```

要点：

- **confirm 三态**：`plan_hash`（须与 pending 中未过期令牌匹配，跨进程持久化于
  `t_io/qmt_pending_liquidate.json`）/ `CONFIRM-LIQUIDATE` 人工主口令 / 其他 → 不执行。
- **token 过期**：返回 `token_expired`，须重新生成计划（防止拿旧计划清新持仓）。
- **幂等防误触**：无 confirm 永不执行；计划哈希随持仓变化自动失效。

## 5. 闭环原则（做T铁律）

1. **底仓不动**：`base` 为底仓，做T只动 `t_qty` 活动仓；EOD 必须 `qty == base`。
2. **正T（先买后卖）**：当日 BUY_LOW 买入的，当日必须 SELL_HIGH 卖出，不得留仓过夜。
3. **反T（先卖后买）**：当日 SELL_HIGH 卖出的，当日必须 BUY_LOW 接回，不得丢筹码。
4. **尾盘审计**：主程序尾盘调用
   `QmtTrader.close_loop_check(HOLDINGS, virtual_trades)`：
   - `unrebuilt = ΣSELL.qty − ΣBUY.qty`（按纯代码聚合，A/B 双账户合并核对）；
   - `unrebuilt > 0` → `BUY_BACK`（反T未接回）；`< 0` → `SELL_OUT`（正T未卖出）；
   - `all_closed=True` 才允许收工。
5. `liquidate_all(scope="t_qty")` 只清活动仓、保留底仓，与闭环原则兼容；
   `scope="all"` 连底仓全清，仅限系统性风险等极端场景。

## 6. 系统性风险触发清仓的对接约定

```
daily_sentiment（上游）
   │ 产出 systemic_risk=True（+ source/note 元信息）
   ▼
main.py 调用 QmtTrader.handle_systemic_risk(meta)
   │
   ├─ 默认（人工确认模式）：
   │    liquidate_all(confirm=None, scope=qmt.auto_liquidate_scope)
   │    → 飞书红卡预警（含计划/金额/确认方式）→ 人工回复 plan_hash
   │      或 CONFIRM-LIQUIDATE 才执行
   │
   └─ 白名单自动模式（config.json 显式开启）：
        "qmt": { "auto_liquidate_on_systemic_risk": true,
                 "auto_liquidate_scope": "t_qty" }
        → liquidate_all(confirm="CONFIRM-LIQUIDATE") 直接执行
        → 仍受「日清仓次数 ≤ 3」「单笔金额上限」护栏约束
```

默认 scope 为 `t_qty`（先撤活动仓、保底仓）；是否启用 `all` 需人工评估后改配置。

## 7. 接入实盘操作清单

1. 安装国金/券商 **miniQMT 客户端**，登录资金账号，保持客户端常驻运行。
2. 用 QMT 自带 Python 或本机 Python311 安装 SDK：`pip install xtquant`
   （xtquant 通常随 QMT 客户端分发，也可从其安装目录复制 `xtquant` 包到 site-packages）。
3. 编辑 `E:\06_T\config.json`，追加：

```json
"qmt": {
  "enabled": true,
  "dry_run": false,
  "account_id": "你的资金账号",
  "mini_qmt_path": "D:\\国金QMT交易端\\userdata_mini",
  "slippage": 0.002,
  "max_order_value": 50000,
  "confirm_token_ttl_sec": 300,
  "auto_liquidate_on_systemic_risk": false,
  "auto_liquidate_scope": "t_qty"
}
```

4. 先保持 `"dry_run": true` 跑 1 天全链路演练（信号→下单→jsonl→飞书卡片），
   确认无误再改 `"dry_run": false`。
5. 在 main.py 的 `module_order` 列表末尾追加 `'qmt_trader'`（由同事 A 负责合入）。
6. 实盘首日建议把 `max_order_value` 调小到 1 万以内试单。

## 8. 风控护栏

| 护栏 | 实现 | 默认值 |
|---|---|---|
| 单笔金额上限 | `place_order` 按 委托价/昨收×(1±slippage) 估额，超限 rejected | 50000 元 |
| 日清仓次数 | `liquidate_all` 执行前统计当日 jsonl 中 `liquidate_execute` 条数 | ≤ 3 次/日 |
| 确认 token TTL | pending 令牌过期即拒绝，须重新生成计划 | 300 秒 |
| 买入整手 | 买入非 100 股整数倍 rejected（卖出允许零股） | — |
| 异常兜底 | 任一 xtquant 调用异常 → 降级 dry-run + 橙卡告警；`place_order`/`liquidate_all` 全方法 try/except，返回 error dict，不抛崩主程序 | — |
| 双模隔离 | CLI `--dry-run` 可强制演练，即使 config 已切实盘也不真实下单（`_force_dry`） | — |

## 9. jsonl 记录格式（t_io/qmt_orders.jsonl）

```json
{"kind":"order","ts":"...","order_id":"DRY-20260718-180828-001","status":"simulated","dry_run":true,"code":"300153","side":"sell","qty":200,"price":null,"est_price":21.207,"est_value":4241.5,"order_type":"market","reason":"一键清仓[t_qty] ..."}
{"kind":"liquidate_plan","ts":"...","scope":"t_qty","plan_hash":"8FCC5D5AEE297EE9","legs":[...],"est_total":271258.0,"dry_run":true,"reason":"..."}
{"kind":"liquidate_execute","ts":"...","scope":"t_qty","plan_hash":"...","dry_run":true,"total_legs":9,"ok_legs":7,"est_total":271258.0,"legs":[{"code":"300153","qty":200,"status":"simulated","order_id":"DRY-..."}]}
```

## 10. 已知限制

- dry-run 持仓的 `available` 以 `qty` 近似（不含当日买入的 T+1 冻结）；实盘以 xtquant 返回为准。
- `max_order_value=50000` 下，大市值持仓单腿卖出会被拒（如 000988 800 股≈10.4 万）。
  清仓拆单（按上限自动切片）列入后续迭代；当前可临时调大上限或分多次执行。
- 市价单金额估算用 昨收×(1±slippage)，与真实成交价存在偏差，护栏为近似值。
- 实盘持仓的 `base/t_qty` 划分仍以本地 `holdings.json` 为准（xtquant 不区分底仓/活动仓）。
