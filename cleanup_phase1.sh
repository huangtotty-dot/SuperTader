#!/bin/bash
# Phase 1: 文档清理

echo "[CLEANUP] Phase 1: Document Cleanup"
echo "===================================="

# 备份当前状态
git branch cleanup-backup-$(date +%s) 2>/dev/null || true

# 删除根目录过程文档
echo "Removing root level process documents..."
rm -f PHASE23_EXECUTION_PLAN.md
rm -f PHASE2_MERGE_PLAN.md
rm -f PHASE2_REGIME_REASSESSMENT.md
rm -f PHASE2_REPETITION_ANALYSIS.md
rm -f PHASE2_VALIDATION_CLEANUP_DECISIONS.md
rm -f PHASE1_DETAILED_DECISIONS.md
rm -f SYSTEM_OPTIMIZATION_PLAN.md
rm -f PHASE_PROGRESS_REPORT.md
rm -f QUICK_REFERENCE_GUIDE.md
rm -f SCHEME_A_EXECUTION_REPORT.txt
rm -f COMPLETE_IMPLEMENTATION_SUMMARY.md
rm -f OPTIMIZATION_QUICK_START.md
rm -f LAUNCH_OPTUNA.md

echo "Removing doc/archive..."
rm -rf doc/archive

echo "Cleaning doc/guides (keeping HUNTER_INTEGRATION)..."
rm -f doc/guides/INTRADAY_SURGE_DEFENSE_GUIDE.md
rm -f doc/guides/K_LINE_BOX_DISPLAY_GUIDE.md
rm -f doc/guides/UI_INTEGRATION_GUIDE.md
rm -f doc/guides/SCHEME_A_DAILY_REVIEW_GUIDE.md
rm -f doc/guides/PRECISE_ENTRY_GUIDE.md

echo "Cleaning .claude working summaries..."
rm -f .claude/WORK_SUMMARY_*.md
rm -f ".claude/实时可操作方案总结.md"
rm -f ".claude/建仓逻辑系统诊断_*.md"
rm -f ".claude/盘中实时风险门控系统设计.md"

echo "[OK] Phase 1 cleanup completed"
ls -la | head -20
du -sh . 2>/dev/null | tail -1
