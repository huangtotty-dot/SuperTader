#!/bin/bash
# Phase 2: 诊断工具清理

echo "[CLEANUP] Phase 2: Diagnostic Tools Removal"
echo "==========================================="

echo "Removing diagnostic scripts from root..."
rm -f deep_water_low_buy.py
rm -f box_breakout_validation.py
rm -f check_fields.py
rm -f check_snapshot.py
rm -f verify_scheme_a_fixes.py
rm -f auction_analyzer.py
rm -f demo_surge_defense.py

echo "Removing diagnostics directory..."
rm -rf diagnostics/

echo "Removing archive_old directory..."
rm -rf archive_old/

echo "Removing clean_gui_processes.bat..."
rm -f clean_gui_processes.bat

echo "Removing root level process/analysis files..."
rm -f compare_analysis.py
rm -f deep_analysis_588170.py
rm -f pre_deploy_checklist.py
rm -f plan.md

echo "[OK] Phase 2 cleanup completed"
ls -la | head -15
du -sh . 2>/dev/null | tail -1
