# -*- coding: utf-8 -*-
"""诊断券商「历史成交 / 交割单」文件：打印真身、表头认领结果与前几行。

历史成交导出的「.xls」扩展名不可信（可能是 xlsx / 老式 xls / Excel 2003 XML / HTML 表 /
制表符文本），列名和列序各家也不同。导入失败时跑这个脚本，把**输出整段发回来**即可定位，
不用上传原始文件。

只读文件、不联网、不写任何东西；默认只打印前 3 行的前 10 列，避免整表外泄。

用法：
    python scripts/probe_trade_file.py "D:\\下载\\table.xls" [更多文件...]
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import trade_history as th  # noqa: E402


def probe(path, rows_show=3, cols_show=10):
    print("=" * 78)
    print(f"文件: {os.path.basename(path)}  ({os.path.getsize(path):,} 字节)")
    if not os.path.exists(path):
        print("  ✗ 文件不存在")
        return
    print(f"  扩展名: {os.path.splitext(path)[1] or '(无)'}")
    print(f"  真身识别: {th.sniff_format(path)}")
    try:
        rows = th.read_excel_rows(path)
    except Exception as e:
        print(f"  ✗ 读取异常: {type(e).__name__}: {e}")
        return
    print(f"  读出 {len(rows)} 行")
    hi = th._find_header(rows)
    print(f"  表头行下标: {hi if hi >= 0 else '✗ 没找到（需同时含「成交日期」「证券代码」类列名）'}")
    if hi >= 0:
        col = th._map_columns(rows[hi])
        pretty = {"date": "成交日期", "time": "成交时间", "account": "资金账号", "code": "证券代码",
                  "name": "证券名称", "op": "操作", "qty": "成交数量", "price": "成交均价",
                  "amount": "成交金额", "avail": "持仓余额", "occur": "发生金额",
                  "cash": "资金余额", "contract": "合同编号", "stamp": "印花税",
                  "commission": "佣金", "transfer": "过户费", "settle": "结算费", "other": "其他费用"}
        print("  列认领: " + " | ".join(f"{pretty.get(f, f)}=第{i}列" for f, i in sorted(col.items(), key=lambda kv: kv[1])))
        missing = [k for k in ("date", "code", "op", "qty") if k not in col]
        print("  必需列缺失: " + (", ".join(pretty.get(m, m) for m in missing) if missing else "无 ✓"))
        print("  表头原文: " + " | ".join(str(c) for c in rows[hi][:cols_show]))
    for i, r in enumerate(rows[hi + 1: hi + 1 + rows_show] if hi >= 0 else rows[:rows_show]):
        print(f"  样本{i + 1}: " + " | ".join(str(c) for c in r[:cols_show]))
    try:
        tr = th.trades_of(path)
        print(f"  → 解析成功: {len(tr)} 笔成交")
        for t in tr[:2]:
            print(f"     {t['date']} {t['code']} {t['name']} {t['op']} qty={t['qty']} "
                  f"price={t['price']} occur={t['occur']}")
    except Exception as e:
        print(f"  → 解析失败: {e}")
        diag = []
        try:
            th.trades_of(path, diag=diag)
        except Exception:
            pass
        if diag:
            print("     诊断: " + diag[0])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for p in sys.argv[1:]:
        probe(p)
    print("=" * 78)
    print("把以上整段发回来即可（不含完整明细，只有前几行的前几列）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
