# 【立即启动】方向B - Optuna参数优化

## 环境准备

### 安装Optuna

```bash
pip install optuna -i https://pypi.tsinghua.edu.cn/simple
```

### 验证安装

```bash
python -c "import optuna; print(f'Optuna {optuna.__version__} installed')"
```

---

## 启动优化

### 方式1：直接运行

```bash
cd e:\superTrader
python optuna_parameter_optimization.py
```

### 方式2：带输出日志

```bash
python optuna_parameter_optimization.py > optuna_run.log 2>&1
```

---

## 运行流程

```
Step 1 (1-2分钟)
  ├─ 从watchlist加载所有建仓股票
  ├─ 过去6个月的日线数据
  └─ 提取历史"放量+上涨"的买点

Step 2 (20-40分钟，取决于买点数量)
  ├─ Optuna启动500次试验
  ├─ 每次试验采样参数 → 评分
  ├─ 智能搜索最优参数组合
  └─ 实时显示进度条和最佳分数

Step 3 (1分钟)
  ├─ 提取最优参数
  ├─ 计算三维评分详情
  └─ 保存结果到 t_io/validation/optuna_optimization_result.json
```

---

## 预期输出

### 屏幕输出样例

```
====================================================================================================
【Optuna参数优化】建仓L2/L3参数智能寻优
====================================================================================================

Step 1: 从watchlist提取历史理想买点...
  ✓ 提取了 287 个历史理想买点
  (来自 38 只候选股)

Step 2: 启动Optuna参数优化...
  目标函数: 最大化综合评分 = 40% upside + 35% downside + 25% consistency
  试验次数: 500

  [████████████████████░░░░░░░░░░░░░░░░] 45% | Trial 225 | Best Score: 0.824

====================================================================================================
【优化完成】
====================================================================================================

【最优参数】

  L2缩量倍数:      0.68x
  L2支撑容错:      ±1.2%
  L2递减天数:      3天
  L3放量倍数:      1.18x
  L3 VWAP容错:     ±0.8%

【评分详情】

  综合评分:        0.826/1.000
  收益潜力(40%):   0.821 (82.1% 的买点达到 fwd5≥3%)
  回撤保护(35%):   0.840 (84.0% 的买点保护 maxdd≥-3%)
  稳定性(25%):     0.812 (81.2% 的命中率)

【统计信息】

  总买点数:        287
  命中买点:        233
  命中率:          81.2%
  平均收益:        +3.21%
  收益标准差:      2.14%
  平均回撤:        -2.31%
  回撤标准差:      1.87%

结果已保存: e:\superTrader\t_io\validation\optuna_optimization_result.json
```

### JSON输出文件

```json
{
  "best_params": {
    "l2_shrink": 0.68,
    "l2_support_tol": 0.012,
    "l2_trend_days": 3,
    "l3_vol_ratio": 1.18,
    "l3_vwap_tol": 0.008
  },
  "best_score": 0.826,
  "n_trials": 500,
  "n_buypoints": 287,
  "n_hit_buypoints": 233,
  "hit_rate": 0.812,
  "upside_score": 0.821,
  "downside_score": 0.840,
  "consistency_score": 0.812,
  "stats": {
    "mean_return": 0.0321,
    "std_return": 0.0214,
    "mean_maxdd": -0.0231,
    "std_maxdd": 0.0187
  }
}
```

---

## 下一步

优化完成后，你会得到：

1. **最优参数JSON文件**
   ```
   t_io/validation/optuna_optimization_result.json
   ```

2. **用这些参数更新代码**
   ```
   universal_entry_v2.py 中的参数配置
   ```

3. **对所有候选股重新评估**
   ```
   包括摩恩电气，看L2/L3是否改善
   ```

---

## 时间估算

| 阶段 | 耗时 |
|------|------|
| Step 1 (数据准备) | 1-2分钟 |
| Step 2 (500次试验) | 20-40分钟 |
| Step 3 (结果整理) | 1分钟 |
| **总计** | **22-43分钟** |

建议：**今晚启动，明天早上查看结果**

---

## 常见问题

### Q: 为什么是500次试验？
A: 
- 参数空间: ~200维
- 500次试验通常能找到局部最优
- 更多试验收益递减

### Q: 如果买点很少（<50）会怎样？
A: 
- 警告提示
- 仍能运行，但优化可能不稳定
- 可考虑扩大lookback_days

### Q: 可以自定义参数空间吗？
A: 
- 可以编辑 optuna_parameter_optimization.py
- 修改 trial.suggest_float/suggest_int 的范围

### Q: 结果可以信赖吗？
A:
- 基于历史287个买点
- 三维评分综合考虑
- 但仍需在实盘验证

---

## 立即启动

```bash
cd e:\superTrader
python optuna_parameter_optimization.py
```

**预计22-43分钟完成。** ✅
