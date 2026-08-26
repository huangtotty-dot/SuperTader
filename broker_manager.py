# -*- coding: utf-8 -*-
"""
broker_manager.py - 多券商账户集成管理器

支持：
- 东方财富（通达信 + tushare API）
- 国盛证券（通达信 + 自有 API）
- 东莞证券（通达信 + 自有 API）

架构：
1. 基础适配器（BrokerAdapter）
2. 具体实现（TdxAdapter, DFCFAdapter, GuoshenAdapter, etc.）
3. 账户管理器（AccountManager）
4. 数据融合（HoldingsMerger）
"""

import json
import os
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import hashlib
from abc import ABC, abstractmethod

# 加密存储认证信息
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


@dataclass
class Position:
    """持仓信息标准格式"""
    code: str
    name: str
    qty: int
    cost: float
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class AccountInfo:
    """账户信息标准格式"""
    account_name: str
    broker: str  # 券商名称
    total_assets: float
    cash: float
    market_value: float
    positions: List[Position]
    fetch_time: str = ""


class BrokerAdapter(ABC):
    """券商适配器基类"""

    def __init__(self, account_name: str):
        self.account_name = account_name
        self.last_fetch = None

    @abstractmethod
    def test_connection(self, auth_info: dict) -> Tuple[bool, str]:
        """测试连接是否可用"""
        pass

    @abstractmethod
    def get_account_info(self, auth_info: dict) -> AccountInfo:
        """获取账户信息（资金、持仓等）"""
        pass

    @abstractmethod
    def get_holdings(self, auth_info: dict) -> List[Position]:
        """仅获取持仓列表"""
        pass


class TdxAdapter(BrokerAdapter):
    """通达信适配器（支持所有券商）"""

    def __init__(self, account_name: str, host: str = "127.0.0.1", port: int = 7709):
        super().__init__(account_name)
        self.host = host
        self.port = port
        self.client = None

    def _connect(self) -> bool:
        """连接到通达信"""
        try:
            from pytdx.client import TdxClient
            self.client = TdxClient()
            self.client.connect(self.host, self.port)
            return True
        except Exception as e:
            print(f"[TDX] 连接失败: {e}")
            return False

    def test_connection(self, auth_info: dict) -> Tuple[bool, str]:
        """测试连接"""
        if self._connect():
            return True, "通达信连接成功"
        return False, "无法连接到通达信（请确保客户端已启动）"

    def get_account_info(self, auth_info: dict) -> AccountInfo:
        """获取账户信息"""
        if not self.client:
            self._connect()

        try:
            # 获取资金信息
            fund = self.client.get_fund()
            total_assets = fund.get('total_asset', 0)
            cash = fund.get('cash', 0)

            # 获取持仓
            positions = self.get_holdings(auth_info)
            market_value = sum(p.market_value for p in positions)

            return AccountInfo(
                account_name=self.account_name,
                broker="通达信",
                total_assets=total_assets,
                cash=cash,
                market_value=market_value,
                positions=positions,
                fetch_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        except Exception as e:
            print(f"[TDX] 获取账户信息失败: {e}")
            return AccountInfo(
                account_name=self.account_name,
                broker="通达信",
                total_assets=0,
                cash=0,
                market_value=0,
                positions=[],
                fetch_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )

    def get_holdings(self, auth_info: dict) -> List[Position]:
        """获取持仓"""
        if not self.client:
            self._connect()

        positions = []
        try:
            raw_positions = self.client.get_position()
            for item in raw_positions:
                pos = Position(
                    code=item.get('stock_code', ''),
                    name=item.get('stock_name', ''),
                    qty=item.get('volume', 0),
                    cost=item.get('price', 0),
                    market_value=item.get('market_value', 0),
                    pnl=item.get('profit', 0),
                    pnl_pct=item.get('profit_pct', 0)
                )
                positions.append(pos)
        except Exception as e:
            print(f"[TDX] 获取持仓失败: {e}")

        return positions


class DFCFAdapter(BrokerAdapter):
    """东方财富适配器（使用 tushare API）"""

    def __init__(self, account_name: str, api_token: str = ""):
        super().__init__(account_name)
        self.api_token = api_token
        self.ts = None

    def _init_ts(self):
        """初始化 tushare"""
        try:
            import tushare as ts
            self.ts = ts
            if self.api_token:
                ts.set_token(self.api_token)
        except ImportError:
            print("[DFCF] tushare 未安装")
            return False
        return True

    def test_connection(self, auth_info: dict) -> Tuple[bool, str]:
        """测试连接"""
        if not self._init_ts():
            return False, "tushare 未安装"

        # 东方财富 API 通常不需要认证就能获取行情
        # 但完整功能需要 token
        if not self.api_token and "token" not in auth_info:
            return False, "需要提供 tushare API token（在 tushare.pro 注册获取）"

        return True, "东方财富 API 连接成功"

    def get_account_info(self, auth_info: dict) -> AccountInfo:
        """获取账户信息"""
        # 注：东方财富 tushare 主要提供行情数据，不提供持仓接口
        # 实际持仓需要通过通达信或在网页登录后爬取
        print("[DFCF] tushare API 不提供持仓数据，请使用通达信接入")
        return AccountInfo(
            account_name=self.account_name,
            broker="东方财富",
            total_assets=0,
            cash=0,
            market_value=0,
            positions=[],
            fetch_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def get_holdings(self, auth_info: dict) -> List[Position]:
        """东方财富的持仓需要其他方式获取"""
        print("[DFCF] 持仓数据需要通达信或网页登录爬取")
        return []


class GuoshenAdapter(BrokerAdapter):
    """国盛证券适配器"""

    def __init__(self, account_name: str):
        super().__init__(account_name)
        # 国盛证券 API 需要单独的认证和 SDK
        # 这里是占位实现，实际需要国盛提供的 SDK

    def test_connection(self, auth_info: dict) -> Tuple[bool, str]:
        """测试连接"""
        # TODO: 实现国盛证券 API 连接
        return False, "国盛证券 API 集成待实现（需要国盛提供的 SDK）"

    def get_account_info(self, auth_info: dict) -> AccountInfo:
        return AccountInfo(
            account_name=self.account_name,
            broker="国盛证券",
            total_assets=0,
            cash=0,
            market_value=0,
            positions=[],
            fetch_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def get_holdings(self, auth_info: dict) -> List[Position]:
        return []


class DongguanAdapter(BrokerAdapter):
    """东莞证券适配器"""

    def __init__(self, account_name: str):
        super().__init__(account_name)
        # 东莞证券 API 需要单独的认证和 SDK

    def test_connection(self, auth_info: dict) -> Tuple[bool, str]:
        """测试连接"""
        # TODO: 实现东莞证券 API 连接
        return False, "东莞证券 API 集成待实现（需要东莞提供的 SDK）"

    def get_account_info(self, auth_info: dict) -> AccountInfo:
        return AccountInfo(
            account_name=self.account_name,
            broker="东莞证券",
            total_assets=0,
            cash=0,
            market_value=0,
            positions=[],
            fetch_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def get_holdings(self, auth_info: dict) -> List[Position]:
        return []


class AccountManager:
    """账户管理器"""

    def __init__(self, data_dir: Path = Path.home() / ".supertrader"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_file = self.data_dir / "accounts.json"
        self.accounts = self._load_accounts()

    def _load_accounts(self) -> Dict[str, dict]:
        """加载账户配置"""
        if self.accounts_file.exists():
            with open(self.accounts_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _save_accounts(self):
        """保存账户配置"""
        with open(self.accounts_file, 'w', encoding='utf-8') as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)

    def add_account(self, account_name: str, broker: str, auth_info: dict) -> Tuple[bool, str]:
        """添加账户"""
        # 获取相应的适配器
        adapter = self._get_adapter(account_name, broker)

        # 测试连接
        ok, msg = adapter.test_connection(auth_info)
        if not ok:
            return False, f"连接失败: {msg}"

        # 存储账户信息（加密认证数据）
        self.accounts[account_name] = {
            "broker": broker,
            "auth": self._encrypt_auth(auth_info),
            "created_at": datetime.now().isoformat(),
            "last_sync": None
        }
        self._save_accounts()

        return True, f"账户 {account_name} 已添加"

    def sync_account(self, account_name: str) -> Tuple[bool, Optional[AccountInfo]]:
        """同步单个账户"""
        if account_name not in self.accounts:
            return False, None

        account_cfg = self.accounts[account_name]
        broker = account_cfg['broker']
        auth_info = self._decrypt_auth(account_cfg['auth'])

        adapter = self._get_adapter(account_name, broker)
        try:
            info = adapter.get_account_info(auth_info)
            self.accounts[account_name]['last_sync'] = datetime.now().isoformat()
            self._save_accounts()
            return True, info
        except Exception as e:
            return False, None

    def sync_all_accounts(self) -> Dict[str, AccountInfo]:
        """同步所有账户"""
        results = {}
        for account_name in self.accounts:
            ok, info = self.sync_account(account_name)
            if ok and info:
                results[account_name] = info
        return results

    def _get_adapter(self, account_name: str, broker: str) -> BrokerAdapter:
        """获取适配器"""
        if broker == "通达信" or broker == "东方财富_TDX":
            return TdxAdapter(account_name)
        elif broker == "东方财富":
            return DFCFAdapter(account_name)
        elif broker == "国盛证券":
            return GuoshenAdapter(account_name)
        elif broker == "东莞证券":
            return DongguanAdapter(account_name)
        else:
            # 默认使用通达信
            return TdxAdapter(account_name)

    def _encrypt_auth(self, auth_info: dict) -> str:
        """加密认证信息"""
        if not HAS_CRYPTO:
            # 未安装 cryptography，简单 base64 编码（不安全，仅示例）
            import base64
            return base64.b64encode(json.dumps(auth_info).encode()).decode()

        # 生成或读取加密密钥
        key_file = self.data_dir / ".key"
        if key_file.exists():
            with open(key_file, 'rb') as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(key_file, 'wb') as f:
                f.write(key)
            os.chmod(key_file, 0o600)  # 仅所有者可读

        cipher = Fernet(key)
        encrypted = cipher.encrypt(json.dumps(auth_info).encode())
        return encrypted.decode()

    def _decrypt_auth(self, encrypted: str) -> dict:
        """解密认证信息"""
        if not HAS_CRYPTO:
            import base64
            return json.loads(base64.b64decode(encrypted).decode())

        key_file = self.data_dir / ".key"
        with open(key_file, 'rb') as f:
            key = f.read()

        cipher = Fernet(key)
        decrypted = cipher.decrypt(encrypted.encode())
        return json.loads(decrypted)


class HoldingsMerger:
    """持仓融合器"""

    def __init__(self, holdings_file: Path):
        self.holdings_file = holdings_file

    def merge(self, accounts_info: Dict[str, AccountInfo]) -> dict:
        """融合所有账户的持仓"""
        merged = {}

        for account_name, info in accounts_info.items():
            for position in info.positions:
                code = position.code
                if code not in merged:
                    merged[code] = {
                        "name": position.name,
                        "qty": 0,
                        "cost_total": 0,
                        "market_value": 0,
                        "accounts": []
                    }

                merged[code]["qty"] += position.qty
                merged[code]["cost_total"] += position.qty * position.cost
                merged[code]["market_value"] += position.market_value
                merged[code]["accounts"].append({
                    "name": account_name,
                    "qty": position.qty,
                    "cost": position.cost,
                    "broker": info.broker
                })

        # 计算加权成本
        for code, data in merged.items():
            if data["qty"] > 0:
                data["cost"] = data["cost_total"] / data["qty"]
            else:
                data["cost"] = 0

        return merged

    def update_holdings_json(self, merged_holdings: dict, preserve_fields: bool = True):
        """更新 holdings.json"""
        if preserve_fields and self.holdings_file.exists():
            with open(self.holdings_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        else:
            existing = {}

        # 保留原有字段，更新持仓数据
        for code, data in merged_holdings.items():
            if code in existing:
                # 保留原有的 type, t_qty 等字段
                existing[code].update({
                    "qty": data["qty"],
                    "cost": data["cost"],
                    "accounts": data["accounts"],
                    "last_sync": datetime.now().isoformat()
                })
            else:
                existing[code] = {
                    "name": data["name"],
                    "qty": data["qty"],
                    "cost": data["cost"],
                    "base": data["qty"],
                    "t_qty": 0,
                    "type": "etf" if code.startswith(('51', '58')) else "stock",
                    "accounts": data["accounts"],
                    "last_sync": datetime.now().isoformat()
                }

        with open(self.holdings_file, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)


# 使用示例
if __name__ == "__main__":
    # 初始化管理器
    manager = AccountManager()

    # 添加账户（第一次使用）
    accounts_to_add = [
        ("账户A_东方财富", "通达信", {}),  # 通达信无需额外认证
        ("账户B_国盛证券", "通达信", {}),
        ("账户C_东莞证券", "通达信", {}),
    ]

    for name, broker, auth in accounts_to_add:
        ok, msg = manager.add_account(name, broker, auth)
        print(f"{msg}")

    # 同步所有账户
    print("\n同步账户...")
    accounts_info = manager.sync_all_accounts()

    # 融合持仓
    merger = HoldingsMerger(Path("holdings.json"))
    merged = merger.merge(accounts_info)
    merger.update_holdings_json(merged)

    print("✅ 账户同步完成")
    print(f"合并持仓: {len(merged)} 只股票")
    for code, data in merged.items():
        print(f"  {code}: {data['qty']} 股 @ {data['cost']:.2f}")
