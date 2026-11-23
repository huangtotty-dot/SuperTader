# -*- coding: utf-8 -*-
"""index_regime 双脑一致性对拍闸门（2026-11-23，纯离线、秒级）。

背景：系统有两份大盘态势（regime）判定实现——
  - 完整版  analysis/index_regime.py（V2.2.4 磁滞状态机，GUI 展示用）
  - 实盘版  execution/auto/_gm/analysis/index_regime.py（830 行精简 fork，gm_main 实盘策略用）
两侧参数表各自维护，此前无任何一致性测试；风险是一侧调参另一侧没跟上，
造成"GUI 说多、策略说空"的静默漂移。本测试把"差异只有这些"钉成闸门。

覆盖三层（全部离线，无网络、无 t_io 读写、不 import 生产 config）：

  层A 参数对拍（AST 静态提取，不 import 任何生产模块）
      完整版有效参数 = analysis/index_regime.py::IR_DEFAULT_PARAMS ← config.py::INDEX_REGIME_PARAMS 覆盖
      实盘版有效参数 = execution/auto/_gm/config/params.py::INDEX_REGIME_PARAMS（fork 直接全量取用）
      断言：共有键值逐项相等；full-only 键精确等于白名单（逐项注明"有意差异"理由）。

  层B 运行时对拍（importlib 按路径加载 + sys.modules stub 掉 config/core.paths）
      完整版 import 链 from config import ... → stub 注入 AST 提取的真实值
      （绕开生产 config.py 的副作用：代理环境清洗、requests 猴补丁、t_io 目录创建与日志文件句柄）；
      实盘版 import 链 from config.params import ... → 同名冲突由 sys.modules 预注册
      "config" 与 "config.params" 两个 stub 解决；全程不碰 gm SDK（fork 本体不 import gm）。
      断言：两侧 _ir_params() 运行时输出 == AST 层期望，stub 忠实度闭环。

  层C 行为对拍（纯 pandas 合成指数日线，零 I/O）
      可对拍层（fork 逐字拷贝的纯函数，输出必须完全一致）：
        _ir_f / _ir_sign / _ir_clip、_ir_hurst_rs_one / _ir_hurst_bar、
        _ir_streak_features（两侧打分栈共同的特征地基，未来单侧改动即报警）。
      已知代际差异（fork 冻结于 V2.0 时代精简逻辑，属设计使然，钉住现状不强行拉齐）：
        _ir_streak_curve_value：完整版=分段线性插值且仅取正值（调用方保证 k>0），
          fork=阶梯取值+负值镜像；本测试断言正锚点处两侧相等，并把非锚点/负值的
          当前差值钉成显式基线。
        _ir_step_regime：完整版含 R2 破位快出/streak 门控/止跌退出/结构硬转向/score 突变，
          fork 为简单磁滞+K 日强跳；仅在两机的磁滞交集子集断言状态结果一致，
          K-up 低分场景的语义分歧钉成显式基线。
      不覆盖（有意）：完整版 detect() 全链路（依赖腾讯/akshare 网络源与 state.json 落盘）、
        _ir_score_trend/_ir_score_env/_ir_kday_eval/_ir_sharp_eval（fork 为大幅简化版，
        输出本就不可能一致，属文档化的精简而非漂移）。

运行：python -m unittest tests.phase3.test_regime_parity -v
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parents[2]
_FULL_SRC = _ROOT / "analysis" / "index_regime.py"
_OVERLAY_SRC = _ROOT / "config.py"
_GM_PARAMS_SRC = _ROOT / "execution" / "auto" / "_gm" / "config" / "params.py"
_GM_SRC = _ROOT / "execution" / "auto" / "_gm" / "analysis" / "index_regime.py"


# ============================================================================
# 层A：AST 静态参数提取（绝不 import 生产模块；与 scripts/audit_gm_param_drift 同流派）
# ============================================================================

def _extract_dict(path: Path, name: str) -> dict:
    """从源码提取顶层 dict 字面量；键/值都必须能被 ast.literal_eval 静态求值，
    求值失败即抛错（不许跳过——出现非字面量说明该表已不再是纯参数表，需人工评审）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name and isinstance(value, ast.Dict):
            out = {}
            for k_node, v_node in zip(value.keys, value.values):
                key = ast.literal_eval(k_node)
                out[key] = ast.literal_eval(v_node)
            return out
    raise AssertionError(f"{path} 中找不到顶层 dict：{name}")


IR_DEFAULTS = _extract_dict(_FULL_SRC, "IR_DEFAULT_PARAMS")          # 完整版模块默认
IR_OVERLAY = _extract_dict(_OVERLAY_SRC, "INDEX_REGIME_PARAMS")      # config.py 覆盖层
IR_GM = _extract_dict(_GM_PARAMS_SRC, "INDEX_REGIME_PARAMS")         # 实盘版参数表

# 完整版运行时有效参数（与 _ir_params() 同语义：模块默认 ← config 覆盖）
IR_FULL_EFFECTIVE = dict(IR_DEFAULTS)
IR_FULL_EFFECTIVE.update(IR_OVERLAY)

# ── 白名单：完整版有效参数中、实盘精简版【有意不实现】的机制参数 ──
# 钉住语义：fork 缺这些键是设计使然（精简版冻结于 V2.0 时代逻辑，代码零引用）；
# 任一侧新增独有键都会打破精确集合相等，必须在此登记理由（这正是闸门要拦的静默漂移）。
FULL_ONLY_PARAM_WHITELIST = {
    # V2.2.4 价格结构强化（_ir_structure_score）；fork 结构分是固定 5/3/4 简化版，零引用
    "ma5_persist_days": "结构强化：MA5 持续站稳/失守确认天数；fork 无 _ir_structure_score",
    "ma20_break_bonus": "结构强化：MA20 破位加减分；fork 无 _ir_structure_score",
    "ma60_break_bonus": "结构强化：MA60 破位加减分；fork 无 _ir_structure_score",
    "ma_touch_bonus": "结构强化：触及关键均线收回/回落分；fork 无 _ir_structure_score",
    "structure_trigger": "结构强信号阈值（日线主判定/盘中 hint）；fork 无该消费端",
    # 首次站上 MA5 恢复加分（deep streak 止跌初期 low-buffer 加分）；fork 未实现该机制
    "ma5_recover_streak_min": "MA5 恢复加分机制；fork 未实现",
    "ma5_recover_bonus_base": "MA5 恢复加分机制；fork 未实现",
    "ma5_recover_bonus_streak": "MA5 恢复加分机制；fork 未实现",
    "ma5_recover_bonus_pos20": "MA5 恢复加分机制；fork 未实现",
    "ma5_recover_bonus_cap": "MA5 恢复加分机制；fork 未实现",
    # uni_down 止跌退出（站上 MA5+阳线确认切 range）；fork 状态机为 V2.0 磁滞简化版，无此分支
    "uni_down_exit_above_ma5": "止跌退出规则；fork 状态机无该分支",
    # E5 涨跌停池数据源开关；fork 的 _ir_fetch_e5_limit_pool 恒降级，键无消费者
    "e5_source": "E5 数据源开关；fork E5 恒降级，无消费者",
    "e5_em_fallback": "E5 东财兜底开关；fork E5 恒降级，无消费者",
    "e5_ths_cache": "E5 同花顺缓存路径；fork E5 恒降级，无消费者",
    # 状态落盘目录；fork 回测不写 state.json（其 _ir_state_dir 恒返回 ""）
    "state_dir": "状态落盘目录；fork 回测不写状态文件",
}

# ── 默认值↔覆盖层登记差集：模块默认值与 config.py 覆盖值有意不同的键 ──
# 有效参数=覆盖值（与 gm 一致），默认值为历史/单机回退档。安全性质：这些键必须一直
# 留在 overlay 中且与 gm 相等，否则完整版会静默回退默认值而与实盘版漂移。
# 若日后把模块默认值对齐覆盖值，请同步删除对应登记。
# 2026-11-23：http_timeout / r0_vol_max 默认值已对齐 overlay（8→15 / 1.15→1.0），登记清空。
REGISTERED_DEFAULT_OVERLAY_GAPS = {
}

# 参数表规模基线（2026-11-23 实测）：防止借"删键"绕过对拍——任一侧键数变化必须更新本基线。
PARAM_COUNT_BASELINE = {"defaults": 119, "overlay": 106, "gm": 104, "full_effective": 119}


# ============================================================================
# 层B/层C：stub 加载两侧模块（隔离生产 config 副作用与 gm SDK）
# ============================================================================

def _install_config_stubs() -> dict:
    """注册 stub：config（完整版 from config import ...）、config.params（实盘版
    from config.params import ...）、core.paths（完整版取 BASE_DIR）。
    stub 的参数值来自层A 对真实源码的 AST 提取，保证与生产配置同源。
    返回被替换的原始 sys.modules 条目供恢复。"""
    names = ("config", "config.params", "core", "core.paths")
    saved = {k: sys.modules.get(k) for k in names}

    cfg = types.ModuleType("config")
    cfg.INDEX_REGIME_PARAMS = dict(IR_OVERLAY)     # 完整版运行时的真实覆盖层
    cfg.INDEX_REGIME_CONTEXT = {}
    cfg.SESSION_CONTEXT = {}
    cfg.log = logging.getLogger("ir_regime_parity_test")

    gm_cfg = types.ModuleType("config.params")
    gm_cfg.INDEX_REGIME_PARAMS = dict(IR_GM)       # 实盘版运行时的真实参数表
    cfg.params = gm_cfg

    core = types.ModuleType("core")
    core.__path__ = []
    paths = types.ModuleType("core.paths")
    paths.BASE_DIR = str(_ROOT)
    core.paths = paths

    sys.modules.update({
        "config": cfg, "config.params": gm_cfg, "core": core, "core.paths": paths,
    })
    return saved


def _restore_modules(saved: dict, loaded_names) -> None:
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    for name in loaded_names:
        sys.modules.pop(name, None)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FULL = None   # 完整版模块（analysis/index_regime.py，stub config 下加载）
GM = None     # 实盘版模块（execution/auto/_gm/analysis/index_regime.py）
_STUB_SAVED = None
_LOADED_NAMES = ("ir_regime_full_under_test", "ir_regime_gm_under_test")


def setUpModule():
    global FULL, GM, _STUB_SAVED
    _STUB_SAVED = _install_config_stubs()
    FULL = _load_module(_LOADED_NAMES[0], _FULL_SRC)
    GM = _load_module(_LOADED_NAMES[1], _GM_SRC)


def tearDownModule():
    _restore_modules(_STUB_SAVED, _LOADED_NAMES)


# ============================================================================
# 合成数据（纯内存，确定性）
# ============================================================================

def _make_df(closes, volumes=None, start="2026-01-02") -> pd.DataFrame:
    """由收盘价序列合成指数日线（open/high/low/close/volume/date），纯 pandas。"""
    close = np.asarray(closes, dtype=float)
    n = close.size
    if volumes is None:
        volumes = np.full(n, 1.2e9)
    dates = pd.bdate_range(start=start, periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.998,
        "high": close * 1.012,
        "low": close * 0.988,
        "close": close,
        "volume": np.asarray(volumes, dtype=float),
    })


def _scenario_frames() -> dict:
    """多形态合成日线：强趋势上行/下行、缠绕震荡、量能脉冲、短样本、退化样本。"""
    rng = np.random.default_rng(20260718)
    n = 160
    up = 3000 + np.linspace(0, 220, n) + rng.normal(0, 2.0, n).cumsum() * 0.3
    down = 3400 - np.linspace(0, 260, n) + rng.normal(0, 2.0, n).cumsum() * 0.3
    chop = 3200 + 55 * np.sin(np.linspace(0, 9 * np.pi, n)) + rng.normal(0, 1.2, n)
    vol_spike = np.full(n, 1.0e9)
    vol_spike[-6:] = 2.3e9                       # 近端放量 → vol_ratio > 1
    fade = np.linspace(3200, 3180, n) + rng.normal(0, 1.0, n)
    vol_fade = np.full(n, 1.0e9)
    vol_fade[-6:] = 0.45e9                       # 近端缩量 → vol_ratio < 1
    return {
        "uptrend": _make_df(up),
        "downtrend": _make_df(down),
        "choppy_vol_spike": _make_df(chop, vol_spike),
        "grind_down_vol_fade": _make_df(fade, vol_fade),
        "short_25": _make_df(up[:25]),           # 20<=n<60：ma60 分支降级
        "degenerate_8": _make_df(up[:8]),        # n<10：ok=False
    }


def _quiet_feat(streak=5):
    """磁滞交集子集的"安静"特征：不触发完整版任何结构硬转向/止跌/score 突变分支，
    使两侧状态机只在共有的磁滞核心里对拍。"""
    return {
        "streak": streak, "close": 100.0, "ma10": 90.0,
        "above_ma5_days": 1, "below_ma5_days": 0, "up_days": 0, "down_days": 0,
        "full_above_ma5_days": 0, "full_below_ma5_days": 0,
        "above_ma20_days": 0, "below_ma20_days": 0,
        "above_ma60_days": 0, "below_ma60_days": 0,
        "touch_ma20": False, "touch_ma60": False,
        "break_ma20": False, "break_ma60": False,
        "close_below_ma60": False, "close_above_ma60": True,
        "ma5_slope_down": False, "ma5_slope_up": True,
    }


# ============================================================================
# 层A 测试：参数表对拍
# ============================================================================

class TestParamTableParity(unittest.TestCase):
    """完整版有效参数 vs 实盘版参数表：共有键零漂移，独有键精确等于白名单。"""

    def test_param_count_baselines(self):
        actual = {
            "defaults": len(IR_DEFAULTS), "overlay": len(IR_OVERLAY),
            "gm": len(IR_GM), "full_effective": len(IR_FULL_EFFECTIVE),
        }
        self.assertEqual(
            actual, PARAM_COUNT_BASELINE,
            "参数表键数漂移（防借删键绕过对拍）：若为有意的参数增删，请同步更新 "
            "PARAM_COUNT_BASELINE 与白名单。\n"
            f"  当前: {actual}\n  基线: {PARAM_COUNT_BASELINE}",
        )

    def test_gm_keys_subset_of_full_effective(self):
        """实盘版引用的每个参数键都必须能在完整版有效参数中找到（fork 同源红线）。"""
        missing = sorted(set(IR_GM) - set(IR_FULL_EFFECTIVE))
        self.assertEqual(
            missing, [],
            f"实盘版参数键在完整版有效参数中不存在（fork 新增独立键？）：{missing}",
        )

    def test_shared_param_values_identical(self):
        """共有键逐项等值——本闸门核心：任一侧改参另一侧没跟上即在此失败。"""
        drift = []
        for k in sorted(set(IR_FULL_EFFECTIVE) & set(IR_GM)):
            fv, gv = IR_FULL_EFFECTIVE[k], IR_GM[k]
            if fv != gv:
                drift.append(f"  {k}: 完整版={fv!r}  实盘版={gv!r}")
        self.assertEqual(
            drift, [],
            "发现白名单外的参数值漂移（请勿为对齐而改生产代码；先人工裁决）：\n"
            + "\n".join(drift),
        )

    def test_full_only_keys_exactly_whitelist(self):
        """完整版独有键必须精确等于登记白名单：新增独有键必须登记理由，
        白名单键若已被 fork 实现/删除则应从白名单清除（防白名单腐化）。"""
        actual = set(IR_FULL_EFFECTIVE) - set(IR_GM)
        want = set(FULL_ONLY_PARAM_WHITELIST)
        new_keys = sorted(actual - want)
        stale = sorted(want - actual)
        msg = []
        if new_keys:
            msg.append("完整版新增独有参数键，未登记白名单：\n  " + "\n  ".join(new_keys))
        if stale:
            msg.append("白名单键已不再是完整版独有（已收敛或被删除），请清理白名单：\n  "
                       + "\n  ".join(stale))
        self.assertFalse(msg, "full-only 参数键与白名单不一致：\n" + "\n".join(msg))

    def test_whitelist_entries_have_reasons(self):
        for k, reason in FULL_ONLY_PARAM_WHITELIST.items():
            with self.subTest(key=k):
                self.assertTrue(str(reason).strip(), f"白名单键 {k} 缺差异理由")

    def test_overlay_keys_within_defaults(self):
        """config.py 红线：INDEX_REGIME_PARAMS 的键必须与模块 IR_DEFAULT_PARAMS
        完全一致（不得成为死键或错误覆盖）；此处校验 overlay ⊆ defaults。"""
        dead = sorted(set(IR_OVERLAY) - set(IR_DEFAULTS))
        self.assertEqual(
            dead, [],
            f"config.py 存在模块默认表中不存在的覆盖键（死键/错键风险）：{dead}",
        )

    def test_registered_default_overlay_gaps(self):
        """模块默认值与 overlay 的差集必须精确等于登记表；且登记键的 overlay 值
        与实盘版相等（有效参数对齐的安全性质——默认值回退路径不得生效）。"""
        actual = {k for k in set(IR_OVERLAY) & set(IR_DEFAULTS)
                  if IR_OVERLAY[k] != IR_DEFAULTS[k]}
        want = set(REGISTERED_DEFAULT_OVERLAY_GAPS)
        self.assertEqual(
            actual, want,
            f"模块默认↔overlay 差集漂移：当前 {sorted(actual)}，登记 {sorted(want)}。"
            "若对齐了默认值请清理登记；若新增有意差集请登记并确认 overlay 与实盘版一致。",
        )
        for k, (dft, ovl) in REGISTERED_DEFAULT_OVERLAY_GAPS.items():
            with self.subTest(key=k):
                self.assertEqual(IR_DEFAULTS[k], dft, f"{k} 模块默认值已变，请更新登记")
                self.assertEqual(IR_OVERLAY[k], ovl, f"{k} overlay 值已变，请更新登记")
                self.assertEqual(IR_OVERLAY[k], IR_GM[k],
                                 f"{k} overlay 与实盘版不等——默认值回退将造成静默漂移")


# ============================================================================
# 层B 测试：运行时 _ir_params() 与 AST 层闭环
# ============================================================================

class TestRuntimeParamsMatchAst(unittest.TestCase):
    """stub 忠实度闭环：按真实源码 AST 注入后，两侧模块运行时取到的参数
    必须与层A 的静态期望完全一致（否则 stub/合并逻辑失真，层C 行为对拍无意义）。"""

    def test_full_runtime_params_match_ast(self):
        self.assertEqual(FULL._ir_params(), IR_FULL_EFFECTIVE)

    def test_gm_runtime_params_match_ast(self):
        self.assertEqual(GM._ir_params(), dict(IR_GM))

    def test_runtime_shared_values_identical(self):
        pf, pg = FULL._ir_params(), GM._ir_params()
        drift = [k for k in sorted(set(pf) & set(pg)) if pf[k] != pg[k]]
        self.assertEqual(drift, [], f"运行时共有键漂移：{drift}")


# ============================================================================
# 层C 测试：行为对拍
# ============================================================================

class TestEnumContractParity(unittest.TestCase):
    """对外契约枚举：状态值/中文名/活动仓系数两侧必须一致（下游按字符串消费）。"""

    def test_regime_values(self):
        self.assertEqual({r.value for r in FULL.IndexRegime},
                         {r.value for r in GM.IndexRegime})
        self.assertEqual({r.value for r in FULL.IndexRegime},
                         {"uni_down", "range", "uni_up"})

    def test_regime_names(self):
        for v, cn in (("uni_down", "单边下行"), ("range", "横盘震荡"), ("uni_up", "单边上涨")):
            with self.subTest(regime=v):
                self.assertEqual(FULL.index_regime_name(v), cn)
                self.assertEqual(GM.index_regime_name(v), cn)

    def test_position_factors(self):
        expect = {"uni_down": 0.6, "range": 1.0, "uni_up": 1.1}
        for v, f in expect.items():
            with self.subTest(regime=v):
                self.assertEqual(FULL.get_regime_position_factor(v), f)
                self.assertEqual(GM.get_regime_position_factor(v), f)


class TestScalarHelpersParity(unittest.TestCase):
    """_ir_f / _ir_sign / _ir_clip：逐字拷贝的小工具，网格抽样必须完全一致。"""

    def test_ir_f(self):
        samples = [0, 1, -1, 3.14159265, -2.718281828, 1e10, -1e-7,
                   float("nan"), float("inf"), -float("inf"), None, "3.5", "bad", True]
        for nd in (2, 4):
            for x in samples:
                with self.subTest(x=x, nd=nd):
                    self.assertEqual(FULL._ir_f(x, nd), GM._ir_f(x, nd))

    def test_ir_sign_and_clip(self):
        for x in (-1e9, -1.5, -0.0, 0.0, 1e-9, 7.7, 1e9):
            with self.subTest(x=x):
                self.assertEqual(FULL._ir_sign(x), GM._ir_sign(x))
                self.assertEqual(FULL._ir_clip(x, -2.0, 5.0), GM._ir_clip(x, -2.0, 5.0))


class TestHurstParity(unittest.TestCase):
    """Hurst R/S 估计：同一合成收盘价序列，两侧输出逐位相等（同代码同输入）。"""

    def _series(self):
        rng = np.random.default_rng(42)
        return {
            "random_walk_240": pd.Series(3000 + rng.normal(0, 1.4, 240).cumsum()),
            "trend_240": pd.Series(3000 + np.linspace(0, 160, 240) + rng.normal(0, 1.0, 240)),
            "mean_revert_240": pd.Series(3100 + 40 * np.sin(np.linspace(0, 12, 240))),
            "short_100": pd.Series(3000 + rng.normal(0, 1.0, 100).cumsum()),  # < window+smooth-1
        }

    def test_hurst_rs_one(self):
        for name, s in self._series().items():
            arr = s.values.astype(float)
            with self.subTest(series=name):
                self.assertEqual(FULL._ir_hurst_rs_one(arr[-120:]),
                                 GM._ir_hurst_rs_one(arr[-120:]))
        # 退化：过短（rets<60）两侧都必须返回 None
        self.assertIsNone(FULL._ir_hurst_rs_one(np.linspace(1, 100, 50)))
        self.assertIsNone(GM._ir_hurst_rs_one(np.linspace(1, 100, 50)))

    def test_hurst_bar(self):
        for name, s in self._series().items():
            with self.subTest(series=name):
                self.assertEqual(FULL._ir_hurst_bar(s, 120, 20),
                                 GM._ir_hurst_bar(s, 120, 20))


class TestStreakFeaturesParity(unittest.TestCase):
    """_ir_streak_features：两侧打分栈共同的特征地基（近逐字拷贝）。
    同一合成日线输入，整个特征 dict 必须完全相等——这是最强的行为对拍，
    任一侧未来改特征口径（窗口/min_periods/交叉定义/touch/break 判定）都会在此报警。"""

    def test_features_identical_across_scenarios(self):
        pf, pg = FULL._ir_params(), GM._ir_params()
        for name, df in _scenario_frames().items():
            with self.subTest(scenario=name):
                f_full = FULL._ir_streak_features(df, pf)
                f_gm = GM._ir_streak_features(df, pg)
                self.assertEqual(
                    f_full, f_gm,
                    f"场景 {name} 特征不一致："
                    + str({k: (f_full.get(k), f_gm.get(k))
                           for k in set(f_full) | set(f_gm)
                           if f_full.get(k) != f_gm.get(k)}),
                )

    def test_features_sanity_uptrend(self):
        """防"两侧同错"：对强上行场景锚定几个必须成立的特征语义。"""
        df = _scenario_frames()["uptrend"]
        feat = FULL._ir_streak_features(df, FULL._ir_params())
        self.assertTrue(feat["ok"])
        self.assertGreater(feat["streak"], 0)                 # 强上行必为多头 streak
        self.assertGreater(feat["close"], feat["ma5"])        # 收盘在 MA5 上方
        self.assertIsNotNone(feat["ma60"])                    # n=160 必须出 MA60


class TestStreakCurveParity(unittest.TestCase):
    """_ir_streak_curve_value：已知代际差异的钉住测试。

    完整版：分段线性插值，仅处理 k>0（调用方自带 k>0 门控，负值返回 0）。
    实盘版：阶梯取值 + 负值镜像（其 _ir_score_trend 直接喂带符号 streak）。
    二者在正锚点 k∈{1,3,5,8,10,13} 及封顶区必然相等——锚点相等正是要钉的
    "streak_curve 参数表零漂移"；非锚点/负值的差值为文档化差异，钉住现状。
    """

    def test_anchor_points_agree(self):
        pf, pg = FULL._ir_params(), GM._ir_params()
        for k in (1, 3, 5, 8, 10, 13, 14, 20, 25):
            with self.subTest(k=k):
                self.assertEqual(FULL._ir_streak_curve_value(k, pf),
                                 GM._ir_streak_curve_value(k, pg),
                                 f"streak 曲线锚点 k={k} 两侧不等——"
                                 "streak_curve/streak_cap 参数可能单侧漂移")

    def test_documented_divergence_pinned(self):
        """钉住当前已知的插值 vs 阶梯差值（若任一侧改曲线语义，此处报警并要求复审）。"""
        pf, pg = FULL._ir_params(), GM._ir_params()
        # (k, 完整版期望, 实盘版期望)：插值落在相邻锚点之间，阶梯取已越过的最高档
        pinned = [(2, 12.0, 8.0), (4, 20.0, 16.0), (9, 34.0, 32.0)]
        for k, want_full, want_gm in pinned:
            with self.subTest(k=k):
                self.assertEqual(FULL._ir_streak_curve_value(k, pf), want_full)
                self.assertEqual(GM._ir_streak_curve_value(k, pg), want_gm)
        # 非整除插值点（8/3 步长）：钉浮点近似值，锚住"插值 vs 阶梯"语义差
        for k, want_full, want_gm in [(6, 24 + 8 / 3, 24.0), (12, 36 + 8 / 3, 36.0)]:
            with self.subTest(k=k):
                self.assertAlmostEqual(FULL._ir_streak_curve_value(k, pf), want_full, places=9)
                self.assertEqual(GM._ir_streak_curve_value(k, pg), want_gm)
        # 负值语义：完整版返回 0（调用方保证 k>0），实盘版做负值镜像
        self.assertEqual(FULL._ir_streak_curve_value(-5, pf), 0.0)
        self.assertEqual(GM._ir_streak_curve_value(-5, pg), -24.0)


class TestStateMachineOverlap(unittest.TestCase):
    """_ir_step_regime 磁滞交集对拍。

    两侧状态机属不同代际（完整版 V2.2.4 多规则层 vs 实盘版 V2.0 磁滞+K 日强跳），
    全量对拍无意义。本类用"安静特征"(_quiet_feat) 屏蔽完整版全部附加规则，
    只对两机共有的磁滞核心断言状态结果一致；K-up 低分场景的语义分歧钉成基线。
    两侧函数签名不同：完整版 (prev,s,s_prev,p,feat,key_day_type,sharp_dir,score_delta)
    →(regime,note)；实盘版 (prev,s,s_prev,days,p,key_day,sharp_triggered,sharp_add,feat)
    →(regime,days,switched)。仅对拍 regime 结果。
    """

    def _step_full(self, prev, s, s_prev, feat=None, key_day_type=None):
        return FULL._ir_step_regime(prev, s, s_prev, FULL._ir_params(),
                                    feat or _quiet_feat(),
                                    key_day_type=key_day_type, sharp_dir=0,
                                    score_delta=None)[0].value

    def _step_gm(self, prev, s, s_prev, days, feat=None, key_day=None):
        return GM._ir_step_regime(prev, s, s_prev, days, GM._ir_params(),
                                  key_day or {"type": None}, False, 0.0,
                                  feat or _quiet_feat())[0].value

    def test_enter_up_after_confirm(self):
        """RANGE + 连续两日 |S|>=enter 且 streak 同向 → 两侧同进 uni_up。"""
        r_full = self._step_full(FULL.IndexRegime.RANGE, 30.0, 28.0)
        r_gm = self._step_gm(GM.IndexRegime.RANGE, 30.0, 28.0, days=1)
        self.assertEqual((r_full, r_gm), ("uni_up", "uni_up"))

    def test_pending_confirm_stays_range(self):
        """RANGE + 首日越阈（前日未越阈）→ 两侧都停留 range 等待确认。"""
        r_full = self._step_full(FULL.IndexRegime.RANGE, 30.0, 10.0)
        r_gm = self._step_gm(GM.IndexRegime.RANGE, 30.0, 10.0, days=0)
        self.assertEqual((r_full, r_gm), ("range", "range"))

    def test_below_enter_stays_range(self):
        r_full = self._step_full(FULL.IndexRegime.RANGE, 10.0, 9.0)
        r_gm = self._step_gm(GM.IndexRegime.RANGE, 10.0, 9.0, days=0)
        self.assertEqual((r_full, r_gm), ("range", "range"))

    def test_exit_up_on_score_fade(self):
        """uni_up 中 |S| 回穿 exit → 两侧同退 range。"""
        r_full = self._step_full(FULL.IndexRegime.UNI_UP, 10.0, 20.0)
        r_gm = self._step_gm(GM.IndexRegime.UNI_UP, 10.0, 20.0, days=3)
        self.assertEqual((r_full, r_gm), ("range", "range"))

    def test_exit_down_on_score_fade(self):
        """uni_down 中 |S| 回穿 exit → 两侧同退 range（完整版走 R2/阈值路径之一）。"""
        feat = _quiet_feat(streak=-5)
        feat["close"] = 80.0   # close < ma10(90)：完整版阈值退出路径（不触 R2 收复）
        r_full = self._step_full(FULL.IndexRegime.UNI_DOWN, -10.0, -20.0, feat=feat)
        r_gm = self._step_gm(GM.IndexRegime.UNI_DOWN, -10.0, -20.0, days=-3, feat=feat)
        self.assertEqual((r_full, r_gm), ("range", "range"))

    def test_hold_uni_up(self):
        r_full = self._step_full(FULL.IndexRegime.UNI_UP, 30.0, 28.0)
        r_gm = self._step_gm(GM.IndexRegime.UNI_UP, 30.0, 28.0, days=3)
        self.assertEqual((r_full, r_gm), ("uni_up", "uni_up"))

    def test_k_up_low_score_documented_divergence(self):
        """K-up 当日但 S 未达入场阈：完整版要求 S>=enter 才快速入场（停留 range），
        实盘版 K 日强制跃迁不看 S（直接跳 uni_up）——代际语义分歧，钉住现状。"""
        r_full = self._step_full(FULL.IndexRegime.RANGE, 10.0, 5.0, key_day_type="k_up")
        r_gm = self._step_gm(GM.IndexRegime.RANGE, 10.0, 5.0, days=0,
                             key_day={"type": "k_up"})
        self.assertEqual(r_full, "range")
        self.assertEqual(r_gm, "uni_up")


if __name__ == "__main__":
    unittest.main(verbosity=2)
