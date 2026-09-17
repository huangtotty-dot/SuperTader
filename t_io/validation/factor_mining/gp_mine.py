# -*- coding: utf-8 -*-
"""GP 臂：遗传规划搜索**因子表达式**（P2）。

## 接线设计

GP 的标准 API 是「在特征矩阵上做算术组合」，而本项目因子层是「日内时序算子」。
接法：把 22 个基线因子当**原始列**，让 GP 在列上进化算术组合 → 产出**复合因子**，
再用 `eval_factor` 的既有机器（z-score 穿越入场 + 固定出场 + 成本）评它。

**适应度 = 费后净均**（必须含成本；否则 GP 会去优化毛值，产出无法落地的表达式）。

## 两条纪律

1. **适应度只在样本内算**：GP 按构造就是样本内最大化，**必然过拟合**。
   OOS（2026-06-01~08-26）留到最后由人工过闸，不进适应度。
2. **退化程序要压掉**：密度 < 0.3 的程序（常数/几乎不穿越）给大惩罚，
   否则 GP 会去钻"几乎不交易 → 无成本 → 净均≈0"的空子。

用法：
  python gp_mine.py --codes <逗号> --pop 100 --gens 10 [--oracle-col]
  --oracle-col : 在特征矩阵里**额外加一列 zz_oracle**（阳性对照），
                 用来验证「GP 能在有明确优势信号时找到它」。找不到 = 接线坏了。
"""
import argparse
import glob
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 't0_schemes'),
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t'), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_factor as E  # noqa: E402
import factor_lib as fl  # noqa: E402

MIN_DENSITY = 0.3
PENALTY = -10.0


def build(codes, oracle_col=False):
    """→ (X, layout, names)。X 每行 = 一个 bar 的原始列；

    layout 每票一项 (start, end, panel, keep, labels, L)，供 `_score` 直接 reshape ——
    避免把 16 万行还原成嵌套 dict（那是 Python 循环，慢且易错）。
    """
    rows, layout = [], []
    for c in codes:
        p = E.load_code(c)
        if not p:
            continue
        keep, labels, L, _nd = E.aligned_matrix(p)
        if not keep:
            continue
        cols = {}
        for fn in list(fl.FACTORS) + (['zz_oracle'] if oracle_col else []):
            if fn == 'tod_bias':            # 需跨日同刻历史，此处略（基线已单独测过）
                continue
            fv = E.compute_factor(p, fn)
            cols[fn] = fv
        names = sorted(cols)
        s = len(rows)
        for d in keep:
            for i in range(L):
                rows.append([float(np.nan_to_num(cols[k][d][i], nan=0.0, posinf=0.0, neginf=0.0))
                             for k in names])
        layout.append((s, len(rows), p, keep, labels, L))
    return np.asarray(rows, float), layout, names


# 模块级状态：make_fitness 的 metric 需**可 pickle**（gplearn 可能包装），
# 故不把大矩阵当闭包捕获，改用模块级引用。
_STATE = {}


def _score(raw):
    """程序输出的扁平因子值 → 费后净均（含成本）；退化程序给 PENALTY。取双向更优者。

    走 `legs_hold_matrix`（向量化，已与慢路径交叉验证 |Δ|=0）——GP 需上千次评估。
    """
    if raw is None:
        return PENALTY
    raw = np.asarray(raw, float).ravel()
    # gplearn 的 make_fitness 会用 np.array([1,1]) 试调一次做类型校验 —— 长度不符时兜住。
    if raw.shape[0] != _STATE.get('n_rows', -1) or not np.all(np.isfinite(raw)):
        return PENALTY
    best = PENALTY
    layout = _STATE['layout']
    tot_days = sum(len(kp) for (_s, _e, _p, kp, _l, _L) in layout)
    for sign in (1, -1):
        legs = []
        for s, e, panel, keep, labels, L in layout:
            M = raw[s:e].reshape(len(keep), L)
            legs.extend(E.legs_hold_matrix(panel, M, sign, keep, labels, L))
        if not legs:
            continue
        if len(legs) / max(tot_days, 1) < MIN_DENSITY:
            continue
        net = float(np.mean([x['net'] for x in legs]))
        if net > best:
            best = net
    return best


def _metric(y, y_pred, w):
    """gplearn 的自定义适应度入口（签名由库固定）。"""
    return _score(y_pred)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--pop', type=int, default=100)
    ap.add_argument('--gens', type=int, default=10)
    ap.add_argument('--parsimony', type=float, default=0.001)
    ap.add_argument('--oracle-col', action='store_true',
                    help='特征矩阵里加入阳性对照列（验证接线是否有效）')
    ap.add_argument('--out', default=os.path.join(HERE, 'gp_ledger.jsonl'))
    args = ap.parse_args()

    from gplearn.genetic import SymbolicRegressor
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(E.v2.CSV_DIR, '*_1year_1min.csv'))})[:8]
    E.v2.END = E.WIN_END
    t0 = time.time()
    X, layout, names = build(codes, oracle_col=args.oracle_col)
    print(f'[gp] codes={len(layout)} 行数={X.shape[0]:,} 列={X.shape[1]} '
          f'({len(names)} 个原始因子{"+oracle" if args.oracle_col else ""})')
    print(f'[gp] 列名: {names}')
    _STATE.update({'layout': layout, 'exit': 'hold', 'n_rows': X.shape[0]})
    from gplearn.fitness import make_fitness
    metric = make_fitness(function=_metric, greater_is_better=True)
    y = np.zeros(X.shape[0])          # GP 需要一个 y；本接法里 y 不参与，适应度看 y_pred
    est = SymbolicRegressor(population_size=args.pop, generations=args.gens,
                            function_set=('add', 'sub', 'mul', 'div'),
                            parsimony_coefficient=args.parsimony,
                            metric=metric, n_jobs=1,
                            random_state=20260917, verbose=1,
                            feature_names=names)
    est.fit(X, y)
    print(f'\n[gp] 最优程序: {est._program}')
    print(f'[gp] 程序适应度(费后净均%) = {est._program.fitness_:.4f}')
    print(f'[gp] 用时 {time.time() - t0:.0f}s')
    rec = {'codes': codes, 'oracle_col': args.oracle_col, 'pop': args.pop, 'gens': args.gens,
           'program': str(est._program), 'fitness': round(float(est._program.fitness_), 4)}
    with open(args.out, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'[gp] -> {args.out}')


if __name__ == '__main__':
    main()
