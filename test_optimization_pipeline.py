#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
优化管线系统测试脚本
"""

import sys
sys.path.insert(0, '.')

from core.config_manager import ConfigManager
from core.optimization_pipeline import OptimizationPipeline


def test_optimization_pipeline():
    """测试优化管线"""
    print("=" * 70)
    print("Test: Optimization Pipeline - Full Validation Workflow")
    print("=" * 70)

    # 加载配置
    print("\n[SETUP] Loading configuration...")
    cfg = ConfigManager(config_dir="config")
    config = cfg.load_version("v2.0_current_20260825")
    print("[OK] Configuration loaded")

    # 创建优化管线
    print("\n[SETUP] Initializing optimization pipeline...")
    pipeline = OptimizationPipeline(output_dir="t_io/optimization")
    print("[OK] Pipeline initialized")

    # 运行完整管线
    print("\n" + "=" * 70)
    print("RUNNING FULL OPTIMIZATION PIPELINE")
    print("=" * 70)

    final_report = pipeline.run_full_pipeline(config)

    # 打印结果摘要
    print("\n" + "=" * 70)
    print("PIPELINE SUMMARY")
    print("=" * 70)

    summary = pipeline.get_summary()
    print("\nPhases executed: %d" % summary['total_phases'])
    print("Final status: %s" % summary['final_status'])
    print("Total duration: %.2f seconds" % summary['total_duration_seconds'])
    print("All passed: %s" % str(summary['all_passed']))

    print("\nPhase details:")
    for report_info in summary['reports']:
        print("  - %s: %s (passed: %d, failed: %d, warnings: %d)" % (
            report_info['phase'],
            report_info['status'],
            report_info['checks_passed'],
            report_info['checks_failed'],
            report_info['warnings']
        ))

    # 保存报告
    print("\n[STEP] Saving reports...")
    for report in pipeline.reports:
        filepath = pipeline.save_report(report)
        print("[OK] Report saved: %s" % filepath)

    # 最终报告
    print("\n" + "=" * 70)
    print("FINAL VALIDATION REPORT")
    print("=" * 70)

    final_report = pipeline.reports[-1]
    print("\nPhase: %s" % final_report.phase)
    print("Status: %s" % final_report.status)
    print("Recommendation: %s" % final_report.recommendation)

    print("\nChecks passed: %d" % len(final_report.checks_passed))
    for check in final_report.checks_passed:
        print("  [OK] %s" % check)

    if final_report.checks_failed:
        print("\nChecks failed: %d" % len(final_report.checks_failed))
        for check in final_report.checks_failed:
            print("  [FAILED] %s" % check)

    if final_report.warnings:
        print("\nWarnings: %d" % len(final_report.warnings))
        for warning in final_report.warnings:
            print("  [WARNING] %s" % warning)

    print("\n" + "=" * 70)
    if summary['all_passed']:
        print("[OK] All optimization phases PASSED")
        print("Configuration is ready for production deployment")
    else:
        print("[INFO] Pipeline completed with some issues")
        print("Address the failed checks before production deployment")
    print("=" * 70)

    return summary['all_passed']


if __name__ == "__main__":
    success = test_optimization_pipeline()
    sys.exit(0 if success else 1)
