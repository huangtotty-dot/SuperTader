#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配置管理系统测试脚本
"""

import sys
sys.path.insert(0, '.')

from core.config_manager import ConfigManager
from core.config_validator import ConfigValidator


def test_config_manager():
    """测试配置管理器"""
    print("=" * 60)
    print("Test 1: Config Manager Basic Functions")
    print("=" * 60)

    cfg = ConfigManager(config_dir="config")

    # 加载版本
    print("\n1. Loading version...")
    try:
        config = cfg.load_version("v2.0_current_20260825")
        print("[OK] Version loaded successfully, config items: %d" % len(config))
        print("  Current version: %s" % cfg.current_version)
        print("  Config hash: %s" % cfg.config_hash)
    except Exception as e:
        print("[FAILED] Version loading failed: %s" % str(e))
        return False

    # 获取参数
    print("\n2. Getting parameters...")
    bb_upper = cfg.get("signal.swing_bb_upper")
    bb_lower = cfg.get("signal.swing_bb_lower")
    print("[OK] signal.swing_bb_upper: %s" % str(bb_upper))
    print("[OK] signal.swing_bb_lower: %s" % str(bb_lower))

    # 设置参数
    print("\n3. Setting parameters...")
    cfg.set("signal.swing_bb_upper", 1.2)
    new_value = cfg.get("signal.swing_bb_upper")
    print("[OK] Updated signal.swing_bb_upper: %s" % str(new_value))

    # 验证配置
    print("\n4. Validating configuration...")
    validator = ConfigValidator()
    is_valid, errors = validator.validate(cfg.current_config)
    report = validator.report(is_valid, errors)
    print(report)

    if not is_valid:
        return False

    # 保存快照
    print("\n5. Saving snapshot...")
    snapshot_file = cfg.save_snapshot("test_snapshot", "Test snapshot")
    print("[OK] Snapshot saved: %s" % snapshot_file)

    # 列出所有版本
    print("\n6. Listing all versions...")
    versions = cfg.list_versions()
    print("[OK] Total versions: %d" % len(versions))
    for v in versions[:5]:  # 只显示前5个
        created = v["created_at"] if v["created_at"] else "Unknown"
        print("  - %s (%s)" % (v['name'], created))
        if v["description"]:
            print("    Description: %s" % v['description'])

    # 导出配置
    print("\n7. Exporting configuration...")
    export_file = cfg.export_config()
    print("[OK] Configuration exported: %s" % export_file)

    print("\n" + "=" * 60)
    print("[OK] Config Manager tests completed")
    print("=" * 60)

    return True


def test_config_diff():
    """测试配置版本对比"""
    print("\n" + "=" * 60)
    print("Test 2: Config Version Comparison")
    print("=" * 60)

    cfg = ConfigManager(config_dir="config")

    # 创建两个版本
    print("\n1. Loading base version...")
    cfg.load_version("v2.0_current_20260825")

    # 修改参数
    print("\n2. Modifying parameters and saving new version...")
    cfg.set("signal.swing_bb_upper", 1.5)
    cfg.set("signal.swing_bb_lower", -1.0)
    snapshot_file = cfg.save_snapshot("v2.1_exp", "Experimental version - expanded bands")
    print("[OK] New version saved")

    # 对比版本
    print("\n3. Comparing versions...")
    try:
        # 获取最新的v2.1快照文件
        import glob
        v21_files = glob.glob("config/versions/v2.1_exp_*.yaml")
        if not v21_files:
            print("[FAILED] v2.1_exp snapshot not found")
            return False

        v21_file = v21_files[-1]  # 最新的
        diff = cfg.diff_versions("v2.0_current_20260825", v21_file)
        print("[OK] Version comparison completed:")
        print("  Added: %d items" % len(diff['added']))
        print("  Removed: %d items" % len(diff['removed']))
        print("  Changed: %d items" % len(diff['changed']))

        if diff['changed']:
            print("\n  Changes details:")
            for key, change in diff['changed'].items():
                print("    %s: %s -> %s" % (key, change['before'], change['after']))
    except Exception as e:
        print("[FAILED] Version comparison failed: %s" % str(e))
        return False

    print("\n" + "=" * 60)
    print("[OK] Config comparison tests completed")
    print("=" * 60)

    return True


if __name__ == "__main__":
    success = test_config_manager()
    if success:
        success = test_config_diff()

    if success:
        print("\n[OK] All tests passed")
        sys.exit(0)
    else:
        print("\n[FAILED] Tests failed")
        sys.exit(1)
