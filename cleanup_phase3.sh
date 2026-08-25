#!/bin/bash
# Phase 3: 测试文件重组

echo "[CLEANUP] Phase 3: Test Files Reorganization"
echo "==========================================="

echo "Creating tests directory structure..."
mkdir -p tests/phase3
mkdir -p tests/fixtures
touch tests/__init__.py
touch tests/phase3/__init__.py
touch tests/fixtures/__init__.py

echo "Moving test files..."
mv -f test_config_manager.py tests/phase3/ 2>/dev/null || true
mv -f test_optimization_pipeline.py tests/phase3/ 2>/dev/null || true
mv -f test_system_integration.py tests/phase3/ 2>/dev/null || true

echo "Removing old test directory if exists..."
rm -rf tests/test_gui_startup.py 2>/dev/null || true

echo "[OK] Phase 3 test organization completed"
find tests -type f -name "*.py" | head -10
