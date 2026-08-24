#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案A三大风险修复方案

风险1: stock_override代码未验证
  → 修复: 在 main.py 中确认并补充 stock_override 逻辑

风险2: 防重桶二次拦截
  → 修复: 在 shadow_signals 中为 stock_override 标的跳过防重桶

风险3: 虚假信号增加
  → 修复: 实现虚假信号监控系统 + 动态回退机制
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
from datetime import datetime
import json

BASE = Path(__file__).resolve().parent


# ════════════════════════════════════════════════════════════════════════════════
# 【风险1修复】stock_override代码验证与补充
# ════════════════════════════════════════════════════════════════════════════════

RISK1_FIX = """
【风险1修复】：stock_override 代码实现

问题: 配置已到位，但 main.py 中的实现逻辑未确认

修复步骤:

1️⃣  在 main.py 中添加 stock_override 检查（约第143行）

   # 在 _compute_resonance() 调用前添加：

   stock_override = config.INDEX_RESONANCE_STOCK_OVERRIDE.get(code, {})
   if stock_override.get("enabled") == False:
       # 这只标的禁用共振门控，直接放行
       resonance_allow = True
       resonance_reason = f"stock_override: {code} 禁用门控"
   else:
       # 使用全局共振门控
       resonance_allow, resonance_reason = _compute_resonance(code, sig.action, price)

2️⃣  验证代码逻辑：

   位置: main.py 约 143-160 行

   现状: _compute_resonance(code, sig.action, price)
         ↓
   修改后:
        if config.INDEX_RESONANCE_STOCK_OVERRIDE.get(code, {}).get("enabled") == False:
            resonance_allow = True
        else:
            resonance_allow, _ = _compute_resonance(...)

3️⃣  验证配置读取（config.py 已就位）：

   ✅ config.py:755 已定义 INDEX_RESONANCE_STOCK_OVERRIDE
   ✅ 588170 已配置 enabled=False
   ✅ 300153 已配置 enabled=False

4️⃣  测试验证：

   grep -n "INDEX_RESONANCE_STOCK_OVERRIDE" main.py
   # 应该看到这个配置被读取

   python -c "from config import INDEX_RESONANCE_STOCK_OVERRIDE; print(INDEX_RESONANCE_STOCK_OVERRIDE)"
   # 应该输出: {'588170.SH': {'enabled': False}, '300153.SZ': {'enabled': False}}
"""


# ════════════════════════════════════════════════════════════════════════════════
# 【风险2修复】防重桶二次拦截
# ════════════════════════════════════════════════════════════════════════════════

RISK2_FIX = """
【风险2修复】：防重桶二次拦截处理

问题: 588170 36次拦截中，6次是共振，29次是防重桶
      即使禁用共振门控，防重桶仍可能再次拦截

修复策略:

方案A: 对禁用共振门控的标的，也跳过防重桶检查
方案B: 对禁用共振门控的标的，降低防重桶的拦截力度
方案C: 对禁用共振门控的标的，允许1小时内2条推送（而非原来的1条）

推荐: 方案A（彻底解决）

实现:

1️⃣  在 main.py 的 shadow_signals 阶段添加：

   # 约第 xxx 行（记录 shadow_signals 时）

   miss_reason = ""
   if stock_override.get("enabled") == False:
       # 禁用门控的标的，跳过防重桶等拦截
       miss_reason = "stock_override: 直接推送，跳过所有拦截"
       allow = True
   else:
       # 正常流程：应用所有拦截
       if resonance blocked: miss_reason = "指数共振拦截"
       elif duplicate blocked: miss_reason = "防重桶拦截"
       elif other blocked: miss_reason = "其他拦截"

2️⃣  核心代码（伪代码）：

   for signal in signals:
       code = signal.code
       stock_cfg = config.INDEX_RESONANCE_STOCK_OVERRIDE.get(code, {})

       if stock_cfg.get("enabled") == False:
           # 激进配置：禁用所有门控
           allow_signal = True
           reason = "stock_override: 禁用门控"
       else:
           # 保守配置：应用所有门控
           allow_signal = check_all_gates(signal)
           reason = get_block_reason(signal)

       record_shadow_signal(code, allow_signal, reason)

3️⃣  验证效果：

   # 查看588170的拦截分布
   grep "588170" t_io/traces/shadow_signals_*.jsonl | grep -c "miss_reason"

   # 修改前: 36次拦截 (6次共振 + 29次防重桶)
   # 修改后: 0次拦截 (全部直接推送)

4️⃣  风险控制：

   - 限制: 仅对 enabled=False 的标的生效
   - 限制: 防重桶在 shadow_signals 层仍会记录，但不会阻断
   - 监控: 记录每条"跳过拦截"的信号，便于后续审计
"""


# ════════════════════════════════════════════════════════════════════════════════
# 【风险3修复】虚假信号监控与动态回退
# ════════════════════════════════════════════════════════════════════════════════

RISK3_FIX = """
【风险3修复】：虚假信号监控 + 动态回退机制

问题: 禁用门控可能在大盘暴跌时无滤防，虚假信号增加

修复方案: 实现虚假信号监控系统 + 自动回退机制

1️⃣  虚假信号定义：

   虚假信号 = 低吸后继续跌幅 > 3%

   监控指标:
   - 低吸推送价格 vs 后续最低价: 差值 > 3%
   - 日浮亏 > 2%: 全天多条信号累积
   - 连续3条错误信号: 同标的短时间内持续亏损

2️⃣  实现虚假信号追踪器：

   class FalseSignalMonitor:
       def __init__(self):
           self.pushed_signals = []  # [{'code': '588170', 'price': 0.996, 'time': '11:02'}]
           self.false_count = 0
           self.true_count = 0
           self.false_ratio = 0.0

       def record_signal(self, code, price, action):
           \"\"\"记录推送的信号\"\"\"
           self.pushed_signals.append({
               'code': code,
               'price': price,
               'action': action,
               'time': datetime.now(),
           })

       def check_signal_outcome(self, code, current_price, original_price):
           \"\"\"检查信号后续表现\"\"\"
           if original_price == 0:
               return None

           drawdown = (original_price - current_price) / original_price
           if drawdown > 0.03:  # 下跌>3%
               self.false_count += 1
               return False  # 虚假信号
           else:
               self.true_count += 1
               return True  # 有效信号

       def get_false_ratio(self):
           \"\"\"获取虚假信号比例\"\"\"
           total = self.true_count + self.false_count
           if total == 0:
               return 0.0
           return self.false_count / total

       def should_rollback(self):
           \"\"\"是否应该回退方案A\"\"\"
           ratio = self.get_false_ratio()
           if ratio > 0.05:  # 虚假 > 5%
               return True
           return False

3️⃣  集成监控到 main.py：

   # 全局监控器
   false_signal_monitor = FalseSignalMonitor()

   # 推送信号时记录
   if signal_allow:
       false_signal_monitor.record_signal(code, price, action)

   # 后续每小时检查一次（或每日收盘时）
   def hourly_check():
       current_prices = fetch_current_prices()
       for sig in false_signal_monitor.pushed_signals:
           if sig['time'] < (now - 1 hour):  # 已过1小时
               current_price = current_prices.get(sig['code'], 0)
               false_signal_monitor.check_signal_outcome(
                   sig['code'], current_price, sig['price']
               )

       ratio = false_signal_monitor.get_false_ratio()
       if ratio > 0.05:
           # 虚假信号>5%，触发回退
           log_alert(f"虚假信号增加到 {ratio:.1%}，准备回退方案A")
           trigger_rollback()

4️⃣  动态回退机制：

   def trigger_rollback():
       \"\"\"动态回退方案A\"\"\"

       # 方案1: 禁用单只标的的覆盖
       if false_ratio > 0.08:  # 虚假>8%
           config.INDEX_RESONANCE_STOCK_OVERRIDE["588170"]["enabled"] = True
           log_info("588170 恢复共振门控")

       # 方案2: 调低评分或提高拦截力度
       if false_ratio > 0.05:  # 虚假>5%
           config.PARAMS["deep_water_signal_score"] = 60  # 改为60分
           log_info("信号评分降低，拦截力度增强")

       # 方案3: 彻底关闭方案A
       if false_ratio > 0.10:  # 虚假>10%
           config.PARAMS["enable_deep_water_mode"] = False
           config.INDEX_RESONANCE_STOCK_OVERRIDE = {}
           log_alert("彻底关闭方案A，回退原方案")

5️⃣  日志与告警：

   # 每日输出监控报告
   def daily_report():
       ratio = false_signal_monitor.get_false_ratio()
       total = false_signal_monitor.true_count + false_signal_monitor.false_count

       print(f\"\"\"
       【虚假信号监控报告】{get_today_str()}

       总信号数: {total}
       有效信号: {false_signal_monitor.true_count}
       虚假信号: {false_signal_monitor.false_count}
       虚假比例: {ratio:.2%}

       状态: {'✅ 正常' if ratio < 0.05 else '⚠️ 警告' if ratio < 0.10 else '❌ 触发回退'}
       \"\"\")

6️⃣  观察期监控计划：

   Day 1 (2026-08-25):
     - 08:30: 启用方案A
     - 12:00: 检查588170推送数
     - 16:00: 统计虚假信号初值

   Day 2 (2026-08-26):
     - 09:30: 检查前一日虚假信号后续表现
     - 16:00: 汇总虚假比例

   Day 3 (2026-08-27):
     - 全天: 重点监控
     - 15:00: 最终评估 (虚假>5% 则回退)

   Day 4 (2026-08-28):
     - 做最终决策 (继续/调整/回退)
"""


# ════════════════════════════════════════════════════════════════════════════════
# 【综合修复总结】
# ════════════════════════════════════════════════════════════════════════════════

COMPREHENSIVE_FIX = """
【三大风险 - 综合修复方案】

┌─ 风险1: stock_override代码未验证 ─────────────────────────────┐
│                                                               │
│ 修复: 在 main.py 第143行添加 stock_override 检查            │
│ 效果: ✅ 确保禁用门控逻辑正确执行                           │
│ 验证: grep "INDEX_RESONANCE_STOCK_OVERRIDE" main.py        │
│ 时间: 5分钟                                                 │
│                                                               │
└──────────────────────────────────────────────────────────────┘

┌─ 风险2: 防重桶二次拦截 ──────────────────────────────────────┐
│                                                               │
│ 修复: 在 shadow_signals 中为禁用门控标的跳过防重桶          │
│ 方案: enabled=False 的标的完全跳过所有拦截                 │
│ 效果: ✅ 588170 从36次拦截→0次拦截(全部推送)             │
│ 验证: grep "588170" shadow_signals | wc -l                  │
│ 时间: 10分钟                                                │
│                                                               │
└──────────────────────────────────────────────────────────────┘

┌─ 风险3: 虚假信号增加 ──────────────────────────────────────────┐
│                                                               │
│ 修复1: 实现 FalseSignalMonitor 监控系统                     │
│ 修复2: 实现动态回退机制（虚假>5%自动回退）                 │
│ 修复3: 日报表与告警系统                                    │
│                                                               │
│ 效果: ✅ 虚假信号自动监控与回退                            │
│ 验证: 观察3日数据                                           │
│ 时间: 20分钟(编码) + 3日(观察)                             │
│                                                               │
└──────────────────────────────────────────────────────────────┘

总修复时间: ~35分钟代码实现 + 3日实盘观察

修复后预期效果:
  ✅ 风险1: stock_override 代码确认无误
  ✅ 风险2: 588170 推送数从0→预期6条(或更多)
  ✅ 风险3: 虚假信号<5%, 自动监控与回退机制完善
"""


# ════════════════════════════════════════════════════════════════════════════════
# 【快速修复清单】
# ════════════════════════════════════════════════════════════════════════════════

QUICK_FIX_CHECKLIST = """
【快速修复 - 35分钟行动清单】

✅ 第一步 (5分钟): 风险1修复 - stock_override 代码

   文件: main.py
   位置: 约第143行 (在 _compute_resonance 调用前)

   添加代码:
   ```
   stock_override = config.INDEX_RESONANCE_STOCK_OVERRIDE.get(code, {})
   if stock_override.get("enabled") == False:
       resonance_allow = True
       resonance_reason = f"stock_override: {code} 禁用门控"
   else:
       resonance_allow, resonance_reason = _compute_resonance(code, sig.action, price)
   ```

   验证:
   ```
   grep -n "INDEX_RESONANCE_STOCK_OVERRIDE" main.py
   python -c "from config import INDEX_RESONANCE_STOCK_OVERRIDE; print(INDEX_RESONANCE_STOCK_OVERRIDE)"
   ```

✅ 第二步 (10分钟): 风险2修复 - 防重桶跳过

   文件: main.py (shadow_signals 记录处)
   位置: 约第xxx行 (记录 miss_reason 时)

   修改逻辑:
   ```
   if stock_override.get("enabled") == False:
       # 激进: 跳过所有拦截
       allow = True
       miss_reason = "stock_override: 直接推送"
   else:
       # 保守: 应用所有拦截
       allow = check_resonance() and check_duplicate() and check_others()
       miss_reason = get_block_reason()
   ```

✅ 第三步 (20分钟): 风险3修复 - 虚假信号监控

   文件: 新建 fake_signal_monitor.py

   内容:
   - FalseSignalMonitor 类
   - record_signal() 记录推送
   - check_signal_outcome() 检查后续表现
   - should_rollback() 判断是否回退
   - trigger_rollback() 动态回退

   集成到 main.py:
   ```
   false_signal_monitor = FalseSignalMonitor()

   # 推送时记录
   false_signal_monitor.record_signal(code, price, action)

   # 每小时检查
   hourly_check()
   ```

✅ 观察期 (3天):

   Day 1: 启用 + 初值统计
   Day 2: 虚假信号后续表现检查
   Day 3: 最终评估 (虚假<5% 才继续)

总耗时: 35分钟编码 + 3日观察
"""


def main():
    """输出修复方案."""
    print("\n" + "=" * 90)
    print("【方案A三大风险修复方案】")
    print("=" * 90)

    print(RISK1_FIX)
    print("\n" + "=" * 90)

    print(RISK2_FIX)
    print("\n" + "=" * 90)

    print(RISK3_FIX)
    print("\n" + "=" * 90)

    print(COMPREHENSIVE_FIX)
    print("\n" + "=" * 90)

    print(QUICK_FIX_CHECKLIST)
    print("\n" + "=" * 90)

    # 输出为文档
    output_path = BASE / "SCHEME_A_RISK_FIXES.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 方案A 三大风险修复方案\n\n")
        f.write(RISK1_FIX)
        f.write("\n\n")
        f.write(RISK2_FIX)
        f.write("\n\n")
        f.write(RISK3_FIX)
        f.write("\n\n")
        f.write(COMPREHENSIVE_FIX)
        f.write("\n\n")
        f.write(QUICK_FIX_CHECKLIST)

    print(f"\n✓ 修复方案已输出: {output_path}")


if __name__ == "__main__":
    main()
