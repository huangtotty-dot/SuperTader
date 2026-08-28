# gm 数据配额实测报告（P0-1）

测试时间: 2026-08-28 10:19:50  end_time=2026-08-28  标的数=41

## 实测结果

### daily_800

```
{
  "freq": "1d",
  "count": 800,
  "symbols": 41,
  "ok": 41,
  "fail": 0,
  "first_rows": 800,
  "total_sec": 70.7,
  "per_sec": 1.72,
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
  "first_rows": 240,
  "total_sec": 11.7,
  "per_sec": 0.28,
  "err_types": {}
}
```

### subscribe_60s

```
{
  "ok": true,
  "note": "subscribe 成功建立"
}
```

### index_daily_900

```
{
  "ok": true,
  "rows": 900,
  "sec": 0.3
}
```

## 结论

**gm 作为主数据源是否可行: 是**

- 日线成功率: 100.0%
- 60s 成功率: 100.0%
- 限流报错: 未检测到
- gm 可作为主数据源

*若结论为配额不足 → 停下找用户，数据主源改为 gm 供 auto 侧、腾讯供 manual 侧（方案 §9-2 重议）。*
