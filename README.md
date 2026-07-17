# 三度猎手 V17.10 模块化说明

本项目将 `selection_v17.10.py` 拆分为多个独立的模块文件，便于维护和测试。

## 文件结构

| 文件 | 功能 | 说明 |
|------|------|------|
| `config.py` | 配置与常量 | 所有导入、环境变量、日志、常量、策略开关 |
| `utils.py` | 通用工具函数 | 纯工具函数，不依赖外部数据 |
| `weekly_strategies.py` | 周线策略 | 周线突破、底部企稳、止跌反抽 |
| `feishu.py` | 飞书通知 | 飞书 webhook 推送、月度排名推送 |
| `stock_pool.py` | 股票池管理 | 全A股加载、概念解析、市值过滤、ST过滤 |
| `data_fetcher.py` | 数据获取与缓存 | akshare/腾讯K线/QT快照数据获取、缓存管理 |
| `signal_detector.py` | 信号检测 | 箱体识别、涨停检测、日线策略检查 |
| `monthly_ranking.py` | 月度收益排名 | 月度涨幅统计、TOP排名 |
| `regression.py` | 验证与回归 | 成交额回归测试、交易日验证 |
| `scan_engine.py` | 扫描引擎 | 主扫描函数、多线程处理、信号收集 |
| `main.py` | 入口 | 命令行参数、策略配置加载、启动扫描 |
| `__init__.py` | 包初始化 | 方便整体导入 |

## 导入关系

- `config.py` 不依赖任何其他模块
- `utils.py` → `config.py`
- `weekly_strategies.py` → `config.py`, `utils.py`
- `stock_pool.py` → `config.py`
- `data_fetcher.py` → `config.py`, `weekly_strategies.py`, `stock_pool.py`
- `signal_detector.py` → `config.py`
- `feishu.py` → `config.py`, `utils.py`, `weekly_strategies.py`, `stock_pool.py`
- `monthly_ranking.py` → `config.py`, `data_fetcher.py`, `stock_pool.py`, `utils.py`, `feishu.py`
- `regression.py` → `config.py`, `data_fetcher.py`, `signal_detector.py`, `utils.py`
- `scan_engine.py` → `config.py`, `utils.py`, `stock_pool.py`, `data_fetcher.py`, `weekly_strategies.py`, `signal_detector.py`, `feishu.py`, `regression.py`
- `main.py` → `config.py`, `regression.py`, `monthly_ranking.py`, `scan_engine.py`

## 运行方式

```bash
cd E:\06_T
python main.py
```

## 环境变量

所有环境变量配置在 `config.py` 中统一管理，原文件逻辑完全保留。

## 原始文件

`selection_v17.10.py` 作为备份保留在原目录。
