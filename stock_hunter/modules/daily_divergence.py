# -*- coding: utf-8 -*-
"""日线 MACD 背离扫描（2026-09-19 owner 要求）。

## 用途
选股猎手打分池（watchlist_jiuyan.json 中「韭研概念」非空的 ~974 只）里，
出现**日线 MACD 底背离**时推飞书。只推底背离 —— 顶背离经 180 天验证无区分度
（日线复合顶背离 count≥2 命中率 53.4% < 无背离基线 57.8%）。

## 复用的根项目设施（勿重写）
  - `analysis.divergence.detect_daily_divergence` —— MACD DIF 峰谷背离检测（含新鲜度）
  - `core.market_data.get_provider().daily(code, days)` —— gm 主源 / 腾讯兜底

## 为什么不在这里实现检测逻辑
背离口径必须**单一源**。根项目 `analysis/divergence.py` 同时服务建仓扫描（position_builder）
与 GUI 建仓表；在猎手里再写一份必然产生口径漂移。

## 性能与容错
974 只 × `daily()` 的 gm 路径**不是 cache-first**（2026-08-31 移除）⇒ 串行会显著拖慢报告。
故：并发 + `days` 收窄 + **总时长预算**；超预算或单只失败一律跳过，
**绝不阻断报告主流程**（与 `send_error_alert` 同级容错）。

⚠️ 验证状态：日线**单次** MACD 背离本身**尚未经本项目验证**。
本模块只负责"检测到就提醒"，不构成买卖信号；文案必须保留此声明。
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_DAYS = 120          # 只需 MACD(26) 预热 + 峰谷，不必 provider 默认的 800 根
# 并发/预算实测（974 只打分池，腾讯日线路径，2026-09-19）：
#   workers=6  → 101s；workers=12 → 45s。取 12 留出余量；超预算即返回部分结果。
DEFAULT_WORKERS = 12
DEFAULT_BUDGET_S = 90.0     # 总时长预算；超时即返回已得结果


def _divergence_of(code: str, days: int):
    """单只：拉日线 → 日线背离。失败返回 None（fail-open）。"""
    try:
        from core.market_data import get_provider
        from analysis.divergence import detect_daily_divergence
        df = get_provider().daily(code, days=days)
        if df is None or df.empty:
            return None
        return detect_daily_divergence(df)
    except Exception:
        return None


def scan_daily_bottom_divergence(codes, days: int = DEFAULT_DAYS,
                                 max_workers: int = DEFAULT_WORKERS,
                                 budget_s: float = DEFAULT_BUDGET_S,
                                 verbose: bool = True):
    """并发扫描日线底背离。

    返回 [{code, type, consec, bars_ago, time}]（只含**底**背离）。
    fail-open：任何异常/超时都只导致少扫几只，不抛给调用方。
    """
    codes = [str(c) for c in dict.fromkeys(codes) if c]
    if not codes:
        return []
    hits, done = [], 0
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_divergence_of, c, days): c for c in codes}
            for fut in as_completed(futs, timeout=budget_s):
                code = futs[fut]
                done += 1
                try:
                    r = fut.result()
                except Exception:
                    continue
                if r and r.get("type") == "底背离":
                    hits.append({"code": code, **r})
    except Exception:
        # TimeoutError（并发预算耗尽）或线程池异常：返回已收集的部分结果
        if verbose:
            print(f"  [WARN] 日线背离扫描提前结束：已处理 {done}/{len(codes)} 只，"
                  f"命中 {len(hits)} 只（结果仍可用）")
        return hits
    if verbose:
        print(f"  [OK] 日线背离扫描完成：{done}/{len(codes)} 只，命中底背离 {len(hits)} 只")
    return hits


def format_alert_lines(hits: list, name_of=None) -> list:
    """把命中结果格式化成飞书 `send_post` 的 content_lines。"""
    lines = []
    for h in hits:
        code = h.get("code", "")
        name = (name_of(code) if name_of else "") or code
        bits = [f"日线底背离（{str(h.get('time', ''))[:10]}，距今{h.get('bars_ago')}根"]
        if h.get("consec"):
            bits.append("连续")
        lines.append(f"🟡 {name}（{code}）{'，'.join(bits)}）")
    if lines:
        lines.append("")
        lines.append("📌 日线单次 MACD 背离尚未经本项目验证；非买卖建议，仅供观察。")
    return lines
