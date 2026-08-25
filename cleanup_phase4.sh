#!/bin/bash
# Phase 4: 数据清理

echo "[CLEANUP] Phase 4: Data Cleanup"
echo "=============================="

echo "Cleaning t_io directory..."

# 删除历史市场数据（保留最新2天）
echo "  Removing old breadth data..."
find t_io/index_regime -name "breadth_*.json" -mtime +2 -delete 2>/dev/null || true

# 删除历史追踪日志
echo "  Removing old traces..."
find t_io/traces -name "*.jsonl" -mtime +2 -delete 2>/dev/null || true

# 删除预开盘历史数据（保留当日）
echo "  Removing old preopen data..."
find t_io/preopen -type f -mtime +0 -delete 2>/dev/null || true

# 删除诊断输出
echo "  Removing diagnostics output..."
rm -rf t_io/diagnostics/ 2>/dev/null || true
rm -f t_io/*.log 2>/dev/null || true

# 删除审查报告
echo "  Removing review reports..."
rm -rf t_io/reviews/ 2>/dev/null || true

# 清理Python缓存
echo "  Removing Python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

# 清理缓存目录
echo "  Removing cache directories..."
rm -rf cache/ 2>/dev/null || true

echo "[OK] Phase 4 data cleanup completed"
du -sh . 2>/dev/null | tail -1
echo "t_io structure:"
ls -la t_io/
