# -*- coding: utf-8 -*-
"""券商「历史成交 / 交割单」解析 + 逐股盈亏·做T 台账（2026-10-10，owner 需求3）。

**输入统一为券商导出的「历史成交」（xls/xlsx）**，可一次给多份、多账户；PDF 交割单路径保留
作为兼容（老数据只有 PDF 时用）。`build_from_files` 是入口。

输入适配（`parse_table_rows` 一律**表头驱动**按列名取数，券商列序/列名不一致也不怕）：
- **.xlsx** → openpyxl；**.xls** → xlrd（按文件魔数判别，不看扩展名）。
- **伪 xls**（国内券商常见：`<table>` 拼的 HTML，扩展名却是 .xls）→ bs4 抽表，GBK/UTF-8 都试。
- **PDF** → pypdf 抽文本。交割单 PDF 是**两张表分页铺开**的：
  前一半页 = 成交明细 `成交日期 证券代码 证券名称 操作 成交数量 成交均价 成交金额 可用余额 发生金额`，
  后一半页 = 费用/资金 `印花税 其他杂费 资金余额 合同编号 佣金 过户费 结算费`；
  两张表**行序一一对应**，据此按下标 join 补齐费用明细与资金余额。
- 历史成交常见的「缺列」都容错：没有 `成交金额` 列 → 按 价×量 补算；没有 `发生金额/余额` 列 →
  成本取 `成交金额±费用`；方向写法 `证券买入/买入/B/1` 与 `卖出/S/2` 都认。

**多份/多账户**：`merge_trades` 在**成交行**层面按内容去重后合并（重叠区间同一笔会在两份单里
各出现一次）；账本按 **(资金账号, 代码)** 分组——两个账户同持一只票不会混进同一个加权平均成本。

口径：
- 已实现盈亏 = 按 (账户,代码) 的**移动加权平均成本**，收入/成本取 `发生金额`（含着各项费用），
  故「全部平仓」时已实现 = 净现金流。
- **做T收益** = 同一账户 + 同一股票 + 同一交易日既买又卖 ⇒ 对冲 `min(当日买量, 当日卖量)`，
  价差取当日**净额均价**（买入含费/卖出扣费），是扣完手续费的净价差。日粒度近似，不区分批次。
  两个必须的排除：`成交数量==0` 的杂项行（纯费用/退补款，单列 `misc`）、**非资金过户行**
  （红股/转增/份额折算：数量变了但成交金额=0，实测会把一天虚增成 3 万元做T）。
- `成交数量==0` 行的「发生金额」计入 `misc`，手续费照计（`total_fees` 覆盖全部成交，含被剔除的
  逆回购），因为「交易总费用」要的是真实现金支出。

纯函数 + 纯磁盘，不联网，可离线单测。
"""
import json
import os
import re

_HEAD_LEFT = ("成交日期", "证券代码", "证券名称")
_HEAD_RIGHT = ("印花税", "其他杂费", "资金余额")
_MARKER = re.compile(r"^=+\s*PAGE\s*\d+\s*=+$", re.I)
# 国债逆回购（深 1318xx / 沪 2040xx）在交割单里买卖方向与股票相反
# （「买入」是资金回款、发生金额为正），按股票口径算会得出 -200% 的假亏损 ⇒ 单列剔除。
_REPO = re.compile(r"^(?:[ＲR]-?\d{3}|GC\d{3})$")

# 表头 → 字段（**顺序即认领优先级**：越靠前的字段先抢列名，避免「成交金额」被「金额」类泛匹配吃掉）。
# 别名按长度降序匹配，长名优先（"成交均价" 先于 "成交价"）。
_COL_ALIASES = (
    # account 必须排在 code 前面：历史成交表里常同时有「股东代码」和「证券代码」，
    # 若让 code 先认领，它会把「股东代码」用泛别名「代码」吃掉。
    ("account", ("资金账号", "资金账户", "资金帐号", "股东帐户", "股东账户", "股东账号",
                 "证券账号", "股东代码", "客户号", "账户", "帐户", "帐号")),
    ("date",    ("成交日期", "发生日期", "交易日期", "清算日期", "日期")),
    ("time",    ("成交时间", "委托时间")),
    ("code",    ("证券代码", "股票代码", "证券编码", "代码")),
    ("name",    ("证券名称", "股票名称", "证券简称", "名称")),
    ("op",      ("操作", "买卖标志", "买卖方向", "委托方向", "交易类别", "业务名称")),
    ("qty",     ("成交数量", "成交股数", "成交笔数", "数量")),
    ("price",   ("成交均价", "成交价格", "成交价", "价格")),
    ("amount",  ("成交金额", "成交额")),
    ("avail",   ("可用余额", "证券余额", "股份余额", "股票余额", "持仓余额")),
    ("occur",   ("发生金额", "发生额", "本次金额", "资金发生")),
    ("cash",    ("资金余额", "现金余额", "余额")),
    ("contract", ("合同编号", "委托编号", "合同序号", "委托序号")),
)
_FEE_ALIASES = (
    ("stamp",     ("印花税",)),
    ("commission", ("佣金", "手续费")),
    ("transfer",  ("过户费",)),
    ("settle",    ("结算费",)),
    ("other",     ("其他杂费", "其他费用", "附加费")),
)
_HEAD_HINT = ("成交日期", "证券代码", "发生日期", "股票代码", "操作")


def _claim(header, used, aliases):
    """在未认领的列里挑最匹配的一列：**别名越长越优先**，同长取更靠左。"""
    best = None                                # ((别名长度, -列下标), 列下标)
    for a in aliases:
        for i, h in enumerate(header):
            if i in used or a not in h:
                continue
            cand = (len(a), -i)
            if best is None or cand > best[0]:
                best = (cand, i)
    return best[1] if best else None


def _map_columns(header):
    """表头行 → {字段: 列下标}；未认领的字段不出现。

    用「别名越长越优先」而不是「从左到右先到先得」：真实历史成交里同时有「余额」、
    「资金余额」和「后证券余额」，先到先得会把资金余额错认成持仓余额。
    """
    used, idx = set(), {}
    for field, aliases in _COL_ALIASES + _FEE_ALIASES:
        hit = _claim(header, used, aliases)
        if hit is not None:
            idx[field] = hit
            used.add(hit)
    return idx


def _find_header(rows):
    """在前 40 行里找表头行下标（同时含日期列与代码列的才是）。

    券商导出常在前面垫「账户信息/查询条件/标题」若干行，所以给足窗口。
    """
    for i, r in enumerate(rows[:40]):
        joined = "".join(str(c) for c in r)
        if sum(1 for h in _HEAD_HINT if h in joined) >= 2:
            return i
    return -1



def _f(x, default=0.0):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return default


def _i(x, default=0):
    try:
        return int(float(str(x).replace(",", "")))
    except Exception:
        return default


def _is_num(x):
    try:
        float(str(x).replace(",", ""))
        return True
    except Exception:
        return False


def _code(raw):
    """券商导出会把前导零吃掉（`2176`→`002176`、`32`→`000032`）；可转债/回购保持 6 位。"""
    s = str(raw).strip()
    if not s.isdigit():
        return s
    return s.zfill(6)


def _date(raw):
    """`20261009` / `2026-10-09` / `2026/1/9` / `2026-10-09 00:00:00` → `YYYY-MM-DD`。

    xlsx 里日期常是 datetime，openpyxl 会给出 `2026-10-09 00:00:00`；券商导出又常写成
    `20261009` 或 `2026/1/9`。三种都要吃掉，否则日期字符串没法比较/排序。
    """
    s = str(raw).strip().split(" ")[0]
    if not s:
        return s
    if len(s) == 8 and s.isdigit():                    # 20261009
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    parts = re.split(r"[-/.]", s)
    if len(parts) >= 3 and len(parts[0]) == 4 and all(p.isdigit() for p in parts[:3]):
        return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return s


def parse_table_rows(rows):
    """二维单元格（xls/xlsx/HTML 表）→ 逐笔成交 dict 列表（表头驱动，按日期升序）。

    与 `parse_rows`（PDF 定长文本）的区别：这里**按列名认领**，所以券商换列序、多加
    「资金余额/证券余额/委托编号」等列都不影响；费用五列若与成交同行出现则直接取用。
    """
    rows = [[("" if c is None else str(c).strip()) for c in r] for r in rows]
    hi = _find_header(rows)
    if hi < 0:
        return []
    col = _map_columns(rows[hi])
    if "date" not in col or "code" not in col or "op" not in col:
        return []
    trades = []
    for r in rows[hi + 1:]:
        def cell(f):
            i = col.get(f)
            return r[i] if (i is not None and i < len(r)) else ""
        code_raw = cell("code")
        if not str(code_raw).strip():
            continue
        # 方向：历史成交里写法五花八门——证券买入/买入/B/1、证券卖出/卖出/S/2
        op_raw = str(cell("op")).strip()
        up = op_raw.upper()
        if "买" in op_raw or up in ("B", "1", "BUY"):
            op = "买入"
        elif "卖" in op_raw or up in ("S", "2", "SELL"):
            op = "卖出"
        else:
            continue
        # ⚠️ 有的券商把**方向写进数量**：卖出记成 -100。数量一律取绝对值，方向只认 op 列——
        # 否则 `qty < 0` 的过滤会把每一条卖出都悄悄丢掉（实测踩过）。
        qty = abs(_i(cell("qty")))
        price = _f(cell("price"))
        amount = abs(_f(cell("amount")))
        # 历史成交常**没有成交金额列**（只有数量×价格）⇒ 补算。注意只在**整列缺失**时补：
        # 若列存在但单元格是 0，那是「非资金过户」（红股/份额折算），必须以 0 为准。
        if "amount" not in col and qty and price:
            amount = round(price * qty, 2)
        fees = {}
        for f, _ in _FEE_ALIASES:
            if f in col:
                fees[f] = _f(cell(f))
        fee_total = round(sum(fees.values()), 2) if fees else None
        t = {
            "date": _date(cell("date")),
            "time": str(cell("time")).strip() if "time" in col else "",
            "account": str(cell("account")).strip() if "account" in col else "",
            "code": _code(cell("code")),
            "name": str(cell("name")).strip(),
            "op": op,
            "qty": qty,
            "price": price,
            "amount": amount,
            "avail": _i(cell("avail")) if "avail" in col else None,
            "occur": _f(cell("occur")) if "occur" in col else 0.0,
            "fees": fees or None,
            "fee_total": fee_total,
            "cash_balance": _f(cell("cash")) if "cash" in col else None,
            "contract": str(cell("contract")).strip() or None,
        }
        if not t["date"]:
            continue
        # 零信息行（数量/金额/发生金额全 0）：新股**申购配号**、登记指定之类的占位记录。
        # 不是成交，也不带任何金额，留着只会在逐股表里多出一个「股票」（实测：马矿配号）。
        if t["qty"] == 0 and t["amount"] == 0 and not t["occur"]:
            continue
        if t["occur"]:
            t["net"] = t["occur"]
        else:
            f = t["fee_total"] or 0.0
            base = t["amount"]
            # 只有**整列缺失**成交金额时才按 价×量 重建。若列在、值就是 0（红股/份额折算），
            # 必须以 0 为准——否则会凭空给这次过户记上 价×量 的成本。
            # 实测：588170 一笔 36000 股@3.673 的折算行被记了 132,228 元假成本，
            # 该票已实现从 +4,117 变成 -128,110。
            if not base and "amount" not in col:
                base = t["price"] * t["qty"]
            t["net"] = (base - f) if op == "卖出" else -(base + f)
        # 非资金过户（红股入账/转增/份额折算）：改了数量但成交金额=0，不是真成交
        t["non_trade"] = bool(t["qty"] > 0 and t["amount"] == 0)
        trades.append(t)
    trades.sort(key=lambda x: x["date"])
    return trades



def parse_rows(lines):
    """文本行 → 逐笔成交 dict 列表（按成交日期**升序**，便于直接跑台账）。

    每笔：`{date, code, name, op, qty, price, amount, avail, occur, fees{...},
    fee_total, cash_balance, contract}`。缺 7 列表时 `fees` 为 None、`cash_balance` 为 None。
    """
    left, right = [], []
    for raw in lines:
        line = str(raw).strip()
        if not line or _MARKER.match(line):
            continue
        p = line.split()
        if len(p) == 9 and p[0] == _HEAD_LEFT[0]:
            continue
        if len(p) == 7 and p[0] == _HEAD_RIGHT[0]:
            continue
        # 用列语义校验，避免把页脚/乱码行误当成交（pypdf 抽文偶有粘连）
        if len(p) == 9 and p[3] in ("买入", "卖出") and _is_num(p[4]) and _is_num(p[6]):
            left.append(p)
        elif len(p) == 7 and _is_num(p[2]) and _is_num(p[6]):
            right.append(p)

    trades = []
    for idx, p in enumerate(left):
        date_raw, code_raw, name, op, qty, avg, amt, avail, occur = p
        t = {
            "date": _date(date_raw),
            "time": "",
            "account": "",                     # PDF 交割单是单一账户导出，无账户列
            "code": _code(code_raw),
            "name": name,
            "op": op,
            "qty": _i(qty),
            "price": _f(avg),
            "amount": _f(amt),
            "avail": _i(avail),
            "occur": _f(occur),
            "fees": None,
            "fee_total": None,
            "cash_balance": None,
            "contract": None,
        }
        if idx < len(right):
            r = right[idx]
            stamp, other, cash_bal, contract, comm, transfer, settle = r
            fees = {
                "stamp": _f(stamp), "other": _f(other), "commission": _f(comm),
                "transfer": _f(transfer), "settle": _f(settle),
            }
            t["fees"] = fees
            t["fee_total"] = round(sum(fees.values()), 2)
            t["cash_balance"] = _f(cash_bal)
            t["contract"] = contract
        # 成本/收入：优先用发生金额（含费），缺失时退回 成交金额±费用
        if t["occur"]:
            t["net"] = t["occur"]
        else:
            f = t["fee_total"] or 0.0
            t["net"] = (t["amount"] - f) if op == "卖出" else -(t["amount"] + f)
        t["non_trade"] = bool(t["qty"] > 0 and t["amount"] == 0)
        trades.append(t)
    # 交割单跨日是**降序**（最新在前），但日**内**已是成交先后顺序 ⇒ 只能按日稳定排序，
    # 否则同日买卖腿会被打乱、加权平均成本算错。
    trades.sort(key=lambda x: x["date"])
    return trades


def daily_last_close(code):
    """日线缓存（t_io/cache/daily_kline）里的最新收盘价；零网络，无缓存 → 0.0。

    用来给**未平仓**持仓算浮动盈亏——同花顺的「盈亏」是**总盈亏（已实现+浮动）**，
    只比已实现会对不上（实测：科创半导 已实现 +4,118 + 浮亏 −12,898 = −8,780.55 = 同花顺值）。
    """
    try:
        from core.position_builder import _DAILY_CACHE_DIR
        rows = json.loads(
            (_DAILY_CACHE_DIR / f"{code}.json").read_text(encoding="utf-8")).get("rows") or []
        if rows:
            return float(rows[-1].get("close") or 0)
    except Exception:
        pass
    return 0.0


_CLOSE_MEMO = {}


def daily_close_on(code, date):
    """该 code 在 date（含）之前的最后收盘价；零网络。供「逐日持仓市值」用。"""
    m = _CLOSE_MEMO.get(code)
    if m is None:
        m = {}
        try:
            from core.position_builder import _DAILY_CACHE_DIR
            rows = json.loads(
                (_DAILY_CACHE_DIR / f"{code}.json").read_text(encoding="utf-8")).get("rows") or []
            for r in rows:
                d0 = str(r.get("date") or "")[:10]
                if d0:
                    m[d0] = float(r.get("close") or 0)
        except Exception:
            pass
        _CLOSE_MEMO[code] = m
    if not m:
        return 0.0
    if date in m:
        return m[date]
    prior = [d0 for d0 in m if d0 <= date]
    return m[max(prior)] if prior else m[min(m)]


def build_ledger(trades, price_fn=None):
    """逐笔成交 → 逐股盈亏台账。

    返回 `{stocks, accounts, monthly, cum, t_monthly, t_stocks, total_*, range, parse_warnings}`。
    `stocks[]` 按已实现降序，含 `realized/unrealized/total_pnl/buy_amt/sell_amt/fees/misc/
    n_trades/open_qty/cost_basis/pnl_pct/first/last`。

    `price_fn(code) -> 最新收盘价`（可选）：给了就为未平仓持仓算 `unrealized` 与 `total_pnl`，
    口径同券商「持仓市值 − 成本」。
    """
    books = {}
    warnings = []
    avail_checks = []                           # [(表内余额, 推算持仓, date, code)]
    excluded = {}
    by_date, by_month = {}, {}
    by_date_buy = {}                            # (账户,日) -> 当日买入金额（收益率趋势的分母）
    by_day_t = {}                               # (账户,代码,日) -> 当日买卖量额，用于算做T
    total_fees = [0.0]                          # 累加器（闭包内可变）
    fee_breakdown = {"stamp": 0.0, "commission": 0.0, "transfer": 0.0,
                     "settle": 0.0, "other": 0.0}
    for t in trades:
        code = t["code"]
        acc = t.get("account") or ""
        # 手续费按「月」聚合：覆盖**全部**成交（含逆回购、含 qty=0 的纯费用行），
        # 因为「交易总费用」要的是真实搬出去的现金，不受盈亏口径的剔除影响。
        mo = t["date"][:7]
        m = by_month.setdefault((acc, mo), {"realized": 0.0, "fees": 0.0, "n_trades": 0})
        m["n_trades"] += 1
        if t["fee_total"]:
            m["fees"] += t["fee_total"]
            total_fees[0] += t["fee_total"]
            fb = t["fees"] or {}
            for k in fee_breakdown:
                fee_breakdown[k] += float(fb.get(k) or 0)

        if _REPO.match(str(t["name"]).strip()):
            e = excluded.setdefault(code, {"code": code, "name": t["name"], "net": 0.0,
                                           "n_trades": 0})
            e["net"] += t["net"]
            e["n_trades"] += 1
            continue
        # 账本按 **(账户, 代码)** 分组：多账户同持一只票时不能混进同一个加权平均成本
        bkey = f"{acc}|{code}" if acc else code
        b = books.setdefault(bkey, {
            "code": code, "account": acc, "name": t["name"], "qty": 0, "cost": 0.0,
            "buy_amt": 0.0, "sell_amt": 0.0, "fees": 0.0, "misc": 0.0,
            "realized": 0.0, "n_trades": 0, "first": t["date"], "last": t["date"],
            "orphan": False,
        })
        b["name"] = t["name"] or b["name"]
        b["last"] = t["date"]
        b["n_trades"] += 1
        if t["fee_total"]:
            b["fees"] += t["fee_total"]

        if t["qty"] == 0:
            # 纯费用/退补款行：不动仓位，单列 misc（真实现金收支）
            b["misc"] += t["net"]
            continue

        # 做T 记账：同一账户同一股票**同一交易日**内的买卖配对（见 t_monthly 口径说明）。
        # 非资金过户行（红股/份额折算，成交金额=0）不是成交，不进 T 统计。
        if not t.get("non_trade"):
            dk = (acc, code, t["date"])
            dt = by_day_t.setdefault(dk, {"bq": 0, "ba": 0.0, "sq": 0, "sa": 0.0})
            if t["op"] == "买入":
                dt["bq"] += t["qty"]
                dt["ba"] += abs(t["net"])
            elif t["op"] == "卖出":
                dt["sq"] += t["qty"]
                dt["sa"] += t["net"]

        if t["op"] == "买入":
            b["qty"] += t["qty"]
            b["cost"] += abs(t["net"])
            b["buy_amt"] += abs(t["net"])
            _bbk = (acc, t["date"])
            by_date_buy[_bbk] = by_date_buy.get(_bbk, 0.0) + abs(t["net"])
        elif t["op"] == "卖出":
            avg = (b["cost"] / b["qty"]) if b["qty"] else 0.0
            matched = min(t["qty"], b["qty"])
            # 卖出腿**没有对应买入腿**（持仓在导出窗口之前建的）：成本未知，绝不能把卖出款
            # 当成纯利润——那等于把浮盈记成已实现。实测东莞账户三花智控首笔就是
            # 「2025-10-10 卖出 200@48.5」无买腿，凭空多出 9,690 元已实现。
            # 只按**配对到的那部分**计提价差；对不上的部分不产生已实现，并标记 orphan。
            per = (t["net"] / t["qty"]) if t["qty"] else 0.0
            if matched < t["qty"]:
                b["orphan"] = True
            rl = matched * (per - avg)
            b["realized"] += rl
            b["qty"] = max(0, b["qty"] - t["qty"])
            b["cost"] = max(0.0, b["cost"] - matched * avg)
            if b["qty"] == 0:
                b["cost"] = 0.0
            b["sell_amt"] += t["net"]
            _bdk = (acc, t["date"])
            by_date[_bdk] = by_date.get(_bdk, 0.0) + rl
            m["realized"] += rl
        else:
            warnings.append(f"{t['date']} {code} 未知操作 {t['op']}")

        if t["avail"] is not None:
            avail_checks.append((t["avail"], b["qty"], t["date"], code))

    # 余额对账：只有当「余额」列确实像**股份余额**时才当异常报。有些券商的该列是资金余额
    # ⇒ 样本够大（≥20 行）且不符比例超 30% 时，判定这列不是股份余额，整体不报。
    if avail_checks:
        bad = [c for c in avail_checks if c[0] != c[1]]
        is_share_balance = (len(avail_checks) < 20) or (len(bad) <= 0.3 * len(avail_checks))
        if is_share_balance:
            for a, q, d, c in bad:
                warnings.append(f"{d} {c} 可用余额 {a} ≠ 推算持仓 {q}")

    stocks = []
    for b in books.values():
        unreal, mv = 0.0, 0.0
        if price_fn and b["qty"] > 0:
            px = float(price_fn(b["code"]) or 0)
            if px:
                mv = px * b["qty"]
                unreal = mv - b["cost"]
        stocks.append({
            "code": b["code"], "account": b["account"], "name": b["name"],
            "realized": round(b["realized"], 2),
            "unrealized": round(unreal, 2),
            "total_pnl": round(b["realized"] + unreal, 2),
            "last_price": round(mv / b["qty"], 3) if mv and b["qty"] else None,
            "market_value": round(mv, 2),
            "misc": round(b["misc"], 2),
            "buy_amt": round(b["buy_amt"], 2),
            "sell_amt": round(b["sell_amt"], 2),
            "fees": round(b["fees"], 2),
            "n_trades": b["n_trades"],
            "open_qty": b["qty"],
            "orphan": bool(b.get("orphan")),      # 含无买腿的卖出 ⇒ 该票已实现偏低，仅供参考
            "cost_basis": round(b["cost"], 2),
            "pnl_pct": round(b["realized"] / b["buy_amt"] * 100, 2) if b["buy_amt"] else 0.0,
            "first": b["first"], "last": b["last"],
        })
    stocks.sort(key=lambda s: -s["realized"])

    # 月度聚合：realized 只统计卖出腿，fees/n_trades 统计该月全部成交
    monthly = [{"account": a, "month": k, "realized": round(v["realized"], 2),
                "fees": round(v["fees"], 2), "n_trades": v["n_trades"]}
               for (a, k), v in sorted(by_month.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    # 逐日累计：**必须按账户各自累计**（早先写成单一 run 是跨账户串起来的，合计曲线会离谱）。
    ser = {}                                   # acc -> {date: (累计已实现, 累计买入)}
    r_acc, b_acc = {}, {}
    for (a, d) in sorted(by_date, key=lambda k: (k[1], k[0])):
        r_acc[a] = r_acc.get(a, 0.0) + by_date[(a, d)]
        b_acc[a] = b_acc.get(a, 0.0) + by_date_buy.get((a, d), 0.0)
        ser.setdefault(a, {})[d] = (r_acc[a], b_acc[a])
    cum = []
    for a, m in ser.items():
        for d, (r, b) in m.items():
            cum.append({"account": a, "date": d, "cum_realized": round(r, 2),
                        "cum_buy": round(b, 2),
                        "pct": round(r / b * 100, 3) if b else 0.0})
    cum.sort(key=lambda e: (e["date"], e["account"]))

    # 两账户**合计**曲线：按日把各账户的累计值相加，某账户当天没成交要**前向填充**（不能当 0）
    all_dates = sorted({d for (_, d) in by_date})
    order = {a: sorted(m) for a, m in ser.items()}
    ptr = {a: 0 for a in ser}
    last = {a: (0.0, 0.0) for a in ser}
    cum_all = []
    for d in all_dates:
        for a in ser:
            while ptr[a] < len(order[a]) and order[a][ptr[a]] <= d:
                last[a] = ser[a][order[a][ptr[a]]]
                ptr[a] += 1
        r = sum(v[0] for v in last.values())
        b = sum(v[1] for v in last.values())
        cum_all.append({"date": d, "cum_realized": round(r, 2), "cum_buy": round(b, 2),
                        "pct": round(r / b * 100, 3) if b else 0.0})

    # ---- 做T 收益 ----
    # 口径：**同一账户 + 同一股票 + 同一交易日**内既买又卖，就认为有一段 T；对冲数量取
    # `min(当日买量, 当日卖量)`，价差用当日**净额口径的均价**（买入含费 / 卖出扣费），
    # 因此结果是扣完手续费的净价差。这是日粒度近似：不区分具体是哪一批股票被卖掉。
    t_month, t_by_stock = {}, {}
    for (acc, code, d), v in by_day_t.items():
        q = min(v["bq"], v["sq"])
        if q <= 0:
            continue
        buy_avg = v["ba"] / v["bq"]
        sell_avg = v["sa"] / v["sq"]
        # 同一天同一只票的买卖均价差一半以上 ⇒ 不是做T，是份额折算/极端数据错位，
        # 硬算会把一天虚增成几万（实测 2026-07-03 588170 拆份额前价格标度差 3 倍）。
        if buy_avg > 0 and abs(sell_avg / buy_avg - 1) > 0.5:
            warnings.append(
                f"{d} {code} 当日买卖均价差 {sell_avg / buy_avg - 1:+.0%}（疑份额折算），"
                f"已跳过该日做T统计")
            continue
        pnl = q * (sell_avg - buy_avg)
        tm = t_month.setdefault((acc, d[:7]), {"realized": 0.0, "n_pairs": 0, "n_days": 0})
        tm["realized"] += pnl
        tm["n_pairs"] += 1
        tm["n_days"] += 1
        sk = f"{acc}|{code}" if acc else code
        ts = t_by_stock.setdefault(sk, {"code": code, "account": acc, "name": "",
                                        "pnl": 0.0, "n_pairs": 0, "qty": 0})
        ts["pnl"] += pnl
        ts["n_pairs"] += 1
        ts["qty"] += q
    _names = {s["code"]: s["name"] for s in stocks}
    for sk, ts in t_by_stock.items():
        ts["name"] = _names.get(ts["code"], ts["code"])
    t_monthly = []
    for (a, k), v in sorted(t_month.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        fees = (by_month.get((a, k)) or {}).get("fees", 0.0)
        t_monthly.append({"account": a, "month": k, "realized": round(v["realized"], 2),
                          "fees": round(fees, 2), "net": round(v["realized"] - fees, 2),
                          "n_pairs": v["n_pairs"], "n_days": v["n_days"]})
    t_stocks = sorted(
        [{"code": v["code"], "account": v["account"], "name": v["name"],
          "pnl": round(v["pnl"], 2), "n_pairs": v["n_pairs"], "qty": v["qty"]}
         for v in t_by_stock.values()],
        key=lambda x: -x["pnl"])
    total_t_pnl = round(sum(x["pnl"] for x in t_stocks), 2)
    # 做T**净**收益 = 做T价差 − 全部交易费用（owner：把手续费扣掉看最后到底赚没赚）
    total_fees_all = round(total_fees[0], 2)
    t_net = round(total_t_pnl - total_fees_all, 2)

    # ---- 按账户汇总 ----
    acc_map = {}
    for s in stocks:
        a = acc_map.setdefault(s["account"], {
            "account": s["account"], "realized": 0.0, "fees": 0.0, "buy_amt": 0.0,
            "n_trades": 0, "n_stocks": 0, "open_qty": 0, "open_cost": 0.0,
            "unrealized": 0.0, "market_value": 0.0})
        a["realized"] += s["realized"]
        a["fees"] += s["fees"]
        a["buy_amt"] += s["buy_amt"]
        a["n_trades"] += s["n_trades"]
        a["n_stocks"] += 1
        a["open_qty"] += s["open_qty"]
        a["open_cost"] += s["cost_basis"]
        a["unrealized"] += s.get("unrealized") or 0.0
        a["market_value"] += s.get("market_value") or 0.0
    for x in t_stocks:
        if x["account"] in acc_map:
            acc_map[x["account"]]["t_pnl"] = round(
                acc_map[x["account"]].get("t_pnl", 0.0) + x["pnl"], 2)
    accounts = []
    for a in acc_map.values():
        a["realized"] = round(a["realized"], 2)
        a["fees"] = round(a["fees"], 2)
        a["buy_amt"] = round(a["buy_amt"], 2)
        a["open_cost"] = round(a["open_cost"], 2)
        a["unrealized"] = round(a["unrealized"], 2)
        a["market_value"] = round(a["market_value"], 2)
        a["total_pnl"] = round(a["realized"] + a["unrealized"], 2)
        a.setdefault("t_pnl", 0.0)
        a["t_net"] = round(a["t_pnl"] - a["fees"], 2)
        a["pnl_pct"] = round(a["realized"] / a["buy_amt"] * 100, 2) if a["buy_amt"] else 0.0
        a["total_pct"] = round(a["total_pnl"] / a["buy_amt"] * 100, 2) if a["buy_amt"] else 0.0
        accounts.append(a)
    accounts.sort(key=lambda x: -x["realized"])

    # ---- 累计总盈亏（含浮动）与月度总盈亏 ----
    # 总盈亏 = 累计现金流(Σ发生金额) + 当日持仓市值。只看已实现会漏掉浮亏：
    # 实测 2026-09 已实现 +3,933，但含浮动是 −17,709（当月大幅建仓，浮亏吃掉了账面盈利）。
    if price_fn:
        grp = {}
        for t in trades:
            grp.setdefault(t["date"], []).append(t)
        pos, occ = {}, {}
        series = {}                                   # acc -> [(date, cum_total)]
        for d in sorted(grp):
            for t in grp[d]:
                a = t.get("account") or ""
                k = (a, t["code"])
                if t["op"] == "买入":
                    pos[k] = pos.get(k, 0) + t["qty"]
                else:
                    pos[k] = max(0, pos.get(k, 0) - t["qty"])
                occ[a] = occ.get(a, 0.0) + t["net"]
            for a in set(occ):
                mv = sum(q * daily_close_on(c, d) for (aa, c), q in pos.items() if aa == a and q > 0)
                series.setdefault(a, []).append((d, occ[a] + mv))
        _tot = {}
        for e in cum:
            a = e["account"]
            s2 = series.get(a) or []
            v = 0.0
            for d, tv in s2:
                if d <= e["date"]:
                    v = tv
                else:
                    break
            e["cum_total"] = round(v, 2)
            _tot[(a, e["date"])] = v
        combo = {}
        for a, s2 in series.items():
            for d, tv in s2:
                combo.setdefault(d, 0.0)
        # 合计：每个日期把各账户的**当日**总盈亏相加（各自前向填充）
        order = {a: series[a] for a in series}
        ptr2 = {a: 0 for a in series}
        lastv = {a: 0.0 for a in series}
        day_tot = {}
        for d in sorted({x for s2 in series.values() for x, _ in s2}):
            for a in series:
                while ptr2[a] < len(order[a]) and order[a][ptr2[a]][0] <= d:
                    lastv[a] = order[a][ptr2[a]][1]
                    ptr2[a] += 1
            day_tot[d] = sum(lastv.values())
        for c in cum_all:
            c["cum_total"] = round(day_tot.get(c["date"], 0.0), 2)
            if c["cum_buy"]:
                c["pct_total"] = round(c["cum_total"] / c["cum_buy"] * 100, 3)
        # 月度总盈亏 = 相邻两点之差
        for lst, key in ((cum_all, None),):
            prev = 0.0
            for c in lst:
                c["delta_total"] = round(c["cum_total"] - prev, 2)
                prev = c["cum_total"]
        # 月度总盈亏 = **各账户各自**相邻月末 cum_total 之差（不能拿合计值写回每一条，
        # 否则前端按账户筛选时会把两账户的合计再加一遍）
        # 月末取**该账户在该月最后一个成交日**的 cum_total（series 覆盖全部成交日，
        # 且已按当日价重估）——不能用 cum（只在「有已实现」的日子才有值，月末会取到月中间）。
        acc_end = {}
        for a, s2 in series.items():
            for d, tv in s2:
                acc_end.setdefault(a, {})[d[:7]] = tv
        for a, ser in acc_end.items():
            ends_a, prev = {}, 0.0
            for k in sorted(ser):
                ends_a[k] = round(ser[k] - prev, 2)
                prev = ser[k]
            for m in monthly:
                if m["account"] == a:
                    m["total_pnl"] = ends_a.get(m["month"])
        for m in monthly:
            m.setdefault("total_pnl", None)

    total_realized = round(sum(s["realized"] for s in stocks), 2)
    total_unrealized = round(sum(s.get("unrealized") or 0.0 for s in stocks), 2)
    total_misc = round(sum(s["misc"] for s in stocks), 2)
    exc = [{"code": e["code"], "name": e["name"], "net": round(e["net"], 2),
            "n_trades": e["n_trades"]} for e in excluded.values()]
    dates = [t["date"] for t in trades]
    return {
        "stocks": stocks,
        "accounts": accounts,
        "monthly": monthly,
        "cum": cum,
        "t_monthly": t_monthly,
        "t_stocks": t_stocks,
        "total_t_pnl": total_t_pnl,
        "cum_all": cum_all,
        "total_t_net": t_net,
        "total_realized": total_realized,
        "total_unrealized": total_unrealized,
        "total_pnl": round(total_realized + total_unrealized, 2),
        "total_misc": total_misc,
        "total_fees": round(total_fees[0], 2),
        "fee_breakdown": {k: round(v, 2) for k, v in fee_breakdown.items()},
        "excluded": exc,
        "total_excluded_net": round(sum(e["net"] for e in exc), 2),
        "range": {
            "start": min(dates) if dates else None,
            "end": max(dates) if dates else None,
            "n_trades": len(trades),
            "n_stocks": len(stocks),
        },
        "parse_warnings": warnings[:50],
    }


def parse_text(text):
    return build_ledger(parse_rows(str(text).splitlines()))


def parse_pdf(path):
    """PDF → 台账。需要 pypdf（见 requirements.txt）。"""
    try:
        from pypdf import PdfReader
    except Exception as e:                     # pragma: no cover - 依赖缺失路径
        raise RuntimeError("缺少 pypdf，无法解析 PDF 交割单：pip install pypdf") from e
    reader = PdfReader(str(path))
    lines = []
    for page in reader.pages:
        try:
            lines.extend((page.extract_text() or "").splitlines())
        except Exception:
            continue
    return build_ledger(parse_rows(lines))


# ---------- xls / xlsx / 伪-xls ----------
# 券商的「xls」实测有五种真身，**扩展名一律不可信**（都叫 .xls）。所以不看扩展名也不赌魔数，
# 而是逐个 reader 试过去，取第一个能解析出成交行的。
#   ① xlsx（zip）            ② 老式 .xls（OLE2/BIFF）      ③ Excel 2003 XML（SpreadsheetML）
#   ④ HTML <table>（伪 xls）  ⑤ 制表符/分号分隔文本

def _decode_text(raw):
    """GBK 系（国内券商主流）与 UTF-8 都试；BOM 也吃掉。"""
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return ""                                  # pragma: no cover


def _read_xlsx(path):
    if open(path, "rb").read(2) != b"PK":      # xlsx 本质是 zip
        return []
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                out.append(["" if c is None else str(c).strip() for c in row])
    finally:
        wb.close()
    return out


def _read_biff_xls(path):
    if open(path, "rb").read(4) != b"\xd0\xcf\x11\xe0":   # OLE2 复合文档
        return []
    import xlrd
    book = xlrd.open_workbook(str(path))
    out = []
    for sh in book.sheets():
        for r in range(sh.nrows):
            out.append([str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)])
    return out


def _read_spreadsheetml(path):
    """Excel 2003 XML（SpreadsheetML）—— 国内券商导「xls」最常见的真身。

    形如 `<?mso-application progid="Excel.Sheet"?><Workbook ...><Worksheet><Table><Row>
    <Cell><Data ss:Type="String">…</Data></Cell>`。注意 `ss:Index` 表示「跳过前面几格」、
    `ss:MergeAcross` 表示横跨几格，都要补空串，否则列会错位。
    """
    raw = open(path, "rb").read()
    head = raw[:4096]
    if b"<Workbook" not in head and b"<ss:Workbook" not in head:
        return []
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_decode_text(raw))
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[:root.tag.index("}") + 1]
    out = []
    for row in root.iter(f"{ns}Row"):
        cells = []
        for c in row.findall(f"{ns}Cell"):
            idx = c.get(f"{ns}Index") or (c.get("Index") if ns else None)
            if idx:
                try:
                    while len(cells) < int(idx) - 1:
                        cells.append("")
                except Exception:
                    pass
            data = c.find(f"{ns}Data")
            cells.append("".join(data.itertext()).strip() if data is not None else "")
            span = c.get(f"{ns}MergeAcross") or (c.get("MergeAcross") if ns else None)
            if span:
                try:
                    cells.extend([""] * int(span))
                except Exception:
                    pass
        if any(cells):
            out.append(cells)
    return out


def _read_html_tables(path):
    """扩展名 .xls、内容其实是 HTML `<table>` 的导出。"""
    raw = open(path, "rb").read()
    head = raw[:4096].lower()
    if b"<table" not in head and b"<html" not in head:
        return []
    text = _decode_text(raw)
    try:
        from bs4 import BeautifulSoup
    except Exception as e:                     # pragma: no cover
        raise RuntimeError("缺少 beautifulsoup4，无法解析 HTML 版成交文件") from e
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")
    out = []
    for tb in soup.find_all("table"):
        for tr in tb.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                out.append(cells)
    return out


def _read_delimited(path):
    """制表符/分号/竖线分隔的文本（有些导出其实就是 TSV 改了个扩展名）。"""
    text = _decode_text(open(path, "rb").read())
    if not text:
        return []
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    head = lines[0]
    delim = max(("\t", ";", ",", "|"), key=lambda d: head.count(d))
    if head.count(delim) < 2:
        return []
    return [l.split(delim) for l in lines]


# (名字, reader) —— 按「常见度」排，命中即停
_READERS = (
    ("xlsx", _read_xlsx),
    ("xls(BIFF)", _read_biff_xls),
    ("Excel2003XML", _read_spreadsheetml),
    ("HTML表格", _read_html_tables),
    ("分隔文本", _read_delimited),
)


def read_excel_rows(path):
    """尽力读出二维表格：逐个 reader 试，返回第一个非空结果（全失败 → []）。"""
    diag = []
    for name, fn in _READERS:
        try:
            rows = fn(path)
        except Exception:
            continue
        if rows:
            return rows
        diag.append(name)
    return []


def sniff_format(path):
    """识别文件真身（供诊断/报错信息用，不影响解析）。"""
    raw = open(path, "rb").read(4096)
    if raw[:2] == b"PK":
        return "xlsx(zip)"
    if raw[:4] == b"\xd0\xcf\x11\xe0":
        return "xls(BIFF/OLE2)"
    low = raw.lower()
    if b"<workbook" in low or b"<ss:workbook" in low:
        return "Excel 2003 XML(SpreadsheetML)"
    if b"<table" in low or b"<html" in low:
        return "HTML 表格"
    return "未知/分隔文本"


def describe_file(path, max_rows=4):
    """给「解析不出来」时用的诊断：真身 + 前几行内容，让用户直接把这段发回来。"""
    lines = [f"格式识别: {sniff_format(path)}"]
    try:
        rows = read_excel_rows(path)
    except Exception as e:
        return " | ".join(lines + [f"读取异常: {type(e).__name__}: {e}"])
    lines.append(f"读到 {len(rows)} 行")
    for i, r in enumerate(rows[:max_rows]):
        lines.append(f"第{i+1}行: " + " | ".join(str(c) for c in r[:8]))
    return " || ".join(lines)


def parse_excel(path):
    """单个 xls/xlsx/伪-xls → 台账。"""
    return build_ledger(parse_table_rows(read_excel_rows(path)))


def parse_any(path):
    """按文件类型分派：pdf → PDF 路径；其余 → 表格路径。"""
    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".pdf":
        return parse_pdf(path)
    return parse_excel(path)


def _merge_key(t):
    # 账户进 key：两个账户同日同价同量的成交不能被当成「重复」互相消掉。
    # （账户缺省时是文件名，见 build_from_files。）
    return (t.get("account") or "", t["date"], t["code"], t["op"], t["qty"],
            t["price"], t["amount"], t["occur"])


def merge_trades(trade_lists):
    """多份文件在**成交行**层面合并：同键保留「各文件出现次数的**最大值**」。

    ⚠️ 不能用「全局去重」。同一份文件内部本来就可能有两笔**完全相同**的成交——一张 20000 股
    的单被撮合成两笔 10000@同价是常态。按集合去重会把其中一笔当重复丢掉，股数凭空少一半、
    加权平均成本全废。实测：588000 少掉一笔 10000@1.871 后，已实现从 **+5,396.60** 变成
    **+24,111.60**（同花顺是 +5,396.60）。

    取「最大出现次数」既能消掉两份文件重叠区间里的重复，又不会误伤同一文件内的真实重复：
    A 有 2 笔、B 有 2 笔（重叠）→ 保留 2 笔；A 有 2 笔、B 有 3 笔 → 保留 3 笔。

    返回 `(trades, dup_dropped)`。
    """
    from collections import Counter
    counts = {}
    for trades in trade_lists:
        for k, n in Counter(_merge_key(t) for t in trades).items():
            counts[k] = max(counts.get(k, 0), n)
    kept, out, dup = {}, [], 0
    for trades in trade_lists:
        for t in trades:
            k = _merge_key(t)
            if kept.get(k, 0) < counts[k]:
                kept[k] = kept.get(k, 0) + 1
                out.append(t)
            else:
                dup += 1
    out.sort(key=lambda x: x["date"])          # 稳定排序：日内顺序保持各文件原样
    return out, dup


def _pdf_lines(path):
    """PDF 逐页抽文本行（普通模式）。"""
    from pypdf import PdfReader
    out = []
    for page in PdfReader(str(path)).pages:
        try:
            out.extend((page.extract_text() or "").splitlines())
        except Exception:
            continue
    return out


# 列名 → 期望的 token 类型，用来判断 PDF 行里「哪一格是空的」
_NUM_COL = ("数量", "均价", "价格", "金额", "发生额", "费用", "费", "税", "佣金", "过户", "余额")
_ID_COL = ("日期", "时间", "代码", "账号", "帐号", "编号", "序号")


def _align_tokens(tokens, header):
    """把一行 token 对齐到表头各列；PDF 里消失的空单元格补成 ""。

    规则（从左到右逐列）：
      · 数值列（数量/均价/金额/费用/税/佣金/余额…）**必须**吃到数字，否则这格是空的；
      · 标识列（日期/代码/账号/编号）一定吃；
      · 其余文本列只在 token 不是纯数字时才吃。
    实测 GT118 交割单每行少两格（「业务备注」为空 + 「过户费」为空），按空格切会让后面
    13 列整体左移——证券账号被当成「账户」、币种当成账户名，盈亏全废。
    """
    if len(tokens) == len(header):
        return list(tokens)
    cells, ti = [], 0
    for h in header:
        if ti >= len(tokens):
            cells.append("")
            continue
        tok = tokens[ti]
        if any(k in h for k in _NUM_COL):        # 数值列：是数字才吃，否则本格为空
            if _is_num(tok):
                cells.append(tok)
                ti += 1
            else:
                cells.append("")
        elif any(k in h for k in _ID_COL):        # 标识列：一定吃
            cells.append(tok)
            ti += 1
        elif _is_num(tok):                        # 文本列遇到纯数字 ⇒ 本格为空
            cells.append("")
        else:
            cells.append(tok)
            ti += 1
    cells.extend(tokens[ti:])                    # 兜底：多出来的接在末尾
    return cells[:len(header)] if len(cells) >= len(header) else cells + [""] * (len(header) - len(cells))


_PDF_ACCT = re.compile(r"资金帐?户\s*[:：]\s*(\S+)")


def _rows_from_pdf(path):
    """交割单 PDF → 成交行（表头驱动 + 空单元格对齐修复）。

    账户名取**页眉里的「资金帐户」**，不用表里的「股东帐户/证券账号」——后者是沪深两个
    股东号（一个资金账户对应两个），拿它分组会把一个账户拆成两个。
    """
    plain = _pdf_lines(path)
    m = _PDF_ACCT.search("\n".join(plain[:40]))
    acct = m.group(1) if m else ""
    rows = [ln.split() for ln in plain]
    hi = _find_header(rows)
    tr = []
    if hi >= 0:
        header = rows[hi]
        fixed = [header] + [_align_tokens(r, header) for r in rows[hi + 1:]]
        tr = parse_table_rows(fixed)
    if not tr:                                  # 兜底：不修复直接给（老式定长排版）
        tr = parse_table_rows(rows) or parse_rows(plain)
    if acct:                                    # 资金账户盖过表里的股东号
        for t in tr:
            t["account"] = acct
    return tr
    # 兜底：普通抽文 + 空格切（表头驱动），再退定长
    plain = _pdf_lines(path)
    tr = parse_table_rows([ln.split() for ln in plain])
    return tr or parse_rows(plain)


def trades_of(path, diag=None):
    """单个文件 → 成交行列表（按日期升序）。

    表格类文件**逐个 reader 试**（扩展名不可信，见 `_READERS`），命中即返回；全试完仍解析不出
    成交行时抛错，错误信息里带**文件真身 + 前几行内容**，方便直接把这段发回来定位。
    `diag` 收下每轮的失败原因列表（可选，供 build_from_files 汇总）。
    """
    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".pdf":
        # 交割单 PDF：各家列序/列名不同（东方「成交日期…」，GT118「发生日期…交易类别…发生数量…」），
        # 且空格抽文会丢掉空单元格 ⇒ 走 _rows_from_pdf 的「按表头字符区间切列」。
        return _rows_from_pdf(path)
    tried = []
    for name, fn in _READERS:
        try:
            rows = fn(path)
        except Exception as e:
            tried.append(f"{name}: {type(e).__name__}")
            continue
        if not rows:
            tried.append(f"{name}: 读不到内容")
            continue
        tr = parse_table_rows(rows)
        if tr:
            return tr
        head = next((r for r in rows[:40]
                     if sum(1 for h in _HEAD_HINT if h in "".join(map(str, r))) >= 2), None)
        tried.append(f"{name}: 读到 {len(rows)} 行、"
                     + (f"表头={[str(c) for c in head[:8]]}" if head else "没找到表头行"))
    if diag is not None:
        diag.append(f"{os.path.basename(str(path))}[{sniff_format(path)}] " + "；".join(tried))
    raise ValueError("解析不出成交行")



def build_from_files(paths, prev_trades=None):
    """**多份**历史成交 → 单一台账（含 `sources` / `dup_dropped` / `parse_warnings` / `trades`）。

    逐个文件只解析一次成交行 → 合并去重 → 统一跑台账；单个文件失败不影响其余。

    `prev_trades`：**上一次已导入的成交行**。券商导出是「每期一份」，日常更新只会导新那几天，
    所以必须把老数据带上再做一次台账——否则只导新文件会把历史冲掉（实测过）。
    合并仍走去重，重叠区间的同一笔只会算一次。
    返回的台账里带 `trades`（合并后的原始成交行），供调用方落盘当下一轮的记忆。
    """
    lists, sources, errors, diag = [], [], [], []
    if prev_trades:
        lists.append(list(prev_trades))
    for p in paths:
        name = os.path.basename(str(p))
        try:
            rows = trades_of(p, diag=diag)
        except Exception:
            errors.append(diag[-1] if diag else f"{name}: 解析失败")
            continue
        if rows:
            # 账户归属：文件里有账户列就用它；**没有就用文件名当账户名**。
            # 券商导出常常不带资金账号（实测两份文件都没有该列），若不这样兜底，两个账户的
            # 同代码成交会并进同一个加权平均成本，逐股盈亏整个失真。
            if not any((t.get("account") or "").strip() for t in rows):
                stem = os.path.splitext(name)[0]
                for t in rows:
                    t["account"] = stem
            lists.append(rows)
            sources.append(name)
        else:
            errors.append(f"{name}: 未解析到成交记录")
    trades, dup = merge_trades(lists)
    led = build_ledger(trades, price_fn=daily_last_close)
    led["trades"] = trades
    led["sources"] = sources
    led["dup_dropped"] = dup
    if errors:
        led["parse_warnings"] = (led.get("parse_warnings") or []) + errors
    return led

