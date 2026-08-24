#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日复盘 Review 自动生成脚本（增强版）
将方案A的优化效果检查集成到 §4.5 做T附章

用法：
    python t_io/validation/daily_review/daily_review.py --date 2026-08-25

此脚本是对现有 daily_review.py 的补充说明。
实际集成方式：在 daily_review.py 的§4.5 段落中调用 check_optimization_effect.py 的函数
"""

# ==================== 集成步骤 ====================
# 
# 在现有的 daily_review.py 中：
# 
# 1. 在 generate_review() 函数中，§4.5 做T附章的生成逻辑里新增：
#
#    from check_optimization_effect import generate_optimization_effect_report
#    effect_report = generate_optimization_effect_report(date_str)
#    
# 2. 把 effect_report 内容插入到 md 中的§4.5 末尾
#
# 3. 保存生成的 Review.md
#
# ==================== 示例：集成代码片段 ====================

INTEGRATION_CODE = '''
# 在 daily_review.py 的 main() 或 generate_review() 中添加：

def _generate_section_4_5_doing_t(date_str: str, holdings: dict, traces_dir: Path) -> str:
    """§4.5 做T附章（含方案A优化效果检查）"""
    
    lines = ["## §4.5 做T附章（V2.2 新增）\\n"]
    
    # 原有的做T逻辑...（略）
    
    # 新增：方案A优化效果检查
    try:
        from check_optimization_effect import generate_optimization_effect_report
        optimization_report = generate_optimization_effect_report(date_str)
        lines.append(optimization_report)
    except Exception as e:
        lines.append(f"⚠️ 方案A效果检查失败: {e}\\n")
    
    return "\\n".join(lines)
'''

print(__doc__)
print("\n" + INTEGRATION_CODE)

# ==================== 手工操作替代方案 ====================
# 如果不想改 daily_review.py，可以每日手工执行：
#
#    python t_io/validation/daily_review/check_optimization_effect.py --date 2026-08-25 >> doc/每日复盘/2026-08-25_复盘.md
#
# 或者在 cron 中自动执行（Linux/Mac）：
#    0 15 * * 1-5  cd /path/to/superTrader && python t_io/validation/daily_review/check_optimization_effect.py --date $(date +%Y-%m-%d) >> doc/每日复盘/$(date +%Y-%m-%d)_复盘.md
