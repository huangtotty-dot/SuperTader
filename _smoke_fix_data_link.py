# -*- coding: utf-8 -*-
"""数据链路修复冒烟测试（离线版：腾讯接口不可达，用合成数据验证逻辑）"""
import io, json, logging, sys, urllib.request, urllib.error
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

sys.path.insert(0, r"E:\06_T")
import pandas as pd
import data_fetcher as dfm
from config import PARAMS

dfm.urllib = urllib
dfm.log = logging.getLogger("smoke")
dfm._now = datetime.now
dfm.get_today_str = lambda: datetime.now().strftime("%Y-%m-%d")

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS {name} {extra}")
    else: fail += 1; print(f"  FAIL {name} {extra}")

# ---------- D4: 腾讯 fqkline 解析（mock urlopen，报文格式按真实接口） ----------
print("== D4: 腾讯 fqkline 解析与 ETF 主链路 ==")
fake_rows = [["2026-08-0%d" % (i % 9 + 1), "1.10", "1.12", "1.15", "1.09", "12345", "13800000"] for i in range(9)]
fake_payload = json.dumps({"code": 0, "data": {"sh588170": {"qfqday": fake_rows}}}).encode("utf-8")
class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
_orig_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout=10: _Resp(fake_payload)
df_t = dfm._fetch_daily_bar_tencent("588170")
urllib.request.urlopen = _orig_urlopen
check("腾讯解析行数", len(df_t) == 9, f"rows={len(df_t)}")
check("腾讯列齐全", all(c in df_t.columns for c in ["date", "open", "close", "high", "low", "volume", "amount"]))
check("数值类型", abs(float(df_t.iloc[0]["close"]) - 1.12) < 1e-9)

# ETF 主链路：腾讯成功时不再依赖 akshare
urllib.request.urlopen = lambda req, timeout=10: _Resp(fake_payload)
df_etf, reason = dfm._fetch_daily_bar("588170", is_etf=True)
urllib.request.urlopen = _orig_urlopen
check("ETF 主链路成功", not df_etf.empty and reason == "", f"rows={len(df_etf)} reason={reason!r}")

# 双链路全灭时原因透出（腾讯抛错 + akshare 抛错）
def _boom(req, timeout=10): raise OSError("net down")
urllib.request.urlopen = _boom
sys.modules["akshare"] = None  # import akshare 会失败
df_none, reason = dfm._fetch_daily_bar("588170", is_etf=True)
urllib.request.urlopen = _orig_urlopen
check("全灭返回空+原因", df_none.empty and "tencent" in reason, repr(reason))

# ---------- 合成 400 根日线，验证 D12/P1-9 ----------
print("== D12/P1-9: 400 天窗口 + 盘中口径 ==")
today = datetime.now().date()
days = []
d = today - timedelta(days=600)
while len(days) < 400:
    if d.weekday() < 5:
        days.append(d)
    d += timedelta(days=1)
days = days[-400:]
assert days[-1].weekday() < 5
closes = [10 + i * 0.01 for i in range(400)]
synth = pd.DataFrame({
    "date": [str(x) for x in days],
    "open": closes, "close": closes,
    "high": [c * 1.01 for c in closes],
    "low": [c * 0.99 for c in closes],
    "volume": [1000] * 400,
})
today_str = str(days[-1])

ctx_e = dfm._build_daily_context_from_df("T1", synth, current_price=0.0, intraday_asof=None)
check("eod scope", ctx_e.get("daily_scope") == "eod")
check("MA250 有效", ctx_e.get("daily_ma250", 0) > 0, f"ma250={ctx_e.get('daily_ma250')}")
check("MA120 有效", ctx_e.get("daily_ma120", 0) > 0)
check("无 daily_ma365 键", "daily_ma365" not in ctx_e and "daily_ma365_slope" not in ctx_e)
try:
    json.dumps(ctx_e, allow_nan=False); check("ctx 无裸 NaN", True)
except ValueError as e:
    check("ctx 无裸 NaN", False, str(e)[:60])
check("eod prev_close=倒数第2根收盘", abs(ctx_e["daily_prev_close"] - closes[-2]) < 1e-9)
check("eod prev_high=最后一根高点", abs(ctx_e["daily_prev_high"] - closes[-1] * 1.01) < 1e-6)

ctx_i = dfm._build_daily_context_from_df("T1", synth, current_price=0.0, intraday_asof=today_str)
check("intraday scope", ctx_i.get("daily_scope") == "intraday")
check("intraday 剔除当日 bar(asof=昨日)", ctx_i.get("daily_asof") == str(days[-2]), f"asof={ctx_i.get('daily_asof')}")
check("intraday prev_close=昨日完整收盘", abs(ctx_i["daily_prev_close"] - closes[-2]) < 1e-9)
check("intraday prev_high=昨日高点", abs(ctx_i["daily_prev_high"] - closes[-2] * 1.01) < 1e-6)
check("intraday prev_close==prev_close_real", abs(ctx_i["daily_prev_close"] - ctx_i["daily_prev_close_real"]) < 1e-9)

# 短历史 ETF（100 根）不再产出裸 NaN
print("== D12: 短历史不产裸 NaN ==")
short = synth.tail(100).reset_index(drop=True)
ctx_s = dfm._build_daily_context_from_df("T2", short, current_price=0.0, intraday_asof=None)
try:
    json.dumps(ctx_s, allow_nan=False); check("短历史 ctx 无裸 NaN", True)
except ValueError as e:
    check("短历史 ctx 无裸 NaN", False, str(e)[:60])
check("短历史 MA250=0(而非NaN)", ctx_s.get("daily_ma250") == 0.0)

print("== _fnum ==")
check("_fnum(nan)=0", dfm._fnum(float("nan")) == 0.0)
check("_fnum(inf)=0", dfm._fnum(float("inf")) == 0.0)
check("_fnum(1.5)=1.5", dfm._fnum(1.5) == 1.5)

# ---------- D10: ETF 基准映射 ----------
print("== D10: ETF 基准映射 ==")
ns = {"Dict": Dict, "List": List, "Optional": Optional, "Any": Any,
      "pd": pd, "json": json, "os": __import__("os"), "datetime": datetime}
with open(r"E:\06_T\utils.py", encoding="utf-8") as f:
    exec(compile(f.read(), "utils.py", "exec"), ns)
bm = ns["_benchmark_meta_for_code"]
check("588170→科创50", bm("588170")["code"] == "sh000688", bm("588170"))
check("588000→科创50", bm("588000")["code"] == "sh000688")
check("516160→上证指数", bm("516160")["code"] == "sh000001")
check("512880→上证指数", bm("512880")["code"] == "sh000001")
check("159915→深证成指", bm("159915")["code"] == "sz399001")
check("个股 688102→科创50(star) 不变", bm("688102")["code"] == "sh000688" and bm("688102")["kind"] == "star")
check("个股 300364→创业板指 不变", bm("300364")["code"] == "sz399006" and bm("300364")["kind"] == "chi_next")
check("个股 600089→上证指数(sse) 不变", bm("600089")["code"] == "sh000001" and bm("600089")["kind"] == "sse")
check("个股 002639→深证成指 不变", bm("002639")["code"] == "sz399001" and bm("002639")["kind"] == "szse")
js = ns["_json_safe"]
check("_json_safe NaN/inf→None", js({"a": float("nan"), "b": [float("inf"), 1.0], "c": {"d": float("-inf")}}) == {"a": None, "b": [None, 1.0], "c": {"d": None}})

# ---------- P1-11: pivot 估算标识 ----------
print("== P1-11: pivot 估算标识 ==")
ns2 = {"Dict": Dict, "List": List, "Optional": Optional, "Any": Any,
       "json": json, "os": __import__("os"), "T_IO_DIR": r"E:\06_T\t_io",
       "datetime": datetime, "timedelta": timedelta,
       "dtime": __import__("datetime").time, "pd": pd}
with open(r"E:\06_T\support_resistance.py", encoding="utf-8") as f:
    exec(compile(f.read(), "support_resistance.py", "exec"), ns2)
sr = type("sr", (), ns2)  # 以命名空间伪模块方式取用函数
lv_est = sr.calc_pivot_levels("588170", {"name": "科创芯片", "pre_close": 1.5}, {})
check("回退编造 estimated=True", lv_est.get("estimated") is True, lv_est)
txt = sr.format_pivot_text([lv_est])
check("推送文案含『估算』", "『估算』" in txt, txt.replace("\n", " / "))
lv_real = sr.calc_pivot_levels("X", {"name": "n", "pre_close": 10},
                               {"daily_prev_high": 10.5, "daily_prev_low": 9.5,
                                "daily_prev_close_real": 10.0, "daily_prev_close": 10.0})
check("真实数据 estimated=False", lv_real.get("estimated") is False)
check("真实文案无『估算』", "『估算』" not in sr.format_pivot_text([lv_real]))
check("levels 含 ref_price 供 P1-7 使用", lv_real.get("ref_price") == 10.0)

print(f"\nRESULT: {ok} pass / {fail} fail")
sys.exit(1 if fail else 0)
