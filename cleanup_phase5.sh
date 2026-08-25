#!/bin/bash
# Phase 5: 配置清理

echo "[CLEANUP] Phase 5: Configuration Cleanup"
echo "========================================="

echo "Current config/versions files:"
ls -la config/versions/

echo "Removing test/experiment snapshots..."
rm -f config/versions/test_snapshot_*.yaml
rm -f config/versions/v2.1_exp_*.yaml

echo "Creating historical archive..."
mkdir -p config/archive/historical

echo "[OK] Phase 5 configuration cleanup completed"
ls -la config/versions/
du -sh config/ 2>/dev/null
