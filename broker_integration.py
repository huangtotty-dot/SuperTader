# -*- coding: utf-8 -*-
"""
broker_integration.py - 与 t_gui 集成的账户管理 API

这个模块提供 pywebview 前端可调用的账户管理接口
"""

import sys
from pathlib import Path
from broker_manager import AccountManager, HoldingsMerger

BASE = Path(__file__).resolve().parent
HOLDINGS_FILE = BASE / "holdings.json"


class BrokerIntegrationAPI:
    """券商集成 API（供 pywebview 调用）"""

    def __init__(self):
        self.manager = AccountManager(Path.home() / ".supertrader")
        self.merger = HoldingsMerger(HOLDINGS_FILE)

    def get_accounts(self):
        """获取所有已配置的账户"""
        return self.manager.accounts

    def add_account(self, account_name: str, broker: str, auth_info: dict) -> dict:
        """添加新账户"""
        ok, msg = self.manager.add_account(account_name, broker, auth_info)
        return {"success": ok, "message": msg}

    def test_account_connection(self, account_name: str, broker: str, auth_info: dict) -> dict:
        """测试账户连接"""
        from broker_manager import TdxAdapter, DFCFAdapter

        if broker.startswith("通达信"):
            adapter = TdxAdapter(account_name)
        elif broker.startswith("东方财富_API"):
            adapter = DFCFAdapter(account_name, auth_info.get("token", ""))
        else:
            adapter = TdxAdapter(account_name)

        ok, msg = adapter.test_connection(auth_info)
        return {"success": ok, "message": msg}

    def sync_account(self, account_name: str) -> dict:
        """同步单个账户"""
        ok, info = self.manager.sync_account(account_name)
        if ok and info:
            return {
                "success": True,
                "account": account_name,
                "total_assets": info.total_assets,
                "cash": info.cash,
                "market_value": info.market_value,
                "positions_count": len(info.positions),
                "fetch_time": info.fetch_time
            }
        return {"success": False, "message": f"同步 {account_name} 失败"}

    def sync_all_accounts(self) -> dict:
        """同步所有账户并融合持仓"""
        accounts_info = self.manager.sync_all_accounts()

        if not accounts_info:
            return {
                "success": False,
                "message": "没有可用的账户或同步失败"
            }

        # 融合持仓
        merged = self.merger.merge(accounts_info)
        self.merger.update_holdings_json(merged)

        # 计算统计信息
        total_value = 0
        total_cost = 0
        total_positions = 0

        for code, data in merged.items():
            total_value += data.get("market_value", 0)
            total_cost += data.get("cost_total", 0)
            total_positions += 1

        return {
            "success": True,
            "message": "所有账户同步完成",
            "accounts_synced": len(accounts_info),
            "total_positions": total_positions,
            "total_market_value": total_value,
            "total_cost": total_cost,
            "accounts": [
                {
                    "name": name,
                    "broker": info.broker,
                    "total_assets": info.total_assets,
                    "cash": info.cash,
                    "market_value": info.market_value,
                    "positions_count": len(info.positions),
                    "fetch_time": info.fetch_time
                }
                for name, info in accounts_info.items()
            ]
        }

    def remove_account(self, account_name: str) -> dict:
        """移除账户"""
        if account_name in self.manager.accounts:
            del self.manager.accounts[account_name]
            self.manager._save_accounts()
            return {"success": True, "message": f"账户 {account_name} 已删除"}
        return {"success": False, "message": f"账户 {account_name} 不存在"}

    def get_merged_holdings(self) -> dict:
        """获取融合后的持仓"""
        if not HOLDINGS_FILE.exists():
            return {}

        import json
        with open(HOLDINGS_FILE, 'r', encoding='utf-8') as f:
            holdings = json.load(f)

        # 计算总资产
        total_value = 0
        total_cost = 0

        for code, data in holdings.items():
            market_value = data.get("market_value", 0)
            if not market_value and data.get("price"):
                market_value = data["price"] * data.get("qty", 0)
            total_value += market_value
            total_cost += data.get("qty", 0) * data.get("cost", 0)

        return {
            "holdings": holdings,
            "total_market_value": total_value,
            "total_cost": total_cost,
            "total_positions": len(holdings),
            "last_sync": max(
                (data.get("last_sync", "") for data in holdings.values()),
                default=""
            )
        }


# 全局实例（供 pywebview 使用）
_broker_api = None


def get_broker_api():
    """获取或创建全局 API 实例"""
    global _broker_api
    if _broker_api is None:
        _broker_api = BrokerIntegrationAPI()
    return _broker_api
