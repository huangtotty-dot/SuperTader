# 账户管理整合方案

**完成时间**: 2026-08-26 11:23  
**目标**: 统一账户信息管理，避免多处维护导致的不一致

## 问题分析

账户信息原本散落在多个地方：
- `holdings.json` - 每只持仓上的 account 字段
- `t_io/state/portfolio_config.json` - 账户总资本和已实现亏损
- `main.py` 代码中的默认值
- `replay_day.py` 的测试数据
- `t_gui.py` 的加载逻辑

这导致：
1. 账户信息更新时需要修改多个文件
2. 新增账户时容易遗漏某个地方
3. UI 显示与后端数据不同步

## 解决方案

### 1. 创建统一的账户配置文件

**文件**: `t_io/state/accounts_config.json`  
**职责**: 账户信息的唯一源头

```json
{
  "accounts": {
    "账户A": {
      "broker": "国盛证券-黄",
      "total_capital": 100021.06,
      "available": 100021.06,
      "holdings": [],
      "enabled": true
    },
    "账户B": {
      "broker": "东兴证券-鲁",
      "total_capital": 85098.18,
      "available": 63546.18,
      "holdings": ["515180", "600481", "002639"],
      "enabled": true
    },
    "账户C": {
      "broker": "东方证券-黄",
      "total_capital": 213618.62,
      "available": 211694.12,
      "holdings": ["588170"],
      "enabled": true
    }
  },
  "realized_loss": {...}
}
```

**字段说明**:
- `broker`: 券商名称+姓氏，用于 UI 显示账户来源
- `total_capital`: 账户总资金
- `available`: 可用资金
- `holdings`: 该账户持有的股票代码列表
- `enabled`: 账户是否启用

### 2. 更新后端加载逻辑

#### t_gui.py 改进

新增常量：
```python
ACCOUNTS_CONFIG = STATE_DIR / "accounts_config.json"
```

改进 `load_portfolio_config()`:
- 优先读 `accounts_config.json`
- 无则降级到 `portfolio_config.json`（向后兼容）

改进 `load_position_manager()`:
- 从统一配置读取账户信息

增强 `load_day()`:
- 新增 `accounts_detail` 字段，返回完整的账户详情

### 3. 更新前端显示

#### app.js 改进

1. 添加 `accountsDetail` 到全局 state
2. 在 `updateSidebarSummary()` 中显示账户详情
3. 在持仓表格中：
   - 账户名称上添加 `title` 提示，显示 broker 信息
   - 鼠标悬停可见券商名称

#### 示例
```html
<td class="cell-dim" title="东兴证券-鲁">账户B etf</td>
```

### 4. 兼容性保证

- `portfolio_config.json` 保留但标记为废弃
- 添加迁移说明指向 `accounts_config.json`
- 后端自动降级处理，无需改动现有流程

## 后续维护流程

**更新账户信息的标准流程**:

1. 编辑 `t_io/state/accounts_config.json`
2. 后端会自动在下次加载时应用
3. 前端显示自动刷新（无需代码改动）

## 文件变更清单

### 新增
- ✅ `t_io/state/accounts_config.json` - 统一账户配置

### 修改
- ✅ `t_io/state/portfolio_config.json` - 标记废弃，指向新文件
- ✅ `t_gui.py` - 添加 ACCOUNTS_CONFIG 常量，改进加载逻辑
- ✅ `web/app.js` - 添加 accountsDetail 显示
- ✅ `holdings.json` - 更新为最新的账户分配

### 保留（无需改动）
- `main.py` - 默认值使用不变
- `replay_day.py` - 测试数据保留，不依赖生产配置

## 验证清单

- [x] accounts_config.json 格式正确
- [x] portfolio_config.json 向后兼容
- [x] t_gui.py 加载逻辑正确
- [x] app.js 显示账户 broker 信息
- [ ] GUI 运行测试（manual）
- [ ] 持仓表格显示账户 tooltip
- [ ] 切换账户后 sidebar 汇总更新

## 后续可选优化

1. 添加账户切换 UI（账户过滤）
2. 账户级别的收益率统计
3. 账户间的风险分散分析
4. 自动同步多账户持仓数据
