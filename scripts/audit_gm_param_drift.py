# -*- coding: utf-8 -*-
"""只读审计 ``_gm`` 参数副本与监控侧 ``config.py`` 的参数漂移。

本模块只使用 AST 和 ``ast.literal_eval``，绝不导入两个生产配置模块。
测试可导入 ``audit_parameters`` / ``compare_parameters`` 复用同一套逻辑。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.py"
GM_PARAMS_PATH = ROOT / "execution" / "auto" / "_gm" / "config" / "params.py"
DEFAULT_REPORT_PATH = ROOT / "doc" / "research" / "2026-10-26_gm参数漂移报告.md"

PARAM_DICTS = (
    "PARAMS",
    "STOCK_PARAMS",
    "INDEX_REGIME_PARAMS",
    "INDEX_INTRADAY_PARAMS",
    "SENTIMENT_PARAMS",
)

CONSISTENT = "一致"
FORMAT_ONLY = "格式差异但语义相同"
DRIFT = "真漂移"

# 仅登记人工阅读两侧源码后确认“同概念、同用途”的异名键。
# 不把 OSC 的 5% 深止损/3% 冲高目标映射到做 T 的 0.8% 止损/止盈：
# 后者名称相似，但策略、持仓 horizon 与消费端均不同，不属于同一参数。
EXPLICIT_ALIASES = {
    "PARAMS::osc_sell_time_hhmm": "SENTIMENT_PARAMS::force_flat_time",
}


@dataclass(frozen=True)
class ParamEntry:
    key: str
    value: Any
    line: int


@dataclass(frozen=True)
class SkippedEntry:
    key: str
    line: int
    reason: str


@dataclass(frozen=True)
class Comparison:
    gm_key: str
    config_key: str
    status: str
    gm: ParamEntry | None
    config: ParamEntry | None
    reason: str
    mapping: str

    @property
    def identity(self) -> str:
        return f"{self.gm_key} <> {self.config_key}"


def _flatten_dict(node: ast.Dict, prefix: str, line: int,
                  values: dict[str, ParamEntry], skipped: list[SkippedEntry]) -> None:
    for key_node, value_node in zip(node.keys, node.values):
        try:
            part = ast.literal_eval(key_node)
        except (ValueError, TypeError, SyntaxError):
            skipped.append(SkippedEntry(prefix, getattr(key_node, "lineno", line),
                                        "字典键不是静态字面量"))
            continue
        if not isinstance(part, (str, int)):
            skipped.append(SkippedEntry(prefix, getattr(key_node, "lineno", line),
                                        f"不支持的字典键类型: {type(part).__name__}"))
            continue
        key = f"{prefix}::{part}"
        if isinstance(value_node, ast.Dict):
            # 空字典也保留一个叶节点，使空/非空变化可见。
            if value_node.keys:
                _flatten_dict(value_node, key, value_node.lineno, values, skipped)
            else:
                values[key] = ParamEntry(key, {}, value_node.lineno)
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError):
            skipped.append(SkippedEntry(key, getattr(value_node, "lineno", line),
                                        "值不是 ast.literal_eval 支持的静态字面量"))
            continue
        values[key] = ParamEntry(key, value, getattr(value_node, "lineno", line))


def extract_parameters(path: Path) -> tuple[dict[str, ParamEntry], list[SkippedEntry]]:
    """提取目标顶层字典中的所有静态参数；不执行源码。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, ParamEntry] = {}
    skipped: list[SkippedEntry] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value_node = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value_node = statement.target, statement.value
        else:
            continue
        if not isinstance(target, ast.Name) or target.id not in PARAM_DICTS:
            continue
        if not isinstance(value_node, ast.Dict):
            skipped.append(SkippedEntry(target.id, statement.lineno,
                                        "目标参数表不是静态字典字面量"))
            continue
        _flatten_dict(value_node, target.id, statement.lineno, values, skipped)
    return values, skipped


def _is_time_key(key: str) -> bool:
    leaf = key.rsplit("::", 1)[-1].lower()
    return (leaf.endswith("_hhmm") or leaf.endswith("_tval")
            or leaf in {"morning_no_sell_until", "take_profit_time_after",
                        "force_flat_time", "alert_window_end"})


def _normalise_time(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not float(value).is_integer():
            return value
        digits = str(int(value)).zfill(4)
    elif isinstance(value, str):
        digits = value.strip()
    else:
        return value
    match = re.fullmatch(r"(\d{1,2}):?(\d{2})", digits)
    if not match:
        return value
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return ("time", f"{hour:02d}:{minute:02d}")
    return value


def _normalise_scalar(value: Any, key: str) -> Any:
    if _is_time_key(key):
        return _normalise_time(value)
    if isinstance(value, str):
        percent = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*%\s*", value)
        if percent:
            return ("number", float(percent.group(1)) / 100.0)
        leaf = key.rsplit("::", 1)[-1].lower()
        if leaf.endswith(("_path", "_dir", "_file")):
            # 两侧都运行在 Windows；斜杠方向与大小写不改变路径语义。
            return ("path", str(Path(value)).replace("\\", "/").casefold())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", float(value))
    return value


def _normalise(value: Any, key: str) -> Any:
    if isinstance(value, list):
        return [_normalise(item, key) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalise(item, key) for item in value)
    if isinstance(value, dict):
        return {item_key: _normalise(item, key) for item_key, item in value.items()}
    return _normalise_scalar(value, key)


def _raw_equal(left: Any, right: Any) -> bool:
    # int/float 只是字面量写法差异；bool 与数字则不能混为一谈。
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right and (not isinstance(left, bool) or type(left) is type(right))


def _format_value(value: Any) -> str:
    return repr(value)


def compare_parameters(gm_values: dict[str, ParamEntry],
                       config_values: dict[str, ParamEntry]) -> list[Comparison]:
    """对照同名重叠键及人工确认的异名映射，结果按键名稳定排序。"""
    pairs: set[tuple[str, str, str]] = set()
    gm_by_scope: dict[str, set[str]] = {}
    config_by_scope: dict[str, set[str]] = {}
    for key in gm_values:
        scope = key.split("::", 1)[0]
        gm_by_scope.setdefault(scope, set()).add(key)
    for key in config_values:
        scope = key.split("::", 1)[0]
        config_by_scope.setdefault(scope, set()).add(key)

    # STOCK_PARAMS 按同一证券代码建立子作用域，避免跨股票误配同名覆盖键。
    if "STOCK_PARAMS" in gm_by_scope or "STOCK_PARAMS" in config_by_scope:
        gm_stock = {key for key in gm_by_scope.get("STOCK_PARAMS", set()) if key.count("::") >= 2}
        config_stock = {key for key in config_by_scope.get("STOCK_PARAMS", set()) if key.count("::") >= 2}
        gm_symbols = {key.split("::")[1] for key in gm_stock}
        config_symbols = {key.split("::")[1] for key in config_stock}
        for symbol in sorted(gm_symbols & config_symbols):
            gm_leaves = {key.split("::", 2)[2] for key in gm_stock if key.split("::")[1] == symbol}
            config_leaves = {key.split("::", 2)[2] for key in config_stock if key.split("::")[1] == symbol}
            # _note 是人工说明而非消费参数；保留提取但不参与报警。
            gm_leaves.discard("_note")
            config_leaves.discard("_note")
            # 同一股票的覆盖表是一个明确对照单元：缺键也属于漂移。
            for leaf in gm_leaves | config_leaves:
                pairs.add((f"STOCK_PARAMS::{symbol}::{leaf}",
                           f"STOCK_PARAMS::{symbol}::{leaf}", "同名"))

    for scope in sorted((set(gm_by_scope) | set(config_by_scope)) - {"STOCK_PARAMS"}):
        gm_keys = gm_by_scope.get(scope, set())
        config_keys = config_by_scope.get(scope, set())
        # 顶层策略各自独有的键不构成重叠；仅同名键直接对照。
        for key in sorted(gm_keys & config_keys):
            pairs.add((key, key, "同名"))

    for gm_key, config_key in EXPLICIT_ALIASES.items():
        pairs.add((gm_key, config_key, "显式语义映射"))

    comparisons: list[Comparison] = []
    for gm_key, config_key, mapping in sorted(pairs):
        gm = gm_values.get(gm_key)
        config = config_values.get(config_key)
        if gm is None or config is None:
            status = DRIFT
            reason = "参数仅一侧存在"
        else:
            normalised_gm = _normalise(gm.value, gm_key)
            normalised_config = _normalise(config.value, config_key)
            if normalised_gm != normalised_config:
                status = DRIFT
                reason = "语义归一化后值仍不同"
            elif _raw_equal(gm.value, config.value):
                status = CONSISTENT
                reason = "字面值与语义均一致"
            else:
                status = FORMAT_ONLY
                reason = "字面格式不同，语义归一化后一致"
        comparisons.append(Comparison(gm_key, config_key, status, gm, config, reason, mapping))
    return comparisons


def audit_parameters(root: Path = ROOT) -> dict[str, Any]:
    config_path = root / "config.py"
    gm_path = root / "execution" / "auto" / "_gm" / "config" / "params.py"
    gm_values, gm_skipped = extract_parameters(gm_path)
    config_values, config_skipped = extract_parameters(config_path)
    comparisons = compare_parameters(gm_values, config_values)
    return {
        "gm_values": gm_values,
        "config_values": config_values,
        "gm_skipped": gm_skipped,
        "config_skipped": config_skipped,
        "comparisons": comparisons,
        "counts": Counter(item.status for item in comparisons),
    }


def _impact(item: Comparison) -> str:
    key = item.gm_key
    leaf = key.rsplit("::", 1)[-1]
    if key.startswith("INDEX_REGIME_PARAMS::"):
        return "大盘态势评分、状态切换或数据窗口可能采用不同口径，进而影响交易侧仓位门控与监控判断。"
    if key.startswith("STOCK_PARAMS::"):
        symbol = key.split("::")[1]
        return f"{symbol} 的个股覆盖在两侧不同，可能造成该标的信号阈值、仓位或风控行为不一致。"
    if "commission" in leaf or "fee" in leaf:
        return "交易成本假设不同，可能造成做 T 净收益、信号性价比与回测/监控结果不一致。"
    if _is_time_key(key):
        return "交易触发或强制退出时点可能不一致，造成订单与监控节奏错位。"
    if any(token in leaf for token in ("qty", "position", "budget", "cash")):
        return "仓位或资金上限口径不同，可能改变下单规模与风险暴露。"
    return "同名决策参数取值不同，可能改变信号、风控或门控结果；需结合各自消费端人工确认后收敛。"


def render_markdown(result: dict[str, Any]) -> str:
    comparisons: list[Comparison] = result["comparisons"]
    counts: Counter = result["counts"]
    skipped = result["gm_skipped"] + result["config_skipped"]
    gm_osc_count = sum(key.startswith("PARAMS::osc_") for key in result["gm_values"])
    config_swing_count = sum("::swing_" in key for key in result["config_values"])
    time_comparison_count = sum(_is_time_key(item.gm_key) for item in comparisons)
    fee_comparison_count = sum(
        "commission" in item.gm_key or "fee" in item.gm_key for item in comparisons
    )
    lines = [
        "# _gm 参数副本 vs 监控侧 config.py 漂移报告",
        "",
        "> 审计日期：2026-10-26。只读 AST 静态审计；未导入生产配置、未修改任何参数。",
        "",
        "## 口径",
        "",
        "- 提取 `PARAMS`、`STOCK_PARAMS`、`INDEX_REGIME_PARAMS`、`INDEX_INTRADAY_PARAMS`、`SENTIMENT_PARAMS` 的静态字面量。",
        "- 顶层同名键直接对照；`STOCK_PARAMS` 仅在同一代码的覆盖表内对照，缺键记为真漂移；策略各自独有的顶层键不纳入重叠键。`_note` 说明文字保留提取但不报警。",
        "- 异名键只采用人工确认的显式语义映射：`osc_sell_time_hhmm` ↔ `force_flat_time`。名称相似但策略语义不同的 OSC/做 T 止损止盈不强行配对。",
        "- 时间统一为 `HH:MM`，百分比字符串统一为小数，Windows 路径统一分隔符与大小写后比较。",
        "",
        "## 汇总",
        "",
        f"- 重叠对照键：**{len(comparisons)}** 项（含显式语义映射）。",
        f"- 一致：**{counts.get(CONSISTENT, 0)}** 项。",
        f"- 格式差异但语义相同：**{counts.get(FORMAT_ONLY, 0)}** 项。",
        f"- 真漂移：**{counts.get(DRIFT, 0)}** 项。",
        f"- AST 提取：_gm **{len(result['gm_values'])}** 个叶参数，config.py **{len(result['config_values'])}** 个叶参数；跳过 **{len(skipped)}** 项。",
        "",
        "## 参数族覆盖",
        "",
        f"- OSC：_gm 共提取 {gm_osc_count} 个 `osc_*` 键；仅 `osc_sell_time_hhmm` 与正本 `force_flat_time` 确认同义并纳入映射，其余为 OSC 专属，无正本对照键。",
        f"- swing：config.py 共提取 {config_swing_count} 个 `swing_*` 键，_gm 无对应键，故没有可配对的 swing 重叠项。",
        f"- fee/费率：{fee_comparison_count} 个重叠项，`commission_rate` 已纳入对照并列示其真漂移。",
        f"- 时间常量：{time_comparison_count} 个重叠项；同名的 `morning_no_sell_until` 与异名 OSC 映射均完成 HHMM/HH:MM 归一化。",
        "",
        "## 真漂移清单（只报告，不修改）",
        "",
    ]
    drifts = [item for item in comparisons if item.status == DRIFT]
    if not drifts:
        lines.append("未发现真漂移。")
    for index, item in enumerate(drifts, 1):
        lines.extend([
            f"### {index}. `{item.gm_key}` ↔ `{item.config_key}`",
            "",
            f"- 判定：{item.reason}；映射方式：{item.mapping}。",
            f"- _gm 值：`{_format_value(item.gm.value) if item.gm else '<缺失>'}`（`execution/auto/_gm/config/params.py:{item.gm.line if item.gm else '-'}`）。",
            f"- config.py 值：`{_format_value(item.config.value) if item.config else '<缺失>'}`（`config.py:{item.config.line if item.config else '-'}`）。",
            f"- 可能影响：{_impact(item)}",
            "",
        ])
    lines.extend(["## 格式差异但语义相同", ""])
    format_only = [item for item in comparisons if item.status == FORMAT_ONLY]
    if not format_only:
        lines.append("无。")
    for item in format_only:
        lines.append(f"- `{item.gm_key}`：`{_format_value(item.gm.value)}`（_gm:{item.gm.line}）↔ `{_format_value(item.config.value)}`（config.py:{item.config.line}），归一化后一致。")
    lines.extend(["", "## AST 跳过项", ""])
    if not skipped:
        lines.append("无。目标参数表的全部叶参数均可由 AST 静态提取。")
    else:
        for item in skipped:
            lines.append(f"- `{item.key}`（行 {item.line}）：{item.reason}。")
    lines.extend([
        "",
        "## 后续约束",
        "",
        "`tests/phase3/test_gm_param_drift.py` 将本次真漂移固化为白名单：白名单外新增漂移直接失败；已收敛的白名单项给出移除提示。",
        "",
    ])
    return "\n".join(lines)


def _jsonable(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "counts": dict(sorted(result["counts"].items())),
        "comparisons": [{
            "gm_key": item.gm_key,
            "config_key": item.config_key,
            "status": item.status,
            "reason": item.reason,
            "mapping": item.mapping,
            "gm": None if item.gm is None else {"value": repr(item.gm.value), "line": item.gm.line},
            "config": None if item.config is None else {"value": repr(item.config.value), "line": item.config.line},
        } for item in result["comparisons"]],
        "skipped": [item.__dict__ for item in result["gm_skipped"] + result["config_skipped"]],
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="将 Markdown 报告写入指定路径；默认输出到 stdout")
    parser.add_argument("--json", action="store_true", help="以确定性 JSON 输出，便于重复运行校验")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = audit_parameters()
    text = (json.dumps(_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            if args.json else render_markdown(result))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
