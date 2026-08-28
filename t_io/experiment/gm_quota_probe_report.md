# gm 数据配额实测报告（P0-1 返工）

测试时间: 2026-08-28 10:55:40  end_time=2026-08-28  标的数=41  token来源=终端会话动态发现(9977f6f9...)

## 实测结果

### daily_800

```
{
  "freq": "1d",
  "count": 800,
  "symbols": 41,
  "ok": 41,
  "fail": 0,
  "min_rows": 340,
  "max_rows": 800,
  "target_rows": 800,
  "below_target": [
    "588170"
  ],
  "below_target_n": 1,
  "empty": [],
  "total_sec": 82.1,
  "per_sec": 2.0,
  "err_types": {}
}
```

### minute_60s_240

```
{
  "freq": "60s",
  "count": 240,
  "symbols": 41,
  "ok": 41,
  "fail": 0,
  "min_rows": 240,
  "max_rows": 240,
  "target_rows": 240,
  "below_target": [],
  "below_target_n": 0,
  "empty": [],
  "total_sec": 16.6,
  "per_sec": 0.41,
  "err_types": {}
}
```

### subscribe_60s

```
{
  "ok": true,
  "note": "subscribe 成功建立（注：裸 subscribe 不能证明收到 bar，见结论）"
}
```

### index_daily_900

```
{
  "ok": true,
  "rows": 900,
  "sec": 5.2
}
```

## 结论

**gm 作为主数据源是否可行: 是**

- 日线成功率: 100.0%（min_rows=340，目标800；低于目标40只之外=1只新上市）
- 60s 成功率: 100.0%（min_rows=240，目标240）
- 限流报错: 未检测到
- 行数校验: 通过（空返回=[]）
- 日线低于目标（新上市预期）: ['588170']
- gm 可作为主数据源

**subscribe 收到 bar 证据（P0 打回项3，已补强）**：`gm_subscribe_probe.py` 在 run() 回测回调上下文
订阅全部 41 只 @60s，on_bar 实际收到唯一标的 **41/41**，无遗漏（回测窗口 2026-08-27 09:30-09:45，
15 分钟窗口内多根 bar 持续确认）。
