# -*- coding: utf-8 -*-
"""
配置管理中心 - ConfigManager

统一管理系统的所有参数配置，支持版本控制、快照、验证和回滚。
"""

import os
import json
import yaml
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import copy


class ConfigManager:
    """配置管理器 - 集中管理所有系统参数"""

    def __init__(self, config_dir: str = "config"):
        """
        初始化配置管理器

        Args:
            config_dir: 配置目录路径
        """
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(exist_ok=True)

        # 创建必要的子目录
        self.versions_dir = self.config_dir / "versions"
        self.versions_dir.mkdir(exist_ok=True)

        # 配置区段目录
        for subdir in ["strategies", "market", "system"]:
            (self.config_dir / subdir).mkdir(exist_ok=True)

        # 当前加载的配置
        self.current_config: Dict[str, Any] = {}
        self.current_version: Optional[str] = None
        self.config_hash: Optional[str] = None

    def _compute_hash(self, config: Dict[str, Any]) -> str:
        """计算配置的哈希值"""
        config_str = json.dumps(config, sort_keys=True, default=str)
        return hashlib.md5(config_str.encode()).hexdigest()

    def _merge_configs(self, *configs: Dict[str, Any]) -> Dict[str, Any]:
        """递归合并多个配置字典"""
        result = {}
        for config in configs:
            for key, value in config.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = self._merge_configs(result[key], value)
                else:
                    result[key] = value
        return result

    def _load_yaml(self, filepath: Path) -> Dict[str, Any]:
        """加载YAML文件"""
        if not filepath.exists():
            return {}

        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}

    def _save_yaml(self, filepath: Path, data: Dict[str, Any]):
        """保存YAML文件"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def load_version(self, version: str) -> Dict[str, Any]:
        """
        加载指定版本的参数

        Args:
            version: 版本名称或版本文件路径

        Returns:
            配置字典
        """
        # 如果是版本号，转换为文件路径
        if not version.endswith('.yaml'):
            version_file = self.versions_dir / f"{version}.yaml"
        else:
            version_file = Path(version)

        if not version_file.exists():
            raise FileNotFoundError(f"配置版本不存在: {version_file}")

        # 加载版本文件
        config = self._load_yaml(version_file)

        # 加载所有区段配置（覆盖版本中的设置）
        for section in ["strategies", "market", "system"]:
            section_dir = self.config_dir / section
            if section_dir.exists():
                for yaml_file in section_dir.glob("*.yaml"):
                    section_config = self._load_yaml(yaml_file)
                    section_name = yaml_file.stem
                    if section_name not in config:
                        config[section_name] = {}
                    config[section_name].update(section_config)

        self.current_config = copy.deepcopy(config)
        self.current_version = str(version_file)
        self.config_hash = self._compute_hash(config)

        return self.current_config

    def get(self, path: str, default: Any = None) -> Any:
        """
        获取参数值（支持点号路径）

        Example:
            cfg.get("signal.swing_bb_upper")  # 返回值
            cfg.get("market.index_levels.upper", 3500)  # 带默认值

        Args:
            path: 参数路径，用点号分隔
            default: 默认值

        Returns:
            参数值
        """
        keys = path.split('.')
        value = self.current_config

        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, path: str, value: Any) -> None:
        """
        设置参数值（支持点号路径）

        Example:
            cfg.set("signal.swing_bb_upper", 1.5)

        Args:
            path: 参数路径，用点号分隔
            value: 参数值
        """
        keys = path.split('.')
        target = self.current_config

        # 创建路径
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            elif not isinstance(target[key], dict):
                raise ValueError(f"路径 {'.'.join(keys[:-1])} 不是字典")
            target = target[key]

        # 设置值
        target[keys[-1]] = value

        # 更新哈希
        self.config_hash = self._compute_hash(self.current_config)

    def validate(self) -> Tuple[bool, List[str]]:
        """
        验证所有参数的合法性

        Returns:
            (是否有效, 错误列表)
        """
        errors = []

        # 验证必要的参数存在
        required_paths = [
            "signal.swing_bb_upper",
            "signal.swing_bb_lower",
            "market.index_levels",
            "system.data_fetch.interval",
        ]

        for path in required_paths:
            if self.get(path) is None:
                errors.append(f"缺少必要参数: {path}")

        # 验证参数类型和范围
        bb_upper = self.get("signal.swing_bb_upper")
        if bb_upper is not None and not isinstance(bb_upper, (int, float)):
            errors.append(f"signal.swing_bb_upper 必须是数字，但得到 {type(bb_upper)}")

        bb_lower = self.get("signal.swing_bb_lower")
        if bb_lower is not None and not isinstance(bb_lower, (int, float)):
            errors.append(f"signal.swing_bb_lower 必须是数字，但得到 {type(bb_lower)}")

        if bb_upper is not None and bb_lower is not None:
            if bb_lower >= bb_upper:
                errors.append(f"signal.swing_bb_lower ({bb_lower}) 必须小于 signal.swing_bb_upper ({bb_upper})")

        return len(errors) == 0, errors

    def save_snapshot(self, name: str, description: str = "") -> str:
        """
        保存当前配置快照

        Args:
            name: 快照名称
            description: 快照描述

        Returns:
            快照文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_name = f"{name}_{timestamp}"
        snapshot_file = self.versions_dir / f"{snapshot_name}.yaml"

        # 保存快照元数据
        metadata = {
            "_metadata": {
                "name": name,
                "snapshot_name": snapshot_name,
                "created_at": datetime.now().isoformat(),
                "description": description,
                "config_hash": self.config_hash,
            },
            **self.current_config
        }

        self._save_yaml(snapshot_file, metadata)
        return str(snapshot_file)

    def list_versions(self) -> List[Dict[str, Any]]:
        """
        列出所有版本

        Returns:
            版本信息列表
        """
        versions = []
        for version_file in sorted(self.versions_dir.glob("*.yaml")):
            config = self._load_yaml(version_file)
            metadata = config.get("_metadata", {})

            versions.append({
                "name": version_file.stem,
                "file": str(version_file),
                "created_at": metadata.get("created_at"),
                "description": metadata.get("description", ""),
                "config_hash": metadata.get("config_hash"),
            })

        return versions

    def diff_versions(self, v1: str, v2: str) -> Dict[str, Any]:
        """
        比较两个版本的差异

        Args:
            v1: 版本1
            v2: 版本2

        Returns:
            差异字典
        """
        config1 = self.load_version(v1)
        config2 = self.load_version(v2)

        diff = {
            "added": {},
            "removed": {},
            "changed": {},
        }

        # 找新增和修改的键
        for key, value in config2.items():
            if key.startswith("_"):
                continue

            if key not in config1:
                diff["added"][key] = value
            elif config1[key] != value:
                diff["changed"][key] = {
                    "before": config1[key],
                    "after": value,
                }

        # 找删除的键
        for key, value in config1.items():
            if key.startswith("_"):
                continue
            if key not in config2:
                diff["removed"][key] = value

        return diff

    def rollback(self, version: str) -> None:
        """
        快速回滚到某个版本

        Args:
            version: 目标版本
        """
        self.load_version(version)

    def export_config(self, filepath: str = None) -> str:
        """
        导出当前配置

        Args:
            filepath: 导出文件路径，不指定则使用默认路径

        Returns:
            导出文件路径
        """
        if filepath is None:
            filepath = f"config_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"

        filepath = Path(filepath)
        self._save_yaml(filepath, self.current_config)
        return str(filepath)

    def import_config(self, filepath: str) -> None:
        """
        导入配置

        Args:
            filepath: 配置文件路径
        """
        config = self._load_yaml(Path(filepath))
        self.current_config = copy.deepcopy(config)
        self.config_hash = self._compute_hash(config)
