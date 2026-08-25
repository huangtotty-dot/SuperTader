# Hunter选股猎手集成指南（P2）

## 集成概述

将改进的箱体突破质量判定集成到stock_hunter中，新增D10评分维度，提升选股质量。

---

## 修改内容

### 1. scorer.py — 新增D10箱体突破质量维度

**文件**：`stock_hunter/modules/scorer.py`

**变更**：
- 文档版本更新：v10 → v11
- 满分更新：37分 → 42分（新增D10: 2分）
- 新增类：`D10箱体突破质量Scorer`

**D10评分规则**：

| 突破等级 | 判定条件 | 得分 | 说明 |
|---------|---------|------|------|
| **strong** | 突破幅度 > 3% | +2分 | 强势突破，高概率后续 |
| **reliable** | 突破幅度 1-3% | +1分 | 可靠突破，推荐参考 |
| **signal** | 突破幅度 0.5-1% | 0分 | 信号级，敏感但误报高 |
| 无突破 | 幅度 < 0.5% | 0分 | 未达突破阈值 |

**代码实现**：

```python
class D10箱体突破质量Scorer(ScorerBase):
    """P2新增：基于改进的check_box_breakout判定的质量评分"""
    name = "D10箱体突破质量"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        breakout_level = stock_data.get("breakout_level")  # signal/reliable/strong/None
        quality_score = stock_data.get("box_quality_score", 0) or 0  # 1-10分

        if breakout_level == "strong":
            return 2, f"强势突破(质量{quality_score:.1f}/10) -> 2分"
        elif breakout_level == "reliable":
            return 1, f"可靠突破(质量{quality_score:.1f}/10) -> 1分"
        elif breakout_level == "signal":
            return 0, f"信号级突破(质量{quality_score:.1f}/10，谨慎) -> 0分"
        else:
            return 0, "无箱体突破或突破不足0.5% -> 0分"
```

---

## 数据流集成

### 数据源

D10评分需要的数据来自GUI的`check_box_breakout`新返回字段：

```python
{
    "broken": bool,           # 是否突破
    "level": str,            # 突破等级: signal/reliable/strong/far_away/None
    "confidence": float,     # 置信度 1-100
    "box": dict,            # 箱体低/高位置
    "price": float,         # 当前价格
    "pct_above": float,    # 超出幅度百分比
}
```

### 集成点

在`market_data.py`的`prepare_data()`函数中，为每只股票调用GUI的`check_box_breakout`：

```python
# 在 prepare_data() 中添加（伪代码）
def prepare_data(codes, date):
    for code in codes:
        # ... 现有数据加载 ...
        
        # P2新增：获取箱体突破信息
        try:
            from t_gui import Api
            api = Api()
            breakout_info = api.check_box_breakout(code)
            
            stock_data["breakout_level"] = breakout_info.get("level")
            stock_data["box_quality_score"] = breakout_info.get("confidence", 0)
            stock_data["box_pct_above"] = breakout_info.get("pct_above", 0)
        except Exception as e:
            log(f"获取{code}箱体突破失败: {e}")
            stock_data["breakout_level"] = None
            stock_data["box_quality_score"] = 0
```

### 可选集成

如果希望更彻底的集成，可以在Hunter的初始化阶段就加载GUI的API：

```python
# 在 __init__.py 中
try:
    from t_gui import Api
    GUI_API = Api()
except ImportError:
    GUI_API = None
    log("Warning: GUI API not available, box breakout scoring will be skipped")
```

---

## 前端显示优化

### 选股结果表格中显示D10

在`web/app.js`中，为Hunter的选股结果添加D10显示：

```javascript
// 在选股结果表格中添加D10列
const d10_score = result.D10箱体突破质量 || 0;
const d10_label = {
    2: "强势突破",
    1: "可靠突破",
    0: "信号级/无突破"
}[d10_score] || "—";

const d10_badge = `<span class="badge ${d10_score === 2 ? 'strong' : d10_score === 1 ? 'reliable' : 'signal'}">${d10_label}</span>`;
```

### 得分排序

默认按总得分倒序，D10作为加分项自动参与排序。

---

## 使用示例

### 回测验证

用回测脚本验证D10的贡献度：

```bash
# 不使用D10的对标（D1-D9）
python box_breakout_validation.py --dimensions D1,D2,D4,D5,D6,D7,D8,D9 --days 180

# 使用D10的新配置（D1-D10）
python box_breakout_validation.py --dimensions D1,D2,D4,D5,D6,D7,D8,D9,D10 --days 180

# 比对胜率差异
```

### 实盘应用

在选股猎手的启动参数中，确保D10被包含：

```python
scorer = ConceptScorer(dimensions=[
    "D1强势形态且新高",
    "D2强势形态",
    "D4首板资金池",
    "D5潜在突破10日",
    "D6潜在突破5日",
    "D7持续性",
    "D8情绪分数",
    "D9活跃程度",
    "D10箱体突破质量",  # P2新增
])
```

---

## 注意事项

### 1. 依赖关系

D10的准确性取决于GUI的`check_box_breakout`函数。确保：
- t_gui.py已升级到P0+P1修复版本（commit 414f6e29）
- K线数据实时更新
- 现价数据准确性

### 2. 容错机制

如果无法获取箱体突破信息（如GUI不可用），D10得分默认为0：

```python
try:
    breakout_info = api.check_box_breakout(code)
    score = D10计算(breakout_info)
except:
    score = 0  # 安全降级
```

### 3. 性能考虑

每只股票调用`check_box_breakout`会触发K线数据加载和箱体检测，可能影响性能：

- **单股时间**：~50-100ms（缓存命中时 <10ms）
- **50只股票**：~2.5-5秒（启用30秒缓存）
- **建议**：启用result caching或异步处理

### 4. 实盘调试

```python
# 调试输出：显示D10的详细判定
log(f"{code}: {stock_data['breakout_level']} ({stock_data['box_quality_score']:.1f}/10)")
```

---

## 后续优化方向

### 短期（1-2周）
- [ ] 验证D10的贡献度（与D1-D9对标）
- [ ] 调整D10的权重（目前2分，可能需要微调）
- [ ] 前端显示优化

### 中期（2-4周）
- [ ] 与实盘成功率对标
- [ ] 判断D10是否应该提权（从2分改为3分）
- [ ] 考虑增加复合条件（如D10+D1的组合）

### 长期
- [ ] 与方案A的divergence验证对标
- [ ] 考虑增加多时间框架验证
- [ ] 构建更复杂的箱体突破质量模型

---

## 集成清单

- [ ] **修改scorer.py** — 添加D10类，更新ConceptScorer
- [ ] **验证语法** — `python -m py_compile stock_hunter/modules/scorer.py`
- [ ] **补充market_data.py** — 在prepare_data中调用check_box_breakout
- [ ] **前端显示** — 在选股结果表中显示D10
- [ ] **功能测试** — 验证D10得分计算正确
- [ ] **回测对标** — 用历史数据验证D10的贡献
- [ ] **实盘上线** — 监控首周表现

---

**集成版本**：P2  
**文档版本**：v1.0  
**完成时间**：2026-08-24  
**状态**：可上线集成

