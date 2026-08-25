# -*- coding: utf-8 -*-
"""
配置验证器 - ConfigValidator

验证配置的完整性、合法性和一致性。
"""

from typing import Dict, List, Tuple, Any, Callable, Optional
from dataclasses import dataclass
from enum import Enum


class ValidationType(Enum):
    """验证类型"""
    REQUIRED = "REQUIRED"           # 必填参数
    TYPE = "TYPE"                   # 类型检查
    RANGE = "RANGE"                 # 范围检查
    ENUM = "ENUM"                   # 枚举值检查
    CUSTOM = "CUSTOM"               # 自定义验证


@dataclass
class ValidationRule:
    """验证规则"""
    path: str                        # 参数路径
    rule_type: ValidationType       # 验证类型
    description: str                # 描述
    params: Dict[str, Any]          # 规则参数


@dataclass
class ValidationError:
    """验证错误"""
    path: str
    rule_type: ValidationType
    message: str
    severity: str = "ERROR"  # ERROR / WARNING


class ConfigValidator:
    """配置验证器"""

    def __init__(self):
        self.rules: List[ValidationRule] = []
        self._setup_default_rules()

    def _setup_default_rules(self):
        """设置默认验证规则"""
        # 信号参数验证
        self.add_rule(ValidationRule(
            path="signal.swing_bb_upper",
            rule_type=ValidationType.REQUIRED,
            description="日内两点上轨必须存在",
            params={}
        ))

        self.add_rule(ValidationRule(
            path="signal.swing_bb_upper",
            rule_type=ValidationType.TYPE,
            description="日内两点上轨必须是数字",
            params={"expected_type": (int, float)}
        ))

        self.add_rule(ValidationRule(
            path="signal.swing_bb_upper",
            rule_type=ValidationType.RANGE,
            description="日内两点上轨应该在0-10之间",
            params={"min": 0, "max": 10}
        ))

        self.add_rule(ValidationRule(
            path="signal.swing_bb_lower",
            rule_type=ValidationType.REQUIRED,
            description="日内两点下轨必须存在",
            params={}
        ))

        self.add_rule(ValidationRule(
            path="signal.swing_bb_lower",
            rule_type=ValidationType.TYPE,
            description="日内两点下轨必须是数字",
            params={"expected_type": (int, float)}
        ))

        self.add_rule(ValidationRule(
            path="signal.swing_bb_lower",
            rule_type=ValidationType.RANGE,
            description="日内两点下轨应该在-10-0之间",
            params={"min": -10, "max": 0}
        ))

        # 市场参数验证
        self.add_rule(ValidationRule(
            path="market.index_levels",
            rule_type=ValidationType.REQUIRED,
            description="大盘阈值必须存在",
            params={}
        ))

        # 系统参数验证
        self.add_rule(ValidationRule(
            path="system.data_fetch.interval",
            rule_type=ValidationType.REQUIRED,
            description="数据获取间隔必须存在",
            params={}
        ))

        self.add_rule(ValidationRule(
            path="system.data_fetch.interval",
            rule_type=ValidationType.TYPE,
            description="数据获取间隔必须是数字",
            params={"expected_type": (int, float)}
        ))

        self.add_rule(ValidationRule(
            path="system.data_fetch.interval",
            rule_type=ValidationType.RANGE,
            description="数据获取间隔应该大于0",
            params={"min": 0, "min_exclusive": True}
        ))

    def add_rule(self, rule: ValidationRule) -> None:
        """添加验证规则"""
        self.rules.append(rule)

    def validate(self, config: Dict[str, Any]) -> Tuple[bool, List[ValidationError]]:
        """
        验证配置

        Args:
            config: 配置字典

        Returns:
            (是否有效, 错误列表)
        """
        errors = []

        for rule in self.rules:
            rule_errors = self._validate_rule(config, rule)
            errors.extend(rule_errors)

        return len(errors) == 0, errors

    def _get_value(self, config: Dict[str, Any], path: str) -> Tuple[bool, Any]:
        """从配置中获取值"""
        keys = path.split('.')
        value = config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return False, None

        return True, value

    def _validate_rule(self, config: Dict[str, Any], rule: ValidationRule) -> List[ValidationError]:
        """验证单个规则"""
        errors = []
        found, value = self._get_value(config, rule.path)

        if rule.rule_type == ValidationType.REQUIRED:
            if not found or value is None:
                errors.append(ValidationError(
                    path=rule.path,
                    rule_type=rule.rule_type,
                    message=rule.description,
                ))

        elif rule.rule_type == ValidationType.TYPE:
            if found and value is not None:
                expected_type = rule.params.get("expected_type")
                if not isinstance(value, expected_type):
                    errors.append(ValidationError(
                        path=rule.path,
                        rule_type=rule.rule_type,
                        message=f"{rule.description}，但得到 {type(value).__name__}",
                    ))

        elif rule.rule_type == ValidationType.RANGE:
            if found and value is not None:
                min_val = rule.params.get("min")
                max_val = rule.params.get("max")
                min_exclusive = rule.params.get("min_exclusive", False)
                max_exclusive = rule.params.get("max_exclusive", False)

                if min_val is not None:
                    if min_exclusive and value <= min_val:
                        errors.append(ValidationError(
                            path=rule.path,
                            rule_type=rule.rule_type,
                            message=f"{rule.description}，但得到 {value}（需要 > {min_val}）",
                        ))
                    elif not min_exclusive and value < min_val:
                        errors.append(ValidationError(
                            path=rule.path,
                            rule_type=rule.rule_type,
                            message=f"{rule.description}，但得到 {value}（需要 >= {min_val}）",
                        ))

                if max_val is not None:
                    if max_exclusive and value >= max_val:
                        errors.append(ValidationError(
                            path=rule.path,
                            rule_type=rule.rule_type,
                            message=f"{rule.description}，但得到 {value}（需要 < {max_val}）",
                        ))
                    elif not max_exclusive and value > max_val:
                        errors.append(ValidationError(
                            path=rule.path,
                            rule_type=rule.rule_type,
                            message=f"{rule.description}，但得到 {value}（需要 <= {max_val}）",
                        ))

        elif rule.rule_type == ValidationType.ENUM:
            if found and value is not None:
                allowed_values = rule.params.get("allowed_values", [])
                if value not in allowed_values:
                    errors.append(ValidationError(
                        path=rule.path,
                        rule_type=rule.rule_type,
                        message=f"{rule.description}，但得到 {value}（允许值: {allowed_values}）",
                    ))

        return errors

    def report(self, is_valid: bool, errors: List[ValidationError]) -> str:
        """生成验证报告"""
        if is_valid:
            return "[OK] Configuration validation passed"

        report_lines = ["[FAILED] Configuration validation failed:"]
        for error in errors:
            report_lines.append(f"  [{error.severity}] {error.path}: {error.message}")

        return "\n".join(report_lines)
