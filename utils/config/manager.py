# -*- coding: utf-8 -*-
"""
配置管理中心 - Config Manager

集中式参数管理系统，支持版本控制、快照、验证等功能。
"""

import json
import yaml
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import asdict

from core.dto import ConfigVersion


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_dir: str = "config"):
        """
        初始化配置管理器

        Args:
            config_dir: 配置文件所在目录
        """
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(exist_ok=True)

        # 当前加载的配置
        self._current_config: Dict[str, Any] = {}
        self._current_version: Optional[str] = None

        # 版本历史
        self._versions: Dict[str, ConfigVersion] = {}

    def load_version(self, version_name: str) -> Dict[str, Any]:
        """
        加载指定版本的配置

        Args:
            version_name: 版本名称 (如 "v2_swing2pt_20260825")

        Returns:
            配置字典

        Raises:
            FileNotFoundError: 如果版本不存在
        """
        version_file = self.config_dir / "versions" / f"{version_name}.yaml"

        if not version_file.exists():
            raise FileNotFoundError(f"配置版本不存在: {version_name}")

        with open(version_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}

        self._current_config = config
        self._current_version = version_name

        return config

    def get(self, path: str, default=None) -> Any:
        """
        获取配置参数值

        Args:
            path: 参数路径 (如 "signal.swing_bb_upper")
            default: 默认值

        Returns:
            参数值
        """
        keys = path.split('.')
        value = self._current_config

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default

        return value if value is not None else default

    def set(self, path: str, value: Any):
        """
        设置配置参数值

        Args:
            path: 参数路径
            value: 新值
        """
        keys = path.split('.')
        config = self._current_config

        for key in keys[:-1]:
            if key not in config:
                config[key] = {}
            config = config[key]

        config[keys[-1]] = value

    def validate(self) -> Dict[str, Any]:
        """
        验证当前配置的合法性

        Returns:
            验证结果 {"valid": bool, "errors": List[str]}
        """
        errors = []

        # 信号参数验证
        bb_upper = self.get("signal.swing_bb_upper")
        if bb_upper is not None and not (0 <= bb_upper <= 3):
            errors.append("signal.swing_bb_upper 应在 [0, 3] 范围内")

        bb_lower = self.get("signal.swing_bb_lower")
        if bb_lower is not None and not (0 <= bb_lower <= 3):
            errors.append("signal.swing_bb_lower 应在 [0, 3] 范围内")

        # RSI 参数验证
        rsi_sell = self.get("signal.swing_sell_rsi")
        if rsi_sell is not None and not (50 <= rsi_sell <= 100):
            errors.append("signal.swing_sell_rsi 应在 [50, 100] 范围内")

        rsi_buy = self.get("signal.swing_buy_rsi")
        if rsi_buy is not None and not (0 <= rsi_buy <= 50):
            errors.append("signal.swing_buy_rsi 应在 [0, 50] 范围内")

        # 更多验证规则...

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "timestamp": datetime.now().isoformat()
        }

    def save_snapshot(self, name: str) -> str:
        """
        保存当前配置快照

        Args:
            name: 快照名称 (如 "exp_20260826")

        Returns:
            版本名称
        """
        version_name = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        version_file = self.config_dir / "versions" / f"{version_name}.yaml"

        # 保存配置
        version_file.parent.mkdir(parents=True, exist_ok=True)
        with open(version_file, 'w', encoding='utf-8') as f:
            yaml.dump(self._current_config, f, default_flow_style=False)

        # 记录版本元数据
        config_hash = hashlib.md5(
            json.dumps(self._current_config, sort_keys=True).encode()
        ).hexdigest()

        version = ConfigVersion(
            name=name,
            version=version_name,
            created_at=datetime.now(),
            description=f"配置快照: {name}",
            config_hash=config_hash,
            config=self._current_config.copy()
        )

        self._versions[version_name] = version

        return version_name

    def diff_versions(self, v1: str, v2: str) -> Dict[str, Any]:
        """
        比较两个版本的差异

        Args:
            v1: 版本1名称
            v2: 版本2名称

        Returns:
            差异信息
        """
        config1 = self.load_version(v1)
        config2 = self.load_version(v2)

        diff = {
            "added": {},
            "removed": {},
            "modified": {}
        }

        # 比较
        all_keys = set(config1.keys()) | set(config2.keys())

        for key in all_keys:
            if key not in config1:
                diff["added"][key] = config2[key]
            elif key not in config2:
                diff["removed"][key] = config1[key]
            elif config1[key] != config2[key]:
                diff["modified"][key] = {
                    "before": config1[key],
                    "after": config2[key]
                }

        return diff

    def rollback(self, version: str):
        """
        快速回滚到某个版本

        Args:
            version: 目标版本名称
        """
        self.load_version(version)
        print(f"已回滚到版本: {version}")

    def list_versions(self) -> List[str]:
        """
        列出所有可用版本

        Returns:
            版本名称列表
        """
        versions_dir = self.config_dir / "versions"
        if not versions_dir.exists():
            return []

        versions = []
        for f in versions_dir.glob("*.yaml"):
            versions.append(f.stem)

        return sorted(versions)


# 全局单例
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器单例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


__all__ = ['ConfigManager', 'get_config_manager']
