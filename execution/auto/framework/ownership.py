# -*- coding: utf-8 -*-
"""`Ownership` —— **一票一策略**归属判定与硬锁（owner 2026-10-12 拍板）。

## 语义（钉死）

多策略共享**同一个掘金模拟盘账户**。若两个策略同时持有同一只票，则「卖谁的、拒单回滚谁、
孤儿成交算谁的」都无从判定。故 owner 选择：

    同一 code 在任一时刻只属于一个策略；被别的策略持有 ⇒ **开仓直接拒绝**（硬锁）。

判据（**算出来的，不是维护出来的** —— 少一份状态就少一处漂移）：

| 情形 | 判定 | 结果 |
|---|---|---|
| 有策略账本（其 `positions.json` 的 open）持有该 code | `owner = 该策略` | 只有它自己能买 |
| 账户持有、但**没有任何策略账本认领**（旧镜像遗留 / 终端手工单） | `LEGACY` | **对所有策略锁定** |
| 账户无持仓、账本也无 | 无主 | 可买 |
| `auto_ownership.json` 里有 override | 以 override 为准 | 人工豁免通道 |

**为什么不用"归属表文件 + 成交时登记"**：那需要买卖两侧各挂一处写入，任何一处漏了/写失败就
永久漂移，而"哪只票属于谁"在**账本**里本来就有唯一真源（卖侧就是靠它决定卖什么）。
override 文件只留作人工豁免（把某只历史持仓**认领**给某策略，或强制锁定某只票）。

## 上线首日的已知后果（务必知悉）

现账户里的历史持仓（旧镜像引擎留下的）**没有任何策略认领** ⇒ 判 `LEGACY` ⇒
**那些 code 当天买不进**（避免把新旧仓位混成一笔）。三条出路：
① 接受（等它们卖掉/清掉后自然解锁）；② 在 `auto_ownership.json` 里把该 code override 成 `osc`；
③ 先清仓。每次拒绝都会落 `risk:strategy_lock_conflict` + 审计事件，不会静默。
"""
from __future__ import annotations

import json
import os

LEGACY = "legacy"          # 账户持有但无策略认领 ⇒ 对所有策略锁定


class Ownership(object):
    """归属判定器。`strategies` = 有 `id` 与 `owns(code)->bool` 的对象列表（宿主注入）。"""

    def __init__(self, strategies=(), override_path: str = None, log=print):
        self.strategies = list(strategies)
        self.override_path = override_path
        self.log = log
        self.overrides = {}        # {code: strategy_id|LEGACY}
        self.load_error = None
        if override_path:
            self._load_overrides()

    # ── override（人工豁免）──────────────────────────────────
    def _load_overrides(self) -> None:
        try:
            with open(self.override_path, encoding="utf-8") as f:
                raw = json.load(f)
            ov = raw.get("overrides") if isinstance(raw, dict) else None
            self.overrides = {str(k): str(v) for k, v in (ov or {}).items()}
        except FileNotFoundError:
            self.overrides = {}
        except Exception as e:
            self.overrides = {}
            self.load_error = "%s: %s" % (type(e).__name__, e)
            self.log("[OWN] ⚠️ override 读取失败（按无 override 处理）: %s" % e)

    def reload_overrides(self) -> None:
        if self.override_path:
            self._load_overrides()

    # ── 查询 ────────────────────────────────────────────────
    def owner_of(self, code: str):
        """该 code 的归属策略 id（`LEGACY` 表示无人认领但被账户持有）；无主 → None。

        ⚠️ 只看策略账本与 override；**不含**账户持仓判断（那要 `held_qty`，见 `can_open`/`snapshot`）。
        """
        code = str(code)
        if code in self.overrides:
            return self.overrides[code]
        for st in self.strategies:
            try:
                if st.owns(code):
                    return st.id
            except Exception as e:
                self.log("[OWN] ⚠️ %s.owns(%s) 异常（按不持有）: %s" % (getattr(st, "id", "?"), code, e))
        return None

    def can_open(self, code: str, strategy: str, held_qty: int = 0):
        """能否为 `strategy` 开仓 code。返回 (ok, reason)。

        `held_qty` = **账户实时持仓量**（调用方从 gateway.get_positions() 取）。
        """
        code = str(code)
        owner = self.owner_of(code)
        if owner is not None:
            if owner == strategy:
                return True, ""
            return False, "held_by:%s" % owner
        if int(held_qty or 0) > 0:
            return False, "unclaimed_account_position(holder=%s)" % int(held_qty)
        return True, ""

    def snapshot(self) -> dict:
        """{code: strategy|LEGACY}（诊断/日志用；只含**已认领**的，不含无主）。"""
        return {c: self.owner_of(c) for c in self._known_codes()}

    def _known_codes(self):
        codes = set(self.overrides)
        for st in self.strategies:
            try:
                codes.update(st.owned_codes())
            except Exception:
                pass
        return sorted(codes)


def default_path(root: str) -> str:
    return os.path.join(root, "t_io", "state", "auto_ownership.json")
