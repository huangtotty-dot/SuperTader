# -*- coding: utf-8 -*-
"""预取公司简介（东财 F10「核心题材」）→ `t_io/cache/company_profile.json`。

owner 2026-10-17：「点开K线图个股的业务简介太过于简单，请通过查询提前补充」。
本地 `watchlist_jiuyan.json` 的 `business_summary` 只有一句"主营专用设备制造业"
（旧东财 F10 被风控 ⇒ 长期没更新），K线弹窗只能显示那一句。

数据源（实测可用）：
    https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code=SH688499
    → hxtc[] = [{KEYWORD, MAINPOINT_CONTENT}]
      第 1 项 = 经营范围；第 2 项 = **主营业务描述**（关键词就是业务名）；其后是行业/竞争优势。

默认只补「你会点开的那几十只」：持仓（手动+自动 qty>0）∪ 建仓股池（watchlist_buy）。
整池预取太吵（5000+ 次请求），用 `--all` 才做。

用法：
    python -I scripts/fetch_company_profiles.py            # 持仓 + 股池
    python -I scripts/fetch_company_profiles.py --all      # 全韭研池（慢，数百次请求）
    python -I scripts/fetch_company_profiles.py --force    # 忽略 7 天新鲜度，强制重抓
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FP = os.path.join(ROOT, "t_io", "cache", "company_profile.json")
FRESH_DAYS = 7


def _load(fp, default):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def target_codes(all_pool=False):
    codes = set()
    for fp in ("t_io/state/holdings_manual.json", "t_io/state/holdings_auto.json"):
        for c, h in (_load(os.path.join(ROOT, fp), {}) or {}).items():
            if isinstance(h, dict) and int(h.get("qty") or 0) > 0:
                codes.add(str(c).split("_")[0])
    wl = _load(os.path.join(ROOT, "t_io/state/watchlist_buy.json"), {}) or {}
    codes |= {str(c).split("_")[0] for c in (wl.get("stocks") or {})}
    if all_pool:
        jy = _load(os.path.join(ROOT, "stock_hunter/watchlist_jiuyan.json"), {}) or {}
        for c, v in jy.items():
            if not isinstance(v, dict):
                continue
            if (str(v.get("jiuyan_category") or "").strip()
                    or any(str(v.get("jiuyan_category%d" % i) or "").strip() for i in range(1, 10))):
                codes.add(str(c).split("_")[0])
    return sorted(c for c in codes if len(c) == 6 and c.isdigit())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="含全韭研池（慢）")
    ap.add_argument("--force", action="store_true", help="忽略 7 天新鲜度")
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location("tg_fetch", os.path.join(ROOT, "t_gui.py"))
    tg = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(tg)
    except SystemExit:
        pass
    api = tg.Api()

    cache = _load(FP, {}) or {}
    todo = []
    now = time.time()
    for c in target_codes(args.all):
        hit = cache.get(c)
        if hit and not args.force:
            try:
                ts = time.mktime(time.strptime(hit.get("updated_at", ""), "%Y-%m-%d %H:%M:%S"))
                if now - ts < FRESH_DAYS * 86400:
                    continue
            except Exception:
                pass
        todo.append(c)
    print(f"目标 {len(todo)} 只（已有缓存 {len(cache)} 只）")

    ok = fail = 0

    def one(c):
        return c, api._fetch_company_profile(c)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (c, p) in enumerate(ex.map(one, todo), 1):
            if p:
                cache[c] = p
                ok += 1
            else:
                fail += 1
            if i % 20 == 0:
                print(f"  进度 {i}/{len(todo)}（成功 {ok} / 失败 {fail}）", flush=True)
            time.sleep(0.15)          # 轻节流，别把东财惹毛
    os.makedirs(os.path.dirname(FP), exist_ok=True)
    tmp = FP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, FP)
    print(f"完成：成功 {ok} · 失败 {fail} · 缓存共 {len(cache)} 只 → {FP}")


if __name__ == "__main__":
    main()
