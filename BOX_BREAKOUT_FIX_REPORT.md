# 箱体突破判定深度修复——执行报告与部署清单（2026-08-24）

## 一、修复概要

### 问题诊断
GUI + 选股猎手中的箱体突破判定质量差，主要原因：
1. **硬阈值生硬**：突破0.3%-8%的范围太宽，导致信号级误报多
2. **触及标准松散**：容差±0.8-8.8%，"接近"就算触及，不够严格
3. **置信分权重不当**：触及次数权重最大，但触及本身不严格 → 虚假高置信
4. **动态参数缺失**：刚突破判定固定20天+15%，不适应不同宽度的箱体
5. **合并过度**：历史箱体被过度合并，失去阶段性特征

### 修复方案（二阶段）

#### Phase 1: P0 立即修复（✓ 已完成）
- **改进check_box_breakout**：从二值(broken/not broken)改为分级
  - signal级(0.5-1%)：敏感但易误报，仅提示
  - reliable级(1-3%)：推荐用于加仓参考
  - strong级(3%+)：高概率后续，适合加仓
- **更新依赖逻辑**：加仓条件改为仅接受reliable/strong级

**代码改动**：
- t_gui.py:887-945行 — check_box_breakout 重写（分级体系）
- t_gui.py:753-759行 — 更新调用侧逻辑

#### Phase 2: P1 本周修复（✓ 已完成）
- **改进_detect_boxes的触及标准**：
  - 容差从±0.8-8.8%改为±0.5%（更精确）
  - 触及算法从`wh >= ups*0.992`改为`wh >= ups*0.995`

- **优化置信分计算**：
  - 触及质量分权重最高：`touch_quality = (up_touch + dn_touch) * 2.0`
  - 其次是横盘度，最后是宽度适中
  - 宽度分类权重：正常箱(5-15%)>微箱(3-5%)=宽幅(15-22%)

- **刚突破判定动态化**：
  - 微箱体(<5%)：5天+10%
  - 正常箱体(5-15%)：10天+12%
  - 宽幅箱体(>15%)：15天+15%
  - 之前统一的20天+15%现在被废弃

- **合并逻辑改进**：
  - 价格重叠判定从50%改为80%（更严格）
  - 时间重叠要求至少5天（避免时间线简单交集）

**代码改动**：
- t_gui.py:1304-1437行 — _detect_boxes 完整重写

---

## 二、修复代码清单

### 修改的文件

#### [t_gui.py](t_gui.py)（API服务层）

**位置1：check_box_breakout（第887-945行）**
- 类型：主逻辑改进
- 原长度：~38行 → 新长度：~60行
- 改动：
  - 返回值新增 `level` 字段（signal/reliable/strong/far_away）
  - 返回值新增 `confidence` 字段（基于箱体宽度）
  - 阈值判定从二值改为分级
  - 新增docstring说明各等级定义

**位置2：load_intraday_add_positions（第753-759行）**
- 类型：依赖调用更新
- 改动：
  - `right_breakout = bool(bx.get("broken"))` 
  - 改为 `right_breakout = bx.get("level") in ("reliable", "strong")`
  - 目的：仅接受可靠级及以上的突破，排除signal级误报
  - `right_detail` 新增突破等级显示

**位置3：_detect_boxes（第1304-1437行）**
- 类型：核心算法改进
- 原长度：~134行 → 新长度：~220行
- 改动详见下文

**位置4：_detect_boxes详细改动**
```
触及标准：
  旧：_up_touches = np.sum(wh >= (ups * 0.992)[:, None], axis=1)  # ±0.8%
  新：_up_touches = np.sum(wh >= (ups * 0.995)[:, None], axis=1)  # ±0.5%
  理由：更精确地检测真实触及，而非"接近"

横盘阈值：
  旧：rel_slope < 0.005  (0.5%/天)
  新：rel_slope < 0.003  (0.3%/天)
  理由：30天内允许涨跌9%还叫"箱体"太宽松

置信分计算：
  旧：conf = (up_touch + dn_touch) * 1.5 + flatness_bonus * 3 + width_bonus
     权重最高的是触及次数 → 质量差的触及也被高估
  新：conf = touch_quality + flatness + width_score
      touch_quality = (up_touch + dn_touch) * 2.0  # 优先但质量已改进
      flatness = max(0, (0.003 - rel_slope) / 0.003) * 2.0
      width_score = {正常5-15%: 1.5, 微/宽: 0.5, 其他: 0}
     权重结构：精确触及 > 横盘度 > 宽度合理 → 质量优先

刚突破判定：
  旧：固定 days_since <= 20 and pct_above < 15%
  新：动态，基于箱体宽度
      if width < 5: days_since <= 5 and pct_above < 10%    # 微箱体敏感
      elif width < 15: days_since <= 10 and pct_above < 12% # 正常箱体
      else: days_since <= 15 and pct_above < 15%             # 宽幅箱体宽松
  理由：宽度越窄，突破越明确，所以容差越严格

合并逻辑：
  旧：price_overlap > price_span * 0.5 and t_overlap
      t_overlap = a["end"] > b["start"] and b["end"] > a["start"]  # 仅要求时间线有交集
  新：price_overlap_pct > 0.80 and overlap_days >= 5
      理由：80%重叠的两个箱体才算同一个；时间重叠要至少5天
      效果：保留历史的分阶段特征，避免把7月箱体和8月箱体合并
```

---

## 三、验证计划

### 阶段1：单元测试（立即）
```bash
# 1. 语法检查
python -m py_compile t_gui.py

# 2. 功能测试 — 用实际股票数据过一遍API
python -c "
from t_gui import Api
api = Api()
result = api.check_box_breakout('000988')
print(f'Breaking level: {result.get(\"level\")}')
print(f'Confidence: {result.get(\"confidence\")}')
"

# 3. 回测脚本（可选）
python box_breakout_validation.py 000988 180
```

### 阶段2：集成测试（24-48小时）
- GUI前端过一遍加仓条件
- 选股猎手中的突破判定是否改进（见下节"后续集成"）
- 观察历史判定是否有虚假突破被过滤

### 阶段3：实盘观察（1周）
- 新修复后的建仓成功率 vs 历史
- 加仓时机准确度是否提升
- "右侧突破箱体"被触发的频率是否合理（应该下降，因为从signal级严格到reliable级）

---

## 四、后续集成（P2）

### Hunter中的突破判定
目前scorer.py中只有简单的"新高"判定（D1/D2/D5/D6），缺乏箱体概念。

**建议集成方案**：
1. 在Hunter的数据加载阶段调用新的`check_box_breakout`
2. 在评分中新增维度 D10：箱体突破质量
   - 强势突破(strong) → +2分
   - 可靠突破(reliable) → +1分
   - signal级及无突破 → 0分
3. 改进D1/D2的定义，加入突破等级权重

**代码位置**：stock_hunter/modules/scorer.py（暂未修改）

---

## 五、配置变更记录

### 新增参数（硬编码，建议后期配置化）

| 参数 | 旧值 | 新值 | 含义 |
|-----|------|------|------|
| touch_tolerance | ±0.8-8.8% | ±0.5% | 触及容差 |
| flatness_threshold | 0.5%/天 | 0.3%/天 | 横盘阈值 |
| 刚突破_days_窄 | 20 | 5 | 微箱体刚突破天数 |
| 刚突破_pct_窄 | 15% | 10% | 微箱体刚突破幅度 |
| 刚突破_days_正常 | 20 | 10 | 正常箱体刚突破天数 |
| 刚突破_pct_正常 | 15% | 12% | 正常箱体刚突破幅度 |
| 刚突破_days_宽 | 20 | 15 | 宽幅箱体刚突破天数 |
| 合并_price_overlap | 50% | 80% | 合并的价格重叠阈值 |
| 合并_time_overlap | 时间线交集 | ≥5天 | 合并的时间重叠要求 |
| 突破_signal_max | - | 1% | signal级上限 |
| 突破_reliable_max | - | 3% | reliable级上限 |
| 突破_strong_min | - | 3% | strong级下限 |

---

## 六、风险与注意事项

### 风险1：加仓条件变严格，频率可能下降
- **原因**：从`broken`(任何突破)改为`level in (reliable, strong)`
- **影响**：右侧加仓被触发的概率 ↓ ~50-70%（需实盘验证）
- **缓解**：可以在position_builder中同时保留其他加仓条件（比如情绪冰点）

### 风险2：合并条件更严格，箱体数量可能增加
- **原因**：合并的price_overlap从50%改为80%
- **影响**：前端展示的"候选箱体"可能从3个增加到5-7个（历史的不再被合并）
- **缓解**：前端UI已有`recent_valid[:3]`的限制，显示最新的3个

### 风险3：动态刚突破判定可能遗漏一些真突破
- **原因**：微箱体只给5天的窗口，可能对2周前突破的小箱体判定不当
- **缓解**：如果实盘发现遗漏，可以调整参数；目前这是故意的"严格"

---

## 七、性能影响

### 计算量
- _detect_boxes 使用了向量化（sliding_window_view），性能不变
- 新增的宽度分类判断 O(1)，无性能影响
- 合并逻辑的时间复杂度：O(n²)但n最多~150/3=50，可接受

### 缓存
- check_box_breakout 的报价缓存（30秒）保留，不变
- 新的`confidence`字段是计算得到，不需额外存储

---

## 八、部署清单

- [ ] 代码审查：Box_Breakout_Quality_Review.md + 本报告
- [ ] 本地测试：python -m py_compile t_gui.py
- [ ] 单元测试：调用check_box_breakout确认返回格式
- [ ] 回测验证：python box_breakout_validation.py
- [ ] Git提交：`feat: 深度修复GUI箱体突破判定质量(P0+P1)`
- [ ] 前端确认：GUI显示新的`level`和`confidence`字段无误
- [ ] 1周实盘观察：加仓成功率等指标

---

## 九、后续改进方向

1. **参数配置化**：当前所有新参数都是硬编码，建议改为config.py可配
2. **验证框架**：类似于方案A的divergence验证，做箱体突破的后市确认验证
3. **Hunter集成**：新增箱体突破的评分维度，提升选股质量
4. **实盘反馈循环**：跟踪加仓成功率，持续优化参数

---

## 十、相关文档

- [Box_Breakout_Quality_Review.md](Box_Breakout_Quality_Review.md) — 深度分析与理论
- [box_breakout_validation.py](box_breakout_validation.py) — 回测脚本
- t_gui.py — 主实现文件
- config.py — 未来的参数配置（建议）

---

**修复完成时间**：2026-08-24 
**修复者**：Claude Code  
**状态**：✓ 代码实现完成，待验证

