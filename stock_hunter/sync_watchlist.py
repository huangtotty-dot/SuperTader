# -*- coding: utf-8 -*-
"""watchlist_jiuyan.json 刷新脚本（2026-09-29）。

**为什么需要它**：该文件在仓库里**没有任何写入方**——是外部生成后拷进来的
（`.gitignore:20` 还把它忽略了，改动不入库）。后果是它会自然腐坏：实测 4000/5041 条
停在 2026-05-30，4 个月里的新股进不来。本脚本给出**可复用**的补数机制，取代一次性打补丁。

**做什么**：
  1. 取全 A 代码表（akshare `stock_info_a_code_name`）
  2. `缺失 = 全量 − 现有`，按 owner 2026-09-29 口径过滤：
       - **纳入** 北交所（`4xxxxx` / `8xxxxx` / `920xxx`）
       - **排除** ST / *ST / 退市（涨跌幅 5%，与突破判定的 0.3~8% 口径不可比）
  3. 新条目回填 `name` / `sector` / `sector_type` / `primary_source` / `updated_at`
  4. 写前自动备份为 `watchlist_jiuyan.backup_YYYY-MM-DD.json`
  5. **幂等**：重跑只补差集，**绝不覆盖已有条目的既有字段**

**用法**：
    python stock_hunter/sync_watchlist.py             # 干跑（默认），只打印将新增什么
    python stock_hunter/sync_watchlist.py --apply     # 真写（先备份）

⚠️ 该文件被 `.gitignore` 忽略 ⇒ 改动**不入库**，换机器/重装需重跑本脚本。
⚠️ `business_summary`（主营一句话）**本脚本不回填**：唯一候选的东财 F10 接口在本机
   被风控（`stock/get`/`clist` 实测 RemoteDisconnected），故留空——「公司资料」按钮的
   实时层（东财所属板块）仍能给出行业/概念。宁可不填，不填错的。
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

WATCHLIST = os.path.join(_HERE, "watchlist_jiuyan.json")
SOURCE_TAG = "sync_20260929"
_ST_KEYWORDS = ("ST", "*ST", "退")
_EM_RETRIES = 8


# ---------- 纯函数（离线可测） ----------
def is_bj(code: str) -> bool:
    """北交所：4xxxxx / 8xxxxx / 920xxx。走 codec，避免各处重复实现前缀规则。"""
    from core.market_data.codec import market_of
    return market_of(code) == "BJ"


def is_excluded_name(name: str) -> bool:
    """ST / *ST / 退市 —— 排除（涨跌幅 5%，与突破判定口径不可比）。"""
    n = str(name or "").upper().replace(" ", "")
    return any(k.upper() in n for k in _ST_KEYWORDS)


def missing_codes(universe: dict, have: dict) -> list:
    """缺失 = 全量 − 现有，再按口径过滤。返回排序后的代码列表。

    纳入北交所；排除 ST/*ST/退。注意 `have` 里已有的条目**一律不动**——
    即使它现在叫 ST（历史存在就保留，本脚本只做增量）。
    """
    out = []
    for code, name in universe.items():
        c = str(code).zfill(6)
        if c in have:
            continue
        if is_excluded_name(name) and not is_bj(c):
            continue
        out.append(c)
    return sorted(out)


def merge_new(store: dict, universe: dict, codes: list, sectors: dict, now: str) -> int:
    """把 codes 合入 store（就地）。已存在的键**不覆盖**任何已有字段。返回新增条数。"""
    added = 0
    for c in codes:
        if c in store:
            continue
        store[c] = {
            "name": universe.get(c, c),
            "business_summary": "",           # 见模块 docstring：东财 F10 被风控，宁可留空
            "concept_boards": [],
            "industry_boards": [],
            "jiuyan_category": "",
            "jiuyan_concept": "",
            "sector": sectors.get(c, ""),
            "sector_type": SOURCE_TAG,
            "primary_source": SOURCE_TAG,
            "updated_at": now,
        }
        added += 1
    return added


def backup_path(today: str) -> str:
    return os.path.join(_HERE, f"watchlist_jiuyan.backup_{today}.json")


# ---------- 网络（仅 main 调用） ----------
def fetch_universe() -> dict:
    """全 A 代码表 → {6位码: 名称}。"""
    import akshare as ak
    df = ak.stock_info_a_code_name()
    return {str(r[0]).zfill(6): str(r[1]) for r in df.itertuples(index=False)}


def fetch_sector(code: str) -> str:
    """东财「所属板块」→ `/` 拼接串；取不到返回 ""。

    只用 `push2.eastmoney.com/api/qt/slist/get`：实测本机 `push2his`/`clist`/`stock/get`
    均被风控，只有 `slist/get` 稳定。8 次重试（仓库既有对策）。
    """
    from core.market_data.codec import market_of
    secid = ("1." if market_of(code) == "SH" else "0.") + code
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    url = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
           f"&fields=f12,f14&secid={secid}&pn=1&np=1&pz=100")
    for _ in range(_EM_RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            data = (json.loads(urllib.request.urlopen(req, timeout=6).read()
                               .decode("utf-8", errors="ignore")).get("data") or {})
            names = [str(x.get("f14")).strip() for x in (data.get("diff") or [])
                     if str(x.get("f14") or "").strip()]
            # ⚠️ 拿到**响应**就返回——哪怕 diff 为空。重试只针对网络/风控**失败**：
            # 实测北交所 4xxxxx/8xxxxx 段是"200 + 空 diff"（不是异常），
            # 若无条件重试会变成每只 8 次空转（300 只 ≈ 50 分钟）。
            return "/".join(names)
        except Exception:
            time.sleep(0.6)
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="刷新 watchlist_jiuyan.json（增量补代码）")
    ap.add_argument("--apply", action="store_true", help="真写（缺省只干跑打印）")
    ap.add_argument("--no-sector", action="store_true", help="跳过东财板块回填（更快、无网络）")
    args = ap.parse_args(argv)

    if not os.path.exists(WATCHLIST):
        print(f"[ERR] 找不到 {WATCHLIST}")
        return 2
    with open(WATCHLIST, encoding="utf-8") as fh:
        store = json.load(fh)
    before = len(store)
    print(f"现有 {before} 条")

    try:
        universe = fetch_universe()
    except Exception as e:
        print(f"[ERR] 取全 A 代码表失败: {type(e).__name__}: {e}")
        return 2
    print(f"全 A {len(universe)} 条")

    codes = missing_codes(universe, store)
    bj = [c for c in codes if is_bj(c)]
    print(f"待补 {len(codes)} 条（其中北交所 {len(bj)}）")
    for c in codes[:15]:
        print(f"   + {c} {universe.get(c)}" + ("  [北交所]" if is_bj(c) else ""))
    if len(codes) > 15:
        print(f"   ... 其余 {len(codes) - 15} 条")

    if not codes:
        print("无需补数（已是最新）")
        return 0
    if not args.apply:
        print("\n[干跑] 未写入。确认无误后加 --apply")
        return 0

    sectors = {}
    if not args.no_sector:
        print(f"\n回填东财板块（{len(codes)} 只，每只最多 8 次重试）…")
        for i, c in enumerate(codes, 1):
            sectors[c] = fetch_sector(c)
            if i % 25 == 0 or i == len(codes):
                print(f"   {i}/{len(codes)}（已取到 {sum(1 for v in sectors.values() if v)}）")

    today = datetime.now().strftime("%Y-%m-%d")
    bk = backup_path(today)
    shutil.copy2(WATCHLIST, bk)
    print(f"\n已备份 → {os.path.basename(bk)}")

    added = merge_new(store, universe, codes, sectors, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(WATCHLIST, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=1)
    print(f"写入完成：{before} → {len(store)}（新增 {added}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
