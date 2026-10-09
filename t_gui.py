# -*- coding: utf-8 -*-
"""
t_gui.py — 做T实盘·盘后复盘决策看板（pywebview 桌面壳）
用法: python t_gui.py

数据只读，不改动任何现有系统行为。前端为 web/ 目录下的纯 HTML/CSS/JS，
通过 pywebview js_api 调用本文件的 Api 方法获取聚合后的当日决策数据。

数据源（全部由现有系统落盘）:
  t_io/validation/daily_review/daily_review_{date}.json   日复盘主聚合
  t_io/validation/daily_review/kpi_{date}.json            K1-K5 KPI 独立文件
  t_io/validation/daily_review/stage_board.json           阶段看板
  t_io/traces/position_builder_{date}.jsonl               建仓扫描逐行日志
  doc/每日复盘/{date}_复盘.md                              复盘报告 markdown
  holdings.json / t_io/state/holdings_daily_{date}.json     持仓（当前 + GUI 日快照；旧 holdings_{date}.json 已于 2026-08-30 清理）
"""
import json
import math
import sys
import threading
import time as _time_mod
from collections import Counter
from datetime import datetime
from pathlib import Path

# Windows 终端 UTF-8 修复（避免 GBK 乱码）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent  # 自解析：生产机 E:\06_T 与本机仓库位置均正确（与 position_builder/config 一致）
OUT = BASE / "t_io" / "validation" / "daily_review"
TRACES = BASE / "t_io" / "traces"
STATE_DIR = BASE / "t_io" / "state"
# 2026-10-04 双文件拆分：手动盘页读 holdings_manual.json；自动盘页经 holdings_repo 读自动侧。
HOLDINGS_MANUAL = STATE_DIR / "holdings_manual.json"
IDX_REGIME = BASE / "t_io" / "index_regime"
LOGS_DIR = BASE / "t_io" / "logs"
INTRADAY_STATE = BASE / "t_io" / "intraday_state.json"
PORTFOLIO = STATE_DIR / "accounts_config.json"  # 2026-08-30 合并：账户配置唯一源头（原 portfolio_config.json 已并入）
PORTFOLIO_LEGACY = STATE_DIR / "portfolio_config.json"  # 旧部署回退（.gszq 等）
BRIDGE_DIR = BASE / "t_io" / "bridge"  # P4-2/3: 自动盘事件总线（heartbeat.json + events_*.jsonl + KILL_SWITCH）

# 内置名称映射（数据缺失 code 时兜底；可由 holdings/add_watch/trace 补充）
NAMES = {
    "000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
    "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
    "300153": "科泰电源", "300364": "中文在线",
}

# W33 A1: 双通道 8 键标签（与 position_builder.CHANNEL_COND_KEYS 同序）
# 方案A (2026-08-15): 建仓条件=时机门控，与 position_builder.COND_LABELS 一致
COND_LABELS = {
    "t_regime": "市场有方向",
    "t_trend": "多头结构",
    "t_drawdown": "回撤到位",
    "t_golden": "MACD金叉(加分)",
}


# 黄金分割（斐波那契回撤/扩展）参数 —— 按周期分档，越长的周期要求越大的摆动幅度
# 回看根数（决定锚点搜索范围）。2026-10-04: daily 250→120，与前端默认视窗根数对齐——
# 原来 250 会让前端为「看得见锚点」把视窗放宽，一屏塞进 250+ 根K线，蜡烛细到看不清。
# 代价：更早的大级别摆动不再被选为锚点（用户 2026-10-04 确认接受）。
_FIB_LOOKBACK = {"daily": 120, "weekly": 120, "monthly": 60,
                 "min30": 160, "min60": 160}   # 分钟档与前端默认视窗根数对齐
_FIB_FRACTAL_N = {"daily": 3, "weekly": 2, "monthly": 1,        # 分形确认根数（越长周期 bar 越少）
                  "min30": 2, "min60": 2}
_FIB_MIN_AMP = {"daily": 5.0, "weekly": 8.0, "monthly": 12.0,   # 摆动最小幅度 %（低于此视为噪声）
                "min30": 3.0, "min60": 3.0}    # 分钟档为初始值，未标定，待图上肉眼校准
_FIB_RETRACE = [0.236, 0.382, 0.5, 0.618, 0.786]                # 回撤位（0.618 即黄金比例）
_FIB_EXTENSION = [1.272, 1.618]                                 # 扩展位（突破后目标）

HUNTER_DIR = BASE / "stock_hunter"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # stock_hunter 模块在 stock_hunter/ 下导入
if str(HUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(HUNTER_DIR))

# 2026-08-15: 选股猎手后台运行状态（进度条轮询用）。进度细节来自 market_data.MARKET_PROGRESS。
import threading as _th
HUNTER_RUN_STATE = {"date": None, "running": False, "result": None}
# 2026-09-21 owner 需求：开盘后每小时自动跑一次「今日数据」。
# 时点**跳过午休**（11:30–13:00 无行情变化，跑了也是重复），15:00 收盘后不再跑。
# 交易日按 weekday<5 近似（沿用仓库既有口径，节假日空跑无害）。
HUNTER_AUTORUN_SLOTS = ("10:30", "11:30", "13:30", "14:30")
_HUNTER_AUTORUN_STATE = {"date": None, "done": set(), "started": False}
_CHART_PREFETCH_STATE = {"started": False}  # 盘后 K线预下载调度（2026-10-04）
# 主线程(js_api)心跳：高频轮询方法每次更新；看门狗发现停跳 ⇒ 判界面卡死并落线程栈（2026-10-08）
_GUI_HB = {"ts": 0.0}
# 建仓推送的「当日去重」状态：自动运行只在候选集变化时推，避免一天重复刷屏
_HUNTER_BUILD_PUSHED_FP = STATE_DIR / "hunter_build_pushed.json"
ROTATION_RUN_STATE = {"running": False, "error": None}
# 2026-08-22: 板块轮动结果缓存（内存+磁盘）——build_rotation_model 约 15s，重复点击/切 view 秒回
_ROTATION_CACHE_DIR = BASE / "t_io" / "cache" / "sector_rotation"
_ROTATION_CACHE_MEM = {}
# 2026-08-23: 每日大盘复盘（LLM）后台线程状态
_REVIEW_RUN_STATE = {"running": False, "error": None}


def _jiuyan_concepts(info):
    """合并股票记录的所有韭研概念（编号字段 jiuyan_concept1..9 + 旧普通字段）。
    返回用 | 连接的去重字符串；无概念返回空串。"""
    if not isinstance(info, dict):
        return ""
    parts = []
    for i in range(1, 10):
        v = info.get(f"jiuyan_concept{i}")
        if v and str(v).strip():
            parts.append(str(v).strip())
    if not parts:
        v = info.get("jiuyan_concept")
        if v and str(v).strip():
            parts.append(str(v).strip())
    seen = set()
    out = []
    for p in parts:
        for c in p.split("|"):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return "|".join(out)


# 行业粗分类规则（2026-09-29，突破箱体按行业分组用）。
#
# 为什么需要它：watchlist 的 `sector` 首段是**国民经济行业分类大类**（约 90 个，且部分条目
# 首段是概念而非行业）⇒ 直接用会碎成「125 只命中 → 55 组、其中 34 组只有 1 只」，反而不直观。
# 这里按关键词收敛到 ~32 个申万一级式大类：实测 125 只命中 → 24 组（单只组仅 5）。
#
# ⚠️ 两条关键约束（都是踩过的）：
#  1. **只匹配首段**，不拼接后续段：后续段是概念，会劫持匹配
#     （`汽车零部件/参股保险/…` 曾被 '保险' 抢成非银金融、`软件开发/互联网保险` 同理）。
#  2. **顺序敏感，特定先于宽泛**：`非金属矿物制品业` 含 '金属'、`黑色金属冶炼` 含 '金属'、
#     `水上运输业` 含 '水'、`酒店` 含 '酒' —— 都必须让更特定的规则先命中。
# 更新规则后请复跑 tests/phase3/test_breakout_daily_breakout.py 里的行业用例。
_INDUSTRY_RULES = (
    ('医药', '医药生物'), ('医疗', '医药生物'), ('中药', '医药生物'), ('生物', '医药生物'),
    ('卫生', '医药生物'), ('化学制药', '医药生物'),
    ('货币金融', '银行'), ('银行', '银行'), ('保险', '非银金融'), ('证券', '非银金融'),
    ('信托', '非银金融'), ('资本市场', '非银金融'), ('期货', '非银金融'), ('金融', '非银金融'),
    ('房地产', '房地产'), ('房屋', '房地产'), ('物业', '房地产'),
    ('半导体', '电子'), ('电子化学品', '电子'), ('消费电子', '电子'), ('军工电子', '电子'),
    ('光学', '电子'), ('元件', '电子'), ('面板', '电子'), ('电子', '电子'),
    ('软件', '计算机'), ('IT服务', '计算机'), ('计算机', '计算机'), ('互联网', '计算机'),
    ('通信', '通信'),
    ('电池', '电力设备'), ('光伏', '电力设备'), ('风电', '电力设备'), ('电网', '电力设备'),
    ('电力设备', '电力设备'), ('电气机械', '电力设备'), ('电机', '电力设备'), ('电源设备', '电力设备'),
    ('汽车', '汽车'),
    ('专用设备', '机械设备'), ('通用设备', '机械设备'), ('工程机械', '机械设备'),
    ('仪器仪表', '机械设备'), ('自动化', '机械设备'), ('机床', '机械设备'), ('轨交', '机械设备'),
    ('运输设备', '机械设备'), ('金属制品', '机械设备'), ('机械', '机械设备'),
    # 建材/钢铁必须排在 metals 之前：'非金属矿物'/'黑色金属' 都含 '金属'
    # ⚠️ 不要加裸的 ('冶','钢铁')：'有色金属冶炼和压延加工业' 含 '冶'，会被它抢成钢铁。
    ('非金属矿物', '建筑材料'), ('建材', '建筑材料'), ('水泥', '建筑材料'), ('玻璃', '建筑材料'),
    ('黑色金属', '钢铁'), ('钢铁', '钢铁'),
    ('建筑', '建筑装饰'), ('装饰', '建筑装饰'), ('土木', '建筑装饰'),
    ('化学', '基础化工'), ('化工', '基础化工'), ('塑料', '基础化工'), ('橡胶', '基础化工'),
    ('石化', '石油石化'), ('石油', '石油石化'), ('油气', '石油石化'),
    ('有色', '有色金属'), ('小金属', '有色金属'), ('工业金属', '有色金属'), ('金属', '有色金属'),
    ('煤炭', '煤炭'), ('采掘', '煤炭'),
    ('酒店', '社会服务'), ('教育', '社会服务'), ('旅游', '社会服务'), ('餐饮', '社会服务'),
    ('服务', '社会服务'),
    ('电力', '公用事业'), ('燃气', '公用事业'), ('水的生产', '公用事业'),
    ('环保', '环保'), ('环境', '环保'),
    ('食品', '食品饮料'), ('饮料', '食品饮料'), ('酒', '食品饮料'), ('精制茶', '食品饮料'),
    ('农林', '农林牧渔'), ('农业', '农林牧渔'), ('农副', '农林牧渔'), ('畜牧', '农林牧渔'),
    ('渔业', '农林牧渔'), ('饲料', '农林牧渔'),
    ('纺织', '纺织服饰'), ('服装', '纺织服饰'), ('服饰', '纺织服饰'),
    ('皮革', '纺织服饰'), ('制鞋', '纺织服饰'),
    ('家电', '家用电器'), ('厨', '家用电器'),
    ('物流', '交通运输'), ('运输', '交通运输'), ('航运', '交通运输'), ('航空', '交通运输'),
    ('港口', '交通运输'), ('铁路', '交通运输'), ('道路', '交通运输'), ('交运', '交通运输'),
    ('批发', '商贸零售'), ('零售', '商贸零售'), ('贸易', '商贸零售'),
    ('商业', '商贸零售'), ('商贸', '商贸零售'),
    ('传媒', '传媒'), ('新闻', '传媒'), ('出版', '传媒'), ('广播', '传媒'),
    ('影视', '传媒'), ('游戏', '传媒'),
    ('造纸', '轻工制造'), ('家具', '轻工制造'), ('包装', '轻工制造'),
    ('轻工', '轻工制造'), ('文教', '轻工制造'), ('玩具', '轻工制造'),
    ('国防', '国防军工'), ('航天', '国防军工'), ('军工', '国防军工'), ('船舶', '国防军工'),
    ('其他', '其他'), ('综合', '综合'),
    # —— 2026-09-29 补充：首段来自**东财行业三级名/长尾大类**，上面的关键词覆盖不到。
    # 按实测未分类 Top30 补齐（加完未分类从 354 只降到 ~150 只）。都是较特定的词，
    # 追加在末尾不会被前面的宽泛规则遮蔽（已复跑审查确认）。
    ('家用电器', '家用电器'), ('照明', '家用电器'),
    ('乘用车', '汽车'), ('商用车', '汽车'), ('底盘', '汽车'), ('车身', '汽车'),
    ('减速器', '机械设备'), ('工控', '机械设备'), ('能源及重型', '机械设备'),
    ('木材', '轻工制造'), ('印刷', '轻工制造'), ('家居', '轻工制造'), ('文娱', '轻工制造'),
    ('仓储', '交通运输'), ('装卸', '交通运输'), ('邮政', '交通运输'),
    ('耐火', '建筑材料'), ('专业工程', '建筑装饰'),
    ('农化', '基础化工'), ('原料药', '医药生物'),
    ('数字媒体', '传媒'), ('住宿', '社会服务'), ('公共设施', '公用事业'),
    ('芯片', '电子'), ('航海', '国防军工'),
)

#: 归不到任何大类时的桶名。与源数据里字面的「其他」区分开——后者是真的"其他"，
#: 前者是**我们不知道**（首段是概念、或 sector 为空），混在一起会误导。
_INDUSTRY_UNKNOWN = "未分类"


def _stock_industry(info) -> str:
    """股票记录 → 粗行业名（申万一级式）。见 `_INDUSTRY_RULES` 的两条约束。"""
    if not isinstance(info, dict):
        return _INDUSTRY_UNKNOWN
    first = str(info.get("sector") or "").split("/")[0].strip()
    if not first:
        return _INDUSTRY_UNKNOWN
    for kw, ind in _INDUSTRY_RULES:
        if kw in first:
            return ind
    return _INDUSTRY_UNKNOWN


def _stock_concepts(info, em_boards=None) -> list:
    """该股的「概念」列表（离线两源合并 + 可选东财第三源，2026-09-30）。

    源：
      1) 韭研概念 `jiuyan_concept1..9` / `jiuyan_concept`（见 `_jiuyan_concepts`）
      2) `sector` **首段以外**的板块段（首段是行业，已由「行业」列表达）
      3) 可选 `em_boards`：东财「所属板块」[{name,kind}]，**仅当 1+2 全空时启用**，
         取 kind=="概念" 的板块名并去掉「概念」后缀（"MicroLED概念"→"MicroLED"）。
         沿用第 2 源的剔除规则（地域/申万层级/与粗行业同名）。

    第 2 源要剔三类噪音，否则会把行业当成概念列出来：
      · **地域**：以「板块」结尾（福建板块）——不是概念
      · **申万层级**：以 Ⅰ/Ⅱ/Ⅲ 结尾（`IT服务Ⅱ`）——是行业层级，不是概念
      · **与粗行业同名**：如 `计算机`（`_stock_industry` 已归入「计算机」）——重复

    ⚠️ 离线覆盖率仅 **46.6%**（两源任一有）。`sector` 是东财「所属板块」混合串，
       而 legacy 条目只有一个行业段 ⇒ **贵州茅台这类完全没有概念**，是数据本身的限制，
       不是 bug。第 3 源由调用方通过 `_em_boards_disk_cached` 按需传入（逐只接口，
       只补命中票；见 `_scan_breakout`）。
    """
    if not isinstance(info, dict):
        return []
    out, seen = [], set()

    def _add(x):
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    for x in _jiuyan_concepts(info).split("|"):
        _add(x)
    ind = _stock_industry(info)
    for p in str(info.get("sector") or "").split("/")[1:]:
        p = p.strip()
        if not p or p.endswith("板块") or p in ind or p.endswith(("Ⅰ", "Ⅱ", "Ⅲ")):
            continue
        _add(p)
    # 第 3 源：离线两源全空时，东财「所属板块」里 kind=="概念" 的板块名
    if not out and em_boards:
        for b in em_boards:
            if not isinstance(b, dict) or b.get("kind") != "概念":
                continue
            nm = str(b.get("name") or "").strip()
            if nm.endswith("概念"):
                nm = nm[:-2].strip()
            if not nm or nm.endswith("板块") or nm in ind or nm.endswith(("Ⅰ", "Ⅱ", "Ⅲ")):
                continue
            _add(nm)
    return out


def _clean(obj):
    """递归清洗为 JSON 可序列化类型：nan/inf -> None，numpy 标量 -> 原生。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _load_json(fp, default=None):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


# ---- K线弹窗「30分/60分」分时取数（2026-10-04）----
# 在线走 tushare **原生** freq；不复用 divergence._resample_minutes（它用 dt.floor，
# A股午休会把 60min 桶错位）。无 token / 超频 → 回退本地 tushare_mins 缓存（零网络）。
# 内存按「30 分钟时段」缓存：同一时段内秒回，跨时段自动重取。
_MIN_BARS_DIR = BASE / "t_io" / "cache" / "tushare_mins"
_MIN_BARS_CACHE: dict = {}
_MIN_BARS_LOCK = threading.Lock()
MIN_BARS_KEEP = 320                                    # 保留根数上限（前端「全部」档）
_MIN_BARS_DAYS = {"30min": 70, "60min": 130}           # 自然日：够 320 根（8/日、4/日）


def _min_bars_ts_code(code):
    """6 位股票/指数码 → tushare ts_code（sh000001→000001.SH）。
    不复用 divergence._ts_code：它不认 sh/sz 前缀（指数会拼错），且有其它调用方。"""
    c = str(code).split("_")[0]
    pre = c[:2].lower()
    if pre in ("sh", "sz", "bj") and c[2:].isdigit():
        return c[2:] + "." + {"sh": "SH", "sz": "SZ", "bj": "BJ"}[pre]
    if len(c) == 9 and c[3] == "." and c[:3].isdigit():       # 已是 ts_code 形式
        return c.upper()
    if len(c) == 6 and c.isdigit():
        ex = "SH" if c[0] in "56" else ("BJ" if c[0] in "48" else "SZ")
        return f"{c}.{ex}"
    return ""


def _min_bars_slot():
    """当前 30 分钟时段键（跨时段才重取）。"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:") + ("00" if now.minute < 30 else "30")


def _norm_min_bars(df):
    """统一分时帧列名/类型/排序；缺列或空 → 空 DataFrame。"""
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume", "date": "time"})
    need = {"time", "open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").drop_duplicates(subset=["time"])
    return df.reset_index(drop=True)


def _calc_ma_and_indicators(d):
    """MA(7 条)/MACD/RSI(Wilder)/BOLL。2026-10-04 从 _build_chart_from_df 的嵌套函数抽出，
    供 K线 payload 缓存「仅补日线末根」时复用（避免整图重建）。"""
    d = d.copy()
    for n in (5, 10, 20, 30, 60, 180, 365):
        d[f"ma{n}"] = d["close"].rolling(n).mean()
    ema12 = d["close"].ewm(span=12, adjust=False).mean()
    ema26 = d["close"].ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = (d["dif"] - d["dea"]) * 2
    from analysis.indicators import wilder_rsi as _wilder_rsi
    d["rsi"] = _wilder_rsi(d["close"], 14)
    d["boll_mid"] = d["close"].rolling(20).mean()
    d["boll_std"] = d["close"].rolling(20).std()
    d["boll_up"] = d["boll_mid"] + 2 * d["boll_std"]
    d["boll_dn"] = d["boll_mid"] - 2 * d["boll_std"]
    return d


def _to_series(d, intraday=False):
    """DataFrame → 前端序列 dict（与 _build_chart_from_df 同口径；抽出以复用）。"""
    import pandas as pd
    _fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    return {
        "dates": [x.strftime(_fmt) for x in d["date"]],
        "ohlc": [[round(o, 3), round(c, 3), round(l, 3), round(h, 3)]
                 for o, c, l, h in zip(d["open"], d["close"], d["low"], d["high"])],
        "volume": [round(float(v), 0) for v in d["volume"]],
        "ma": [[round(x, 3) if not pd.isna(x) else None for x in d[f"ma{n}"]]
               for n in (5, 10, 20, 30, 60, 180, 365)],
        "macd": {"dif": [round(x, 3) if not pd.isna(x) else None for x in d["dif"]],
                 "dea": [round(x, 3) if not pd.isna(x) else None for x in d["dea"]],
                 "hist": [round(x, 3) if not pd.isna(x) else None for x in d["macd_hist"]]},
        "rsi": [round(x, 1) if not pd.isna(x) else None for x in d["rsi"]],
        "boll": {"mid": [round(x, 3) if not pd.isna(x) else None for x in d["boll_mid"]],
                 "up": [round(x, 3) if not pd.isna(x) else None for x in d["boll_up"]],
                 "dn": [round(x, 3) if not pd.isna(x) else None for x in d["boll_dn"]]},
    }


def _fetch_min_bars_online(ts_code, freq, days):
    """tushare 原生 30/60 分钟线。异常/空 → 空 DataFrame（由调用方回退磁盘缓存）。"""
    import pandas as pd
    try:
        from analysis.index_regime_intraday import _iri_tushare_pro
        pro = _iri_tushare_pro()
        start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        df = pro.stk_mins(ts_code=ts_code, freq=freq,
                          start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    except Exception:
        return pd.DataFrame()
    return _norm_min_bars(df)


def _fetch_min_bars_disk(ts_code, freq):
    """本地缓存兜底。2026-10-04：优先读**新鲜**的 plain 档（盘后预下载写这里），
    再回退历史档；多档间按末行时间取**最新者**（原来固定优先陈旧的 `_d540`，
    当 plain 档更新时会被陈旧数据掩盖）。"""
    import pandas as pd
    try:
        from core import chart_cache as _cc
        fresh = _cc.load_minute_history(ts_code, freq)
        if fresh is not None and not fresh.empty:
            return fresh
    except Exception:
        pass
    best, best_last = pd.DataFrame(), ""
    for name in (f"{ts_code}_{freq}_d540.json", f"{ts_code}_{freq}.json"):
        fp = _MIN_BARS_DIR / name
        if not fp.exists():
            continue
        try:
            rows = (json.loads(fp.read_text(encoding="utf-8")) or {}).get("rows") or []
            df = _norm_min_bars(pd.DataFrame(rows)) if rows else pd.DataFrame()
        except Exception:
            continue
        if df.empty:
            continue
        _last = str(df["time"].iloc[-1])
        if _last >= best_last:
            best, best_last = df, _last
    return best


def _fetch_min_bars(code, freq="30min", days=None):
    """原生 30/60 分钟线，最近 MIN_BARS_KEEP 根。
    任何失败都返回空 DataFrame，调用方据此降级为「该周期无分时数据」。"""
    import pandas as pd
    ts_code = _min_bars_ts_code(code)
    if not ts_code or freq not in _MIN_BARS_DAYS:
        return pd.DataFrame()
    days = days or _MIN_BARS_DAYS[freq]
    ck = (ts_code, freq, days)
    slot = _min_bars_slot()
    with _MIN_BARS_LOCK:
        hit = _MIN_BARS_CACHE.get(ck)
        if hit and hit[0] == slot:
            return hit[1].copy()

    df = _fetch_min_bars_online(ts_code, freq, days)
    if df.empty:
        df = _fetch_min_bars_disk(ts_code, freq)
    if df.empty:
        return df
    df = df.tail(MIN_BARS_KEEP).reset_index(drop=True)
    with _MIN_BARS_LOCK:
        if len(_MIN_BARS_CACHE) > 2000:      # 无界增长防护（2026-10-04）：超限整表清空
            _MIN_BARS_CACHE.clear()
        _MIN_BARS_CACHE[ck] = (slot, df)
    return df.copy()


_ACCT_MAP_CACHE = {"ts": 0.0, "map": {}}


# ---- 东财「所属板块」磁盘缓存层（2026-09-30，突破面板概念列补拉专用） ----
#: 结构 {code: {"ts": epoch, "boards": [{name,kind}]}}；概念不常变，正缓存 7 天，
#: 负缓存（拉空=风控或真无板块）1 天，防风控期反复 8 次重试拖慢扫描。
_EM_BOARDS_DISK_FP = BASE / "t_io" / "cache" / "em_boards.json"
_EM_BOARDS_DISK_TTL = 7 * 86400.0
_EM_BOARDS_DISK_NEG_TTL = 1 * 86400.0
_EM_BOARDS_DISK_LOCK = threading.Lock()
_EM_BOARDS_DISK_MEM = {"data": None}     # 整文件内存镜像，避免逐只重复读盘


def _em_boards_disk_cached(api, code) -> list:
    """东财「所属板块」三级取数：Api 内存缓存(600s) → 磁盘(7天/负1天) → 在线。

    读写全部 **fail-open**（文件坏=没缓存；写失败=忽略），绝不抛。
    `Api._profile_cache` 的 600s TTL 面板重开即失效，磁盘层负责跨进程复用。
    """
    c = str(code).split("_")[0]
    now = _time_mod.time()
    hit = Api._profile_cache.get(c)
    if hit and (now - hit[0]) < Api._PROFILE_TTL:
        return hit[1]
    with _EM_BOARDS_DISK_LOCK:
        if _EM_BOARDS_DISK_MEM["data"] is None:
            raw = _load_json(_EM_BOARDS_DISK_FP, None)
            _EM_BOARDS_DISK_MEM["data"] = raw if isinstance(raw, dict) else {}
        disk = _EM_BOARDS_DISK_MEM["data"]
        rec = disk.get(c)
        if isinstance(rec, dict) and isinstance(rec.get("boards"), list):
            boards = rec["boards"]
            ttl = _EM_BOARDS_DISK_TTL if boards else _EM_BOARDS_DISK_NEG_TTL
            try:
                fresh = (now - float(rec.get("ts") or 0)) < ttl
            except Exception:
                fresh = False
            if fresh:
                Api._profile_cache[c] = (now, boards)
                return boards
    boards = api._em_stock_boards(c)       # 在线补拉（自身 8 次重试、永不抛）
    with _EM_BOARDS_DISK_LOCK:
        disk[c] = {"ts": now, "boards": boards}
        try:
            _EM_BOARDS_DISK_FP.parent.mkdir(parents=True, exist_ok=True)
            tmp = _EM_BOARDS_DISK_FP.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_EM_BOARDS_DISK_FP)
        except Exception:
            pass
    return boards


def _hunter_is_intraday(date) -> bool:
    """该日期是否为「今日且盘中」（工作日 9:15-15:00）。

    盘中判定集中在此，供「盘中手动跑只算不推飞书」等处复用（2026-09-29）。
    """
    now = datetime.now()
    return (str(date) == now.strftime("%Y-%m-%d")
            and 915 <= now.hour * 100 + now.minute <= 1500
            and now.weekday() < 5)


def _account_of(code) -> str:
    """code → 账户名，自 accounts_config.json 各账户的 holdings 清单派生（TTL 300s）。

    2026-09-14 持仓并表：holdings.json 不再存 `account` 字段，归属改由账户配置声明。
    """
    import time as _t
    base = str(code).split("_")[0]
    if _t.time() - _ACCT_MAP_CACHE["ts"] > 300:
        cfg = _load_json(STATE_DIR / "accounts_config.json", {}) or {}
        m = {}
        for a, v in (cfg.get("accounts") or {}).items():
            for c in ((v or {}).get("holdings") or []):
                m[str(c).split("_")[0]] = a
        _ACCT_MAP_CACHE.update({"ts": _t.time(), "map": m})
    return _ACCT_MAP_CACHE["map"].get(base, "")


def _reconcile_cash():
    """现金口径（净值法 §八）：accounts_config 最新人工 reconcile 的各启用账户
    available（可用资金）之和。取不到返回 (None, "missing")——净值法下 cash 缺失时
    equity/alpha 必须置 null 并标注，禁止硬算。
    2026-09-27：跳过 paper=true 的仿真账户（账户C 国盛掘金仿真），
    防止纸面现金混入真实净值口径（holdings_daily 市值侧也只有实盘持仓）。"""
    fp = PORTFOLIO if PORTFOLIO.exists() else PORTFOLIO_LEGACY
    cfg = _load_json(fp, {}) or {}
    accs = cfg.get("accounts") or {}
    total, found = 0.0, False
    for a in accs.values():
        if not isinstance(a, dict) or not a.get("enabled", True):
            continue
        if a.get("paper"):
            continue
        v = a.get("available")
        if isinstance(v, (int, float)):
            total += float(v)
            found = True
    if not found:
        return None, "missing"
    return round(total, 2), "accounts_config"


# 技术标签 TTL 缓存：GUI 每 10s 轮询 refresh_pb → load_stock_tags_batch（单次约 7-12s，
# 期间大量 pandas + 网络在 pywebview 主线程执行会冻结界面）。改为 TTL 缓存 + 后台异步重算，
# 轮询永远读缓存即时返回，界面不卡。TTL 取 120s：标签变化慢，过长 TTL 减少后台重算的 CPU 尖峰。
_TAGS_TTL = 120.0

# 30 分钟趋势判定（2026-10-04，方案 doc/solutions/2026-10-04_30分钟趋势判定方案.md）：
# 技术标签的「上行/下行/震荡」由日线斜率改按 30min 三层状态机。开关便于回滚；
# 数据不可用（网络/根数不足）时自动回退日线斜率，保持旧行为。
_TREND30_ENABLED = True
_TAGS_CACHE: dict = {}
_TAGS_LOCK = threading.Lock()
_TAGS_RUNNING = False

# 突破扫描状态/缓存的同步锁（2026-09-29）。此前 `_breakout_scan`/`_breakout_cache` **零同步**：
# 后台 daemon 线程写、pywebview 线程每 800ms 读 ⇒ 可读到 "status=done 但 stocks 还是上一批"
# 的中间态；force 重扫还能与残留线程重叠写同一 state/磁盘。
_BREAKOUT_LOCK = threading.Lock()


class Api:
    """暴露给前端的 js_api 方法（pywebview 序列化返回值）。"""

    # 东财标的（平均股价）短缓存 {secid: (ts, price, pre_close, src)}，见 _em_last_price
    _em_cache = {}
    # 东财「所属板块」缓存 {code: (ts, boards)}，见 load_stock_profile（TTL 见 _PROFILE_TTL）
    _profile_cache = {}
    _PROFILE_TTL = 600.0
    # 两市成交额缓存 {"payload": (ts, payload)}，见 load_market_turnover
    _turnover_cache = {}
    # 近 N 日成交额缓存，见 load_turnover_history
    _turnover_hist_cache = {}

    def __init__(self):
        self._dates_cache = None
        # 建仓/加仓信号增量轮询内存态
        self._pos = {"date": None, "offset": 0, "seen": set()}

    # ---------- 日期发现 ----------
    def available_dates(self):
        dates = set()
        for p in OUT.glob("daily_review_*.json"):
            stem = p.stem
            if stem.startswith("daily_review_") and len(stem) == len("daily_review_") + 10:
                dates.add(stem[len("daily_review_"):])
        today = datetime.now().strftime("%Y-%m-%d")
        dates.add(today)  # 今天始终在首位（默认选中今天进入LIVE，即使盘前尚无数据）
        return sorted(dates, reverse=True)

    # ---------- 单日完整载荷 ----------
    def load_day(self, date=None):
        if not date:
            dates = self.available_dates()
            if not dates:
                return {"date": None, "error": "未找到 daily_review_*.json，请先运行 daily_review.py"}
            date = dates[0]

        out = {"date": date}
        today = datetime.now().strftime("%Y-%m-%d")
        dr_path = OUT / f"daily_review_{date}.json"
        if not dr_path.exists():
            # 今天盘中可能尚无 daily_review，返回部分载荷供实时模式用
            if date == today:
                out.update({
                    "sig_stat": {}, "shadow": {"total": None, "near": {}},
                    "qty_freeze": {}, "closed_loop": {}, "audit_problems": None,
                    "settle": {}, "watch": {}, "kpi": {},
                    "add_watch": self.compute_add_watch(date),
                    "positions": self._load_positions(date, {}),
                    "position_builder": self._agg_position_builder(date),
                    "stage_board": self._load_stage_board(),
                    "portfolio_config": self.load_portfolio_config(),
                    "report_md": "", "name_map": {},
                })
                return _clean(out)
            return {"date": date, "error": f"无 {date} 复盘数据"}

        try:
            dr = json.loads(open(dr_path, encoding="utf-8").read())
        except Exception as e:
            return {"date": date, "error": f"读取 {dr_path.name} 失败: {e}"}

        out["sig_stat"] = dr.get("sig_stat", {})
        out["shadow"] = {"total": dr.get("shadow_total"), "near": dr.get("shadow_near_±3", {})}
        out["qty_freeze"] = dr.get("qty_freeze", {})
        out["closed_loop"] = dr.get("closed_loop", {})
        out["audit_problems"] = dr.get("audit_problems")
        out["settle"] = dr.get("settle", {})
        out["add_watch"] = dr.get("add_watch", {})
        # 加仓观察为空 或 缺 conditions（突破箱体条件）→ 实时计算
        dr_aw = out["add_watch"]
        if not dr_aw or not any(
            isinstance(v, dict) and "conditions" in v for v in dr_aw.values()):
            out["add_watch"] = self.compute_add_watch(date)
        out["watch"] = dr.get("watch", {})

        # KPI：优先独立文件，缺失时回退到日复盘内嵌 kpi
        kpi_fp = OUT / f"kpi_{date}.json"
        if kpi_fp.exists():
            out["kpi"] = _load_json(kpi_fp, {})
        else:
            out["kpi"] = dr.get("kpi", {})

        out["positions"] = self._load_positions(date, out.get("kpi", {}))
        out["position_builder"] = self._agg_position_builder(date)
        out["stage_board"] = self._load_stage_board()

        out["portfolio_config"] = self.load_portfolio_config()
        out["name_map"] = self._build_name_map(
            out["sig_stat"], out["add_watch"], out["position_builder"], out["positions"]["current"]
        )
        return _clean(out)

    def _build_name_map(self, sig_stat, add_watch, pb, current):
        """汇总各数据源构建 {code: name} 映射，供信号条/持仓/结算显示股票名。
        （828fcea6 误删本方法只留调用，load_day 完整路径会 AttributeError；此处恢复原实现）"""
        names = dict(NAMES)
        for src in (sig_stat, add_watch, current):
            for code, info in (src or {}).items():
                nm = info.get("name") if isinstance(info, dict) else None
                if nm:
                    names[code] = nm
        for code, rec in ((pb or {}).get("by_code") or {}).items():
            for bucket in rec.values():
                row = bucket.get("best") or bucket.get("latest")
                if row and row.get("name"):
                    names[code] = row["name"]
        return names

    # ---------- K4 跨日胜率 ----------
    def kpi_trend(self, days=10):
        dates = self.available_dates()[: int(days)]
        pts = []
        for d in reversed(dates):
            k4 = None
            kpi_fp = OUT / f"kpi_{d}.json"
            if kpi_fp.exists():
                k4 = _load_json(kpi_fp, {}).get("K4_rolling_wr")
            else:
                dr = _load_json(OUT / f"daily_review_{d}.json", {})
                k4 = dr.get("kpi", {}).get("K4_rolling_wr")
            if not k4:
                continue
            buy = k4.get("buy") or {}
            sell = k4.get("sell") or {}
            pts.append({
                "date": d,
                "buy_wr": buy.get("wr"),
                "buy_n": buy.get("n"),
                "sell_wr": sell.get("wr"),
                "sell_n": sell.get("n"),
            })
        return _clean(pts)

    # ---------- 大盘趋势打分 ----------
    def load_market_score(self, date=None):
        """跨日 S 打分曲线 + 当日盘中曲线。"""
        out = {"history": [], "intraday": []}
        state = _load_json(IDX_REGIME / "state.json", {})
        hist = state.get("history") or state.get("score_history") or []
        if state.get("history"):
            out["history"] = [
                {"date": h.get("date"), "S": h.get("S"), "sadj": h.get("sadj"),
                 "regime": h.get("regime")}
                for h in state["history"][-20:]
            ]
        else:
            out["history"] = [
                {"date": h.get("date"), "S": h.get("S"), "regime": None}
                for h in hist
            ]
        out["last_regime"] = state.get("last_regime")
        out["days_in_regime"] = state.get("days_in_regime")

        # 当日盘中曲线（jsonl 逐行容错）
        if date:
            fp = IDX_REGIME / "traces" / f"index_regime_{date}.jsonl"
            if fp.exists():
                for line in open(fp, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    out["intraday"].append({
                        "ts": r.get("ts"), "time": (r.get("ts") or "")[-8:],
                        "score": r.get("score"), "regime": r.get("regime"),
                        "regime_name": r.get("regime_name"),
                    })
        return _clean(out)

    # ---------- 两市成交额（2026-09-30） ----------
    # 成交额只看分钟线（见 _market_turnover 的三条口径说明），60s 去重：GUI 10s 轮询不至于每轮打两次 GM。
    _TURNOVER_TTL = 60.0

    def load_market_turnover(self):
        """两市成交额（外层硬超时 2.5s；超时给 {available:False} 占位，后台继续、下轮缓存命中）。"""
        return self._bounded(self._market_turnover_impl, 2.5, {"available": False})

    def _market_turnover_impl(self):
        now = _time_mod.time()
        hit = Api._turnover_cache.get("payload")
        if hit and (now - hit[0]) < self._TURNOVER_TTL:
            return hit[1]
        try:
            out = self._market_turnover()
        except Exception as e:                      # 兜底：绝不让一张卡片拖垮指数板
            out = {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
        Api._turnover_cache["payload"] = (now, out)
        return out

    @staticmethod
    def _market_turnover():
        """两市成交额 + 与**昨日同期**的变化（owner 2026-09-30）。

        三条口径（都是实测踩过才知道的）：

        1) **两条腿 = 上证指数 + 深证综指(sz399106)**，不是深证成指(sz399001)。
           实测 2026-09-30 分钟线「分时合计 ÷ 日线 amount」：
             上证指数 1.000、深证综指 1.000、**深证成指 0.471**。
           ⇒ 深证成指的**日线** amount 是深市全市场（≈深证综指），但它的**分钟** amount
             只有成分股。盘中要按分时累计，用深证成指会把深市少算一半。
           注：本口径与 `analysis/index_regime._two_market_amount`（上证+深证成指）**数值一致**
           —— 因为那用的是日线字段，而深证成指日线 == 深证综指日线。

        2) **只读分钟线**（不用日线）：日线在盘中/盘后初段**不含当日 bar**
           （gm history_n 实测 16:00 后仍只返回到昨日），要拿当日得走 forming bar，
           而 forming bar 只在 `end_date is None` 时拼接、会回写共享长历史缓存
           （见 memory「指数日线缓存会被静默截短」）。分钟线一次拿到今昨两天，零缓存风险。

        3) **盘中比昨日同期、收盘比全日**：今日是累计值，10:00 时可能才走 20%，
           直接比昨日全日会显示 −80%，严重误导。故盘中取昨日**同一时刻**的累计值对比
           （owner 选定的口径）；已收盘（或今天不是交易日）则两边都取全日。

        返回 {available, amount, amount_prev, pct, basis: "同期"|"全日", as_of, day, prev_day,
              legs, bse_included}。
        """
        from core.market_data.facade import get_provider
        from core.market_data import sina_index
        per_leg, legs, degraded = {}, [], []

        def _norm(df):
            d = df.copy()
            d["_day"] = d["time"].astype(str).str[:10]
            d["_hm"] = d["time"].astype(str).str[11:16]
            return d

        # 沪深两腿：GM 指数分钟线（含 amount）
        for sym in ("sh000001", "sz399106"):     # 上证指数 + 深证综指
            df = get_provider().index_minute(sym, count_bars=800, freq="300s")
            if df is None or df.empty:
                return {"available": False, "error": f"分钟线不可得({sym})",
                        "degraded": ["index_minute"]}
            per_leg[sym] = _norm(df)
            legs.append(sym)

        # 北交所腿：只能走新浪（见 sina_index 模块 docstring）。取不到则**如实降级为沪深口径**，
        # 而不是静默少算 ~120 亿 —— 那正是本次与同花顺对不上的原因。
        bse = sina_index.fetch_index_minutes("bj899050", scale=5, datalen=400)
        bse_included = bse is not None and not bse.empty and float(bse["amount"].sum()) > 0
        if bse_included:
            per_leg["bj899050"] = _norm(bse)
            legs.append("bj899050")
        else:
            degraded.append("bse_turnover")

        out = Api._turnover_from_minutes(per_leg, datetime.now())
        out["legs"] = legs
        out["bse_included"] = bse_included
        if degraded:
            out["degraded"] = degraded
        return out

    @staticmethod
    def _turnover_from_minutes(per_leg, now):
        """纯计算（无 IO，便于离线单测）：{腿: 分钟线} + 当前时刻 → 成交额与变化。

        见 `_market_turnover` 的三条口径说明。关键点：
          · 「当前交易日」取分钟数据里的**最新一天**，不是日历日期 ⇒ 周末/节假日自动落到上一交易日
          · 盘中取两边 **≤ 当前时刻** 的累计；收盘后（或今天非交易日）两边都取全日
        """
        cur_day = max(d["_day"].max() for d in per_leg.values())
        days = sorted({x for d in per_leg.values() for x in d["_day"].unique()})
        prior = [x for x in days if x < cur_day]
        if not prior:
            return {"available": False, "error": "分钟线里没有上一交易日", "degraded": ["index_minute"]}
        prev_day = prior[-1]

        hm = now.strftime("%H:%M")
        # 盘中 = 今天就是当前交易日 且 未到收盘；否则（周末/收盘后）两边都按全日
        intraday = (cur_day == now.strftime("%Y-%m-%d") and hm < "15:00")

        def _sum(day):
            per, as_of = {}, None
            for sym, d in per_leg.items():
                sub = d[d["_day"] == day]
                if intraday:
                    sub = sub[sub["_hm"] <= hm]
                if len(sub):
                    per[sym] = float(sub["amount"].sum())
                    last = str(sub["_hm"].iloc[-1])
                    as_of = last if as_of is None else max(as_of, last)
            return per, as_of

        per_cur, as_of = _sum(cur_day)
        per_prev, _ = _sum(prev_day)
        amount, amount_prev = sum(per_cur.values()), sum(per_prev.values())
        if not amount or not amount_prev:
            return {"available": False, "error": "成交额累计为 0", "degraded": ["index_minute"]}
        return {
            "available": True,
            "amount": amount, "amount_prev": amount_prev,
            "pct": (amount / amount_prev - 1.0) * 100.0,
            "basis": "同期" if intraday else "全日",
            "as_of": as_of, "day": cur_day, "prev_day": prev_day,
            "by_leg": per_cur,          # 分腿拆解（前端 tooltip 用）
        }

    # ---------- 近 N 日成交额（「盘中状态」卡柱状图，2026-09-30） ----------
    _TURNOVER_HIST_TTL = 120.0

    def load_turnover_history(self, days=60):
        """近 N 日全市场成交额（外层硬超时 2.0s；超时返回 warming，前端显示加载中、稍后自填）。"""
        return self._bounded(lambda: self._turnover_history_cached(days), 2.0,
                             {"available": False, "warming": True, "days": []})

    def _turnover_history_cached(self, days=60):
        """近 N 个交易日的**全市场**成交额（柱状图用）。失败返回 {available: False, error}，永不抛。"""
        now = _time_mod.time()
        hit = Api._turnover_hist_cache.get("payload")
        if hit and (now - hit[0]) < self._TURNOVER_HIST_TTL:
            return hit[1]
        try:
            out = self._turnover_history_impl(days)
        except Exception as e:
            out = {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
        Api._turnover_hist_cache["payload"] = (now, out)
        return out

    def _turnover_history_impl(self, days):
        """拼装近 N 日全市场成交额（与 `load_market_turnover` 同口径：沪 + 深 + 北）。

        三源：
          · 沪深：`index_daily(上证指数/深证综指)` 的 amount。**传 end_date** ⇒ provider 不回写
            共享长历史缓存（不传会把 800 行截短，见 memory「指数日线缓存会被静默截短」）。
          · 北交所：新浪 `bj899050` **30 分钟**线按日求和。为什么不用别的精度：
            日线**无 amount**（只有 volume）；5 分钟线 `datalen` 上限只够 ~32 天；
            30 分钟 `datalen=1500` 给 188 天，且按日合计与 5 分钟口径**逐位一致**（实测 125.3/122.4 亿）。
          · 今日那一根：用 `load_market_turnover()` 的实时值**覆盖** ⇒ 柱状图最后一根与
            第 8 张卡片上的数字严格一致，不会出现"图上和卡上不一样"。

        ⚠️ 盘中 GM 日线不含当日 ⇒ 若实时交易日不在日线序列里，**补一根**。
        """
        from core.market_data.facade import get_provider
        from core.market_data import sina_index
        today = datetime.now().strftime("%Y-%m-%d")
        prov = get_provider()

        hs = {}
        for sym in ("sh000001", "sz399106"):
            df = prov.index_daily(sym, days=int(days) + 60, end_date=today)
            if df is None or df.empty:
                return {"available": False, "error": f"指数日线不可得({sym})",
                        "degraded": ["index_daily"]}
            for _, r in df.iterrows():
                d = str(r["date"])
                hs[d] = hs.get(d, 0.0) + float(r.get("amount") or 0.0)
        if not hs:
            return {"available": False, "error": "沪深日线为空", "degraded": ["index_daily"]}

        live = self.load_market_turnover()
        live_day = live.get("day") if live.get("available") else None
        live_amount = live.get("amount") if live.get("available") else None

        bse = {}
        sm = sina_index.fetch_index_minutes("bj899050", scale=30, datalen=1500)
        if sm is not None and not sm.empty:
            sm = sm.copy()
            sm["_d"] = sm["time"].astype(str).str[:10]
            bse = sm.groupby("_d")["amount"].sum().to_dict()

        return Api._assemble_turnover_history(hs, bse, days, live_day, live_amount, datetime.now())

    @staticmethod
    def _assemble_turnover_history(hs, bse, days, live_day, live_amount, now):
        """纯计算（无 IO，便于离线单测）：沪深/北交所按日成交额 + 实时值 → 柱状图数据。

        · 交易日集合 = 沪深日线日期 ∪ {实时交易日}（盘中 GM 日线不含当日 ⇒ 必须补这一根）
        · 最后一根的 amount 用**实时值覆盖** ⇒ 与「两市成交额」卡片数字严格一致
        · `in_progress` 只标最后一根，且仅在「该日就是今天且未到 15:00」时
        · `delta_pct` 环比**前一交易日**；首日无前值 ⇒ None
        """
        all_dates = sorted(set(hs) | ({live_day} if live_day else set()))
        dates = all_dates[-int(days):]
        rows = [{"date": d, "amount": float(hs.get(d, 0.0)) + float(bse.get(d, 0.0))} for d in dates]
        if live_day and live_amount is not None and rows and rows[-1]["date"] == live_day:
            rows[-1]["amount"] = float(live_amount)
        today = now.strftime("%Y-%m-%d")
        in_progress = bool(rows and rows[-1]["date"] == today and now.strftime("%H:%M") < "15:00")
        for i, r in enumerate(rows):
            prev = rows[i - 1]["amount"] if i > 0 else None
            r["delta_pct"] = ((r["amount"] / prev - 1.0) * 100.0) if prev else None
            r["in_progress"] = bool(in_progress and i == len(rows) - 1)
            r["bse"] = float(bse.get(r["date"], 0.0))
        return {"available": True, "days": rows, "in_progress": in_progress,
                "bse_included": bool(bse), "legs": ["sh000001", "sz399106", "bj899050"]}

    # ---------- 主要指数概览（2026-09-28：GUI_INDEX_BOARD 单一真源 + 掘金主源） ----------
    @staticmethod
    def _bounded(fn, timeout, default):
        """硬超时执行 fn；超时返回 default（后台线程继续跑，跑完会写各自的缓存/占位）。
        2026-10-07：GUI 的重显示端点（指数板/成交额历史）容易在 GM/东财抖动时卡十几秒，
        而 js_api 是主线程同步调用 ⇒ 冻界面。用这个把它们的时间上界钉住。"""
        import concurrent.futures as _cf
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(fn).result(timeout=timeout)
        except Exception:
            return default
        finally:
            ex.shutdown(wait=False)

    def report_slow_call(self, name, ms):
        """前端上报的慢 js_api 调用（>=800ms）→ 追加到 `t_io/logs/gui_slow_calls.log`。
        2026-10-07：GUI 无自身日志，卡死只能靠外部探测；这里给"卡在哪"留痕。失败静默。"""
        try:
            log_fp = BASE / "t_io" / "logs" / "gui_slow_calls.log"
            log_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {str(name)[:40]} {float(ms):.0f}ms\n")
        except Exception:
            pass
        return {"ok": True}

    def load_indices(self):
        """指数状态板（外层硬超时 2.5s；超时给占位、后台继续，防 GM 抖动冻界面）。"""
        return self._bounded(self._load_indices_impl, 2.5,
                             {"ts": None, "indices": [], "regime": None, "error": "指数板取数超时"})

    def _load_indices_impl(self):
        """拉指数板实时行情 + 大盘 regime。返回 {ts, indices:[{symbol,name,price,change,change_pct,source}], ...}

        指数列表来自 core.board_index.GUI_INDEX_BOARD（**单一真源**，7 项）。
        实时源：**GM 主源** —— get_provider().index_snapshot()（内部 gm.api.current，指数符号经
        _gm_index_symbol 正确编码）；GM 不可用时 facade 内建降级腾讯 index_auction。
        返回的 source 字段标明本条实际来自 "gm" 还是 "tencent"。
        平均股价（source="em"）非交易所指数、掘金无此标的 → 单列走东财 push2delay。
        """
        out = {"ts": None, "indices": [], "regime": None, "days_in_regime": None}
        try:
            import os as _os
            import urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"

            from core.board_index import gui_board
            board = gui_board()

            # 真指数：GM 主源（facade 内建腾讯兜底）
            gm_items = [i for i in board if i.get("source") == "gm"]
            snaps = {}
            if gm_items:
                try:
                    from core.market_data import get_provider
                    snaps = get_provider().index_snapshot([i["symbol"] for i in gm_items]) or {}
                except Exception:
                    snaps = {}

            for i in board:
                sym, name, src = i["symbol"], i["name"], i.get("source")
                if src == "gm":
                    d = snaps.get(sym) or {}
                    price = d.get("price")
                    if not price:
                        # 取不到也占位：卡片置灰显示原因，不静默消失（否则用户不知道为什么少一个）
                        out["indices"].append({"symbol": sym, "name": name,
                                               "error": "掘金与腾讯均取不到", "source": "gm"})
                        continue
                    out["indices"].append({
                        "symbol": sym, "name": name, "price": price,
                        "change": (round(d["change"], 3) if d.get("change") is not None else None),
                        "change_pct": (round(d["change_pct"], 2)
                                       if d.get("change_pct") is not None else None),
                        "source": d.get("source") or "gm",
                    })
                elif src == "em":
                    # 东财特殊条目（A股平均股价等）：非交易所指数，掘金无此标的
                    secid = str(sym)[2:] if str(sym).startswith("em") else str(sym)
                    price, pre_close, esrc = self._em_last_price_bounded(secid)
                    if not price:
                        out["indices"].append({"symbol": sym, "name": name,
                                               "error": "东财风控/不可达", "source": "em"})
                        continue
                    chg = (price - pre_close) if pre_close else None
                    out["indices"].append({
                        "symbol": sym, "name": name, "price": price,
                        "change": round(chg, 3) if chg is not None else None,
                        "change_pct": (round(chg / pre_close * 100.0, 2)
                                       if (chg is not None and pre_close) else None),
                        "source": esrc,
                    })
            out["ts"] = datetime.now().strftime("%H:%M:%S")
        except Exception:
            pass
        # 大盘 regime 状态
        try:
            state = _load_json(IDX_REGIME / "state.json", {})
            out["regime"] = state.get("last_regime")
            out["days_in_regime"] = state.get("days_in_regime")
        except Exception:
            pass
        return _clean(out)

    # ---------- 东财特殊标的报价（平均股价等；2026-09-28） ----------
    def _em_last_price(self, secid: str):
        """东财标的实时价 → (price, pre_close, src)；取不到返回 (None, None, None)。

        push2delay/push2his 均有**间歇风控**（仓库既有注释：重试 8 次多数能成），
        故两条路都重试。全部失败时返回 None，由调用方决定是否跳过该卡片。
        退到 K 线时**只在能确定昨收时才给 pre_close**——宁可不显示涨跌幅，也不显示错的。
        """
        import os as _os
        import time as _t
        import urllib.request as _ur
        # 短缓存：本函数在 10s 轮询里被调，若东财持续风控，
        # 每次都跑「4 次实时重试 + 2×8 次 K 线重试」会把界面拖死。
        # 成功缓存 120s，**失败也缓存 60s**（负缓存）——否则东财一挂，每轮都白等几秒。
        # 代价只是 平均股价 最多滞后 2 分钟（情绪展示指标，不影响交易）。
        _cache = Api._em_cache
        _hit = _cache.get(secid)
        if _hit:
            _ttl = 120 if _hit[1] else 60
            if (_t.time() - _hit[0]) < _ttl:
                return _hit[1], _hit[2], _hit[3]
        for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"]:
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        # 1) 实时（带重试）——2026-10-07：重试 4→2、超时 5→3s（东财风控时原设置首拉要 ~14s，冻界面）
        for _ in range(2):
            try:
                url = (f"https://push2delay.eastmoney.com/api/qt/stock/get?"
                       f"secid={secid}&fields=f43,f44,f45,f57,f58")
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://quote.eastmoney.com/"})
                ed = (json.loads(_ur.urlopen(req, timeout=3).read()
                                 .decode("utf-8", errors="ignore")).get("data") or {})
                px, pc = (ed.get("f43") or 0) / 100.0, (ed.get("f44") or 0) / 100.0
                if px and pc:
                    _cache[secid] = (_t.time(), px, pc, "em")
                    return px, pc, "em"
            except Exception:
                _t.sleep(0.4)
        # 2) 退到 K 线（analysis.index_divergence._em_bars 自带 8 次重试）
        try:
            from analysis.index_divergence import _em_bars
            bars = _em_bars("em" + str(secid), "30min")
            if bars is not None and not bars.empty:
                px = float(bars["close"].iloc[-1])
                pc = None
                daily = _em_bars("em" + str(secid), "日线")
                today = datetime.now().strftime("%Y-%m-%d")
                if daily is not None and not daily.empty and len(daily) >= 2:
                    # 仅当日线最新一根是今天时，才能确定昨收 = 倒数第二根
                    # （不引 pandas：t_gui 只在函数内局部 import pd）
                    if str(daily["time"].iloc[-1])[:10] == today:
                        pc = float(daily["close"].iloc[-2])
                if pc:
                    _cache[secid] = (_t.time(), px, pc, "em-kline")
                return px, pc, "em-kline"
        except Exception:
            pass
        _cache[secid] = (_t.time(), None, None, None)      # 负缓存，见函数开头注释
        return None, None, None

    def _em_last_price_bounded(self, secid, timeout=1.0):
        """带硬超时的 `_em_last_price`（2026-10-07）：东财风控时它最坏会跑「重试 + K线兜底」
        十几秒，而 `load_indices` 在主线程上 ⇒ 冻界面。超时即返回 (None,None,None)，后台线程继续
        （跑完会写负缓存，下一轮直接命中）。"""
        import concurrent.futures as _cf
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(self._em_last_price, secid).result(timeout=timeout)
        except Exception:
            return None, None, None
        finally:
            ex.shutdown(wait=False)

    # ---------- 指数背离（2026-09-28） ----------
    _div_cache = {}          # {"t": ts, "v": res}——见 load_index_divergence 的 TTL 说明
    _DIV_TTL = 180           # 秒

    def load_index_divergence(self):
        """指数板各指数 30min/60min/日线的顶/底背离（**观察提示，非交易信号**）。

        检测核复用 analysis.divergence；证据分级与免责说明随每条返回。
        同一事件按 key 去重（前端与飞书共用同一 key）。

        ⚠️ **必须带 TTL 缓存**：一次检测要打 12+ 次 GM 调用（7 指数 × 分时/日线），
        而前端把它挂在 **10s** 的实时轮询上。无缓存时若 GM 变慢，每轮都要等十几秒，
        请求堆积会把 pywebview 拖到窗口释放（2026-09-28 实测：GM 超时期间日志出现
        `index_minute ... 超时>12s` + `[pywebview] Error` + ObjectDisposedException）。
        背离是低频事件，180s 粒度足够（后端 main.py 的推送钩子本身也是 300s 节奏）。
        """
        import time as _t
        now = _t.time()
        hit = Api._div_cache
        if hit.get("v") is not None and (now - hit.get("t", 0)) < Api._DIV_TTL:
            return hit["v"]
        # 2026-10-07 SWR：冷/过期时**后台算 + 立即返回**（旧值或 warming），不再在主线程等 ~6s
        # （一次检测 12+ 次 GM 调用）。前端挂在 60s 的 loadAndRender 上 ⇒ 后台算完下一轮即出。
        if not getattr(self, "_div_running", False):
            self._div_running = True

            def _bg():
                try:
                    from analysis.index_divergence import detect_index_divergence
                    res = _clean(detect_index_divergence())
                except Exception as e:
                    res = _clean({"alerts": [], "watching": [], "health": {},
                                  "error": f"{type(e).__name__}: {str(e)[:160]}"})
                Api._div_cache = {"t": _t.time(), "v": res}
                self._div_running = False
            _th.Thread(target=_bg, daemon=True).start()
        if hit.get("v") is not None:
            return hit["v"]
        return _clean({"alerts": [], "watching": [], "health": {}, "warming": True})

    # ---------- 持仓成本历史 ----------
    def load_cost_history(self):
        """读全部 holdings 快照 + 人工校准文件，按股按日聚合 cost。
        校准优先（src=人工校准），否则快照值（src=快照）。"""
        dates, stocks = [], {}
        calib = _load_json(STATE_DIR / "cost_calibration.json", {}).get("calibrations", {})
        for fp in sorted(STATE_DIR.glob("holdings_*.json")):
            if "holdings_daily" in fp.stem:
                continue  # 跳过 holdings_daily_* 文件（结构不同）
            d = fp.stem.replace("holdings_", "")
            # 2026-10-04 拆分：holdings_manual/auto.json 也匹配 holdings_* 通配，
            # 只接受日期名（YYYY-MM-DD）的快照文件，防新文件名被当成 bogus 日期污染成本图。
            if len(d) != 10 or d[4] != "-" or d[7] != "-":
                continue
            try:
                snap = json.loads(open(fp, encoding="utf-8").read())
            except Exception:
                continue
            if not snap:
                continue
            dates.append(d)
            calib_day = calib.get(d, {})
            for code, info in snap.items():
                if not isinstance(info, dict):
                    continue
                cost = info.get("cost")
                if cost is None:
                    continue
                src = "快照"
                if code in calib_day and calib_day[code] is not None:
                    cost = float(calib_day[code])
                    src = "人工校准"
                st = stocks.setdefault(code, {"name": info.get("name", code), "points": []})
                qty = info.get("qty", 0) or 0
                pc = info.get("pre_close") or 0
                pnl_amt = round((pc - float(cost)) * qty, 2) if (pc and qty) else None
                pnl_pct = round((pc / float(cost) - 1) * 100, 2) if (pc and float(cost)) else None
                st["points"].append({"date": d, "cost": float(cost), "src": src,
                                     "pnl_amt": pnl_amt, "pnl_pct": pnl_pct,
                                     "qty": qty, "pre_close": pc})

        # 今日有效成本（校准优先，否则当前 holdings）供预填
        today = datetime.now().strftime("%Y-%m-%d")
        cur = _load_json(HOLDINGS_MANUAL, {})
        effective = {}
        calib_today = calib.get(today, {})
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            if code in calib_today and calib_today[code] is not None:
                effective[code] = {"cost": float(calib_today[code]), "src": "人工校准"}
            elif info.get("cost") is not None:
                effective[code] = {"cost": float(info["cost"]), "src": "快照"}
        return _clean({"dates": dates, "stocks": stocks,
                       "effective_today": effective,
                       "calibrated_dates": sorted(calib.keys())})

    def save_cost_calibration(self, date, costs):
        """保存人工校准成本（date → {code: cost}），原子写。"""
        fp = STATE_DIR / "cost_calibration.json"
        data = _load_json(fp, {})
        if not isinstance(data, dict) or "calibrations" not in data:
            data = {"version": 1, "calibrations": {}}
        calib = data.setdefault("calibrations", {})
        day = calib.setdefault(date, {})
        if isinstance(costs, dict):
            for code, cost in costs.items():
                try:
                    val = float(cost)
                except (TypeError, ValueError):
                    continue
                day[code] = val if val > 0 else None
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "updated_at": data["updated_at"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 实时行情（顶部行情条） ----------
    # 行情刷新：SWR（stale-while-revalidate）。实测 snapshot_auction(49 只) 约 230ms，
    # 前端每 10s 轮询一次，这 230ms 全卡在 pywebview 线程上（K线弹窗卡顿排查 2026-10-04）。
    # 改为：命中新鲜窗口直接返回；略旧则**立即返回旧值 + 后台线程刷新**；过旧才阻塞重拉。
    _QUOTES_SWR_FRESH = 8.0     # 新鲜期：直接返回，连后台刷新都不触发
    _QUOTES_SWR_MAX = 90.0      # 超过此年龄宁可阻塞也拉一次（防后台长期失败把价格冻住）

    def load_quotes(self):
        """腾讯实时行情（SWR 包装；首次调用仍为阻塞拉取）。"""
        c = getattr(self, "_quotes_cache", None)
        if c is not None:
            _ts, _val = c
            _age = _time_mod.time() - _ts
            if _age < self._QUOTES_SWR_FRESH:
                return _val
            if _age < self._QUOTES_SWR_MAX and not getattr(self, "_quotes_refreshing", False):
                self._quotes_refreshing = True

                def _bg():
                    try:
                        self._load_quotes_sync()
                    except Exception:
                        pass
                    finally:
                        self._quotes_refreshing = False

                _th.Thread(target=_bg, name="quotes-refresh", daemon=True).start()
                return _val
        return self._load_quotes_sync()

    def _load_quotes_sync(self):
        """阻塞拉取一次并写入缓存。"""
        _r = self._load_quotes_build()
        self._quotes_cache = (_time_mod.time(), _r)
        return _r

    def _load_quotes_build(self):
        """拉腾讯实时行情（持仓 + watchlist 候选股），失败回退 pre_close。"""
        cur = dict(_load_json(HOLDINGS_MANUAL, {}))
        # 合并 watchlist_buy 候选股（非持仓的也拉，供建仓表实时价）
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
        for code, info in (wl.get("stocks", {}) or {}).items():
            if code not in cur and isinstance(info, dict):
                cur[code] = {"name": info.get("name", code), "qty": 0, "cost": None,
                             "pre_close": 0, "in_watchlist": True}
        out = {"source": "fallback", "ts": None, "quotes": []}
        if not cur:
            return out
        def _to_symbol(c):
            """600176→sh600176, 000988→sz000988。后缀 _B 等剥离。"""
            base = c.split("_")[0]
            return ("sh" + base if base[0] in "56" else "sz" + base)
        symbols = {}
        for code in cur:
            sym = _to_symbol(code)
            if sym not in symbols.values():  # 同一只股票（如 000988/000988_B）只请求一次
                symbols[code] = sym
        # P1-2 收敛：tencent_provider.snapshot_auction（快照/竞价专用保留腾讯）
        from core.market_data.tencent_provider import TencentProvider
        snaps = TencentProvider().snapshot_auction(list(symbols.keys()))
        # 2026-10-06: 保留批量快照（含 ts_date/open/high/low/vol_hand）供 K线「补当日 bar」复用，
        # 免去每只票单独打一次快照（假期/首帧实测 1.9s/只）。
        try:
            self._quotes_snaps = (_time_mod.time(), snaps or {})
        except Exception:
            pass

        ts = datetime.now().strftime("%H:%M:%S")
        out["ts"] = ts
        if not snaps:
            # 回退：用 pre_close
            out["source"] = "fallback"
            for code, info in cur.items():
                if not isinstance(info, dict):
                    continue
                pc = info.get("pre_close") or 0
                out["quotes"].append({
                    "code": code, "name": info.get("name", code),
                    "price": pc, "pre_close": pc, "change": 0.0, "change_pct": 0.0,
                    "cost": info.get("cost"), "pnl_pct": None, "offline": True,
                    "qty": info.get("qty", 0), "base": info.get("base", 0),
                    "t_qty": info.get("t_qty", 0),
                })
            self._write_daily_holdings(out)
            return out

        out["source"] = "live"
        # 1) snapshot_auction → {base_code: {price,pre_close,change,change_pct}}
        px = {}
        for base, d in snaps.items():
            price = d.get("price") or 0
            pre_close = d.get("pre_close") or 0
            px[base] = {
                "price": price, "pre_close": pre_close,
                "change": round(price - pre_close, 3) if price and pre_close else 0.0,
                "change_pct": d.get("pct") or 0.0,
            }

        # 2) 按 holdings 条目驱动 → 每个条目用自己的 code/cost/qty，查同一个 base 的价格
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            base = code.split("_")[0]    # 000988_B → 000988
            p = px.get(base)
            if p is None:
                continue
            cost = info.get("cost")
            pnl = (p["price"] / cost - 1) * 100 if cost else None
            out["quotes"].append({
                "code": code, "name": info.get("name", code),
                "price": p["price"], "pre_close": p["pre_close"],
                "change": p["change"], "change_pct": p["change_pct"],
                "cost": cost, "pnl_pct": pnl, "offline": False,
                "qty": info.get("qty", 0), "base": info.get("base", 0),
                "t_qty": info.get("t_qty", 0),
            })
        self._write_daily_holdings(out)
        return _clean(out)

    def save_daily_holdings(self):
        """写今日持仓每日快照（含数量/成本/盈亏）。走同步路径取**新鲜**报价，不用 SWR 旧值。"""
        return self._write_daily_holdings(self._load_quotes_sync())

    def _write_daily_holdings(self, q):
        """数量/成本读用户每天更新的 holdings.json，盈亏按盘中实时价计算。
        2026-09-27 净值法改造：summary 增加 cash/available/cash_source（现金口径
        = accounts_config 各启用账户 available 之和，取不到为 null + "missing"）；
        顶层增加 eod 标志（交易日 15:00 后写 = true，复盘只认 eod=true 的行）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        fp = STATE_DIR / f"holdings_daily_{today}.json"
        cur = _load_json(HOLDINGS_MANUAL, {})
        rows = []
        total_value = total_cost = total_pnl = 0.0
        for qq in q.get("quotes", []):
            code = qq.get("code")
            info = cur.get(code, {})
            if not isinstance(info, dict):
                continue
            qty = info.get("qty", 0)
            cost = info.get("cost")
            price = qq.get("price")
            pnl_amt = None
            if cost and price and qty:
                pnl_amt = round((price - cost) * qty, 2)
                total_value += price * qty
                total_cost += cost * qty
                total_pnl += (price - cost) * qty
            rows.append({
                "code": code, "name": qq.get("name", code),
                "account": _account_of(code), "type": info.get("type", ""),
                "qty": qty, "base": info.get("base", 0),
                "cost": cost, "pre_close": info.get("pre_close"),
                "price": price, "change_pct": qq.get("change_pct"),
                "pnl_pct": qq.get("pnl_pct"), "pnl_amt": pnl_amt,
                "offline": bool(qq.get("offline")),
            })
        summary = {
            "total_value": round(total_value, 2) if total_value else None,
            "total_cost": round(total_cost, 2) if total_cost else None,
            "total_pnl": round(total_pnl, 2) if total_pnl else None,
            "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else None,
        }
        # 现金落盘（净值法 equity = 持仓市值 + 现金）：accounts_config 最新 reconcile
        cash, cash_source = _reconcile_cash()
        summary["cash"] = cash
        summary["available"] = cash
        summary["cash_source"] = cash_source
        # EOD 定值：交易日 15:00 后快照价格即收盘价，标 eod=true；盘中/周末 eod=false。
        # 交易日按 weekday<5 近似（沿用仓库既有口径，法定节假日会误判为 eod=true）。
        now = datetime.now()
        is_eod = now.weekday() < 5 and now.strftime("%H:%M") >= "15:00"
        data = {
            "date": today,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source": q.get("source", "fallback"),
            "eod": is_eod,
            "holdings": rows,
            "summary": summary,
        }
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_clean(data), f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "path": str(fp)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 实时 console ----------
    KEY_LINE_WORDS = ["推送", "信号得分", "拦截", "阻断", "熔断", "告警", "建仓",
                      "策略卡", "竞价", "接回", "WARNING", "ERROR", "异常",
                      "仓控", "急跌", "追涨", "成交", "闭环", "已推送",
                      "大盘", "评分=", "进攻", "防守", "触发",
                      "已卖", "已买", "未接回", "启动自检"]
    NOISE_LINE_WORDS = ["扫描心跳", "缓存", "数据更新完成", "poll", "网络重试",
                        "本轮耗时", "等待", "进入下一轮", "非交易时段", "低频保活"]

    def load_console(self, date, since=0):
        """增量读日志。since=字节偏移，返回新增行+新偏移。"""
        _GUI_HB["ts"] = _time_mod.time()   # 前端 2s 轮询打点：看门狗据此判界面是否卡死
        fp = LOGS_DIR / f"t_trader_sys_{date}.log"
        out = {"lines": [], "offset": since, "exists": fp.exists(), "eof": True}
        if not fp.exists():
            return out
        try:
            size = fp.stat().st_size
            if size < since:
                since = 0  # 日志轮转/重建
            with open(fp, encoding="utf-8", errors="replace") as f:
                f.seek(since)
                data = f.read()
            out["offset"] = size
            out["eof"] = size == 0 or data == "" or data.endswith("\n")
            for line in data.splitlines():
                if not line.strip():
                    continue
                out["lines"].append(self._parse_log_line(line))
        except Exception:
            pass
        return out

    # ---------- 加仓观察（实时计算，不依赖 daily_review） ----------
    _ADD_WATCH_TTL = 120.0   # 加仓观察缓存新鲜期（秒）

    def compute_add_watch(self, date):
        """加仓观察（SWR 包装，2026-10-09）：`load_day`（主线程）会调它，实测冷算 ~5.6s
        （9 只 × 逐只 5min 指标 + 箱体），改 SWR ⇒ 命中直返；过期先返回旧值 + 后台重算；冷启动才同步算。"""
        now = _time_mod.time()
        _h = (getattr(self, "_add_watch_cache", None) or {}).get(date)
        if _h and (now - _h[0]) < self._ADD_WATCH_TTL:
            return _h[1]
        if _h and not getattr(self, "_add_watch_refreshing", False):
            self._add_watch_refreshing = True

            def _bg():
                try:
                    _r = self._compute_add_watch_impl(date)
                    self._add_watch_cache = {date: (_time_mod.time(), _r)}
                except Exception:
                    pass
                finally:
                    self._add_watch_refreshing = False
            _th.Thread(target=_bg, name="add-watch-refresh", daemon=True).start()
            return _h[1]
        _r = self._compute_add_watch_impl(date)
        self._add_watch_cache = {date: (_time_mod.time(), _r)}
        return _r

    def _compute_add_watch_impl(self, date):
        """从分钟快照+最新价实时计算支撑位距离，返回 add_watch 同结构数据。
        替代 daily_review 收盘后才生成的静态 add_watch。"""
        out = {}
        cur = _load_json(HOLDINGS_MANUAL, {})
        if not cur:
            return out

        # 先尝试加载当前快照获取 daily_context（含 MA 支撑位）
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            if not (info.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不进加仓观察
            # 找分钟快照
            ym = datetime.strptime(date, "%Y-%m-%d").strftime("%Y/%m") if len(date) == 10 else datetime.now().strftime("%Y/%m")
            snap_dir = SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots" / ym
            snap_fp = snap_dir / f"{code}_{date}.json"
            if not snap_fp.exists():
                # 尝试带后缀
                candidates = list(snap_dir.glob(f"{code}*{date}.json"))
                snap_fp = candidates[0] if candidates else None
            if not snap_fp or not snap_fp.exists():
                continue

            try:
                snap = json.loads(open(snap_fp, encoding="utf-8").read())
            except Exception:
                continue

            daily_ctx = snap.get("daily_context", {}) if isinstance(snap, dict) else {}
            bars = snap.get("bars", []) if isinstance(snap, dict) else (snap if isinstance(snap, list) else [])
            # 补算日线指标（旧快照缺 daily_macd_golden/trend 等字段时，用日线现算）
            if daily_ctx.get("daily_macd_golden") is None or not daily_ctx.get("daily_trend_bg"):
                self._ensure_daily_ctx_indicators(code, daily_ctx)

            # fix P0-3: 日低改用 bars 的真实 low 最小值（分钟收盘价会丢下影线）；
            # 15:00 前"收盘"实为盘中现价，打 is_intraday 标志，文案统一"现价(盘中暂定)"
            closes = [float(b.get("close", 0)) for b in bars if b.get("close")]
            lows = [float(b.get("low", 0)) for b in bars if b.get("low")]
            if not closes:
                continue
            day_low = min(lows) if lows else min(closes)
            day_close = closes[-1]
            is_intraday = (date == datetime.now().strftime("%Y-%m-%d")
                           and datetime.now().strftime("%H:%M") < "15:00")
            px_word = "现" if is_intraday else "收"
            sfx = "(盘中暂定)" if is_intraday else ""
            # fix P2-16: ETF 标识，缺 MA/VWAP 时写"不适用(ETF)"
            is_etf = str(cur[code].get("type", "")).lower() == "etf"
            # fix P0-6/P1-8: VWAP 只读快照根级 last_vwap，缺失不回退 MA 冒充
            vwap_raw = snap.get("last_vwap") if isinstance(snap, dict) else None
            vwap_val = (float(vwap_raw) if vwap_raw
                        and not (isinstance(vwap_raw, float) and math.isnan(vwap_raw)) else None)

            # 支撑位
            raw_supports = {}
            for key, label in [("daily_ma5", "MA5"), ("daily_ma10", "MA10"),
                               ("daily_ma20", "MA20"), ("daily_ma60", "MA60")]:
                val = daily_ctx.get(key)
                if val and not (isinstance(val, float) and math.isnan(val)):
                    raw_supports[label] = float(val)
            # 近20日低点（fix P1-8: daily_20d_low 无生产端，不再回退 daily_support_level 冒充）
            low20 = daily_ctx.get("daily_20d_low")
            if low20 and not (isinstance(low20, float) and math.isnan(low20)):
                raw_supports["近20日低点"] = float(low20)
            if vwap_val:
                raw_supports["日内VWAP"] = float(vwap_val)
            # fix P1-8: 同一价位(差<0.1%)多标签去重合并为一个标签，如 "MA20/日内VWAP"
            supports = {}
            for label, val in raw_supports.items():
                hit = None
                for k, v in supports.items():
                    if v and abs(val - v) / v < 0.001:
                        hit = k
                        break
                if hit is not None:
                    if label not in hit.split("/"):
                        supports[hit + "/" + label] = supports.pop(hit)
                else:
                    supports[label] = val

            # 回踩事件：fix P1-6 触及窗放宽到 ±1.0%，基于修复后的真实日低(bars low)判定
            events, near = [], []
            for label, level in supports.items():
                dist = (day_low - level) / level * 100 if level else 0
                abs_dist = abs(dist)
                if abs_dist <= 1.0:
                    status = "守住" if day_close >= level else "破位"
                    events.append({"level": label, "support": round(level, 3),
                                   "dist%": round(dist, 2), "status": status})
                elif abs_dist <= 3:
                    stype = "刺穿收回" if (day_low < level and day_close >= level) else \
                            ("刺穿破位" if day_low < level else "临近未触")
                    near.append({"level": label, "support": round(level, 3),
                                 "dist%": round(dist, 2), "type": stype})

            # ===== 加仓两组判定：左侧(冰点) + 右侧(突破箱体) =====
            not_applicable = []
            # ---- 右侧加仓：突破箱体 ----
            bx = self.check_box_breakout(code)
            # P0修复：改为分级突破判定，仅"可靠级"及以上作为加仓条件
            right_breakout = bx.get("level") in ("reliable", "strong")  # 排除signal级的误报
            right_detail = (f"突破箱体上沿{bx.get('box',{}).get('high')}，"
                           f"超出{bx.get('pct_above')}%，等级:{bx.get('level')}"
                           if bx.get("broken") else "未突破箱体")

            # ---- 左侧加仓：情绪冰点（W33 A1 判据 + 5分钟确认）----
            # 日线冰点: 转向确认(金叉或站上MA5) AND BOLL冰点 AND 缩量；RSI 降展示层
            _mc_golden = bool(daily_ctx.get("daily_macd_golden"))
            _mc_rsi = daily_ctx.get("daily_rsi")
            _mc_boll = daily_ctx.get("daily_boll_pct")
            _mc_vol = daily_ctx.get("daily_vol_today")
            _mc_volma = daily_ctx.get("daily_vol_ma5")
            _mc_ma5 = daily_ctx.get("daily_ma5")
            _mc_price = daily_ctx.get("daily_price_ref")
            d_turn = _mc_golden or (_mc_ma5 is not None and _mc_price is not None
                                    and float(_mc_ma5) > 0 and float(_mc_price) > float(_mc_ma5))
            d_rsi = (_mc_rsi is not None and not (isinstance(_mc_rsi, float) and math.isnan(_mc_rsi)) and float(_mc_rsi) < 35)
            d_boll = (_mc_boll is not None and not (isinstance(_mc_boll, float) and math.isnan(_mc_boll)) and float(_mc_boll) <= 0.15)
            d_shrink = (_mc_vol is not None and _mc_volma is not None and _mc_volma > 0 and float(_mc_vol) / float(_mc_volma) < 0.8)
            daily_iceberg = d_turn and d_boll and d_shrink   # 转向 + 冰点2项全过（W33 冰点通道 signal 判据）

            # 5分钟冰点确认（盘中快照有分钟数据时）
            m5_iceberg = True
            m5_note = "盘后(无分钟数据)"
            if bars and len(bars) >= 30:
                try:
                    import pandas as _pd
                    from core.position_builder import resample_to_5min, add_5min_indicators
                    _df = _pd.DataFrame(bars)
                    if "time" in _df.columns:
                        _df["time"] = _pd.to_datetime(_df["time"], errors="coerce")
                    _df5 = add_5min_indicators(resample_to_5min(_df))
                    m5_hits = 0
                    if "dif_5m" in _df5.columns and "dea_5m" in _df5.columns:
                        _up = (_df5["dif_5m"] > _df5["dea_5m"]) & (_df5["dif_5m"].shift(1) <= _df5["dea_5m"].shift(1))
                        if bool(_up.tail(5).any()): m5_hits += 1
                    if "rsi_5m" in _df5.columns and not _pd.isna(_df5["rsi_5m"].iloc[-1]) and float(_df5["rsi_5m"].iloc[-1]) < 30:
                        m5_hits += 1
                    if "bb_pct_5m" in _df5.columns and float(_df5["bb_pct_5m"].iloc[-1]) <= 0.15:
                        m5_hits += 1
                    if "volume" in _df5.columns and len(_df5) >= 25:
                        _recent = _df5["volume"].tail(5).mean()
                        _prior = _df5["volume"].iloc[-25:-5].mean()
                        if _prior > 0 and _recent / _prior < 0.8:
                            m5_hits += 1
                    m5_iceberg = m5_hits >= 3
                    m5_note = f"5分钟冰点 {m5_hits}/4"
                except Exception:
                    m5_iceberg = True
                    m5_note = "5分钟计算失败"

            left_iceberg = daily_iceberg and m5_iceberg   # 左侧加仓 = 日线冰点 + 5分钟冰点

            left_conditions = [
                {"name": "转向确认", "met": d_turn,
                 "detail": "金叉或站上MA5=通过" if d_turn else "金叉/站上MA5=未过"},
                {"name": "RSI超卖(展示)", "met": d_rsi,
                 "detail": f"日线RSI={float(_mc_rsi):.1f}" if d_rsi or (_mc_rsi is not None and not math.isnan(_mc_rsi)) else "日线RSI=无"},
                {"name": "BOLL冰点", "met": d_boll,
                 "detail": f"日线bb_pct={float(_mc_boll):.3f}" if d_boll or (_mc_boll is not None and not math.isnan(_mc_boll)) else "日线BOLL=无"},
                {"name": "缩量止跌", "met": d_shrink,
                 "detail": "日线量<5日均量×0.8" if d_shrink else "日线量未缩"},
                {"name": "5分钟确认", "met": bool(m5_iceberg), "detail": m5_note},
            ]

            conditions = left_conditions + [{"name": "右侧突破箱体", "met": right_breakout, "detail": right_detail}]
            met_count = sum(1 for c in conditions if c["met"])
            # 加仓时机判定（timing_gate: 多头追强/空头抄底/震荡降频）——仅供 GUI 展示加仓是否被时机门控
            _tm = {}
            try:
                from core.timing_gate import timing_verdict as _timing_verdict
                _g = _timing_verdict(str(code).split("_")[0], datetime.now().strftime("%Y-%m-%d"))
                _tm = {"regime": _g["regime"], "go": _g["go"], "reason": _g["reason"]}
            except Exception:
                _tm = {}
            out[code] = {
                "name": cur[code].get("name", code),
                "day_low": round(day_low, 3), "close": round(day_close, 3),
                "is_intraday": bool(is_intraday),  # fix P0-3: True 时 close 实为现价
                "box_boost": right_breakout,  # 右侧突破箱体
                "left_iceberg": bool(left_iceberg),  # 左侧冰点(日线+5分钟)
                "daily_iceberg": bool(daily_iceberg),
                "right_breakout": right_breakout,
                "not_applicable": not_applicable,
                "vwap": round(float(vwap_val), 3) if vwap_val else None,
                "supports": {k: round(v, 3) for k, v in supports.items()},
                "events": events, "near": near,
                "conditions": conditions, "met_count": met_count,
                "timing": _tm,
            }

        total = len([c for c in cur if isinstance(cur.get(c), dict) and (cur.get(c, {}).get("qty") or 0) > 0])
        ok = len(out)
        # fix P1-10: _progress 为内部统计键，以 _ 前缀标识，前端按 _ 前缀过滤，不计入股票数
        out["_progress"] = {"total_holdings": total, "snapshots_ok": ok, "snapshots_miss": total - ok}
        return _clean(out)

    def _ensure_daily_ctx_indicators(self, code, daily_ctx):
        """补算 daily_ctx 的日线 MACD/趋势字段（旧快照缺失时）。就地更新 daily_ctx。"""
        try:
            import pandas as pd
            _c6 = str(code).split("_")[0]
            # 2026-10-09: **本地日线缓存优先**——原来逐只 `fetch_daily_kline` 走 GM（实测 ~1.8s/只），
            # `compute_add_watch` 在 `load_day`（主线程）上跑 9 只 ⇒ 17.3s 冻界面（看门狗抓到）。
            df = None
            try:
                import core.chart_cache as _cc
                df = _cc.load_daily_display(_c6)
            except Exception:
                df = None
            if df is None or df.empty:
                from core.position_builder import fetch_daily_kline
                df = fetch_daily_kline(_c6)
            if df.empty or len(df) < 30:
                return
            c = df["close"].astype(float)
            ema12 = c.ewm(span=12, adjust=False).mean()
            ema26 = c.ewm(span=26, adjust=False).mean()
            macd_dif = (ema12 - ema26).values
            macd_dea = pd.Series(macd_dif).ewm(span=9, adjust=False).mean().values
            s_dif, s_dea = pd.Series(macd_dif), pd.Series(macd_dea)
            cross_up = (s_dif > s_dea) & (s_dif.shift(1) <= s_dea.shift(1))
            daily_ctx["daily_macd_dif"] = float(macd_dif[-1])
            daily_ctx["daily_macd_dea"] = float(macd_dea[-1])
            daily_ctx["daily_macd_golden"] = bool(cross_up.tail(5).any())
            # 趋势背景：用 MA 排列粗略推断（上行/下行/震荡）
            ma5 = float(c.rolling(5).mean().iloc[-1])
            ma20 = float(c.rolling(20).mean().iloc[-1])
            ma60 = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else ma20
            cur_px = float(c.iloc[-1])
            if not daily_ctx.get("daily_trend_bg"):
                if cur_px < ma60 and ma20 <= ma60:
                    daily_ctx["daily_trend_bg"] = "downtrend"
                elif ma5 > ma20 > ma60 and cur_px >= ma5:
                    daily_ctx["daily_trend_bg"] = "uptrend"
                else:
                    daily_ctx["daily_trend_bg"] = "range"
        except Exception:
            pass

    # ---------- 突破箱体判定（建仓+加仓共用） ----------
    def check_box_breakout(self, code):
        """判定是否突破当前/刚突破箱体上沿 + 分级突破质量。
        返回 {broken, level, box, price, pct_above, confidence}。

        突破等级（level）：
          - signal: 信号级(0.5-1%)，敏感但低可靠，仅提示
          - reliable: 可靠突破(1-3%)，需辅助确认，可作参考
          - strong: 强势突破(3%+)，高概率后续，适合加仓
          - far_away: 已远离>8%，看不出是否有效
        """
        # 2026-10-09: 本函数只用到 `boxes` 与 `current_price`（**都是日线派生**）——改读**本地日线缓存**
        # + `_detect_boxes`，不再走 `load_stock_chart` 全量建图（含分钟/指标；盘中每 2 分钟重建，
        # 9 只持仓 ⇒ `load_day→compute_add_watch` 阻塞 17s，看门狗抓到）。缓存 miss 才回退原路径。
        boxes, _h_price = [], None
        try:
            import core.chart_cache as _cc
            _dfd = _cc.load_daily_display(str(code).split("_")[0])
            if _dfd is not None and not _dfd.empty:
                boxes = self._detect_boxes(_dfd)
                _h_price = float(_dfd["close"].iloc[-1])
        except Exception:
            boxes = []
        if not boxes:
            h = self.load_stock_chart(code)
            if not h.get("available"):
                return {"broken": False, "level": None, "error": h.get("error", "")}
            boxes = h.get("boxes", [])
            _h_price = h.get("current_price")
        # fix P0-4: 现价改用 load_quotes 实时报价（30秒缓存避免逐股重复拉网），失败回退日线收盘
        now = datetime.now()
        qc = getattr(self, "_box_quote_cache", None)
        if not qc or (now - qc[0]).total_seconds() > 30:
            px_map = {}
            try:
                for qq in self.load_quotes().get("quotes", []):
                    if qq.get("price") and not qq.get("offline"):
                        px_map[qq.get("code")] = float(qq["price"])
            except Exception:
                px_map = {}
            qc = (now, px_map)
            self._box_quote_cache = qc
        cur = qc[1].get(code) or qc[1].get(code.split("_")[0]) or _h_price
        if not cur:
            return {"broken": False, "level": None, "error": "无可用现价"}
        # fix P0-4: 候选箱体纳入 rel==1（刚突破）；rel 判定基于日线收盘，与实时现价解耦
        cur_boxes = [b for b in boxes if b.get("rel") in (0, 1)]
        # 现价 > 候选箱体上沿 → 判定突破级别
        for box in cur_boxes:
            if cur > box["high"]:
                pct_above = (cur - box["high"]) / box["high"] * 100 if box["high"] else 0
                # 根据突破幅度判定级别（box宽度作为质量权重）
                box_width_pct = (box["high"] - box["low"]) / box["low"] * 100 if box["low"] else 0
                # 宽度作为确信度：宽箱体(>10%)突破容差可更松，窄箱体(<5%)必须严格
                confidence = min(100, max(10, box_width_pct))  # confidence: 10~100

                if pct_above <= 0.5:
                    level = None  # 未达突破阈值
                elif pct_above <= 1:
                    level = "signal"  # 信号级：敏感但易误报
                elif pct_above <= 3:
                    level = "reliable"  # 可靠突破：推荐用于加仓参考
                elif pct_above <= 8:
                    level = "strong"  # 强势突破：已形成上升趋势
                else:
                    level = "far_away"  # 已远离，无法判定是否有效

                if level:
                    return {"broken": True, "level": level, "box": {"low": box["low"], "high": box["high"]},
                            "price": cur, "pct_above": round(pct_above, 2), "confidence": round(confidence, 0)}
                else:
                    return {"broken": False, "level": None, "price": cur,
                            "near_box": {"low": box["low"], "high": box["high"]},
                            "pct_above": round(pct_above, 2),
                            "reason": "未达突破阈值(仅0.5%以下)"}
        # 无候选箱体或现价在箱体内 → 未突破
        return {"broken": False, "level": None, "price": cur}

    # ---------- 持仓日线超买/顶背离体检 ----------
    def load_ob_analysis(self, date=None):
        """每只持仓：日线超买指标(RSI/KDJ-J/CCI/BOLL) + 顶背离(MACD/RSI/KDJ/量价) + 建仓建议。

        2026-10-04 卡顿修复：原来逐只 `load_stock_chart`（冷取数）⇒ 冷启动实测 **14.3s** 同步阻塞
        在 pywebview 主线程。改为**只用已预热的图表缓存**：未就绪的持仓本轮跳过、计入 `pending`，
        由前端稍后重拉（启动时 `prewarm_holdings_charts` 已在后台填缓存）。UI 不再被冻住。
        """
        import numpy as np
        import pandas as pd
        cur = _load_json(HOLDINGS_MANUAL, {})
        out = {"stocks": []}
        _today = datetime.now().strftime("%Y-%m-%d")
        _chart_cache = getattr(self, "_stock_chart_cache", {})
        pending = 0

        for code, info in cur.items():
            if not isinstance(info, dict) or code.startswith("_"):
                continue
            if not (info.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不进入持仓体检
            base_code = code.split("_")[0]  # 000988_B → 000988
            _hit = _chart_cache.get(f"{_today}_{base_code}")
            if _hit is None:
                pending += 1          # 图表尚未预热好：跳过，不计入本轮（前端会重拉）
                continue
            h = _hit[1]
            if not h.get("available"):
                out["stocks"].append({"code": code, "name": info.get("name", code),
                                      "error": h.get("error", "无数据")})
                continue
            d = h["period_data"]["daily"]
            # 技术标签（2026-10-07）：与建仓表同口径（复用 `_stock_tags_from_df`）。用已缓存的
            # 日线 payload 重建 df，**零额外网络**；标签口径/顺序与建仓表完全一致。
            _live_close = None
            try:
                import pandas as _pd
                _tdf = _pd.DataFrame({
                    "date": _pd.to_datetime(d["dates"]),
                    "open": [x[0] for x in d["ohlc"]], "close": [x[1] for x in d["ohlc"]],
                    "low": [x[2] for x in d["ohlc"]], "high": [x[3] for x in d["ohlc"]],
                    "volume": [float(v) for v in d["volume"]],
                })
                # 2026-10-09: 图表**内存缓存盘中最多 15 分钟旧** ⇒ 用**实时快照**（load_quotes 每 10s 的
                # 批量快照）校正当日那根，否则标签/现价会停在旧价（实测 600362 价已回 MA5 上方、
                # 标签仍显示 15 分钟前的「破5日线」）。零额外网络；非当日/无快照则原样。
                _live = self._today_forming_bar(base_code, allow_network=False)
                if _live and str(_live["date"]) == str(_tdf["date"].iloc[-1])[:10]:
                    _li = _tdf.index[-1]
                    _tdf.at[_li, "close"] = float(_live["close"])
                    _tdf.at[_li, "high"] = max(float(_tdf.at[_li, "high"]), float(_live["high"]))
                    _tdf.at[_li, "low"] = min(float(_tdf.at[_li, "low"]), float(_live["low"]))
                    _live_close = float(_live["close"])
                _tags = self._stock_tags_from_df(_tdf, base_code).get("tags", [])
            except Exception:
                _tags = []
            closes = [x[1] for x in d["ohlc"]]
            highs = [x[3] for x in d["ohlc"]]
            lows = [x[2] for x in d["ohlc"]]
            volumes = d["volume"]
            rsi = d["rsi"]
            dif = d["macd"]["dif"]
            boll_up = d["boll"]["up"]
            n = len(closes)
            if n < 30:
                continue

            # KDJ(9,3,3)
            k_arr, d_arr, j_arr = [50.0], [50.0], [50.0]
            for i in range(1, n):
                hh = max(highs[max(0, i - 8):i + 1])
                ll = min(lows[max(0, i - 8):i + 1])
                rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
                k = 2 / 3 * k_arr[-1] + 1 / 3 * rsv
                dd = 2 / 3 * d_arr[-1] + 1 / 3 * k
                k_arr.append(k); d_arr.append(dd); j_arr.append(3 * k - 2 * dd)

            # CCI(14)
            cci_arr = []
            for i in range(n):
                if i < 13:
                    cci_arr.append(None); continue
                tp = (highs[i] + lows[i] + closes[i]) / 3
                ma_tp = sum((highs[j] + lows[j] + closes[j]) / 3
                            for j in range(i - 13, i + 1)) / 14
                md = sum(abs((highs[j] + lows[j] + closes[j]) / 3 - ma_tp)
                         for j in range(i - 13, i + 1)) / 14
                cci_arr.append((tp - ma_tp) / (0.015 * md) if md else 0)

            # 当前超买状态
            cur_rsi = rsi[-1] if rsi and rsi[-1] is not None else 0
            cur_j = j_arr[-1]
            cur_cci = cci_arr[-1] or 0
            cur_close = _live_close if _live_close else closes[-1]   # 现价优先实时快照（见上方校正）
            cur_boll = boll_up[-1] if boll_up and boll_up[-1] is not None else 0
            ob = {
                "rsi": bool(cur_rsi > 70),
                "kdj": bool(cur_j > 100),
                "cci": bool(cur_cci > 100),
                "boll": bool(cur_boll and cur_close > cur_boll),
            }
            ob["count"] = sum(1 for v in ob.values() if v)

            # 顶背离检测（近60日）
            div = {"macd": False, "rsi": False, "kdj": False, "vol": False}
            win = range(max(2, n - 60), n)
            # 找近60日两个局部价格高点
            highs_list = list(win)
            price_peaks = []
            for i in range(2, len(win) - 2):
                idx = list(win)[i]
                if highs[idx] >= highs[idx - 1] and highs[idx] >= highs[idx - 2] and \
                   highs[idx] >= highs[idx + 1] and highs[idx] >= highs[idx + 2]:
                    price_peaks.append(idx)
            if len(price_peaks) >= 2:
                p2, p1 = price_peaks[-2], price_peaks[-1]
                # MACD 顶背离: 价创新高 但 DIF 未创新高
                if highs[p1] > highs[p2] and dif[p1] is not None and dif[p2] is not None and dif[p1] < dif[p2]:
                    div["macd"] = True
                # RSI 顶背离
                if highs[p1] > highs[p2] and rsi[p1] is not None and rsi[p2] is not None and rsi[p1] < rsi[p2]:
                    div["rsi"] = True
                # KDJ 顶背离
                if highs[p1] > highs[p2] and j_arr[p1] < j_arr[p2]:
                    div["kdj"] = True
                # 量价背离: 价新高 量萎缩
                if highs[p1] > highs[p2] and volumes[p1] < volumes[p2] * 0.9:
                    div["vol"] = True
            div["count"] = sum(1 for v in div.values() if v)

            # 趋势方向（通道下行=风险因子）
            ch = (h.get("channel") or {})
            trend_down = ch.get("direction") == "down"
            trend_up = ch.get("direction") == "up"

            # 风险提醒建议（2026-09-11 两次实验校准：ob_signal_calib + rsi_sensitivity）：
            # 实证：RSI 阈值 68~75 差异不大(均 T+5≈+0.8%/跌52%)；RSI>80 才明显转弱(-0.06%/56%)；
            # RSI超买+日线顶背离反而偏强(+1.8~3.2%)→ 顶背离不作升级因子；KDJ/CCI/BOLL 为动能。
            # → 高 = RSI>80 或 (RSI>70 且趋势下行)；中 = RSI>70 或趋势下行；顶背离仅展示不计风险。
            rsi_hot = bool(cur_rsi > 70)
            if cur_rsi > 80:
                risk = "高"
                advice = "🚨 RSI极度超买(>80)：实证 T+5 转平、56% 下跌，注意回落/减仓"
            elif rsi_hot and trend_down:
                risk = "高"
                advice = "🚨 RSI超买+趋势下行：回落风险高，反弹减仓/回避"
            elif rsi_hot:
                risk = "中"
                advice = "⚠ RSI超买(>70)：短线偏热，注意回调（实证跌占52%>基线45%）"
            elif trend_down:
                risk = "中"
                advice = "⚠ 趋势下行：不追高，反弹减仓"
            elif div["count"] >= 2:
                risk = "低"
                advice = "✓ 风险低；顶背离≥2 仅观察（日线前瞻性弱，勿据此减仓）"
            else:
                risk = "低"
                advice = "✓ 指标中性（KDJ/CCI/BOLL 偏强属动能）：持有/关注"

            out["stocks"].append({
                "code": code, "name": info.get("name", code),
                "price": cur_close,
                "trend": ch.get("direction", "flat"),
                "risk": risk,
                "overbought": {"rsi": round(cur_rsi, 1), "kdj": round(cur_j, 1),
                               "cci": round(cur_cci, 1), "boll": bool(ob["boll"]), "count": ob["count"]},
                "divergence": div,
                "advice": advice,
                "tags": _tags,
            })
        if pending:
            out["pending"] = pending      # 未就绪的持仓数（>0 ⇒ 前端稍后重拉）
        return _clean(out)

    # ---------- 入场三层评判（L1/L2/L3建议） ----------
    def load_entry_verdict(self, date=None):
        """候选股入场评判：L1追高风险 + L2缩量支撑 + L3日内共振 → 综合建议。

        返回格式:
        {
          "stocks": [
            {
              "code": "300058",
              "name": "蓝色光标",
              "market_regime": "range_up",
              "market_score": 60,
              "l1": {"status": "✅", "detail": "安全", "risk_score": 0, "threshold": 35},
              "l2": {"status": "✅", "detail": "缩量0.60x", "is_consolidating": True},
              "l3": {"status": "❌", "detail": "放量不足", "resonance": False},
              "verdict": "wait_resonance",
              "action": "等待日内共振",
              "expected_when": "盘中"
            },
            ...
          ]
        }
        """
        try:
            from strategies.universal_precise_entry import batch_check_all_candidates
            from datetime import datetime as dt

            date_str = date or dt.now().strftime("%Y-%m-%d")
            results = batch_check_all_candidates(date_str)

            # 从候选池加载股票名称
            try:
                candidates = _load_json(BASE / "candidates.json", {})
            except:
                candidates = {}

            stocks = []
            for r in results:
                code = r.get("code", "")
                if not code:
                    continue

                # 构建L1状态
                l1_info = r.get("l1", {})
                l1_detail = l1_info.get("detail", "未知")
                l1_risk = l1_info.get("risk_score", 0)
                l1_threshold = l1_info.get("risk_threshold", 35)
                if l1_risk <= l1_threshold:
                    l1_status = "✅"
                elif l1_risk > l1_threshold * 1.5:
                    l1_status = "❌"
                else:
                    l1_status = "⚠️"

                # 构建L2状态
                l2_info = r.get("l2", {})
                l2_is_ok = l2_info.get("is_consolidating", False)
                l2_detail = l2_info.get("detail", "待评估")
                l2_status = "✅" if l2_is_ok else "❌"

                # 构建L3状态
                l3_info = r.get("l3", {})
                l3_is_ok = l3_info.get("resonance", False)
                l3_detail = l3_info.get("detail", "待评估")
                l3_status = "✅" if l3_is_ok else "❌"

                # 生成行动建议和预期时间
                verdict = r.get("verdict", "unknown")
                if verdict == "ready_to_buy":
                    action = "🟢 可以买入"
                    expected_when = "立即"
                elif verdict == "wait_resonance":
                    action = "⏳ 等待日内共振"
                    expected_when = "盘中"
                elif verdict == "wait_consolidation":
                    action = "⏳ 继续缩量巩固"
                    expected_when = "3-5天"
                elif verdict == "wait_cool_down":
                    action = "⏳ 等待冷却"
                    expected_when = "1-3天"
                elif verdict == "avoid_chase":
                    action = "🔴 避免追高"
                    expected_when = "观察"
                else:
                    action = "❓ 未知"
                    expected_when = "-"

                stocks.append({
                    "code": code,
                    "name": candidates.get(code, {}).get("name", code),
                    "market_regime": r.get("market_regime", "unknown"),
                    "market_score": int(r.get("market_score", 0)),
                    "l1": {
                        "status": l1_status,
                        "detail": l1_detail,
                        "risk_score": int(l1_risk),
                        "threshold": int(l1_threshold)
                    },
                    "l2": {
                        "status": l2_status,
                        "detail": l2_detail,
                        "is_consolidating": bool(l2_is_ok)
                    },
                    "l3": {
                        "status": l3_status,
                        "detail": l3_detail,
                        "resonance": bool(l3_is_ok)
                    },
                    "verdict": verdict,
                    "action": action,
                    "expected_when": expected_when
                })

            return _clean({
                "date": date_str,
                "stocks": stocks,
                "summary": {
                    "total": len(stocks),
                    "ready": len([s for s in stocks if s["verdict"] == "ready_to_buy"]),
                    "wait_resonance": len([s for s in stocks if s["verdict"] == "wait_resonance"]),
                    "waiting": len([s for s in stocks if s["verdict"] in ["wait_consolidation", "wait_cool_down"]]),
                    "avoid": len([s for s in stocks if s["verdict"] == "avoid_chase"])
                }
            })
        except Exception as e:
            return _clean({
                "error": f"入场评判失败: {str(e)}",
                "stocks": []
            })

    # ---------- 严重顶背离报警 ----------
    def alert_severe_divergence(self, date=None):
        """严重顶背离告警——已停用（2026-08-19）。

        极值后市确认验证（37只候选池、约3年、1819事件，见 t_io/validation/w35_divergence/
        divergence_验证报告_daily.md）显示：count≥2 顶背离命中率53.4%反而低于无背离基线
        57.8%，告警无区分度、会大量误报，故不再推送飞书/触发独立警报。
        持仓体检表(load_ob_analysis)仍展示顶背离信息，供与超买/趋势组合参考。"""
        return _clean({"alerts": [], "disabled": True})

    # ---------- 个股技术分析弹窗 ----------
    def load_stock_chart(self, code, version=None, want_minutes=False):
        """日线(本地缓存秒回/网络兜底) → 7 条 MA + MACD/RSI/BOLL → resample 周/月 + 30分/60分
        → 支撑压力。支持带前缀指数代码(sh000001/sz399001 等)。内存缓存：同一标的当日重复用。

        version：前端持有的上一次版本号。若与缓存一致 ⇒ 只回 `{"unchanged": True}`（几百字节），
        不序列化那 300KB payload —— 前端每 10s 轮询一次，这个握手把桥上的传输整个省掉。

        want_minutes（2026-10-06）：**默认 False ⇒ 不取 30/60分**。分钟线只能逐只打 tushare
        （实测 1.6s/周期、无法批量），是日线视图首次打开的主延迟。前端仅在切到 30分/60分 Tab
        时才传 True，日线/周/月视图因此不再被分钟取数阻塞。
        """
        out = {"code": code, "name": code, "available": False, "error": ""}
        if not hasattr(self, "_stock_chart_cache"):
            self._stock_chart_cache = {}
        cache_key = f"{datetime.now().strftime('%Y-%m-%d')}_{code}"
        if cache_key in self._stock_chart_cache:
            _ts, _res = self._stock_chart_cache[cache_key]
            # fix P0-15: 盘前缓存的图缺今日K线(最后日期<今天)，或盘中超15分钟 → 重算，避免图停在昨日。
            # 2026-10-04 卡顿修复：原来只看「最后日期<今天」就判 stale ⇒ 周末/假期**每次轮询都全量重建**
            # （实测热路径 2.8s、冷 16.7s，跑在主线程上直接冻界面）。补两道闸：非交易日不可能出新K线；
            # 交易日内也最多 2 分钟重建一次，而不是每 10s 一次。
            _dates = _res.get("period_data", {}).get("daily", {}).get("dates") or []
            _last = str(_dates[-1]) if _dates else ""
            _now = datetime.now()
            _today = _now.strftime("%Y-%m-%d")
            _age = (_now - _ts).total_seconds()
            _new_bar_possible = (_last < _today and _now.weekday() < 5
                                 and _now.strftime("%H:%M") >= "09:30")
            _stale = ((_new_bar_possible and _age > 120)
                      or (_last == _today and _age > 15 * 60))
            if not _stale:
                # want_minutes 但缓存里没有分时（首帧为提速未取）⇒ 落到重建（带分时）
                _has_min = bool((_res.get("period_data") or {}).get("min30"))
                if not (want_minutes and not _has_min):
                    _v = _res.get("version")
                    if version is not None and _v is not None and version == _v:
                        return {"code": code, "available": True, "unchanged": True, "version": _v}
                    return _res

        # 东财标的(em前缀)磁盘缓存：K线静态(每日更新)，当日缓存避免东财接口重试
        if str(code).startswith("em"):
            try:
                _em_cache = BASE / "t_io" / "cache" / f"em_kline_{str(code)[2:]}.json"
                if _em_cache.exists():
                    _c = _load_json(_em_cache, None)
                    if _c and _c.get("date") == datetime.now().strftime("%Y-%m-%d") and _c.get("rows"):
                        _cdf = pd.DataFrame(_c["rows"])
                        _cdf["date"] = pd.to_datetime(_cdf["date"])
                        out["name"] = _c.get("name") or code
                        return self._build_chart_from_df(_cdf, out, code)
            except Exception:
                pass

        code_str = str(code)
        # em前缀 → 东财secid(如 em47.800005=A股平均股价)；sx000000 → 指数；纯6位 → 股票
        is_em = code_str.startswith("em")
        is_index = code_str[:2] in ("sh", "sz", "bj") and code_str[2:].isdigit()
        symbol = code_str if is_index else ("sh" + code_str if code_str[0] in "56" else "sz" + code_str)
        # 2026-08-24: 指数/东财日线磁盘缓存（当日秒回，避免每次网络拉 400 根慢）
        chart_cache_fp = BASE / "t_io" / "cache" / "stock_chart" / f"{code_str}.json"
        if is_index or is_em:
            try:
                if chart_cache_fp.exists():
                    _cc = json.loads(chart_cache_fp.read_text(encoding="utf-8"))
                    if _cc.get("date") == datetime.now().strftime("%Y-%m-%d") and _cc.get("rows"):
                        rows = _cc["rows"]
            except Exception:
                pass

        # 2026-10-04 cache-first：盘后预下载的「图表 payload」命中则**瞬开**，不走 GM 优先路径
        # （GM 不可达时每次卡满 12s 超时）。miss 时行为完全不变。
        # 2026-10-06：**覆盖指数**（此前 `not is_index` 把指数排除 → 指数每次走网络、且用错取数函数）。
        if not is_em:
            _hit = self._serve_payload_cache(code_str, version, want_minutes)
            if _hit is not None:
                return _hit

        # 本地日线缓存（个股 `{code}.json` / 指数 `index_{code}.json`）
        rows = []
        if not is_em:
            rows = self._daily_rows_cache_first(code_str)
            if not rows:
                # 北交所：日线四源全不通（memory），`bj_daily.fetch_bj_daily` 逐只重试 8 次 ≈18s
                # ⇒ 卡死主线程。有缓存/payload 的 BJ 码已在上面命中；无缓存则**立即优雅降级**。
                try:
                    from core.market_data.codec import market_of
                    if not is_index and market_of(code_str) == "BJ":
                        out["error"] = "北交所行情源暂不可用（已知受限）"
                        return _clean(out)
                except Exception:
                    pass
                try:
                    # miss → 带 6s 硬超时，防慢源冻主线程（指数走 index_daily）
                    _df = self._fetch_daily_bounded(code_str)
                    if _df is not None and not _df.empty:
                        for _r in _df.itertuples(index=False):
                            rows.append({"date": str(_r.date), "open": float(_r.open),
                                         "close": float(_r.close), "high": float(_r.high),
                                         "low": float(_r.low), "volume": float(_r.volume)})
                except Exception:
                    rows = []

        # 本地缓存不可用 → 走网络拉 400 根
        if not rows:
            import os as _os, urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"

            if is_em:
                # 东财 secid (em47.800005 → 47.800005)，平均股价等腾讯无代码的标的
                # push2his 间歇断连(风控)，重试 8 次，多数 3-6 次内成功
                data = {}
                rows = []
                for _att in range(8):
                    try:
                        secid = code_str[2:]
                        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                               f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
                               f"&klt=101&fqt=1&beg=0&end=20500101&lmt=400")
                        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                        "Referer": "https://quote.eastmoney.com/"})
                        raw = _ur.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                        data = json.loads(raw)
                        klines = (data.get("data") or {}).get("klines") or []
                        if not klines:
                            continue
                        rows = []
                        for item in klines:
                            parts = item.split(",")
                            if len(parts) >= 6:
                                rows.append({
                                    "date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
                                    "high": float(parts[3]), "low": float(parts[4]), "volume": float(parts[5]),
                                })
                        if rows:
                            break
                    except Exception:
                        import time as _t
                        _t.sleep(1)
                if rows:
                    out["name"] = (data.get("data") or {}).get("name") or code
                elif not out["error"]:
                    out["error"] = "东财拉取日线失败"
                    return out
            else:
                # P1-2 收敛：market_data provider（gm 主源/腾讯兜底）；指数走 index_daily
                from core.market_data import get_provider
                try:
                    _prov = get_provider()
                    if is_index:
                        # 传 end_date ⇒ 不回写共享长历史缓存（防静默截短 index_*.json）
                        df = _prov.index_daily(code, 400, end_date=datetime.now().strftime("%Y-%m-%d"))
                    else:
                        df = _prov.daily(code, 400)
                    rows = []
                    if df is not None and not df.empty:
                        for r in df.itertuples():
                            rows.append({"date": r.date, "open": r.open, "close": r.close,
                                         "high": r.high, "low": r.low, "volume": r.volume})
                except Exception as e:
                    out["error"] = f"拉取日线失败: {e}"
                    return out

        # 2026-08-24: 网络拉取的指数/东财日线写磁盘缓存（当日，下次秒回）
        if rows and (is_index or is_em):
            try:
                chart_cache_fp.parent.mkdir(parents=True, exist_ok=True)
                chart_cache_fp.write_text(
                    json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "rows": rows},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        if not rows:
            out["error"] = "无日线数据"
            return out
        if out["name"] == code:
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                nm = (jy.get(code, {}) or {}).get("name", "") if isinstance(jy.get(code), dict) else ""
                if nm:
                    out["name"] = nm
            except Exception:
                pass

        try:
            import pandas as pd
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            out = self._build_chart_from_df(df, out, code,
                                            min_frames={} if not want_minutes else None)
        except Exception as e:
            out["error"] = f"计算失败: {e}"
            return out

        result = _clean(out)
        # 东财标的写磁盘缓存（当日有效，避免后续东财接口重试）
        if str(code).startswith("em") and result.get("available"):
            try:
                _em_cache = BASE / "t_io" / "cache" / f"em_kline_{str(code)[2:]}.json"
                _em_cache.write_text(json.dumps({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "name": result.get("name"),
                    "rows": rows,
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        self._cache_chart(cache_key, result)
        # 机会式 payload 落盘（2026-10-06）：任何**真正建完**的图都落一份磁盘 payload，
        # 使非小池码「这次开了、下次（含新会话）瞬开」。后台、失败静默、有 LRU 容量上限。
        if result.get("available") and not is_em:
            try:
                import core.chart_cache as _cc
                _cc.save_payload(code_str, result, rows)
            except Exception:
                pass
        return result

    # ---------- 公司资料（K 线弹窗「📋 公司资料」） ----------
    @staticmethod
    def _em_secid(code):
        """6 位码 → 东财 secid。沪市=1.，深市/北交所=0.（东财北交所亦用 0.）。"""
        from core.market_data.codec import market_of
        c = str(code).split("_")[0]
        return ("1." if market_of(c) == "SH" else "0.") + c

    def _em_stock_boards(self, code):
        """东财「所属板块」→ [{name, kind}]；取不到返回 []（永不抛）。

        ⚠️ 只走 `push2.eastmoney.com/api/qt/slist/get`：2026-09-29 实测本机
        `push2his`（K线）**整体不可达**（对照组平安银行亦 RemoteDisconnected，已排除代理），
        `clist`/`stock/get` 同样被风控，只有 `slist/get` 稳定可用。故不依赖其它东财主机。

        `kind` 是**板块名启发式**（含"概念"→概念；以"板块"结尾→地域；其余→其他），
        **不是**东财语义 —— 该接口不带板块类型字段（实测 f13/f152 恒定）。界面上如实标注。
        """
        import os as _os
        import time as _t
        import urllib.request as _ur
        c = str(code).split("_")[0]
        _hit = Api._profile_cache.get(c)
        if _hit and (_t.time() - _hit[0]) < Api._PROFILE_TTL:
            return _hit[1]
        for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"):
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        url = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
               f"&fields=f12,f14&secid={self._em_secid(c)}&pn=1&np=1&pz=100")
        for _ in range(8):                       # 东财间歇风控，仓库既有对策=8 次重试
            try:
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://quote.eastmoney.com/"})
                data = (json.loads(_ur.urlopen(req, timeout=6).read()
                                   .decode("utf-8", errors="ignore")).get("data") or {})
                diff = data.get("diff") or []
                boards = []
                for x in diff:
                    nm = str(x.get("f14") or "").strip()
                    if not nm:
                        continue
                    if "概念" in nm:
                        kind = "概念"
                    elif nm.endswith("板块"):
                        kind = "地域"
                    else:
                        kind = "其他"
                    boards.append({"name": nm, "kind": kind})
                if boards:
                    Api._profile_cache[c] = (_t.time(), boards)
                    return boards
            except Exception:
                _t.sleep(0.6)
        Api._profile_cache[c] = (_t.time(), [])   # 负缓存：别让每次点开都等 8 次重试
        return []

    def load_stock_profile(self, code, live=False):
        """公司资料。两层（owner 2026-09-29：本地先显示 + 按钮拉实时）：

          - **本地层**（`live=False`，秒开、零网络）：`watchlist_jiuyan.json` 的
            `business_summary`（主营一句话）、`sector`（`/` 拼接的行业+概念混列）、
            `_jiuyan_concepts`（韭研概念）
          - **实时层**（`live=True`）：东财「所属板块」（见 `_em_stock_boards`）

        三条口径说明（避免过度承诺）：
          - 东财板块列表不带类型字段 ⇒ 行业/概念/地域只能按**板块名启发式**划分，界面标注。
          - 本地 `sector` 是历史遗留混合串，行业与概念混在一起，**原样展示、不擅自归类**。
          - 该文件的 `concept_boards`/`industry_boards` 字段 **0/5041 全空**（从未写入），故不读。

        与 `load_stock_chart` 同契约：**永不抛**，失败置 `error` 且 `ok=False`。
        """
        out = {"code": str(code), "name": None, "ok": False, "error": None,
               "business_summary": "", "sector": "", "sector_list": [],
               "local_concepts": [], "boards": [], "boards_source": None,
               "boards_note": "东财口径；行业/概念/地域按板块名启发式划分"}
        try:
            jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
            info = jy.get(str(code).split("_")[0]) if isinstance(jy, dict) else None
            if isinstance(info, dict):
                out["name"] = info.get("name")
                out["business_summary"] = str(info.get("business_summary") or "").strip()
                sec = str(info.get("sector") or "").strip()
                out["sector"] = sec
                out["sector_list"] = [s.strip() for s in sec.split("/") if s.strip()]
                out["local_concepts"] = [s for s in _jiuyan_concepts(info).split("|") if s]
            out["ok"] = True
        except Exception as e:
            out["error"] = f"读取本地资料失败: {e}"
            return _clean(out)

        if live:
            try:
                boards = self._em_stock_boards(str(code))
                if boards:
                    out["boards"] = boards
                    out["boards_source"] = "em"
                else:
                    out["error"] = "东财所属板块取不到（风控或网络）"
            except Exception as e:                # 兜底：本地层仍可用
                out["error"] = f"东财取数失败: {e}"
        return _clean(out)

    # ---------- K线 cache-first（2026-10-04） ----------
    def _cache_chart(self, key, res):
        """写内存图表缓存（容量上限 500，防全池无界增长）。"""
        if not hasattr(self, "_stock_chart_cache"):
            self._stock_chart_cache = {}
        if len(self._stock_chart_cache) > 500:
            self._stock_chart_cache.clear()
        self._stock_chart_cache[key] = (datetime.now(), res)

    @staticmethod
    def _handshake(res, version):
        """版本握手：数据未变只回 {unchanged:true}（省掉 ~300KB 过桥 + 重画）。"""
        _v = res.get("version")
        if version is not None and _v is not None and version == _v:
            return {"code": res.get("code"), "available": True, "unchanged": True, "version": _v}
        return res

    def _serve_payload_cache(self, code, version, want_minutes=False):
        """盘后预下载/机会式落盘的图表 payload 命中 → 瞬开结果；未命中返回 None。

        want_minutes 但 payload 内无分时 ⇒ 返回 None（落到重建带分时）。
        """
        try:
            import core.chart_cache as _cc
            hit = _cc.load_payload(code)
            if not hit:
                return None
            if want_minutes and not (hit.get("payload") or {}).get("period_data", {}).get("min30"):
                return None
            # 2026-10-08: payload 比**日线缓存**旧 ⇒ 判过期、回落日线缓存路径（零网络、序列完整）。
            # 背景：`MAX_DISPLAY_GAP_DAYS=12` 容忍长假，会把「缺了 1 个交易日」的 payload 也当新鲜
            # 直接回放（实测 600362：payload 停在 09-30、日线缓存已到 10-08 ⇒ MA5 用旧序列 ⇒ 误报破5日线）。
            try:
                _dfc = _cc.load_daily_display(code)
                if _dfc is not None and not _dfc.empty:
                    if str(hit.get("last_daily_date") or "") < str(_dfc["date"].iloc[-1])[:10]:
                        return None
            except Exception:
                pass
            res = self._payload_to_result(hit, code)
            if not res or not res.get("available"):
                return None
            self._cache_chart(f"{datetime.now().strftime('%Y-%m-%d')}_{code}", res)
            return self._handshake(res, version)
        except Exception:
            return None

    def _fetch_daily_bounded(self, code, timeout=6.0):
        """带硬超时的日线取数（2026-10-06）：防止任何慢/挂死的数据源把 pywebview 主线程冻住。
        超时返回 None（取数线程继续在后台跑完，不回收）。指数走 `index_daily`（`daily` 解不了指数码）。"""
        import concurrent.futures as _cf
        _c = str(code).split("_")[0]
        _is_index = _c[:2].lower() in ("sh", "sz", "bj") and _c[2:].isdigit()
        try:
            from core.market_data import get_provider
            from core.position_builder import fetch_daily_kline
            prov = get_provider()
        except Exception:
            return None

        def _go():
            if _is_index:
                # 传 end_date ⇒ provider **不回写**共享长历史缓存（否则 400 行会静默截短 index_*.json）
                return prov.index_daily(_c, 400, end_date=datetime.now().strftime("%Y-%m-%d"))
            return fetch_daily_kline(_c)
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(_go).result(timeout=timeout)
        except Exception:
            return None
        finally:
            ex.shutdown(wait=False)

    def _daily_rows_cache_first(self, code):
        """盘后预下载的日线历史缓存（+当日 forming bar）→ rows；miss 返回 []。

        补当日 bar **复用 `_today_forming_bar`（走 load_quotes 批量快照）**，不用
        `facade.append_forming_bar` —— 后者会**逐只单打一次网络快照**（实测 ~1.9s/只）。
        """
        try:
            import core.chart_cache as _cc
            df = _cc.load_daily_display(code)
            if df is None or df.empty:
                return []
            rows = [{"date": str(_r.date), "open": float(_r.open), "close": float(_r.close),
                     "high": float(_r.high), "low": float(_r.low), "volume": float(_r.volume)}
                    for _r in df.itertuples(index=False)]
            try:
                _today = datetime.now().strftime("%Y-%m-%d")
                _last = str(rows[-1]["date"]) if rows else ""
                if _last < _today:
                    bar = self._today_forming_bar(code, allow_network=False)
                    if bar and str(bar["date"]) > _last:
                        rows.append(bar)
            except Exception:
                pass
            return rows
        except Exception:
            return []

    def _payload_to_result(self, hit, code):
        """payload 缓存 → 结果 dict。日内（可能出新 bar）把当日那根补进 daily 序列并重算日线指标；
        非交易时段直接返回收盘态（真·瞬开）。其他周期/箱体/通道沿用收盘态（≤1 根偏差）。"""
        import copy
        payload = copy.deepcopy(hit.get("payload") or {})
        if not payload:
            return None
        payload["code"] = code
        payload.setdefault("name", code)
        daily_rows = list(hit.get("daily_rows") or [])
        last = str(hit.get("last_daily_date") or "")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        _new_bar_possible = (last < _today and _now.weekday() < 5
                             and _now.strftime("%H:%M") >= "09:30")
        if _new_bar_possible:
            bar = self._today_forming_bar(code, allow_network=False)
            if bar and str(bar["date"]) > last:
                try:
                    self._rebuild_payload_daily(payload, daily_rows + [bar])
                except Exception:
                    pass
        return payload

    def _today_forming_bar(self, code, allow_network=True):
        """腾讯快照 → 当日 forming bar（ts_date 闸，与 facade._maybe_append_forming 同口径）。

        2026-10-06：**优先复用 `load_quotes` 每 10s 批量拉的快照**（含 ts_date/open/high/low/vol_hand）
        —— 小池标的零额外网络；快照非当日（休市/假期）直接判「不补」，不再白打一次 1.9s 的单只快照。

        allow_network=False：不在批量快照内就**不单独打网络**（快路径用，保证打开即出图）。
        """
        try:
            base = str(code).split("_")[0]
            today = datetime.now().strftime("%Y-%m-%d")
            _c = getattr(self, "_quotes_snaps", None)
            if _c and (_time_mod.time() - _c[0]) < 120:
                _s = (_c[1] or {}).get(base)
                if _s:
                    if _s.get("ts_date") != today:
                        return None       # 快照非当日 ⇒ 休市/假期，不补（免无谓网络）
                    _px = _s.get("price") or 0
                    if _px > 0:
                        return {"date": today, "open": _s.get("open") or _px,
                                "high": _s.get("high") or _px, "low": _s.get("low") or _px,
                                "close": _px, "volume": _s.get("vol_hand") or 0.0}
            if not allow_network:
                return None               # 快路径：不在批量快照内即不补（不打网络）
            # 回退：单只快照（网络）
            from core.market_data import get_provider
            snap = get_provider().snapshot([base]).get(base)
            if not snap or not snap.get("price"):
                return None
            if snap.get("ts_date") != today:
                return None
            px = snap["price"]
            return {"date": today, "open": snap.get("open") or px, "high": snap.get("high") or px,
                    "low": snap.get("low") or px, "close": px, "volume": snap.get("volume") or 0.0}
        except Exception:
            return None

    def _rebuild_payload_daily(self, payload, daily_rows):
        """用 raw daily rows 重算日线序列，替换 payload 的 daily + current_price + version。"""
        import pandas as pd
        df = pd.DataFrame(daily_rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        d = _calc_ma_and_indicators(df)
        payload["period_data"]["daily"] = _to_series(d)
        payload["current_price"] = round(float(d["close"].iloc[-1]), 3)
        payload["version"] = self._chart_version(payload["period_data"])

    def _build_chart_from_df(self, df, out, code, min_frames=None):
        """由日线 DataFrame 构建 K 线弹窗数据（MA/MACD/RSI/BOLL + 周/月/30分/60分 + 支撑箱体通道）。

        min_frames: {"min30": df, "min60": df} 直接注入（测试用，避免联网）；None → 自行取数
        （仅普通个股；指数/em 标的跳过 ⇒ 这两个 Tab 显示「无分时数据」）。
        注：levels/boxes/channel/current_price **仍只用日线算、全 Tab 共用**（owner 2026-10-04 决定）。
        """
        import pandas as pd

        calc_ma_and_indicators = _calc_ma_and_indicators   # 模块级（2026-10-04 抽出，供 payload 补 bar 复用）

        daily = calc_ma_and_indicators(df)
        weekly = calc_ma_and_indicators(df.resample("W-FRI", on="date").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())
        # 月线频率兼容：pandas≥2.2 用 "ME"，旧版用 "M"
        # 2026-08-24 fix: pandas 3.x 次版本为 0，原 split[1]>="2" 误判为旧 "M"(已移除)；用版本元组比较
        _maj, _min = (int(x) for x in pd.__version__.split(".")[:2])
        _month_freq = "ME" if (_maj, _min) >= (2, 2) else "M"
        monthly = calc_ma_and_indicators(df.resample(_month_freq, on="date").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())

        to_series = _to_series   # 模块级（2026-10-04 抽出）

        out["period_data"] = {
            "daily": to_series(daily),
            "weekly": to_series(weekly),
            "monthly": to_series(monthly),
        }

        # 30分/60分（2026-10-04）：原生分钟线，日期带时间。取不到就**不放这两个键**，
        # 前端据此显示「该周期无分时数据」，而不是抛错。
        _raw = min_frames
        if _raw is None:
            _raw = {}
            _c = str(code).split("_")[0]
            if not _c.startswith("em"):      # em 是合成标的(如A股平均股价)，无分时源
                for _key, _freq in (("min30", "30min"), ("min60", "60min")):
                    try:
                        _raw[_key] = _fetch_min_bars(_c, _freq)
                    except Exception:
                        _raw[_key] = None
        _frames = {"daily": daily, "weekly": weekly, "monthly": monthly}
        for _key in ("min30", "min60"):
            _mf = (_raw or {}).get(_key)
            # 太短算不出 MA20/BOLL，放出来只会是一片空缺
            if _mf is None or getattr(_mf, "empty", True) or len(_mf) < 30:
                continue
            _mf = _mf.rename(columns={"time": "date"}) if "time" in _mf.columns else _mf
            _frames[_key] = _mf
            out["period_data"][_key] = to_series(calc_ma_and_indicators(_mf), intraday=True)

        # 黄金分割按周期各算一份（前端切 Tab 即换锚点）
        for _name, _d in _frames.items():
            out["period_data"][_name]["fib"] = self._calc_fibonacci(_d, _name)
        out["levels"] = self._calc_support_resistance(daily)
        out["boxes"] = self._detect_boxes(daily)
        out["channel"] = self._detect_channel(daily)
        out["current_price"] = round(float(daily["close"].iloc[-1]), 3)
        # 数据身份版本号（不是时间）：前端拿它做「没变就别重传/重画」的握手，见 load_stock_chart。
        out["version"] = self._chart_version(out["period_data"])
        out["available"] = True
        return out

    @staticmethod
    def _chart_version(period_data):
        """数据身份版本号：各周期 (末日期:根数) 串联。build 与 payload 补 bar 共用，保证握手一致。"""
        def _sig(k):
            _s = (period_data or {}).get(k)
            _d = (_s or {}).get("dates") or []
            return f"{_d[-1]}:{len(_d)}" if _d else "-:0"
        return "|".join(_sig(k) for k in
                        ("daily", "weekly", "monthly", "min30", "min60"))

    def _detect_boxes(self, daily):
        """检测箱体（P1修复）：严格触及标准 + 优化置信分。
        分位数(88/12)定义初期边界，严格触及验证，置信分=触及质量+时间持久度+宽度合理性。

        P1修复内容：
        1. 触及标准从松散(±0.8-8.8%)改为精确：必须在边界±0.5%内触及
        2. 置信分权重调整：触及质量优先于触及次数
        3. 宽度范围分类：<5%(微箱) 5-12%(正常) 12-22%(宽幅)，权重不同
        4. 合并逻辑改进：避免过度合并历史箱体
        """
        import numpy as np
        d = daily
        if len(d) < 30:
            return []

        recent = d.tail(150).reset_index(drop=True)
        closes = recent["close"].values
        highs = recent["high"].values
        lows = recent["low"].values
        dates = recent["date"].values
        n = len(recent)
        last_close = float(closes[-1])
        last_date = dates[-1]

        WIN = 30
        # 向量化滑窗：分位数/斜率/触及全窗口一次算完（与逐窗循环结果一致，快 ~15x）
        from numpy.lib.stride_tricks import sliding_window_view
        wh = sliding_window_view(highs, WIN)
        wl = sliding_window_view(lows, WIN)
        wc = sliding_window_view(closes, WIN)
        ups = np.percentile(wh, 88, axis=1)
        dns = np.percentile(wl, 12, axis=1)
        _xc = np.arange(WIN) - (WIN - 1) / 2.0
        _denom = float(np.sum(_xc * _xc))
        _slopes = (wc @ _xc) / _denom
        _means = wc.mean(axis=1)
        _rel_slopes = np.abs(_slopes) / np.where(_means == 0, 1e-9, _means)
        # P1修复：触及标准从±0.8-8.8%改为±0.5%(更精确)
        _up_touches = np.sum(wh >= (ups * 0.995)[:, None], axis=1)  # 99.5% 以上算"精确触及"
        _dn_touches = np.sum(wl <= (dns * 1.005)[:, None], axis=1)  # 100.5% 以下算"精确触及"
        _widths = (ups - dns) / np.where(_means == 0, 1e-9, _means) * 100
        # 滑窗收集候选箱体（用区间位置唯一标识，避免重复）
        boxes = {}
        for start in range(0, n - WIN + 1, 3):
            up = float(ups[start])
            dn = float(dns[start])
            rel_slope = float(_rel_slopes[start])
            up_touch = int(_up_touches[start])
            dn_touch = int(_dn_touches[start])
            width_pct = float(_widths[start])
            # P1修复：候选条件更严格 — 横盘<0.3%/天(rather than 0.5%) + 宽度3-22% + 双边精确触及≥2
            if rel_slope < 0.003 and 3.0 <= width_pct <= 22.0 and up_touch >= 2 and dn_touch >= 2:
                key = (round(up, 3), round(dn, 3))
                # P1修复：置信分优化 — 触及质量(precision)优先于触及次数
                # 触及质量分 = (精确触及数 * 2)，其次是横盘度，最后是宽度适中
                touch_quality = (up_touch + dn_touch) * 2.0  # 优先权最高
                flatness = max(0, (0.003 - rel_slope) / 0.003) * 2.0  # 越横盘越好
                # 宽度权重分化：正常箱体(5-15%)得分最高
                if 5 <= width_pct <= 15:
                    width_score = 1.5
                elif 3 <= width_pct < 5 or 15 < width_pct <= 22:
                    width_score = 0.5
                else:
                    width_score = 0
                conf = touch_quality + flatness + width_score
                if key not in boxes or conf > boxes[key]["conf"]:
                    s = dates[start].strftime("%Y-%m-%d") if hasattr(dates[start], "strftime") else str(dates[start])[:10]
                    e = dates[start + WIN - 1].strftime("%Y-%m-%d") if hasattr(dates[start+WIN-1], "strftime") else str(dates[start+WIN-1])[:10]
                    boxes[key] = {"start": s, "end": e, "low": round(dn, 3), "high": round(up, 3),
                                  "touches": (up_touch, dn_touch), "width": round(width_pct, 1),
                                  "conf": round(conf, 1), "rel": 0}

        # 关联现价关系 + 刚突破判定（改进）
        # 刚突破的定义现在基于箱体宽度动态调整，而不是固定的20天+15%
        result = []
        today_days = 999
        try:
            today_days = int(pd.Timestamp(last_date).timestamp() / 86400 - pd.Timestamp(dates[0]).timestamp() / 86400)
        except Exception:
            pass
        for key, b in boxes.items():
            end_date = b["end"]
            try:
                end_dt = pd.Timestamp(end_date)
                days_since = int((pd.Timestamp(last_date) - end_dt).days)
            except Exception:
                days_since = 999

            # P1修复：刚突破判定改为动态，基于箱体宽度
            width = b["width"]
            if width < 5:
                # 微箱体(宽度<5%)：突破后5天内+10%以内算"刚突破"
                recently_broke = days_since <= 5 and (last_close - b["high"]) / b["high"] < 0.10 if b["high"] else False
            elif width < 15:
                # 正常箱体(5-15%)：突破后10天内+12%以内算"刚突破"
                recently_broke = days_since <= 10 and (last_close - b["high"]) / b["high"] < 0.12 if b["high"] else False
            else:
                # 宽幅箱体(>15%)：突破后15天内+15%以内算"刚突破"
                recently_broke = days_since <= 15 and (last_close - b["high"]) / b["high"] < 0.15 if b["high"] else False

            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0  # 现价在箱体内
            elif last_close > b["high"] and recently_broke:
                b["rel"] = 1  # 刚突破上方
            elif last_close > b["high"]:
                b["rel"] = -1  # 上方历史箱体
            else:
                b["rel"] = -2  # 下方历史箱体
            # 距现价距离（用于排序）
            b["dist"] = abs(b["center"] if "center" in b else (b["high"] + b["low"]) / 2 - last_close)
            result.append(b)

        # 合并重叠箱体（P1改进：更严格的重叠条件，避免过度合并）
        def overlap(a, b):
            price_overlap = min(a["high"], b["high"]) - max(a["low"], b["low"])
            price_span = min(a["high"] - a["low"], b["high"] - b["low"])
            # P1修复：价格重叠从>50%改为>80%（更严格，保留历史分阶段特征）
            price_overlap_pct = price_overlap / max(price_span, 1e-9) if price_span > 0 else 0
            # 时间重叠条件也更严格：不仅要有交集，还要至少重叠5天
            try:
                s1 = pd.Timestamp(a["start"])
                e1 = pd.Timestamp(a["end"])
                s2 = pd.Timestamp(b["start"])
                e2 = pd.Timestamp(b["end"])
                overlap_days = (min(e1, e2) - max(s1, s2)).days
                t_overlap = overlap_days >= 5
            except Exception:
                t_overlap = a["end"] > b["start"] and b["end"] > a["start"]
            return price_overlap_pct > 0.8 and t_overlap

        merged = []
        for b in result:
            hit = None
            for m in merged:
                if overlap(m, b):
                    hit = m
                    break
            if hit:
                hit["low"] = min(hit["low"], b["low"])
                hit["high"] = max(hit["high"], b["high"])
                hit["start"] = min(hit["start"], b["start"])
                hit["end"] = max(hit["end"], b["end"])
                hit["conf"] = round(hit["conf"] + b["conf"], 1)
                hit["touches"] = (max(hit["touches"][0], b["touches"][0]), max(hit["touches"][1], b["touches"][1]))
            else:
                merged.append(dict(b))

        # 重算 rel + center + days + 近期有效性
        import datetime as _dt
        now_dt = _dt.datetime.now()
        recent_valid = []
        for b in merged:
            b["center"] = round((b["high"] + b["low"]) / 2, 3)
            try:
                s = _dt.datetime.strptime(b["start"][:10], "%Y-%m-%d")
                e = _dt.datetime.strptime(b["end"][:10], "%Y-%m-%d")
                b["days"] = (e - s).days
                b["days_since_end"] = (now_dt - e).days
            except Exception:
                b["days"] = 0
                b["days_since_end"] = 999
            # 只保留近期箱体（结束距今 ≤45 天），远历史箱体无参考意义
            if b["days_since_end"] > 45:
                continue
            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0
            elif last_close > b["high"]:
                b["rel"] = -1
            else:
                b["rel"] = -2
            recent_valid.append(b)

        # 排序：现价箱体(rel=0) > 上方历史(rel=-1) > 下方历史(rel=-2)；再按置信分
        recent_valid.sort(key=lambda b: (
            0 if b["rel"] == 0 else 1 if b["rel"] == -1 else 2,
            -b["conf"]))

        # P1改进：为K线图添加箱体质量评分信息
        for b in recent_valid:
            # 质量评分维度
            width = b["width"]
            touches = b["touches"]
            days = b["days"]

            # 宽度评级
            if width < 5:
                width_grade = "微"  # 微箱体，敏感但容易假突破
            elif width < 15:
                width_grade = "优"  # 正常箱体，最稳定
            else:
                width_grade = "宽"  # 宽幅箱体，波动大

            # 触及质量评级（基于精确触及次数）
            touch_quality = touches[0] + touches[1]
            if touch_quality >= 6:
                touch_grade = "极强"  # 4+次精确触及，非常稳定
            elif touch_quality >= 4:
                touch_grade = "强"    # 2-3次精确触及，较稳定
            else:
                touch_grade = "弱"    # 1次精确触及，可能噪音

            # 综合质量评分（1-10分）
            quality_score = (touch_quality * 1.5 + max(0, 15 - width) * 0.3 + max(0, 30 - days) * 0.1)
            quality_score = min(10, max(1, round(quality_score, 1)))

            b["width_grade"] = width_grade
            b["touch_grade"] = touch_grade
            b["quality_score"] = quality_score
            # 用于K线图显示的关键字段
            b["display"] = f"{width_grade}箱({width:.1f}%) 触及{touch_grade}({touch_quality}次) 质量{quality_score}/10"

        return recent_valid[:3]

    def _detect_channel(self, daily):
        """检测上行/下行通道：最近 40 日高点/低点线性回归 → 上下轨。
        斜率>0 上行通道，<0 下行通道。返回双轨线端点 + 方向。"""
        import numpy as np
        d = daily
        if len(d) < 25:
            return {"direction": "flat", "slope": 0, "up_line": [], "dn_line": []}
        recent = d.tail(40).reset_index(drop=True)
        n = len(recent)
        x = np.arange(n)
        highs = recent["high"].values
        lows = recent["low"].values

        def regline(vals):
            slope, intercept = np.polyfit(x, vals, 1)
            return slope, intercept, slope * (n - 1) + intercept

        up_slope, up_i, up_end = regline(highs)
        dn_slope, dn_i, dn_end = regline(lows)

        slope = (up_slope + dn_slope) / 2
        # 归一化斜率：每日变化 / 均价，>0.15%/天 = 上行
        avg_price = float(recent["close"].mean()) or 1e-9
        norm_slope = slope / avg_price
        direction = "up" if norm_slope > 0.0015 else ("down" if norm_slope < -0.0015 else "flat")

        # 通道方向反转检测：用更早的 40 日（第 40~80 根往前）回归，与当前方向对比
        reversal = None
        try:
            if len(d) >= 80:
                prev = d.iloc[-80:-40].reset_index(drop=True)
                if len(prev) >= 25:
                    px = np.arange(len(prev))
                    ph = prev["high"].values
                    pl = prev["low"].values
                    p_slope, _ = np.polyfit(px, ph, 1)
                    p_slope2, _ = np.polyfit(px, pl, 1)
                    p_norm = (p_slope + p_slope2) / 2 / (float(prev["close"].mean()) or 1e-9)
                    prev_dir = "up" if p_norm > 0.0015 else ("down" if p_norm < -0.0015 else "flat")
                    if direction in ("up", "down") and prev_dir in ("up", "down") and direction != prev_dir:
                        reversal = "up_to_down" if prev_dir == "up" else "down_to_up"
        except Exception:
            reversal = None

        # 趋势描述补充
        start_price = float(recent["close"].iloc[0])
        end_price = float(recent["close"].iloc[-1])
        ret_40d = (end_price - start_price) / start_price * 100 if start_price else 0
        # 现价在通道中的位置（0=下轨, 100=上轨）：用回归线在当前 x(n-1) 处的值
        up_here, dn_here = up_end, dn_end
        pos_pct = (end_price - dn_here) / (up_here - dn_here) * 100 if up_here != dn_here else 50

        return {
            "direction": direction,
            "slope": round(float(slope), 4),
            "norm_slope_pct": round(norm_slope * 100, 3),
            "up_line": [round(float(up_i), 3), round(float(up_end), 3)],
            "dn_line": [round(float(dn_i), 3), round(float(dn_end), 3)],
            "ret_40d": round(float(ret_40d), 1),
            "pos_pct": round(max(0, min(100, float(pos_pct))), 0),
            "reversal": reversal,   # up_to_down / down_to_up / None
        }

    def _calc_support_resistance(self, daily):
        """按现价强制分类 + 聚类合并 + 强度排序 → 每个方向保留最重要的 3 个。"""
        import pandas as pd
        d = daily
        last_close = float(d["close"].iloc[-1])
        H, L, C = float(d["high"].iloc[-1]), float(d["low"].iloc[-1]), last_close
        PP = (H + L + C) / 3
        candidates = []  # (price, label, strength)

        def add(price, label, strength):
            if not price or price <= 0 or price is None: return
            candidates.append((round(float(price), 2), label, strength))

        # pivot 中枢（注意: R1=2PP-L 才是上方压力, S1=2PP-H 是下方支撑, 但都可能失效）
        add(2 * PP - L, "R1中枢", 2)
        add(2 * PP - H, "S1中枢", 2)
        # 均线（近价均线更有效）
        for n, label, base_strength in ((20, "MA20", 2), (60, "MA60", 2), (180, "MA180", 1)):
            val = d[f"ma{n}"].iloc[-1]
            if not pd.isna(val):
                add(float(val), label, base_strength)
        # 前高/前低（120日局部极值）
        recent = d.tail(120)
        for idx in range(5, len(recent) - 1):
            r = recent.iloc[idx]
            win = recent.iloc[max(0, idx - 3):idx + 4]
            if r["high"] == win["high"].max() and float(r["high"]) > 0:
                add(float(r["high"]), "前高", 2)
            if r["low"] == win["low"].min() and float(r["low"]) > 0:
                add(float(r["low"]), "前低", 2)
        # 量密集区（成交量大的价位，参考性强）
        top_vol = d.nlargest(8, "volume")
        for _, r in top_vol.iterrows():
            add(float(r["close"]), "量密集", 3)

        # 1) 按现价强制分类 + 过滤太近的
        sep = last_close * 0.003  # 0.3% 内忽略
        sup_cand = [(p, l, s) for p, l, s in candidates if p < last_close - sep]
        res_cand = [(p, l, s) for p, l, s in candidates if p > last_close + sep]

        # 2) 聚类合并：价格差 <1.5% 的归一组，取强度最高 + 距现价最近的代表
        def cluster(items):
            items = sorted(items, key=lambda x: x[0])
            groups = []
            for p, l, s in items:
                if groups and abs(p - groups[-1][0]) / groups[-1][0] < 0.015:
                    gp, gl, gs = groups[-1]
                    new_s = max(s, gs)
                    rep_p = p if abs(p - last_close) < abs(gp - last_close) else gp
                    groups[-1] = (rep_p, gl if gs >= s else l, new_s)
                else:
                    groups.append((p, l, s))
            return groups

        sup_cand = cluster(sup_cand)
        res_cand = cluster(res_cand)

        # 3) 强度 = 原始强度 + 距现价近的加成（近的更有参考价值）
        def score(item):
            p, l, s = item
            dist_pct = abs(p - last_close) / last_close * 100
            near_bonus = max(0, 3 - dist_pct * 0.15)  # 距现价越近加分越多
            return s * 0.6 + near_bonus

        sup_cand.sort(key=score, reverse=True)
        res_cand.sort(key=score, reverse=True)

        def fmt(items):
            # 每方向只保留 score 最高的 1 个（最重要）
            if not items:
                return []
            best = max(items, key=lambda it: score(it))
            p, l, s = best
            return [{"price": p, "label": l, "strength": s}]

        return {"supports": fmt(sup_cand), "resistances": fmt(res_cand)}

    def _calc_fibonacci(self, daily, period="daily"):
        """黄金分割：自动锚定该周期**最显著的一段摆动** → 回撤位/扩展位。

        锚点选择：回看 N 根（见 _FIB_LOOKBACK，daily=120 与前端默认视窗对齐）
        → 分形找摆动点（复用 analysis.divergence._local_extrema）
        → 合成"高-低"交替序列 → 滤掉幅度不足的噪声摆动 → 取幅度最大的一段。
        全部不达标时降级为回看区间的最高/最低价（fallback=True）。

        比例位公式（L=摆动低点, H=摆动高点, rng=H-L）：
          上涨（低在前）：回撤 H-rng*ratio，扩展 L+rng*ext
          下跌（高在前）：回撤 L+rng*ratio，扩展 H-rng*ext
        side 按现价分：低于现价=支撑，高于现价=阻力。
        """
        from analysis.divergence import _local_extrema

        n_bars = _FIB_FRACTAL_N.get(period, 3)
        lookback = _FIB_LOOKBACK.get(period, 250)
        min_amp = _FIB_MIN_AMP.get(period, 5.0)
        if daily is None or len(daily) < 2 * n_bars + 5:
            return {"available": False, "reason": "样本不足"}

        d = daily.tail(lookback).reset_index(drop=True)
        n = len(d)
        if n < 2 * n_bars + 5:
            return {"available": False, "reason": "样本不足"}
        # 锚点索引要相对**完整周期序列**（前端 period.dates 是全量），故补齐偏移
        off = len(daily) - n
        highs = d["high"].astype(float).values
        lows = d["low"].astype(float).values
        # 分钟周期的锚点日期要带时间，否则与日线格式混同、图上标不出取自哪根
        _dfmt = "%Y-%m-%d %H:%M" if period in ("min30", "min60") else "%Y-%m-%d"
        dates = [x.strftime(_dfmt) for x in d["date"]]

        peaks, troughs = _local_extrema(highs, lows, n_bars)

        # 1) 合成交替序列：同类型相邻只保留更极端者（否则两者属同一段走势，不构成摆动）
        piv = sorted([(i, "H") for i in peaks] + [(i, "L") for i in troughs])
        seq = []
        for idx, kind in piv:
            px = float(highs[idx]) if kind == "H" else float(lows[idx])
            if seq and seq[-1][1] == kind:
                if (px > seq[-1][2]) if kind == "H" else (px < seq[-1][2]):
                    seq[-1] = (idx, kind, px)
                continue
            seq.append((idx, kind, px))

        def amp(lo_pt, hi_pt):
            base = lo_pt[2]
            return (hi_pt[2] - lo_pt[2]) / base * 100 if base else 0.0

        # 2) 相邻两点对 → 规范成 (低点, 高点) + 方向；3) 过滤幅度不足的噪声摆动
        cands = []
        for a, b in zip(seq, seq[1:]):
            lo_pt, hi_pt = (a, b) if a[1] == "L" else (b, a)
            if hi_pt[2] <= lo_pt[2]:
                continue
            cands.append((lo_pt, hi_pt, "up" if a[1] == "L" else "down", amp(lo_pt, hi_pt)))

        fallback = False
        ok = [c for c in cands if c[3] >= min_amp]
        if ok:
            # 幅度最大者；幅度接近(差<2%)时取更近的一段
            best = max(c[3] for c in ok)
            lo_pt, hi_pt, direction, amplitude = max(
                [c for c in ok if c[3] >= best * 0.98], key=lambda c: c[1][0])
        elif cands:
            lo_pt, hi_pt, direction, amplitude = max(cands, key=lambda c: c[3])
            fallback = True
        else:
            # 无交替摆动点（单边行情）→ 降级为回看区间最高/最低
            hi_i, lo_i = int(d["high"].idxmax()), int(d["low"].idxmin())
            lo_pt, hi_pt = (lo_i, "L", float(lows[lo_i])), (hi_i, "H", float(highs[hi_i]))
            direction = "up" if lo_i < hi_i else "down"
            amplitude = amp(lo_pt, hi_pt)
            fallback = True

        L, H = lo_pt[2], hi_pt[2]
        rng = H - L
        if rng <= 0:
            return {"available": False, "reason": "摆动幅度为零"}

        cur = float(d["close"].iloc[-1])
        levels = []

        def add(ratio, price, kind):
            price = round(float(price), 3)
            if price <= 0:
                return  # 大幅下跌摆动时扩展位可能算到 0 以下，无意义 → 丢弃
            levels.append({
                "ratio": ratio, "price": price, "kind": kind,
                "label": f"{ratio * 100:.1f}%".replace(".0%", "%"),
                "side": "support" if price < cur else "resistance",
                "golden": abs(ratio - 0.618) < 1e-9,
            })

        up = direction == "up"
        for rt in _FIB_RETRACE:
            add(rt, (H - rng * rt) if up else (L + rng * rt), "retracement")
        for ex in _FIB_EXTENSION:
            add(ex, (L + rng * ex) if up else (H - rng * ex), "extension")

        return {
            "available": True,
            "direction": direction,
            "fallback": fallback,
            "swing": {
                "low": {"price": round(L, 3), "index": int(lo_pt[0]) + off, "date": dates[lo_pt[0]]},
                "high": {"price": round(H, 3), "index": int(hi_pt[0]) + off, "date": dates[hi_pt[0]]},
                "amplitude_pct": round(amplitude, 2),
                "bars": int(abs(hi_pt[0] - lo_pt[0])),
                "lookback": int(n),
            },
            "levels": levels,
        }

    # ---------- 选股猎手（概念评分，与 Excel 报告一致） ----------
    def run_hunter(self, date=None, auto=False):
        """后台运行选股猎手（拉取+评分），立即返回；前端轮询 hunter_progress 看进度。

        auto=True 表示定时自动运行（见 HUNTER_AUTORUN_SLOTS）：只有在候选集变化时才推
        飞书，避免一天重复刷屏；手动运行(auto=False)每次都推。
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        if HUNTER_RUN_STATE.get("running") and HUNTER_RUN_STATE.get("date") == date:
            return {"started": True, "running": True, "date": date}
        # 清除旧结果，防读到上一个日期的缓存
        HUNTER_RUN_STATE["date"] = date
        HUNTER_RUN_STATE["running"] = True
        HUNTER_RUN_STATE["result"] = None
        try:
            from modules.market_data import MARKET_PROGRESS as _MP
            _MP.update({"running": True, "phase": "准备", "done": 0, "total": 0, "msg": "启动中"})
        except Exception:
            pass

        def _work():
            try:
                res = self._load_hunter_impl(date)
                HUNTER_RUN_STATE["result"] = res
                # 2026-09-29 owner 拍板：**盘中手动运行只算不推**。
                # 背景：盘中 GO 列现在会算出候选（见 _hunter_build_conformance），而手动运行是
                # dedup=False（每次全推）⇒ 反复点「刷新数据」会把飞书群刷屏。
                # 定时自动档(auto)盘中的建仓推送**保持原样**——2026-09-21 拍板放开的正是它。
                if auto or not _hunter_is_intraday(date):
                    self._push_hunter_build_candidates(res, date, dedup=auto)
            except Exception as e:
                HUNTER_RUN_STATE["result"] = {"available": False, "error": f"选股猎手运行失败: {e}"}
            finally:
                HUNTER_RUN_STATE["running"] = False

        _th.Thread(target=_work, daemon=True).start()
        return {"started": True, "running": True, "date": date}

    def _push_hunter_build_candidates(self, res, date, dedup=False):
        """猎手跑完后，把「符合建仓条件」的股票**按板块逐条**推送到飞书（无候选则不推）。

        口径 = GUI「建仓」列的绿色 x·GO —— 即 _hunter_build_conformance 的时机门控
        （市场有方向/多头结构/回撤到位/金叉加分）。2026-09-21 owner 需求。

        dedup=True（定时自动运行）：**按板块去重** —— 某板块候选集与当日已推的相同则跳过，
        出现新票才推该板块；dedup=False（手动运行）每次全推。
        任何失败只打印，绝不影响猎手结果。可用 stock_hunter/config.json 的
        feishu.push_build_signals=false 关闭。
        """
        try:
            from modules.push_feishu import build_build_candidates, send_build_candidates
            import modules.data_loader as _hdl
            _cfg = _hdl.DataLoader._load_config()
            if not (_cfg.get("feishu", {}) or {}).get("push_build_signals", True):
                print("[建仓推送] 已由配置关闭（feishu.push_build_signals=false），跳过")
                return
            groups = build_build_candidates((res or {}).get("sector_stocks") or {})
            if not groups:
                print("[建仓推送] 无符合建仓条件的股票，跳过")
                return
            _d = str(date).replace("-", "")

            # 当日已推状态（按板块记候选集）；跨日自然清空
            _prev = _load_json(_HUNTER_BUILD_PUSHED_FP, {}) or {}
            _pushed = dict(_prev.get("sectors") or {}) if str(_prev.get("date")) == _d else {}
            if dedup:
                _todo = [g for g in groups
                         if _pushed.get(g["sector"]) != sorted(s["代码"] for s in g["stocks"])]
                if not _todo:
                    print(f"[建仓推送] 各板块候选集均未变化，跳过（{len(groups)} 个板块）")
                    return
                groups = _todo

            total = sum(len(g["stocks"]) for g in groups)
            r = send_build_candidates(_cfg, groups, _d)

            # 记录已推板块的候选集（自动/手动都记，供后续自动运行比对）
            for g in groups:
                _pushed[g["sector"]] = sorted(s["代码"] for s in g["stocks"])
            try:
                _HUNTER_BUILD_PUSHED_FP.write_text(json.dumps(
                    {"date": _d, "sectors": _pushed,
                     "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                    encoding="utf-8")
            except Exception:
                pass
            print(f"[建仓推送] {total} 只 / {len(groups)} 个板块 → 成功{r.get('sent')} 失败{r.get('failed')}")
        except Exception as e:
            print(f"[建仓推送] 失败（已忽略，不影响猎手）: {str(e)[:150]}")

    def hunter_progress(self):
        """返回选股猎手运行进度（供前端进度条轮询）。"""
        try:
            from modules.market_data import MARKET_PROGRESS as _MP
            mp = dict(_MP)
        except Exception:
            mp = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        return {
            "running": bool(HUNTER_RUN_STATE.get("running")),
            "ready": bool(HUNTER_RUN_STATE.get("result")),
            "date": HUNTER_RUN_STATE.get("date"),
            "phase": mp.get("phase", ""),
            "done": int(mp.get("done") or 0),
            "total": int(mp.get("total") or 0),
            "msg": mp.get("msg", ""),
        }

    def load_hunter(self, date=None):
        """选股猎手数据。运行中→running；有该日结果→返回；都没有→**起后台跑 + 返回 running**
        （2026-10-07：原为同步跑整轮扫描，实测 **156s**，会冻死 pywebview 主线程）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        if HUNTER_RUN_STATE.get("running") and HUNTER_RUN_STATE.get("date") == date:
            return {"available": False, "running": True, "date": date}
        if HUNTER_RUN_STATE.get("date") == date and HUNTER_RUN_STATE.get("result"):
            return HUNTER_RUN_STATE["result"]
        # 都没有：触发一次后台运行（与前端 run_hunter 同机制），立即返回 running，前端轮询进度后重取
        try:
            self.run_hunter(date, auto=False)
        except Exception:
            pass
        return {"available": False, "running": True, "date": date}

    def _hunter_build_conformance(self, codes, date):
        """计算各股建仓信号符合度（时机门控 GO：市场有方向/多头结构/回撤到位/金叉加分）。

        读 t_io/cache/daily_kline 日线缓存算特征（零网络；全池约 20s；未缓存跳过显示"—"）。

        2026-09-29 起**盘中不再跳过**（原为「手动点击时维持原状」以省资源）——owner 要求
        盘中点按钮即按当时实时 K 线算出建仓符合度。之所以敢放开：该缓存已由猎手本轮拉取
        **回写当日实时 bar**（见 market_data 的 merge_daily_cache 调用），零网络也能拿到实时价；
        此前不放开的后半段理由（缓存陈旧、GO 列是昨天的）已随回写一并消除。
        返回 {code: {go, regime, met, conds:{t_*:bool}, reason}}。"""
        result = {}
        try:
            import pandas as _pd
            from core.position_builder import _DAILY_CACHE_DIR
            from config import ENTRY_TIMING_PARAMS as _ETP
            # 指数 regime（读指数日线缓存，零网络）
            regime_by_date = {}
            try:
                _idx = json.loads((BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json").read_text(encoding="utf-8")).get("rows", [])
                _idx_df = _pd.DataFrame(_idx)
                _idx_df["close"] = _pd.to_numeric(_idx_df["close"])
                _idx_df["ma60"] = _idx_df["close"].rolling(60).mean()
                _idx_df["date"] = _idx_df["date"].astype(str)
                for _, _r in _idx_df.iterrows():
                    if _r["close"] > _r["ma60"]:
                        regime_by_date[str(_r["date"])] = "trend_up"
                    elif _r["close"] < _r["ma60"] * 0.97:
                        regime_by_date[str(_r["date"])] = "trend_dn"
                    else:
                        regime_by_date[str(_r["date"])] = "range"
            except Exception:
                pass
            _regime = regime_by_date.get(str(date), "range")
            for i, code in enumerate(codes):
                if i % 50 == 0:
                    try:
                        from modules.market_data import MARKET_PROGRESS as _MP
                        _MP.update({"done": i, "total": len(codes), "msg": f"计算建仓符合度 {i}/{len(codes)}"})
                    except Exception:
                        pass
                _fp = _DAILY_CACHE_DIR / f"{str(code).split('_')[0]}.json"
                if not _fp.exists():
                    continue
                try:
                    rows = json.loads(_fp.read_text(encoding="utf-8")).get("rows", [])
                    if len(rows) < 61:
                        continue
                    from core import build_decision as _bd
                    _df = _pd.DataFrame(rows)
                    _f = _bd.features_from_daily(_df, str(date))
                    if not _f:
                        continue
                    # 2026-10-08 owner：猎手个股行与**建仓信号扫描同规则同展示** —— 一律走
                    # build_decision 的单一真源（features_from_daily/timing_decision/verdict_from_timing），
                    # 不再自己手算，保证 判定/得分/通过X/3/条件/否决 与建仓表逐列一致。
                    _dec = _bd.timing_decision(_f, _regime, _ETP)
                    _verdict, _score = _bd.verdict_from_timing(bool(_dec["go"]), _regime, _f, False, _ETP)
                    _reachable = _regime in ("trend_up", "trend_dn")
                    _trend = bool(_f.get("trend_multihead"))
                    _dd_ok = _bd.dd_threshold_ok(float(_f["drawdown"]), _regime) if "drawdown" in _f else False
                    _golden = bool(_f.get("macd_golden_5d"))
                    conds = {"t_regime": bool(_reachable), "t_trend": _trend, "t_drawdown": _dd_ok,
                             "t_golden": _golden, "t_veto": not _dec["veto"]}
                    result[str(code)] = {
                        "go": bool(_dec["go"]), "regime": _regime,
                        "met": int(_reachable) + int(_trend) + int(_dd_ok),
                        "conds": conds, "reason": "；".join(_dec["reasons"]),
                        "score": int(_score), "score_ceiling": 100 if _reachable else 70,
                        "signal_reachable": bool(_reachable), "verdict": _verdict,
                        "veto": list(_dec["veto"]), "above_ma5": _f.get("above_ma5"),
                        "price": _f.get("price"),
                    }
                except Exception:
                    continue
        except Exception:
            pass
        return result

    def _load_hunter_impl(self, date=None):
        """运行 stock_hunter 打分管线，返回 DataLoader 原生产出的表格数据。
        与 Excel 报告 Sheet 1/2/3 数据结构对齐。"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        out = {"date": date, "available": False, "error": ""}
        try:
            import pandas as pd
            from modules.data_loader import DataLoader as HLoader
            from modules.market_data import MarketDataFetcher
            from modules.scorer import ConceptScorer
            from modules.ranker import Top5Ranker
        except Exception as e:
            out["error"] = f"stock_hunter 模块加载失败: {e}"
            return out

        try:
            cfg_path = HUNTER_DIR / "config.json"
            if not cfg_path.exists():
                out["error"] = "stock_hunter/config.json 不存在"
                return out
            with open(cfg_path, encoding="utf-8") as f:
                hunter_cfg = json.load(f)
        except Exception as e:
            out["error"] = f"加载配置失败: {e}"
            return out

        try:
            loader = HLoader(config=hunter_cfg)
            watchlist = loader.load_watchlist()
            if watchlist is None or watchlist.empty:
                # 诊断信息：输出更多细节帮助排查问题
                watchlist_path = HUNTER_DIR / "watchlist_jiuyan.json"
                log.warning(f"[HUNTER] watchlist 加载失败")
                log.warning(f"  - watchlist_path: {watchlist_path}")
                log.warning(f"  - exists: {watchlist_path.exists()}")
                if watchlist_path.exists():
                    try:
                        test_data = json.loads(watchlist_path.read_text(encoding='utf-8'))
                        log.warning(f"  - file size: {watchlist_path.stat().st_size} bytes")
                        log.warning(f"  - records: {len(test_data)}")
                    except Exception as e:
                        log.warning(f"  - read error: {e}")
                out["error"] = f"watchlist 加载失败 (path: {watchlist_path}, exists: {watchlist_path.exists()})"
                return out

            df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()
            codes = list(dict.fromkeys(df_pool["代码"].astype(str).tolist()))

            st_codes = set()
            if "名称" in watchlist.columns:
                st_mask = watchlist["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
                st_codes = set(watchlist.loc[st_mask, "代码"].astype(str).tolist())
            fetcher = MarketDataFetcher(data_dir=str(HUNTER_DIR / "data"), st_codes=st_codes)
            # fix 2026-08-24: 行情拉取近乎全空 → 报错；2026-08-25: 腾讯限流多为瞬态，
            # 不足阈值先整轮重试一次（5s 后），仍不足才报错，避免偶发限流整日断供
            _min_ok = max(1, int(len(codes) * 0.01))
            market_df = pd.DataFrame()
            for _attempt in range(2):
                market_df = fetcher.fetch_for_date(codes, date)
                if len(market_df) >= _min_ok:
                    break
                if _attempt == 0:
                    try:
                        from modules.market_data import MARKET_PROGRESS as _MP
                        _MP.update({"phase": f"行情拉取异常(仅{len(market_df)}只)，5s后整轮重试",
                                    "done": 0, "total": 0, "msg": ""})
                    except Exception:
                        pass
                    import time as _t
                    _t.sleep(5)
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                # done/total 归零 → 前端显示不确定进度条，避免继承拉取期的"100%"误导卡死观感
                _MP.update({"phase": "概念打分", "done": 0, "total": 0, "msg": f"已拉取行情 {len(market_df)} 只，正在打分"})
            except Exception:
                pass
            if len(market_df) < _min_ok:
                out["error"] = (f"行情拉取失败：仅获取 {len(market_df)}/{len(codes)} 只，无法评分。"
                                f"多为腾讯接口限流/网络异常，请稍后重试。"
                                f"若反复失败，可检查 stock_hunter/data/market_{date.replace('-', '')}.csv 缓存是否损坏。")
                return out
            if not market_df.empty:
                if "名称" in market_df.columns:
                    market_df = market_df.drop(columns=["名称"])
                watchlist = watchlist.merge(market_df, on="代码", how="left")
                loader.set_watchlist(watchlist)
                # 用含行情数据的 watchlist 重建 pool
                df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()

            # 打分（含行情数据的 pool）
            dims = hunter_cfg.get("scoring", {}).get("dimensions", [])
            scorer = ConceptScorer(dimensions=dims if dims else None)
            stock_list = []
            for _, row in df_pool.iterrows():
                s = row.to_dict()
                s.setdefault("涨停", int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0)
                s.setdefault("连板天数", 0)
                stock_list.append(s)
            scored = scorer.compute_batch(stock_list)
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"phase": "构建板块/个股明细", "done": 0, "total": 0, "msg": f"打分完成 {len(scored)} 只"})
            except Exception:
                pass

            score_map = {str(s.get("代码", "")): s for s in scored}
            for col in ["总得分", "涨停", "D1强势形态且新高", "D2强势形态",
                        "D4首板资金池", "D5潜在突破10日", "D6潜在突破5日",
                        "D7持续性", "D8情绪分数", "D9活跃程度", "大成交额额外加分"]:
                watchlist[col] = watchlist["代码"].map(
                    lambda x, c=col: score_map.get(str(x), {}).get(c, 0))
            loader.set_watchlist(watchlist)  # ← 关键：评分写入后必须回传
        except Exception as e:
            out["error"] = f"数据/评分失败: {e}"
            return out

        try:
            # 使用 DataLoader 原生方法生成 Excel 同款表格
            import numpy as np
            df_summary = loader.load_concept_summary()
            df_detail = loader.load_detail_ranking()
            top5_list = []  # TOP5 按概念
            top5_ranker = Top5Ranker()
            for _, row in df_pool.iterrows():
                pass  # TOP5 用 ranker.select
            # 按韭研分类聚合并选 TOP5
            for category in sorted(df_pool["韭研分类"].unique()):
                if not category: continue
                cat_df = df_pool[df_pool["韭研分类"] == category]
                stocks = []
                for _, row in cat_df.iterrows():
                    d = row.to_dict()
                    for k, v in (score_map.get(str(d.get("代码", "")), {}) or {}).items():
                        d[k] = v
                    stocks.append(d)
                if stocks:
                    t5 = top5_ranker.select(stocks)
                    top5_list.append({
                        "category": category,
                        "stocks": [{"name": t.get("名称", ""), "code": t.get("代码", ""),
                                    "score": int(float(t.get("总得分", 0) or 0) if str(t.get("总得分")) != "nan" else 0),
                                    "change_pct": round(float(t.get("涨跌幅", 0) or 0), 2) if str(t.get("涨跌幅")) not in ("nan", "None", "") else 0.0}
                                   for t in (t5 or [])[:5]],
                    })

            # DataFrame → 可序列化
            def df_to_rows(df):
                if df is None or df.empty: return [], []
                cols = [str(c) for c in df.columns]
                rows = []
                for _, row in df.iterrows():
                    rows.append({str(c): (None if isinstance(row[c], float) and np.isnan(row[c]) else row[c]) for c in cols})
                return cols, rows


            # 板块热度趋势（对比前一日）
            heat_trends = {}
            try:
                from modules.heat_tracker import load_history as load_heat_history
                heat_hist = load_heat_history()
                today_k = date.replace("-", "")
                dates_sorted = sorted(heat_hist.keys())
                prev_k = dates_sorted[-2] if len(dates_sorted) >= 2 and dates_sorted[-1] == today_k else (
                    dates_sorted[-1] if dates_sorted and dates_sorted[-1] < today_k else None)
                today_heat = {s["板块"]: s for s in heat_hist.get(today_k, [])}
                prev_heat = {s["板块"]: s for s in heat_hist.get(prev_k, [])} if prev_k else {}
                for name, t in today_heat.items():
                    p = prev_heat.get(name, {})
                    heat_trends[name] = {
                        "heat": t.get("热度分"), "trend": t.get("趋势", ""),
                        "prev_heat": p.get("热度分"), "prev_trend": p.get("趋势", ""),
                        "stock_count": t.get("股票数量"), "up_pct": t.get("上涨家数占比%"),
                        "limit_up": t.get("涨停数"), "vol_ratio": t.get("成交额放大倍数"),
                    }
            except Exception:
                pass

            # 板块→个股明细（D5/D6 异常信息）
            sector_stocks = {}
            try:
                for category in sorted(df_pool["韭研分类"].unique()):
                    if not category: continue
                    cat_df = df_pool[df_pool["韭研分类"] == category]
                    stocks = []
                    for _, row in cat_df.iterrows():
                        d = row.to_dict()
                        code = str(d.get("代码", ""))
                        sm = score_map.get(code, {}) or {}
                        d5 = sm.get("D5潜在突破10日", 0) or 0
                        d6 = sm.get("D6潜在突破5日", 0) or 0
                        d9 = sm.get("D9活跃程度", 0) or 0
                        total = sm.get("总得分", 0) or 0
                        try: total = int(float(total))
                        except: total = 0
                        try: d5 = int(float(d5))
                        except: d5 = 0
                        try: d6 = int(float(d6))
                        except: d6 = 0
                        try: d9 = int(float(d9))
                        except: d9 = 0
                        stocks.append({
                            "name": d.get("名称", ""), "code": code,
                            # 细分（韭研概念，| 分隔多分类）——供前端展开板块后按下一级分类分组
                            "concepts": [c.strip() for c in str(d.get("韭研概念", "") or "").split("|") if c.strip()],
                            "score": total, "d5": d5, "d6": d6, "d9": d9,
                            "change_pct": round(float(d.get("涨跌幅", 0) or 0), 2) if str(d.get("涨跌幅")) not in ("nan", "None", "") else 0.0,
                            "limit_up": int(float(d.get("涨停", 0) or 0)) if str(d.get("涨停")) not in ("nan", "None", "") else 0,
                        })
                    stocks.sort(key=lambda x: -x["score"])
                    sector_stocks[category] = stocks
            except Exception:
                pass  # 个股明细非致命

            # 盘后建仓信号符合度注入（实盘盘中跳过省资源；盘后点击"今日数据"时显示）
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"phase": "计算建仓符合度", "done": 0, "total": len(codes), "msg": ""})
            except Exception:
                pass
            try:
                build_conf = self._hunter_build_conformance(codes, date)
                if build_conf:
                    for cat, stocks in sector_stocks.items():
                        for s in stocks:
                            c = build_conf.get(str(s.get("code", "")))
                            if c:
                                s["build_go"] = c["go"]
                                s["build_regime"] = c["regime"]
                                s["build_met"] = c["met"]
                                s["build_conds"] = c["conds"]
                                s["build_reason"] = c["reason"]
                                s["build_score"] = c.get("score")
                                s["build_ceiling"] = c.get("score_ceiling")
                                s["build_reachable"] = c.get("signal_reachable")
                                s["build_verdict"] = c.get("verdict")
                                s["build_veto"] = c.get("veto") or []
                                s["build_above_ma5"] = c.get("above_ma5")
                                s["build_price"] = c.get("price")
            except Exception:
                pass
            # 建仓符合股靠前显示：GO(时机放行)优先 → 符合条件数 → 建仓得分（与建仓表同口径）
            for cat, stocks in sector_stocks.items():
                stocks.sort(key=lambda s: (
                    -(1 if s.get("build_go") else 0),
                    -(s.get("build_met") or 0),
                    -(s.get("build_score") or 0),
                ))

            # 3) 生成排名表 + 注入热度/趋势/股票数
            sum_cols, sum_rows = df_to_rows(df_summary)
            for row in sum_rows:
                cat = row.get("板块", "")
                ht = heat_trends.get(cat, {})
                row["股票数"] = len(sector_stocks.get(cat, []))
                row["热度"] = ht.get("heat")
                trend_str = (ht.get("trend") or "")
                if ht.get("heat") is not None and ht.get("prev_heat") is not None:
                    delta = int(ht["heat"] - ht["prev_heat"])
                    trend_str += (" +" if delta >= 0 else " ") + str(delta)
                row["趋势"] = trend_str
            sum_cols = ["排名", "板块", "平均分", "涨停数", "热度", "趋势", "前三强", "股票数"]

            # 4) 概念得分趋势（近14天热度+均分）
            concept_trends = {}
            try:
                from modules.heat_tracker import load_history as load_heat_history
                heat_hist = load_heat_history()
                all_dates = sorted(heat_hist.keys())[-14:]
                for cat in set(r["板块"] for r in sum_rows):
                    pts = []
                    for d in all_dates:
                        items = heat_hist.get(d, [])
                        hit = next((s for s in items if s["板块"] == cat), None)
                        pts.append({
                            "date": d[4:],  # MMDD
                            "heat": hit.get("热度分") if hit else None,
                            "avg": hit.get("平均分") if hit else None,
                        })
                    concept_trends[cat] = pts
            except Exception:
                pass

            # 2026-08-16: 自动保存 heat history 快照（板块汇总），日期列表/历史视图随运行积累
            try:
                from modules.heat_tracker import save_daily_summary as _save_hs
                _save_hs(date.replace("-", ""), list(sum_rows))
            except Exception:
                pass

            out["available"] = True
            out["pool_size"] = len(codes)
            out["summary_cols"] = sum_cols
            out["summary_rows"] = sum_rows
            out["top5"] = top5_list
            out["heat_trends"] = heat_trends
            out["sector_stocks"] = sector_stocks
            out["concept_trends"] = concept_trends
            out["refreshed_at"] = datetime.now().strftime("%H:%M:%S")
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"running": False, "phase": "完成", "done": 1, "total": 1, "msg": "选股猎手运行完成"})
            except Exception:
                pass
        except Exception as e:
            out["error"] = f"排名生成失败: {e}"
            return out

        return _clean(out)

    # ---------- 批量通道标注（板块成分股） ----------
    def load_channel_batch(self, codes):
        """批量拉日线算通道方向（分批并发，支持全部成分股）。返回 {code: trend}。"""
        import threading
        from core.position_builder import fetch_daily_kline
        codes = [str(c) for c in (codes or []) if c]
        result = {}
        lock = threading.Lock()

        def work(code):
            try:
                df = fetch_daily_kline(code)
                if df.empty or len(df) < 25:
                    trend = "flat"
                else:
                    import numpy as np
                    recent = df.tail(40)
                    closes = recent["close"].values
                    slope = np.polyfit(np.arange(len(closes)), closes, 1)[0]
                    norm = slope / (closes.mean() or 1e-9)
                    trend = "up" if norm > 0.0015 else ("down" if norm < -0.0015 else "flat")
                with lock:
                    result[code] = trend
            except Exception:
                with lock:
                    result[code] = "flat"

        # 分批并发（每批 30，避免太多线程）
        for i in range(0, len(codes), 30):
            batch = codes[i:i + 30]
            threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in batch]
            for t in threads: t.start()
            for t in threads: t.join(timeout=20)
        return _clean({"trends": result})

    # ---------- 个股技术标签引擎 ----------
    def _live_quote_forming(self, code):
        """实时快照 → {price, open, high, low, volume, ts_date} 或 None。P1-2 收敛：走 market_data provider。
        K线主机(ifzq)被 WAF 501 拦截时 fetch_daily_kline 会静默回退缺当日K线的旧缓存，
        用实时快照补一条当日 forming bar，避免技术标签按昨日收盘误判。
        ts_date 新鲜度语义保留：开盘前/快照陈旧时返回昨日日期，_stock_tags_one 据此不补 forming bar。"""
        from core.market_data import get_provider
        base = str(code).split("_")[0]
        return get_provider().snapshot([base]).get(base)

    def _trend30_trend(self, code):
        """30 分钟趋势判定（2026-10-04 方案）。返回 (trend|None, src)。
        不可用（开关关闭/网络/根数不足）→ (None, src)，调用方回退日线斜率。"""
        if not _TREND30_ENABLED:
            return None, "disabled"
        try:
            from analysis.trend30.adapter import get_trend30
            r = get_trend30(code)
        except Exception as e:
            return None, f"error:{type(e).__name__}"
        if r.get("source") != "30min" or not r.get("trend"):
            return None, str(r.get("source") or "error")
        return r["trend"], "30min"

    def _stock_tags_one(self, code):
        """单只股票技术标签（取数 + 补当日 forming bar）→ `_stock_tags_from_df`。"""
        import pandas as pd
        from core.position_builder import fetch_daily_kline
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return {"trend": "flat", "tags": []}
        # P0: ifzq K线主机被 WAF 501 拦截时 fetch_daily_kline 静默回退旧缓存（缺当日 forming bar），
        # 破5/10日线 等标签会用昨日收盘误判（现价已站上均线仍显示破线）。补当日实时 forming bar。
        try:
            # 仅当实时快照时间戳为今日才补 forming bar：开盘前/快照陈旧时腾讯返回昨收，
            # 补进去会把昨收重复计入 MA5 → cur 看似低于虚高的 MA5，误判"破5日线"（08-28 事故）。
            if str(df["date"].iloc[-1]) != datetime.now().strftime("%Y-%m-%d"):
                live = self._live_quote_forming(code)
                if live and live.get("ts_date") == datetime.now().strftime("%Y-%m-%d"):
                    fb = pd.DataFrame([{"date": datetime.now().strftime("%Y-%m-%d"),
                                        "open": live["open"], "close": live["price"],
                                        "high": live["high"], "low": live["low"],
                                        "volume": live["volume"]}])
                    df = pd.concat([df, fb], ignore_index=True)
        except Exception:
            pass
        return self._stock_tags_from_df(df, code)

    def _stock_tags_from_df(self, df, code):
        """由**日线 df** 计算技术标签（纯计算，无取数）。批量标签与持仓体检共用同一口径。"""
        import numpy as np
        import pandas as pd
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        volumes = df["volume"].values
        cur = float(closes[-1])
        n = len(closes)

        # 通道方向（2026-10-04：改按 30 分钟趋势判定；不可用时回退日线斜率）
        recent = df.tail(40)
        rc = recent["close"].values
        slope = np.polyfit(np.arange(len(rc)), rc, 1)[0]
        norm = slope / (rc.mean() or 1e-9)
        daily_trend = "up" if norm > 0.0015 else ("down" if norm < -0.0015 else "flat")
        trend, _trend_src = self._trend30_trend(code)
        if trend is None:                      # 30min 不可用 → 回退日线（保持旧行为）
            trend, _trend_src = daily_trend, "daily"

        # 精密箱体（365日滑窗+斜率+触及验证+重叠合并）
        boxes = self._detect_boxes(df)
        cur_box = next((b for b in boxes if b.get("rel") == 0), None)
        near_box = boxes[0] if boxes else None

        tags = []
        # 箱体位置 + 突破/跌破
        if cur_box:
            lo, hi = cur_box["low"], cur_box["high"]
            if hi > lo:
                pos = (cur - lo) / (hi - lo)
                if pos > 0.85:
                    tags.append({"label": "箱体上沿", "color": "up"})
                elif pos < 0.15:
                    tags.append({"label": "箱体下沿", "color": "down"})
                else:
                    tags.append({"label": "箱体内部", "color": "neutral"})
                if cur > hi:
                    pct = (cur - hi) / hi * 100
                    if pct <= 8:
                        tags.append({"label": "向上突破", "color": "up"})
                    else:
                        # 高于箱体上沿 >8% → 已完全脱离箱体（区别于刚突破的"向上突破"）
                        tags.append({"label": "完全突破", "color": "up"})
                elif cur < lo:
                    tags.append({"label": "跌破下沿", "color": "down"})
        elif near_box and cur > near_box["high"]:
            pct = (cur - near_box["high"]) / near_box["high"] * 100
            if pct <= 8:
                tags.append({"label": "向上突破", "color": "up"})
            else:
                tags.append({"label": "完全突破", "color": "up"})

        # 筑底/筑顶（近20日横盘）
        win = closes[-20:]
        win_vol = volumes[-20:]
        vol_shrink = win_vol.mean() < (volumes[-60:-20].mean() or 1e9) * 0.8 if len(volumes) >= 60 else False
        price_flat = (max(win) - min(win)) / (win.mean() or 1e-9) < 0.10
        if price_flat and trend == "flat":
            if cur_box and cur < (cur_box["low"] + cur_box["high"]) / 2:
                tags.append({"label": "筑底" if vol_shrink else "筑底中", "color": "neutral"})
            elif cur_box and cur > (cur_box["low"] + cur_box["high"]) / 2:
                tags.append({"label": "筑顶", "color": "warn"})

        # 背离（近60日局部高低点）
        win_idx = range(max(2, n - 60), n)
        idxs = list(win_idx)
        price_peaks = []
        price_troughs = []
        for i in range(2, len(idxs) - 2):
            idx = idxs[i]
            if highs[idx] >= highs[idx - 1] and highs[idx] >= highs[idx - 2] and \
               highs[idx] >= highs[idx + 1] and highs[idx] >= highs[idx + 2]:
                price_peaks.append(idx)
            if lows[idx] <= lows[idx - 1] and lows[idx] <= lows[idx - 2] and \
               lows[idx] <= lows[idx + 1] and lows[idx] <= lows[idx + 2]:
                price_troughs.append(idx)
        # MACD
        ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean()
        ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean()
        dif = (ema12 - ema26).values
        # RSI — Wilder 平滑（2026-09-21 统一口径）
        from analysis.indicators import wilder_rsi as _wilder_rsi2
        rsi = _wilder_rsi2(pd.Series(closes), 14).values
        # 顶背离
        if len(price_peaks) >= 2:
            p2, p1 = price_peaks[-2], price_peaks[-1]
            if highs[p1] > highs[p2] and dif[p1] < dif[p2]:
                tags.append({"label": "顶背离", "color": "warn"})
        # 底背离
        if len(price_troughs) >= 2:
            t2, t1 = price_troughs[-2], price_troughs[-1]
            if lows[t1] < lows[t2] and dif[t1] > dif[t2]:
                tags.append({"label": "底背离", "color": "neutral"})

        # 超买/超卖
        cur_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        if cur_rsi > 70:
            tags.append({"label": "超买", "color": "warn"})
        elif cur_rsi < 30:
            tags.append({"label": "超卖", "color": "neutral"})

        # 破5日线/破10日线（现价处于均线下方）
        _ma5_s = pd.Series(closes).rolling(5).mean()
        _ma10_s = pd.Series(closes).rolling(10).mean()
        if not np.isnan(_ma5_s.iloc[-1]) and cur < _ma5_s.iloc[-1]:
            tags.append({"label": "破5日线", "color": "down"})
        if not np.isnan(_ma10_s.iloc[-1]) and cur < _ma10_s.iloc[-1]:
            tags.append({"label": "破10日线", "color": "down"})

        # 通道标签（放最前）
        trend_label = {"up": {"label": "上行", "color": "up"},
                       "down": {"label": "下行", "color": "down"},
                       "flat": {"label": "震荡", "color": "neutral"}}[trend]
        tags.insert(0, trend_label)

        box_pos = None
        if cur_box and cur_box["high"] > cur_box["low"]:
            box_pos = round((cur - cur_box["low"]) / (cur_box["high"] - cur_box["low"]), 2)
        return {"trend": trend, "box_pos": box_pos, "price": round(cur, 3),
                "trend_src": _trend_src, "tags": tags[:6]}

    def load_stock_tags_batch(self, codes):
        """批量拉技术标签（并发，ThreadPoolExecutor），带 TTL 缓存 + 后台异步重算。
        返回 {code: {trend, box_pos, tags}}。GUI 每 10s 轮询 refresh_pb 时走缓存即时返回，
        避免 7-12s 的批量计算（pandas + 网络）阻塞 pywebview 主线程冻结界面。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        global _TAGS_RUNNING
        codes = [str(c) for c in (codes or []) if c]
        if not codes:
            return _clean({"tags": {}})
        today = datetime.now().strftime("%Y-%m-%d")
        now = _time_mod.time()
        # P0 修复(2026-09-01): 缓存 key 加 codes 指纹——此前只按日期键 → 突破扫描 17 批/不同板块
        # 全部命中第 1 批缓存，只扫前 80 只且互相污染标签。改为 (today, codes指纹) 隔离各批次。
        import hashlib
        _codes_fp = hashlib.md5(",".join(sorted(set(codes))).encode()).hexdigest()[:12]
        _cache_key = f"{today}:{_codes_fp}"

        def _compute():
            result = {}
            with ThreadPoolExecutor(max_workers=40) as ex:
                futures = {ex.submit(self._stock_tags_one, c): c for c in codes}
                for fut in as_completed(futures, timeout=90):
                    code = futures[fut]
                    try:
                        result[code] = fut.result()
                    except Exception:
                        result[code] = {"trend": "flat", "tags": []}
            return result

        def _cache_store(tags):
            with _TAGS_LOCK:
                if len(_TAGS_CACHE) > 60:  # 防指纹 key 无限增长：超阈值清空（重算成本可接受）
                    _TAGS_CACHE.clear()
                _TAGS_CACHE[_cache_key] = {"ts": _time_mod.time(), "tags": tags}

        def _spawn_bg():
            """后台重算一次（全局单飞：_TAGS_RUNNING 保证同时只有一批在算）。"""
            global _TAGS_RUNNING
            if _TAGS_RUNNING:
                return
            _TAGS_RUNNING = True

            def _bg():
                global _TAGS_RUNNING
                try:
                    _cache_store(_compute())
                except Exception:
                    pass
                finally:
                    _TAGS_RUNNING = False
            threading.Thread(target=_bg, daemon=True).start()

        with _TAGS_LOCK:
            cached = _TAGS_CACHE.get(_cache_key)
            if cached and (now - cached["ts"]) < _TAGS_TTL:
                return _clean({"tags": cached["tags"]})
            if cached:
                # 缓存过期 → 后台重算，先返回旧值，界面不阻塞
                _spawn_bg()
                return _clean({"tags": cached["tags"]})
        # 冷启动（无缓存）：2026-10-04 改为后台算 + 立即返回空，不再同步阻塞。
        # 原实现首次进建仓表/破位表会同步跑 40 线程 pandas+网络（实测 23~35s），把 pywebview
        # 主线程冻住。代价：标签首次先空着，前端 10s 轮询，约 20s 后自填。调用方均 `.get(code, {})`
        # 守卫，空 map 安全。
        _spawn_bg()
        return _clean({"tags": {}})

    # ---------- 突破箱体股票聚合 ----------
    # 当日有效突破参数（2026-09-29，owner 口径：昨收 ≤ 上沿 < 今价，幅度 0.3~8%）
    _BK_MIN_PCT = 0.3
    _BK_MAX_PCT = 8.0
    _BK_MIN_BARS = 30      # _detect_boxes 在 <30 根时静默返回 []，必须显式挡在前面
    _BK_SCAN_BARS = 200    # 取数窗口：_detect_boxes 只用 tail(150)，200 根足够且能开大 batch
    # 2026-10-08：900 → 200。900 只×200 根 ≈ 18 万行，GM SDK 里 pandas 由 list-of-dicts 拼表 +
    # 逐码 groupby 会**长时间持 GIL**，实测把 pywebview 主线程饿死（freeze 看门狗抓到主线程卡在
    # evaluate_js、后台 `_scan_breakout→daily_many`）。降到 200 每次只 ~4 万行、GIL 持有缩 ~4.5×，
    # 批间再显式让出，界面不再冻（代价：GM 调用次数变多、整轮慢一些，但在后台线程）。
    _BK_BATCH = 200
    # 批量取数整批空时的等待（疑 GM 冷却窗/瞬时不可达）；覆盖为 0 可让测试不真等
    _GM_BATCH_RETRY_WAIT = 62

    def _breakout_pool_codes(self):
        """扫描池 = watchlist_jiuyan.json 的**全部** 6 位码。

        2026-09-29 改：去掉 `_jiuyan_concepts(i).strip()` 过滤。原过滤把 5041 只砍到 **1258 只**
        （多数条目没有概念字段），与 owner「扫该文件里出现的所有股票」的要求不符。
        """
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        return [c for c, i in jy.items() if isinstance(i, dict) and c.isdigit()]

    def _box_top_for(self, hist):
        """as-of 上一交易日的箱体上沿：取**含昨收**那只箱体（rel==0）的 high。

        `_detect_boxes` 在 `low <= last_close <= high` 时置 rel=0；此处 last_close 即昨收，
        故 rel==0 的箱体正是"昨收还在里面"的那只 —— 它的上沿就是今日要突破的线。
        必须用**排除当日**的切片：带上当日跳空的话上沿会被抬高，今天反而判不出突破。
        """
        if hist is None or len(hist) < self._BK_MIN_BARS:
            return None
        for b in self._detect_boxes(hist):
            if b.get("rel") == 0 and b.get("high"):
                return float(b["high"])
        return None

    def _breakout_probe_one(self, code, df, date=None):
        """单只「当日有效突破」判定，命中返回 dict，否则 None。

        口径（owner 2026-09-29）：上沿取自 as-of 上一交易日的箱体，命中条件
        `昨收 ≤ 上沿 < 今价` 且幅度 ∈ [0.3%, 8%]。

        两道**必须**的闸：
          - **交易日闸**：`df` 末根必须就是今日。`daily_many` 只在交易日 09:15-23:59 且快照
            ts_date=当日 时才补 forming bar，故周末/盘前末根=上一交易日 → 此处返回 None。
            没有这道闸，周六会把**周五**的突破当成"今天"报出去。
          - **长度闸**：切片后需 ≥ `_BK_MIN_BARS` 根（恰好 30 根全量的票切完只剩 29，
            `_detect_boxes` 会静默返回 [] 而无声漏掉）。
        """
        if df is None or df.empty:
            return None
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        # 2026-10-09：支持**任选日期**扫描——先按目标日 as-of 切片（去掉目标日之后的 bar，
        # 否则历史日扫描会用到"今天"的末根 = 前视），再要求切片末根**恰好等于目标日**。
        df = df[df["date"].astype(str) <= today]
        if df.empty or str(df["date"].iloc[-1]) != today:
            return None
        if len(df) < self._BK_MIN_BARS + 1:
            return None
        box_top = self._box_top_for(df.iloc[:-1])
        if not box_top:
            return None
        prev_close = float(df["close"].iloc[-2])
        cur = float(df["close"].iloc[-1])
        # 对 rel==0 的箱体 `low ≤ 昨收 ≤ high` 恒成立 ⇒ `prev_close <= box_top` 恒真，
        # 真正起作用的是"今价站上上沿"。两半都留着以对齐 owner 的表述口径。
        if not (prev_close <= box_top < cur):
            return None
        pct = (cur - box_top) / box_top * 100
        if not (self._BK_MIN_PCT <= pct <= self._BK_MAX_PCT):
            return None
        return {"price": round(cur, 3), "prev_close": round(prev_close, 3),
                "box_top": round(box_top, 3), "pct_above": round(pct, 2)}

    def _breakout_disk_path(self, today):
        return BASE / "t_io" / "cache" / f"breakout_{today}.json"

    def _scan_breakout(self, codes, state, date=None):
        """全池扫描「当日有效突破」。state 非空时更新进度（done/total/found/stocks/no_data）。

        2026-09-29 重写要点：
          - 取数改 `facade.daily_many`（GM `history` 列表式，350 只/批 + 批量 forming bar），
            取代逐只 `load_stock_tags_batch` —— 后者走 GM **单线程串行** + 90s 批超时**静默丢票**，
            全池 5000+ 只不可行。
          - 判定改「当日有效突破」，取代旧 `向上突破`（旧标签只看"当前价在箱体上沿之上"，
            没有任何当日成分 ⇒ 三周前突破的票每天照旧被扫出来）。
          - 北交所：`daily_many` 分流到 bj_daily，取不到的码**不出现在返回里** ⇒ 此处计入 no_data
            如实汇报，而不是静默当成"没突破"。
        """
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        from core.market_data.facade import get_provider
        from core.market_data.codec import market_of
        breakouts = []
        total = len(codes)
        dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        rest_total = sum(1 for c in codes if market_of(c) != "BJ")
        # no_data 拆分成因（2026-10-09）：整池取数失败（GM 不可达）≠ 北交所取不到 ≠ 无当日 bar。
        no_data = rest_missing = bj_missing = stale = ok = 0
        BATCH = self._BK_BATCH
        _waited = False
        for i in range(0, total, BATCH):
            chunk = codes[i:i + BATCH]
            try:
                frames = get_provider().daily_many(chunk, days=self._BK_SCAN_BARS)
            except Exception as e:      # 兜底：单批异常绝不终止整轮
                print(f"[WARN] breakout daily_many 失败({len(chunk)}只): "
                      f"{type(e).__name__}: {str(e)[:80]}", flush=True)
                frames = {}
            if not frames and chunk and not _waited:
                # 整批空 ⇒ 疑 GM 冷却窗/瞬时不可达（facade 60s 冷却内后续批全跳过）→ 等窗后重试一次
                # （只等一次，避免 GM 真下线时逐批 62s 打转；仍空则由 fetch_failed 如实汇报）
                _time_mod.sleep(self._GM_BATCH_RETRY_WAIT)
                _waited = True
                try:
                    frames = get_provider().daily_many(chunk, days=self._BK_SCAN_BARS)
                except Exception:
                    frames = {}
            for code in chunk:
                fr = frames.get(code)
                if fr is None or getattr(fr, "empty", True):
                    no_data += 1
                    if market_of(code) == "BJ":
                        bj_missing += 1
                    else:
                        rest_missing += 1
                    continue
                # 有帧但**无当日 bar** → 未就绪/非交易日/停牌（区别于"取数失败"）
                _sl = fr[fr["date"].astype(str) <= dt_target]
                if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                    stale += 1
                    continue
                ok += 1
                hit = self._breakout_probe_one(code, fr, date)
                if not hit:
                    continue
                info = jy.get(code) if isinstance(jy.get(code), dict) else None
                nm = (info or {}).get("name", code)
                concepts = _stock_concepts(info)
                csrc = "offline" if concepts else "none"
                if not concepts:
                    # 离线两源为空：东财磁盘缓存层补拉（仅命中票，~30只/天，fail-open 不阻塞扫描）
                    concepts = _stock_concepts(
                        info, em_boards=_em_boards_disk_cached(self, code))
                    if concepts:
                        csrc = "em"
                breakouts.append({"code": code, "name": nm,
                                  "industry": _stock_industry(info),
                                  "concepts": concepts,
                                  "concepts_source": csrc,
                                  "tags": [{"label": "当日有效突破", "color": "up"}], **hit})
            if state is not None:
                with _BREAKOUT_LOCK:
                    state.update({"done": min(i + BATCH, total), "found": len(breakouts),
                                  "stocks": list(breakouts), "no_data": no_data,
                                  "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                                  "stale": stale, "ok": ok})
            _time_mod.sleep(0.05)   # 2026-10-08：批间显式让出，避免连续持 GIL 把主线程饿死
        breakouts.sort(key=lambda x: -(x.get("pct_above") or 0))
        # 整池不可用判据（任一成立即视为"取数失败，结果不可信"）：
        #   ① 无一只带当日 bar（ok==0）—— GM 整池不可达 / 非交易日 / 行情未就绪；
        #   ② 非北交所码大面积无帧（≥50%）—— GM `daily_batch` 不可达（批量路径无腾讯兜底）。
        fetch_failed = total > 0 and (ok == 0
                                      or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": total, "found": len(breakouts),
                              "stocks": list(breakouts), "no_data": no_data,
                              "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                              "stale": stale, "ok": ok, "fetch_failed": bool(fetch_failed)})
        return breakouts

    def _bk_cache_get(self, key):
        with _BREAKOUT_LOCK:
            return (getattr(self, "_breakout_cache", None) or {}).get(key)

    def _bk_cache_put(self, key, val):
        with _BREAKOUT_LOCK:
            if not hasattr(self, "_breakout_cache"):
                self._breakout_cache = {}
            self._breakout_cache[key] = val

    def load_breakout_stocks(self, date=None):
        """同步全量扫描突破箱体（前端走后端后台线程时用 start_breakout_scan）。
        `date` 缺省=今日；可传任意交易日做历史扫描。结果缓存到内存+磁盘（按日），避免重复扫描。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        hit = self._bk_cache_get(cache_key)
        if hit is not None:
            return hit
        disk_fp = self._breakout_disk_path(today)
        if disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._bk_cache_put(cache_key, disk)
                return disk

        codes = self._breakout_pool_codes()
        if not codes:
            return {"stocks": [], "count": 0}
        st = {}
        breakouts = self._scan_breakout(codes, st, today)
        result = _clean({"date": today, "stocks": breakouts, "count": len(breakouts),
                         "no_data": st.get("no_data", 0),
                         "rest_no_data": st.get("rest_no_data", 0),
                         "bj_no_data": st.get("bj_no_data", 0),
                         "stale": st.get("stale", 0), "ok": st.get("ok", 0),
                         "fetch_failed": bool(st.get("fetch_failed", False))})
        if not result.get("fetch_failed"):
            self._bk_cache_put(cache_key, result)
            try:
                disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return result

    def start_breakout_scan(self, force=False, date=None):
        """启动后台突破扫描（幂等：内存/磁盘缓存命中→立即 done；扫描中→返回当前进度）。
        `date` 缺省=今日；可传任意交易日做历史扫描（结果按日缓存到 `breakout_{date}.json`）。
        force=True 绕开该日缓存强制重扫（前端「🔄 重新扫描」）。
        返回 {status: idle|running|done|error, date, total, done, found, stocks, no_data?, error?}。

        2026-09-29：状态与缓存全部改由 `_BREAKOUT_LOCK` 保护；「扫描中」判定与
        `self._breakout_scan` 赋值在同一临界区内完成，防止两个 force 重扫并发跑同一池。
        """
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        if not force:
            hit = self._bk_cache_get(cache_key)
            if hit is not None:
                return {"status": "done", "date": today, "total": 0, "done": 0,
                        "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                        "no_data": hit.get("no_data", 0),
                        "rest_no_data": hit.get("rest_no_data", 0),
                        "bj_no_data": hit.get("bj_no_data", 0),
                        "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                        "fetch_failed": bool(hit.get("fetch_failed", False))}
            disk_fp = self._breakout_disk_path(today)
            if disk_fp.exists():
                disk = _load_json(disk_fp, None)
                if disk and isinstance(disk, dict) and "stocks" in disk:
                    self._bk_cache_put(cache_key, disk)
                    return {"status": "done", "date": today, "total": 0, "done": 0,
                            "found": disk.get("count", 0), "stocks": disk.get("stocks", []),
                            "no_data": disk.get("no_data", 0),
                            "rest_no_data": disk.get("rest_no_data", 0),
                            "bj_no_data": disk.get("bj_no_data", 0),
                            "stale": disk.get("stale", 0), "ok": disk.get("ok", 0),
                            "fetch_failed": bool(disk.get("fetch_failed", False))}

        with _BREAKOUT_LOCK:
            cur = getattr(self, "_breakout_scan", None)
            if cur and cur.get("status") == "running":
                return dict(cur)
            codes = self._breakout_pool_codes()
            if not codes:
                return {"status": "done", "total": 0, "done": 0, "found": 0, "stocks": []}
            state = {"status": "running", "date": today, "total": len(codes), "done": 0, "found": 0,
                     "stocks": [], "no_data": 0}
            self._breakout_scan = state
            if force:
                # 强制重扫必须**先失效**旧缓存：`get_breakout_scan` 优先返回缓存，
                # 否则前端 poll 第一次就拿到上一轮结果、判为 done 并停止轮询 ⇒
                # 用户看到的是旧数据，"重新扫描"形同无效。
                # （状态已在上方置 running，故此刻起 poll 会读到 running 而非 idle。）
                if hasattr(self, "_breakout_cache"):
                    self._breakout_cache.pop(cache_key, None)
                try:
                    self._breakout_disk_path(today).unlink(missing_ok=True)
                except Exception:
                    pass

        def run():
            try:
                breakouts = self._scan_breakout(codes, state, today)
                result = _clean({"date": today, "stocks": breakouts, "count": len(breakouts),
                                 "no_data": state.get("no_data", 0),
                                 "rest_no_data": state.get("rest_no_data", 0),
                                 "bj_no_data": state.get("bj_no_data", 0),
                                 "stale": state.get("stale", 0), "ok": state.get("ok", 0),
                                 "fetch_failed": bool(state.get("fetch_failed", False))})
                # 取数失败（GM 整池不可达 / 非交易日 / 行情未就绪）→ **不落盘**，
                # 避免把"假 0"缓存成当日定论（2026-10-09 事故：整池 no_data 被当成真 0）。
                if not result.get("fetch_failed"):
                    self._bk_cache_put(cache_key, result)
                    try:
                        self._breakout_disk_path(today).write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                with _BREAKOUT_LOCK:
                    state.update({"status": "done", "done": len(codes),
                                  "found": len(breakouts), "stocks": breakouts})
            except Exception as e:
                with _BREAKOUT_LOCK:
                    state.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return dict(state)

    def get_breakout_scan(self, date=None):
        """轮询后台突破扫描进度。done 后返回完整结果（含磁盘/内存缓存命中）。
        `date` 缺省=今日；扫描中且日期匹配才返回 running，否则 idle。

        返回的是**快照副本**（`dict(state)`），避免 800ms 读线程读到
        「status=done 但 stocks 仍是上一批」的中间态。
        """
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        hit = self._bk_cache_get(cache_key)
        if hit is not None:
            return {"status": "done", "date": today, "total": 0, "done": 0,
                    "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                    "no_data": hit.get("no_data", 0),
                    "rest_no_data": hit.get("rest_no_data", 0),
                    "bj_no_data": hit.get("bj_no_data", 0),
                    "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                    "fetch_failed": bool(hit.get("fetch_failed", False))}
        with _BREAKOUT_LOCK:
            cur = getattr(self, "_breakout_scan", None)
            if cur and cur.get("date") == today:
                return dict(cur)
        return {"status": "idle", "date": today, "total": 0, "done": 0, "found": 0, "stocks": []}

    # ---------- 全市场「刚刚站上5日线」扫描（2026-10-09） ----------
    # 口径复用 core/position_builder.check_ma_break 的 reclaim5（只做「隔夜回站」）：
    #   basis=截至昨日的收盘；prev_MA5=mean(basis[-5:])；cur_MA5=(sum(basis[-4:])+price)/5；
    #   reclaim5 = prev_close < prev_MA5 and price > cur_MA5。
    # 取数走 daily_many 批量帧（当日 forming bar 的 close=最新价），零逐只调用；池沿用突破扫描的全市场池。
    _RC_BARS = 30
    _RC_BATCH = 200

    def _reclaim_probe_one(self, code, df, date=None):
        """单只「刚刚站上5日线」判定（口径单一源 core/ma_reclaim.reclaim5_frame），命中返回 dict 否则 None。"""
        from core.ma_reclaim import reclaim5_frame
        target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        return reclaim5_frame(df, target)

    def _reclaim_disk_path(self, today):
        return BASE / "t_io" / "cache" / f"reclaim_{today}.json"

    def _rc_cache_get(self, key):
        with _BREAKOUT_LOCK:
            return (getattr(self, "_reclaim_cache", None) or {}).get(key)

    def _rc_cache_put(self, key, val):
        with _BREAKOUT_LOCK:
            if not hasattr(self, "_reclaim_cache"):
                self._reclaim_cache = {}
            self._reclaim_cache[key] = val

    def _scan_reclaim(self, codes, state, date=None):
        """全市场扫描「刚刚站上5日线」。state 非空时更新进度（分类同突破扫描）。"""
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        from core.market_data.facade import get_provider
        from core.market_data.codec import market_of
        hits = []
        total = len(codes)
        dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        rest_total = sum(1 for c in codes if market_of(c) != "BJ")
        no_data = rest_missing = bj_missing = stale = ok = 0
        BATCH = self._RC_BATCH
        _waited = False
        for i in range(0, total, BATCH):
            chunk = codes[i:i + BATCH]
            try:
                frames = get_provider().daily_many(chunk, days=self._RC_BARS)
            except Exception as e:      # 兜底：单批异常绝不终止整轮
                print(f"[WARN] reclaim daily_many 失败({len(chunk)}只): "
                      f"{type(e).__name__}: {str(e)[:80]}", flush=True)
                frames = {}
            if not frames and chunk and not _waited:
                _time_mod.sleep(self._GM_BATCH_RETRY_WAIT)
                _waited = True
                try:
                    frames = get_provider().daily_many(chunk, days=self._RC_BARS)
                except Exception:
                    frames = {}
            for code in chunk:
                fr = frames.get(code)
                if fr is None or getattr(fr, "empty", True):
                    no_data += 1
                    if market_of(code) == "BJ":
                        bj_missing += 1
                    else:
                        rest_missing += 1
                    continue
                _sl = fr[fr["date"].astype(str) <= dt_target]
                if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                    stale += 1
                    continue
                ok += 1
                hit = self._reclaim_probe_one(code, fr, date)
                if not hit:
                    continue
                info = jy.get(code) if isinstance(jy.get(code), dict) else None
                nm = (info or {}).get("name", code)
                concepts = _stock_concepts(info)
                csrc = "offline" if concepts else "none"
                if not concepts:
                    concepts = _stock_concepts(
                        info, em_boards=_em_boards_disk_cached(self, code))
                    if concepts:
                        csrc = "em"
                hits.append({"code": code, "name": nm,
                             "industry": _stock_industry(info),
                             "concepts": concepts, "concepts_source": csrc,
                             "tags": [{"label": "站上5日线", "color": "up"}], **hit})
            if state is not None:
                with _BREAKOUT_LOCK:
                    state.update({"done": min(i + BATCH, total), "found": len(hits),
                                  "stocks": list(hits), "no_data": no_data,
                                  "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                                  "stale": stale, "ok": ok})
            _time_mod.sleep(0.05)
        hits.sort(key=lambda x: -(x.get("dev5_pct") if x.get("dev5_pct") is not None else -1e9))
        fetch_failed = total > 0 and (ok == 0
                                      or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": total, "found": len(hits), "stocks": list(hits),
                              "no_data": no_data, "rest_no_data": rest_missing,
                              "bj_no_data": bj_missing, "stale": stale, "ok": ok,
                              "fetch_failed": bool(fetch_failed)})
        return hits

    def load_reclaim_stocks(self, date=None):
        """同步全市场扫描「刚刚站上5日线」。结果缓存内存+磁盘（按日）；取数失败不落盘。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        hit = self._rc_cache_get(cache_key)
        if hit is not None:
            return hit
        disk_fp = self._reclaim_disk_path(today)
        if disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._rc_cache_put(cache_key, disk)
                return disk
        codes = self._breakout_pool_codes()
        if not codes:
            return {"stocks": [], "count": 0}
        st = {}
        hits = self._scan_reclaim(codes, st, today)
        result = _clean({"date": today, "stocks": hits, "count": len(hits),
                         "no_data": st.get("no_data", 0),
                         "rest_no_data": st.get("rest_no_data", 0),
                         "bj_no_data": st.get("bj_no_data", 0),
                         "stale": st.get("stale", 0), "ok": st.get("ok", 0),
                         "fetch_failed": bool(st.get("fetch_failed", False))})
        if not result.get("fetch_failed"):
            self._rc_cache_put(cache_key, result)
            try:
                disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return result

    def start_reclaim_scan(self, force=False, date=None):
        """启动后台「刚刚站上5日线」全市场扫描（幂等：缓存命中→done；扫描中→进度）。
        force=True 绕开该日缓存强制重扫（前端「🔄 扫描该日」）。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        if not force:
            hit = self._rc_cache_get(cache_key)
            if hit is not None:
                return {"status": "done", "date": today, "total": 0, "done": 0,
                        "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                        "no_data": hit.get("no_data", 0),
                        "rest_no_data": hit.get("rest_no_data", 0),
                        "bj_no_data": hit.get("bj_no_data", 0),
                        "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                        "fetch_failed": bool(hit.get("fetch_failed", False))}
            disk_fp = self._reclaim_disk_path(today)
            if disk_fp.exists():
                disk = _load_json(disk_fp, None)
                if disk and isinstance(disk, dict) and "stocks" in disk:
                    self._rc_cache_put(cache_key, disk)
                    return {"status": "done", "date": today, "total": 0, "done": 0,
                            "found": disk.get("count", 0), "stocks": disk.get("stocks", []),
                            "no_data": disk.get("no_data", 0),
                            "rest_no_data": disk.get("rest_no_data", 0),
                            "bj_no_data": disk.get("bj_no_data", 0),
                            "stale": disk.get("stale", 0), "ok": disk.get("ok", 0),
                            "fetch_failed": bool(disk.get("fetch_failed", False))}

        with _BREAKOUT_LOCK:
            cur = getattr(self, "_reclaim_scan", None)
            if cur and cur.get("status") == "running":
                return dict(cur)
            codes = self._breakout_pool_codes()
            if not codes:
                return {"status": "done", "total": 0, "done": 0, "found": 0, "stocks": []}
            state = {"status": "running", "date": today, "total": len(codes), "done": 0, "found": 0,
                     "stocks": [], "no_data": 0}
            self._reclaim_scan = state
            if force:
                if hasattr(self, "_reclaim_cache"):
                    self._reclaim_cache.pop(cache_key, None)
                try:
                    self._reclaim_disk_path(today).unlink(missing_ok=True)
                except Exception:
                    pass

        def run():
            try:
                hits = self._scan_reclaim(codes, state, today)
                result = _clean({"date": today, "stocks": hits, "count": len(hits),
                                 "no_data": state.get("no_data", 0),
                                 "rest_no_data": state.get("rest_no_data", 0),
                                 "bj_no_data": state.get("bj_no_data", 0),
                                 "stale": state.get("stale", 0), "ok": state.get("ok", 0),
                                 "fetch_failed": bool(state.get("fetch_failed", False))})
                if not result.get("fetch_failed"):
                    self._rc_cache_put(cache_key, result)
                    try:
                        self._reclaim_disk_path(today).write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                with _BREAKOUT_LOCK:
                    state.update({"status": "done", "done": len(codes),
                                  "found": len(hits), "stocks": hits})
            except Exception as e:
                with _BREAKOUT_LOCK:
                    state.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return dict(state)

    def get_reclaim_scan(self, date=None):
        """轮询后台「刚刚站上5日线」扫描进度。done 后返回完整结果（含缓存命中）。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        hit = self._rc_cache_get(cache_key)
        if hit is not None:
            return {"status": "done", "date": today, "total": 0, "done": 0,
                    "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                    "no_data": hit.get("no_data", 0),
                    "rest_no_data": hit.get("rest_no_data", 0),
                    "bj_no_data": hit.get("bj_no_data", 0),
                    "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                    "fetch_failed": bool(hit.get("fetch_failed", False))}
        with _BREAKOUT_LOCK:
            cur = getattr(self, "_reclaim_scan", None)
            if cur and cur.get("date") == today:
                return dict(cur)
        return {"status": "idle", "date": today, "total": 0, "done": 0, "found": 0, "stocks": []}

    # ---------- 选股猎手历史 ----------
    def available_hunter_dates(self):
        """返回可选日期（降序）：heat history 日期 + 最近 120 个工作日，支持扫描任意日期。"""
        dates = set()
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            dates.update(hist.keys())
        except Exception:
            pass
        from datetime import timedelta
        _today = datetime.now().date()
        for _i in range(120):
            _d = _today - timedelta(days=_i)
            if _d.weekday() < 5:
                dates.add(_d.strftime("%Y%m%d"))
        return [{"date": d[:4] + "-" + d[4:6] + "-" + d[6:8]} for d in sorted(dates, reverse=True)]

    def load_hunter_history(self, date):
        """读指定日期选股猎手数据：heat history 快照（秒级）优先；非快照日期 → 全量运行（支持扫描任意日期）。"""
        out = {"date": date, "available": False, "error": ""}
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            key = date.replace("-", "")
            items = hist.get(key, [])
            if not items:
                # 2026-08-16: 非历史快照日期 → 全量运行（拉取该日行情+计算），支持任意日期扫描
                return self._load_hunter_impl(date)
            rows = []
            for i, s in enumerate(items):
                rows.append({
                    "排名": i + 1,
                    "板块": s.get("板块", ""),
                    "股票数量": s.get("股票数量", 0),
                    "平均分": s.get("平均分", 0),
                    "涨停数": s.get("涨停数", 0),
                    "热度": s.get("热度分"),
                    "趋势": s.get("趋势", ""),
                    "前三强": "",
                })
            # 板块→成分股（从 watchlist_jiuyan.json 静态筛选，非实时价）
            sector_stocks = {}
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                for code, info in jy.items():
                    if not isinstance(info, dict):
                        continue
                    # sector 字段 + 韭研概念（含编号字段）双源匹配（覆盖更全）
                    concepts = _jiuyan_concepts(info) or str(info.get("概念", ""))
                    sector_field = str(info.get("sector", ""))
                    nm = info.get("name", info.get("名称", code))
                    all_text = (sector_field + "_" + concepts).replace("|", "_").replace("/", "_")
                    parts = [c.strip() for c in all_text.split("_") if c.strip() and len(c.strip()) >= 2]
                    for s in items:
                        sector = s.get("板块", "")
                        if not sector:
                            continue
                        matched = any(sector == p or sector in p or p in sector
                                      for p in parts)
                        if matched:
                            sector_stocks.setdefault(sector, []).append(
                                {"code": code, "name": nm,
                                 "concepts": [c.strip() for c in str(concepts).split("|") if c.strip()],
                                 "score": 0, "d5": 0, "d6": 0,
                                 "d9": 0, "change_pct": 0, "limit_up": 0})
                            break
            except Exception:
                pass

            out["available"] = True
            out["summary_cols"] = ["排名", "板块", "股票数量", "平均分", "涨停数", "热度", "趋势", "前三强"]
            out["summary_rows"] = rows
            out["sector_stocks"] = sector_stocks
            out["is_history"] = True
            out["refreshed_at"] = date
            return _clean(out)
        except Exception as e:
            out["error"] = f"读取历史失败: {e}"
            return out

    def sector_history(self, sector):
        """板块历史：近 N 日该板块的热度/均分/涨停/股票数趋势。"""
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            points = []
            for d in sorted(hist.keys()):
                hit = next((s for s in hist.get(d, []) if s.get("板块") == sector), None)
                if hit:
                    points.append({
                        "date": d[4:6] + "-" + d[6:8],
                        "heat": hit.get("热度分"),
                        "avg": hit.get("平均分"),
                        "limit_up": hit.get("涨停数"),
                        "count": hit.get("股票数量"),
                    })
            return _clean({"sector": sector, "points": points[-30:]})
        except Exception as e:
            return {"sector": sector, "error": str(e), "points": []}

    # ---------- 板块轮动（移植自 sector-rotation-v2） ----------

    @staticmethod
    def _rotation_df_to_rows(df):
        """DataFrame → JSON 安全行列表。"""
        if df is None or df.empty:
            return []
        import numpy as np
        import pandas as pd
        rows = []
        for _, row in df.iterrows():
            item = {}
            for c in df.columns:
                v = row[c]
                if isinstance(v, (np.integer,)):
                    v = int(v)
                elif isinstance(v, (np.floating,)):
                    v = float(v)
                elif isinstance(v, np.bool_):
                    v = bool(v)
                elif isinstance(v, (pd.Timestamp, datetime)):
                    v = str(v)
                item[str(c)] = v
            rows.append(item)
        return _clean(rows)

    def sector_rotation_dates(self):
        """板块轮动可用交易日 + 缓存就绪状态。"""
        try:
            from sector_rotation import data_fetch as _df
            dates = _df.available_dates()
            ready, message = _df.cache_readiness()
            return {"ready": ready, "dates": dates, "message": message}
        except Exception as e:
            return {"ready": False, "dates": [], "message": f"板块轮动初始化失败: {e}"}

    def sector_rotation_progress(self):
        """板块轮动日线缓存构建进度（供前端轮询）。"""
        try:
            from sector_rotation import data_fetch as _df
            prog = dict(_df.ROTATION_PROGRESS)
        except Exception:
            prog = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        return {
            "running": bool(ROTATION_RUN_STATE.get("running")) or bool(prog.get("running")),
            "error": ROTATION_RUN_STATE.get("error"),
            "phase": prog.get("phase", ""),
            "done": int(prog.get("done") or 0),
            "total": int(prog.get("total") or 0),
            "msg": prog.get("msg", ""),
        }

    def _rotation_bootstrap_work(self, codes):
        try:
            from sector_rotation import data_fetch as _df
            _df.bootstrap_daily_cache(codes)
        except Exception as e:
            ROTATION_RUN_STATE["error"] = str(e)
        finally:
            ROTATION_RUN_STATE["running"] = False

    def _build_rotation(self, date, view, tail_days):
        from sector_rotation import data_fetch as _df
        from sector_rotation.engine import build_rotation_model
        daily, industry, dates = _df.load_rotation_inputs(view, date)
        if daily.empty:
            raise ValueError("日线缓存为空，请先构建。")
        if not date or date not in dates:
            date = dates[-1]
        _ckey = (view, str(date), int(tail_days or 18))
        # 命中缓存（内存 → 磁盘），避免 build_rotation_model(~15s) 重复计算
        _cached = _ROTATION_CACHE_MEM.get(_ckey)
        if _cached is None:
            try:
                _fp = _ROTATION_CACHE_DIR / f"{_ckey[0]}_{_ckey[1]}_{_ckey[2]}.json"
                if _fp.exists():
                    _cached = json.loads(_fp.read_text(encoding="utf-8"))
            except Exception:
                _cached = None
        if _cached is not None:
            return _cached
        model = build_rotation_model(
            daily, industry,
            as_of=date, tail_days=int(tail_days or 18),
            include_growth_indices=(view == "industry"),
        )
        _result = {
            "as_of": model.as_of,
            "market_state": model.market_state,
            "summary": _clean(model.summary),
            "sector_frame": self._rotation_df_to_rows(model.sector_frame),
            "trail_frame": self._rotation_df_to_rows(model.trail_frame),
            "family_frame": self._rotation_df_to_rows(model.family_frame),
            "leaders_frame": self._rotation_df_to_rows(model.leaders_frame),
            "dates": dates,
        }
        try:
            _ROTATION_CACHE_MEM[_ckey] = _result
            _ROTATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (_ROTATION_CACHE_DIR / f"{_ckey[0]}_{_ckey[1]}_{_ckey[2]}.json").write_text(
                json.dumps(_result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return _result

    def load_sector_rotation(self, date=None, view="industry", tail_days=18):
        """板块轮动主入口。缓存未就绪 → 后台 bootstrap 并返回进度；就绪 → 返回轮动模型。"""
        try:
            from sector_rotation import data_fetch as _df
            try:
                _df.update_today_if_needed()   # 工作日收盘后自动补当日快照
            except Exception:
                pass
            ready, message = _df.cache_readiness()
            if not ready:
                if not ROTATION_RUN_STATE.get("running") and not _df.ROTATION_PROGRESS.get("running"):
                    ROTATION_RUN_STATE["running"] = True
                    ROTATION_RUN_STATE["error"] = None
                    codes = _df.full_market_codes()
                    _th.Thread(target=self._rotation_bootstrap_work, args=(codes,), daemon=True).start()
                return {"status": "bootstrapping", "message": message, "progress": self.sector_rotation_progress()}
            try:
                result = self._build_rotation(date, view, tail_days)
            except Exception as e:
                return {"status": "error", "message": str(e)}
            return {"status": "ok", **result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ---------- 每日大盘复盘（LLM，2026-08-23 新增） ----------
    def _review_bootstrap_work(self, date, cfg):
        try:
            from core.market_review import run_market_review_stream
            _REVIEW_RUN_STATE["output"] = ""
            def _on_text(t):
                _REVIEW_RUN_STATE["output"] = (_REVIEW_RUN_STATE.get("output") or "") + t
            run_market_review_stream(date, cfg, _on_text)
        except Exception as e:
            _REVIEW_RUN_STATE["error"] = str(e)
        finally:
            _REVIEW_RUN_STATE["running"] = False

    def get_llm_config(self):
        from core.market_review import load_llm_config
        return load_llm_config()

    def save_llm_config(self, base_url, model, api_key, reasoning_effort=""):
        from core.market_review import save_llm_config as _s
        return _s(base_url, model, api_key, reasoning_effort)

    def run_daily_review(self, date, base_url, model, api_key, reasoning_effort=""):
        """大盘复盘（后台线程）。模型配置保存后即触发。返回 {status: running/error}。"""
        try:
            from core.market_review import save_llm_config
        except Exception as e:
            return {"status": "error", "message": f"market_review 加载失败: {e}"}
        cfg = save_llm_config(base_url, model, api_key, reasoning_effort)
        if not cfg.get("saved"):
            return {"status": "error", "message": "模型配置不完整（base_url / model / api_key 必填）"}
        if _REVIEW_RUN_STATE.get("running"):
            return {"status": "running", "message": "已有复盘进行中"}
        _REVIEW_RUN_STATE.update({"running": True, "error": None, "output": ""})
        _th.Thread(target=self._review_bootstrap_work, args=(date, cfg), daemon=True).start()
        return {"status": "running"}

    def daily_review_progress(self):
        return {"running": bool(_REVIEW_RUN_STATE.get("running")), "error": _REVIEW_RUN_STATE.get("error"),
                "output": _REVIEW_RUN_STATE.get("output") or ""}

    def get_daily_review(self, date):
        fp = BASE / "t_io" / "validation" / "daily_review" / f"market_review_{date}.md"
        data = {}
        jp = BASE / "t_io" / "validation" / "daily_review" / f"market_review_{date}.json"
        try:
            if jp.exists():
                data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        try:
            if fp.exists():
                return {"text": fp.read_text(encoding="utf-8"), "exists": True, "data": data}
        except Exception:
            pass
        return {"text": "", "exists": False, "data": {}}

    def get_daily_review_list(self):
        """历史复盘日期列表（2026-08-23：存档翻看）。"""
        d = BASE / "t_io" / "validation" / "daily_review"
        dates = []
        try:
            for fp in sorted(d.glob("market_review_*.md")):
                st = fp.stem.replace("market_review_", "")
                if st and len(st) == 10:
                    dates.append(st)
        except Exception:
            pass
        return {"dates": dates}

    def get_margin_balance(self, days=30):
        """近 N 日两融余额（融资融券余额），供每日复盘两融面板（2026-08-29）。"""
        try:
            from core.market_review import fetch_margin_balance
            return fetch_margin_balance(None, days=int(days or 30))
        except Exception as e:
            return {"missing": True, "reason": str(e)[:120], "series": []}

    def get_zt_dt_history(self, days=30):
        """近 N 日涨停/跌停数（读 sentiment_daily.jsonl），供每日复盘涨停跌停面板走势图（2026-08-29）。"""
        fp = BASE / "t_io" / "logs" / "sentiment_daily.jsonl"
        try:
            rows = []
            if fp.exists():
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("date"):
                            rows.append({"date": d["date"], "zt": d.get("zt_count"), "dt": d.get("dt_count")})
            rows.sort(key=lambda r: r["date"])
            # 按 date 去重（同一天多次生成 eod/tail 快照，保留最后一条）
            dedup = {}
            for r in rows:
                dedup[r["date"]] = r
            rows = [dedup[k] for k in sorted(dedup)]
            n = int(days) if days else 0
            return {"series": rows[-n:] if n > 0 else rows}
        except Exception:
            return {"series": []}

    # ---------- 独立配置（账户总资金+已实现亏损） ----------
    def load_portfolio_config(self):
        """读 t_io/state/accounts_config.json（独立于 holdings.json，用户更新持仓不会覆盖）。
        2026-08-30 起账户配置唯一源头为 accounts_config.json；旧 portfolio_config.json 仅作回退。"""
        fp = PORTFOLIO if PORTFOLIO.exists() else PORTFOLIO_LEGACY
        data = _load_json(fp, {})
        return _clean({
            "accounts": data.get("accounts", {}),
            "realized_loss": data.get("realized_loss", {}),
        })

    def load_auto_status(self):
        """P4-3 自动盘页：读 t_io/bridge（heartbeat.json + 当日 events + KILL_SWITCH）。
        返回 heartbeat{positions/cash/index_regime/index_score} + order/fill/reject/risk 计数
        + 最新 10 条事件 + kill_switch 状态。GM 格式持仓 key 经 codec 转内部码。"""
        hb = _load_json(BRIDGE_DIR / "heartbeat.json", {})
        out = {"heartbeat": None, "events": {"order": 0, "fill": 0, "reject": 0, "risk": 0},
               "latest": [], "kill_switch": (BRIDGE_DIR / "KILL_SWITCH").exists(),
               "bridge_dir": str(BRIDGE_DIR)}
        if hb:
            try:
                from core.market_data.codec import to_internal
            except Exception:
                to_internal = lambda g: str(g).split(".")[-1]
            positions = {}
            for gk, p in (hb.get("positions", {}) or {}).items():
                try:
                    positions[to_internal(str(gk))] = p
                except Exception:
                    positions[str(gk)] = p
            out["heartbeat"] = {
                "time": hb.get("time"), "bar": hb.get("bar"),
                "positions": positions, "cash": hb.get("cash"),
                "index_regime": hb.get("index_regime"), "index_score": hb.get("index_score"),
            }
        date_str = datetime.now().strftime("%Y%m%d")
        ep = BRIDGE_DIR / f"events_{date_str}.jsonl"
        latest = []
        if ep.exists():
            try:
                with open(ep, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        ev = e.get("event")
                        if ev in out["events"]:
                            out["events"][ev] += 1
                            latest.append(e)
            except Exception:
                pass
        out["latest"] = latest[-10:]
        return _clean(out)

    def load_buy_confirm_pending(self):
        """人工确认闸（2026-08-30）：读 BUY_PENDING.json（引擎写）待确认买入请求 + 当日已拒绝清单，
        并从 BUY_DECISION.json 组出已应答集合（防 GUI 重启重弹）。date!=今日 → 忽略。"""
        today = datetime.now().strftime("%Y-%m-%d")
        bp = _load_json(BRIDGE_DIR / "BUY_PENDING.json", {})
        if not isinstance(bp, dict) or bp.get("date") != today:
            return {"date": today, "pending": [], "rejected_today": [], "answered": {}}
        reqs = [r for r in (bp.get("pending") or {}).values()
                if isinstance(r, dict) and r.get("code")]
        reqs.sort(key=lambda r: (r.get("request_ts") or 0))
        dec = _load_json(BRIDGE_DIR / "BUY_DECISION.json", {})
        answered = {}
        if isinstance(dec, dict):
            for code, d in (dec.get("decisions") or {}).items():
                if isinstance(d, dict) and d.get("request_id"):
                    answered[code] = d["request_id"]
        return _clean({"date": today, "pending": reqs,
                       "rejected_today": bp.get("rejected_today") or [],
                       "answered": answered})

    def respond_buy_confirm(self, code, request_id, decision):
        """人工确认闸：写用户确认/拒绝到 BUY_DECISION.json（GUI 单写者，tmp+replace 原子写）。
        引擎只消费 request_id 与内存 pending 匹配的 decision，陈旧/错配一律忽略。"""
        if decision not in ("confirm", "reject"):
            return {"ok": False, "error": "decision 必须为 confirm/reject"}
        if not code or not request_id:
            return {"ok": False, "error": "code/request_id 不能为空"}
        fp = BRIDGE_DIR / "BUY_DECISION.json"
        data = _load_json(fp, {})
        if not isinstance(data, dict) or "decisions" not in data:
            data = {"date": datetime.now().strftime("%Y-%m-%d"), "decisions": {}}
        (data.setdefault("decisions", {}))[code] = {
            "request_id": request_id, "decision": decision,
            "ts": datetime.now().timestamp()}
        data["date"] = datetime.now().strftime("%Y-%m-%d")
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "request_id": request_id, "decision": decision}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 自动盘：建仓扫描 / 添加标的 / 手动建仓做T衔接（2026-08-30） ----------

    def _auto_pool_module(self):
        """加载 config/auto_pool.py（config 无 __init__.py，需把 config 目录入 path）。"""
        if str(BASE / "config") not in sys.path:
            sys.path.insert(0, str(BASE / "config"))
        try:
            import auto_pool
            return auto_pool
        except Exception:
            return None

    def _auto_pool_codes(self):
        """当前 auto 池 6 位码（基于自动侧真源 holdings_auto.json 实时派生，不依赖 auto_pool 模块缓存——新增标的立即可见）。"""
        try:
            from src.holdings_repo import load_auto_pool
            return list(load_auto_pool().keys())
        except Exception:
            return []

    def load_auto_scan(self, date=None):
        """自动盘建仓扫描结果：读 TRACES/auto_scan_{date}.jsonl 聚合 + 合并 auto 池全量
        （未扫描/新增标的 → verdict=pending 待扫描行，保证添加后立即显示）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        fp = TRACES / f"auto_scan_{date}.jsonl"
        latest = {}
        if fp.exists():
            for line in open(fp, encoding="utf-8").read().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                code = r.get("code")
                if not code:
                    continue
                if code not in latest or (r.get("scan_time") or "") > (latest[code].get("scan_time") or ""):
                    latest[code] = r
        # 合并 auto 池全量：不在 trace 的（新增/未扫）→ pending 待扫描行
        try:
            from src.holdings_repo import load_auto
            full = load_auto()
        except Exception:
            full = {}
        for code in self._auto_pool_codes():
            if code in latest:
                continue
            h = full.get(code) or {}
            latest[code] = {
                "code": code, "scan_time": "", "date": date,
                "name": h.get("name", code),
                "base": int(h.get("base") or 0),
                "held": bool(int(h.get("qty") or 0)),
                "verdict": "pending", "score": 0,
                "regime": "", "go": False, "reasons": [], "veto": [],
            }
        rows = sorted(latest.values(), key=lambda r: -(r.get("score") or 0))
        counts = Counter(r.get("verdict", "weak") for r in rows)
        return _clean({"has_data": True, "date": date, "rows": rows, "counts": dict(counts)})

    def run_auto_scan(self, date=None):
        """对 auto 池全量跑建仓判定（EOD 口径，df_1min=None 跳过 W35 日内确认），
        逐行追加 TRACES/auto_scan_{date}.jsonl，返回聚合结果。咨询性扫描，以引擎闸链为准。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        try:
            from src.holdings_repo import load_auto
            from execution.auto.build_decision_auto import decide
            from core.market_data import get_provider
            from config import ENTRY_TIMING_PARAMS
        except Exception as e:
            return {"has_data": False, "error": f"依赖导入失败: {e}"}
        if self._auto_pool_module() is None:
            return {"has_data": False, "error": "auto_pool 不可用"}
        hold = load_auto()
        try:
            prov = get_provider()
            idx = prov.index_daily("sh000001", 400)
        except Exception as e:
            return {"has_data": False, "error": f"指数数据获取失败: {e}"}
        fp = TRACES / f"auto_scan_{date}.jsonl"
        fp.parent.mkdir(parents=True, exist_ok=True)
        _scan_time = datetime.now().strftime("%H:%M:%S")
        for code in self._auto_pool_codes():  # 基于 holdings 实时派生（新增标的也扫），非 auto_pool 模块缓存
            row = {"code": code, "scan_time": _scan_time, "date": date,
                   "name": (hold.get(code) or {}).get("name", code),
                   "base": int((hold.get(code) or {}).get("base") or 0),
                   "held": bool(int((hold.get(code) or {}).get("qty") or 0))}
            try:
                df = prov.daily(code, 400)
                dec = decide(df, idx, date, params=ENTRY_TIMING_PARAMS, df_1min=None)
                row.update({"verdict": dec.get("verdict", "weak"),
                            "go": bool(dec.get("go")), "score": dec.get("score", 0),
                            "regime": dec.get("regime"), "reasons": dec.get("reasons", []),
                            "veto": dec.get("veto", []),
                            "data_insufficient": bool(dec.get("data_insufficient"))})
                _f = dec.get("features") or {}
                if _f.get("price"):
                    row["price"] = _f["price"]
                # 2026-08-31: 构造与手动盘建仓表一致的条件圆点（t_regime/t_trend/t_drawdown/t_golden + t_veto）
                _cond = {}
                _regime = dec.get("regime")
                _cond["t_regime"] = _regime in ("trend_up", "trend_dn")
                _cond["t_trend"] = bool(_f.get("trend_multihead"))
                _dd = _f.get("drawdown")
                if _dd is not None:
                    _cond["t_drawdown"] = (float(_dd) >= -0.03 if _regime != "trend_dn" else float(_dd) < -0.10)
                else:
                    _cond["t_drawdown"] = False
                _cond["t_golden"] = bool(_f.get("macd_golden_5d"))
                if dec.get("veto"):
                    _cond["t_veto"] = False
                row["conditions"] = _cond
            except Exception as e:
                row.update({"verdict": "scan_error", "error": str(e)[:80]})
            try:
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return self.load_auto_scan(date)

    def add_auto_stock(self, code, name, base, type=None):
        """添加新股票到 auto 池（pool=auto + base 目标底仓）→ 原子写自动侧 holdings_auto.json；
        若 code 在 watchlist 且 pool=manual → 改 auto（防引擎 validate_pool_split 拒绝启动）。
        引擎需重启才含该标的。"""
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            base = int(base)
        except (TypeError, ValueError):
            return {"ok": False, "error": "目标底仓须为整数"}
        if base < 100 or base % 100 != 0:
            return {"ok": False, "error": "目标底仓须 ≥100 且为 100 的整数倍"}
        _ap = self._auto_pool_module()
        if _ap is not None and not _ap.is_manual(code):
            return {"ok": False, "error": f"{code} 已在 auto 池"}
        try:
            from src.holdings_repo import upsert_auto_entry, get_entry
            from core.market_data.codec import to_gm
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # 已在 auto/both 池 → 拒绝（基于当前自动侧而非 auto_pool 模块缓存，防重复添加）
        _cur = get_entry(code) or {}
        if str(_cur.get("pool") or "") in ("auto", "both"):
            return {"ok": False, "error": f"{code} 已在 auto 池"}
        try:
            gm_symbol = to_gm(code)
        except Exception:
            gm_symbol = ("SHSE." if code.startswith(("6", "5")) else "SZSE.") + code
        if type is None:
            type = "etf" if code.startswith("5") else "stock"
        try:  # 一步写入：身份 + pool=auto + 目标底仓 base（经网关统一审计）
            upsert_auto_entry(code, name=name or code, gm_symbol=gm_symbol,
                              type=type, base=base, actor="gui", reason="添加自动盘标的")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings_auto 失败: {e}"}
        return {"ok": True, "code": code, "gm_symbol": gm_symbol, "type": type,
                "base": base, "restart_required": True,
                "msg": f"已加入 auto 池（目标底仓 {base}），重启掘金策略后生效"}

    def manual_auto_build(self, code, qty, action="build"):
        """自动盘手动建仓/加仓入口（仅限 auto 池内）：写自动侧 holdings_auto.json（base 设/加）+
        写 AUTO_BUILD.json 武装标记（引擎重启后 BASE 建仓跳过确认闸直接做T）。
        同时清除该 code 既有 BUY_PENDING 请求（防双通道）。"""
        code = str(code or "").strip()
        if action not in ("build", "add"):
            return {"ok": False, "error": "action 必须为 build/add"}
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"ok": False, "error": "数量须为整数"}
        if qty < 100 or qty % 100 != 0:
            return {"ok": False, "error": "数量须 ≥100 且为 100 的整数倍"}
        _ap = self._auto_pool_module()
        if _ap is not None and _ap.is_manual(code):
            return {"ok": False, "error": f"{code} 不在 auto 池（仅限 auto 池内已有股票）"}
        try:
            from src.holdings_repo import load_auto, save_auto
        except Exception as e:
            return {"ok": False, "error": str(e)}
        full = load_auto()
        entry = dict(full.get(code) or {})
        if not entry:
            return {"ok": False, "error": f"{code} 不在自动侧持仓真源"}
        old_base = int(entry.get("base") or 0)
        new_base = qty if action == "build" else old_base + qty
        entry["base"] = new_base
        if str(entry.get("pool") or "") == "manual":
            entry["pool"] = "auto"
        try:
            save_auto({code: entry}, actor="gui", reason="手动建仓/加仓武装")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings_auto 失败: {e}"}
        # 写 AUTO_BUILD.json 武装标记（GUI 直读直写 bridge，与 respond_buy_confirm 同款原子写）
        try:
            ab_fp = BRIDGE_DIR / "AUTO_BUILD.json"
            ab = _load_json(ab_fp, {}) or {}
            ab.setdefault("requests", {})
            ab["requests"][code] = {"action": action, "qty": new_base,
                                    "ts": datetime.now().timestamp()}
            ab["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tmp = ab_fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ab, f, ensure_ascii=False, indent=2)
            tmp.replace(ab_fp)
        except Exception as e:
            return {"ok": False, "error": f"写武装标记失败: {e}"}
        # 清除该 code 的既有 BUY_PENDING 请求（防"武装"与"旧请求"双通道）
        try:
            bp_fp = BRIDGE_DIR / "BUY_PENDING.json"
            bp = _load_json(bp_fp, {}) or {}
            if code in (bp.get("pending") or {}):
                bp["pending"].pop(code, None)
                tmp = bp_fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(bp, f, ensure_ascii=False, indent=2)
                tmp.replace(bp_fp)
        except Exception:
            pass
        return {"ok": True, "code": code, "action": action, "base": new_base,
                "restart_required": True,
                "msg": f"已武装 {'建仓' if action == 'build' else '加仓'} {new_base} 股，"
                       "重启掘金策略后引擎将自动建仓并开始做T"}

    def clear_auto_build(self, code):
        """撤销自动盘手动建仓/加仓武装标记（从 AUTO_BUILD.json 删除该 code）。"""
        try:
            ab_fp = BRIDGE_DIR / "AUTO_BUILD.json"
            ab = _load_json(ab_fp, {}) or {}
            if code in (ab.get("requests") or {}):
                ab["requests"].pop(code, None)
                ab["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                tmp = ab_fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(ab, f, ensure_ascii=False, indent=2)
                tmp.replace(ab_fp)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def load_auto_build_armed(self):
        """读 AUTO_BUILD.json 武装标记（手动建仓/加仓，引擎待消费，一次性）。"""
        ab = _load_json(BRIDGE_DIR / "AUTO_BUILD.json", {}) or {}
        return _clean({"requests": ab.get("requests") or {}})

    def remove_auto_stock(self, code):
        """从 auto 池删除标的（仅 qty/base 均为 0 的候选；有持仓拒绝）。同步 watchlist pool 改回 manual。
        引擎需重启后不再含该标的。"""
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            from src.holdings_repo import load_auto, delete_entry, sync_watchlist_pool
        except Exception as e:
            return {"ok": False, "error": str(e)}
        full = load_auto()
        entry = full.get(code)
        if not entry:
            return {"ok": False, "error": f"{code} 不在自动侧持仓真源"}
        if int(entry.get("qty") or 0) > 0 or int(entry.get("base") or 0) > 0:
            return {"ok": False, "error": f"{code} 有持仓（qty>0），不能从 auto 池删除"}
        try:
            # side="auto"：仅从自动文件移除；both 码保留手动副本并降 pool=manual
            delete_entry(code, side="auto", actor="gui", reason="删除auto标的")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings_auto 失败: {e}"}
        # watchlist 该 code pool 改回 manual（已离开 auto 池，防悬空 auto/both 标记）
        try:
            sync_watchlist_pool(code, "manual")
        except Exception:
            pass
        return {"ok": True, "code": code,
                "msg": f"已从 auto 池删除 {code}（重启掘金策略后生效）"}

    def load_position_manager(self):
        """仓位管理器：每只持仓的目标/当前市值、资金占比、超配欠配。
        目标比例 = STOCK_PARAMS 个股 stock_qty_base_pct 或 全局默认。"""
        try:
            import config
        except Exception:
            config = None
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        total_capital = sum(float(a.get("total_capital") or 0) for a in accounts.values())
        cur = _load_json(HOLDINGS_MANUAL, {})
        # 实时价
        px_map = {}
        try:
            for q in self.load_quotes().get("quotes", []):
                if q.get("price"):
                    px_map[q.get("code")] = float(q["price"])
        except Exception:
            pass
        # 按基础代码合并 A/B 双账户
        merged = {}
        for code, h in cur.items():
            if not isinstance(h, dict):
                continue
            base = str(code).split("_")[0]
            merged.setdefault(base, {"name": h.get("name", code), "codes": []})
            merged[base]["codes"].append((code, h))
        default_pct = 0.30
        if config is not None:
            default_pct = config.PARAMS.get("stock_qty_base_pct", 0.30)
        # 第一遍：收集各股原始比例 + 当前市值，归一化使总和=100%（目标市值总和=总资金）
        raw = []
        for base, info in merged.items():
            sp = {}
            if config is not None:
                sp = config.STOCK_PARAMS.get(base, {}) or {}
            raw_pct = sp.get("stock_qty_base_pct", default_pct)
            mkt_val = 0.0
            total_qty = 0
            cost_total = 0.0
            for code, h in info["codes"]:
                px = px_map.get(code) or px_map.get(base) or float(h.get("pre_close") or 0)
                qty = int(h.get("qty") or 0)
                mkt_val += px * qty
                total_qty += qty
                cost_total += float(h.get("cost") or 0) * qty
            if total_qty <= 0:
                continue  # fix 2026-08-20: 已清仓(base 全部 qty=0)不进仓位管理器
            raw.append({"base": base, "name": info["name"], "raw_pct": raw_pct,
                        "mkt_val": mkt_val, "total_qty": total_qty,
                        "cost": (cost_total / total_qty) if total_qty else 0})
        # W33 A3: 归一化/欠配缺口/分批 抽到 config.build_position_gap 共享（避免 GUI/扫描器两处漂移）
        _cost_map = {r["base"]: r["cost"] for r in raw}
        gap_ctx = config.build_position_gap(total_capital, raw, default_pct) if config else None
        # 2026-09-14: manual 做T 下线——"可T仓位"列（config.suggest_t_budget → t_suggest/t_ratio/t_amp）删除。
        rows = []
        for r in (gap_ctx["rows"] if gap_ctx else []):
            row = dict(r)
            row["cost"] = round(_cost_map.get(r["code"], 0), 3) if r.get("total_qty") else 0
            rows.append(row)
        rows.sort(key=lambda x: -x["pct"])
        return _clean({
            "total_capital": round(total_capital, 0),
            "rows": rows,
            "sum_mkt": round(sum(r["mkt_val"] for r in rows), 0),
            "sum_pct": round(sum(r["pct"] for r in rows), 1),
        })

    @staticmethod
    def _parse_log_line(line):
        """HH:MM:SS [LEVEL] msg  →  {t, level, msg}。"""
        t, level, msg = "", "", line
        try:
            if len(line) >= 8 and line[2] == ":" and line[5] == ":":
                t = line[:8]
                rest = line[8:].strip()
                if rest.startswith("[") and "]" in rest:
                    level, msg = rest[1:rest.index("]")], rest[rest.index("]") + 1:].strip()
                else:
                    msg = rest
        except Exception:
            pass
        return {"t": t, "level": level, "msg": msg,
                "key": Api._is_key_line(line)}

    @staticmethod
    def _is_key_line(line):
        for w in Api.KEY_LINE_WORDS:
            if w in line:
                return True
        for w in Api.NOISE_LINE_WORDS:
            if w in line:
                return False
        return False

    # ---------- 盘中实时载荷（仅今天） ----------
    def load_live(self, date):
        """decision_trace 尾部 + intraday_state + 大盘盘中尾部。"""
        _GUI_HB["ts"] = _time_mod.time()   # 10s 轮询打点
        out = {"signals": [], "intraday_state": {}, "market_intraday": []}

        fp = TRACES / f"decision_trace_{date}.jsonl"
        if fp.exists():
            rows = []
            for line in open(fp, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
            # 取尾部：非 HOLD 优先 + 近阈 HOLD（差 5 分内）+ 普通 HOLD 补足到 20
            non_hold = [r for r in rows if r.get("decision") not in ("HOLD", None)]
            near_hold = [
                r for r in rows if r.get("decision") == "HOLD"
                and ((r.get("buy_score") or 0) >= (r.get("buy_threshold") or 99) - 5
                     or (r.get("sell_score") or 0) >= (r.get("sell_threshold") or 99) - 5)
            ]
            tail = (non_hold + near_hold + [r for r in rows if r.get("decision") == "HOLD"])[-20:]
            def _sig(r):
                bs = r.get("buy_score") or 0; ss = r.get("sell_score") or 0
                bt = r.get("buy_threshold") or 99; st = r.get("sell_threshold") or 99
                near = r.get("decision") == "HOLD" and (bs >= bt - 5 or ss >= st - 5)
                sw = r.get("swing_meta") or {}
                reason = r.get("decision_reason")
                if r.get("decision") == "HOLD" and sw.get("wait"):
                    reason = sw.get("wait")
                return {
                    "scan_time": r.get("scan_time"), "code": r.get("code"),
                    "name": r.get("name"), "price": r.get("price"),
                    "buy_score": bs, "sell_score": ss, "decision": r.get("decision"),
                    "reason": reason,
                    "swing_meta": sw,
                    "buy_threshold": bt, "sell_threshold": st,
                    "near": near,
                }
            out["signals"] = [_sig(r) for r in tail]

        out["intraday_state"] = _load_json(INTRADAY_STATE, {})
        out["market_intraday"] = self.load_market_score(date).get("intraday", [])
        out["add_watch"] = self.compute_add_watch(date)
        return _clean(out)

    # ---------- 建仓/加仓信号增量轮询 ----------
    def poll_new_position_signals(self, date):
        """增量读 position_builder，返回新增 signal（scan_type=intraday）。
        in_holdings=true → 加仓，false → 建仓。首次/切日期/轮转只建基线。"""
        out = {"signals": [], "baseline": True}
        fp = TRACES / f"position_builder_{date}.jsonl"
        if not fp.exists():
            self._pos["date"] = None
            self._pos["offset"] = 0
            self._pos["seen"] = set()
            return out
        try:
            size = fp.stat().st_size
        except Exception:
            return out

        st = self._pos
        if st["date"] != date or size < st["offset"]:
            st["date"] = date
            st["offset"] = size
            st["seen"] = set()
            return out
        if size <= st["offset"]:
            return {"signals": [], "baseline": False}

        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                f.seek(st["offset"])
                data = f.read()
        except Exception:
            return out
        st["offset"] = size
        out["baseline"] = False

        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("scan_type") != "intraday" or r.get("verdict") != "signal":
                continue
            key = (r.get("scan_time"), r.get("code"))
            if key in st["seen"]:
                continue
            st["seen"].add(key)
            in_hold = bool(r.get("in_holdings"))
            out["signals"].append({
                "scan_time": r.get("scan_time"),
                "code": r.get("code"), "name": r.get("name"),
                "price": r.get("price"),
                "composite_score": r.get("composite_score"),
                "verdict": r.get("verdict"),
                "in_holdings": in_hold,
                "type": "加仓" if in_hold else "建仓",
                "suggested_qty": (r.get("position") or {}).get("suggested_qty")
                    if isinstance(r.get("position"), dict) else r.get("suggested_qty"),
                "suggested_price": (r.get("position") or {}).get("suggested_price")
                    if isinstance(r.get("position"), dict) else r.get("suggested_price"),
            })
        return _clean(out)

    # ---------- 建仓股池增删 ----------
    def search_stock(self, query):
        """按代码或名称模糊搜索股票。返回匹配列表（最多10条）。"""
        import urllib.request as _ur, os as _os
        for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"]:
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        query = (query or "").strip()
        if not query:
            return {"results": []}
        results = []
        # 1. 精确代码: provider 快照直查（名称；竞价/快照专用腾讯，gm 无中文名）
        if query.isdigit() and len(query) == 6:
            try:
                from core.market_data.tencent_provider import TencentProvider
                snap = TencentProvider().snapshot_auction([query])
                if query in snap and snap[query].get("name"):
                    results.append({"code": query, "name": snap[query]["name"]})
            except Exception:
                pass
        # 2. 名称模糊: 从 watchlist_jiuyan.json 匹配（{code: {name,...}} 或 list）
        if not results:
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                if isinstance(jy, dict):
                    for code, info in jy.items():
                        name = str(info.get("名称", "") if isinstance(info, dict) else "")
                        if query in name or query in code:
                            results.append({"code": code, "name": name})
                            if len(results) >= 10:
                                break
                elif isinstance(jy, list):
                    for s in jy[:800]:
                        code = str(s.get("代码", ""))
                        name = str(s.get("名称", ""))
                        if query in name or query in code:
                            results.append({"code": code, "name": name})
                            if len(results) >= 10:
                                break
            except Exception:
                pass
        return _clean({"results": results})

    def add_to_watchlist(self, code, name):
        """将股票加入 watchlist_buy.json（status=monitoring）。

        人工盘/自动盘**互不阻塞**（2026-09-20 owner 拍板）：auto 池标的也可加入人工盘，
        人工盘建仓表会显示它（带「自动」徽章）。仍照旧写 pool=auto，
        以免触发引擎 validate_pool_split 的启动守卫（pool=manual 且属 AUTO_POOL 才冲突）。
        """
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {"stocks": {}, "total_capital": 300000, "max_per_stock_pct": 0.2})
        stocks = wl.setdefault("stocks", {})
        if code in stocks:
            stocks[code]["status"] = "monitoring"
        else:
            # T-4(2026-09-02): 新建条目缺省 manual 会与 auto 池标的冲突（002409 教训）——
            # 若该码属 auto/both 池则照抄持仓池归属。
            # fix 2026-09-21: 原先一律写 "auto" 把 both 压平 → _is_manual_pool 判 False
            # → 该标的永远不进人工盘扫描（表现：建仓表一直"等待扫描"）。照抄原值即可
            # 满足启动守卫（守卫只拒 pool=="manual"）。
            _entry = {"name": name, "status": "monitoring", "composite_score": 0,
                      "criteria_met": {}, "suggested_qty": 0, "in_holdings": False}
            try:
                from src.holdings_repo import get_entry
                _h = get_entry(code)
                if _h and str(_h.get("pool") or "") in ("auto", "both"):
                    _entry["pool"] = str(_h.get("pool"))
            except Exception:
                pass
            stocks[code] = _entry
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "code": code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_and_scan(self, code, name):
        """加入建仓股池并**立即单股扫描**，返回可展示的成败明细（GUI「+ 添加」用）。

        为什么需要它：扫描表 _agg_position_builder 只从扫描轨迹
        position_builder_{date}.jsonl 构行，而 add_to_watchlist 只写 watchlist_buy.json
        ⇒ 新加的股不扫描就永远不出现在表里，用户会误判"没加成功"。

        返回 {ok, code, name, scan_date, visible_in_manual, scan:{verdict,
        composite_score, block_reason, reason}, scan_error}；失败 {ok:False, error}。
        """
        r = self.add_to_watchlist(code, name)
        if not r.get("ok"):
            return r

        # 扫描只写"今天"的轨迹，不写历史文件（避免篡改历史）；
        # 若用户正在看历史日期，前端据此提示"切回今日可见该行"
        scan_date = datetime.now().strftime("%Y-%m-%d")
        scan = None
        scan_error = None
        try:
            from core.position_builder import run_position_scan
            res = run_position_scan(date_str=scan_date, target_code=code,
                                    scan_type="manual", silent=True, no_feishu=True) or []
            if res:
                _r0 = res[0]
                # reason = 卡点 > 扫描内部错误 > note，兜底给一句人话，
                # 避免 insufficient_data（无分钟数据/未开盘）这类情况前端只能显示英文枚举
                _errs = [str(x) for x in (_r0.get("errors") or []) if str(x).strip()]
                _note = str(_r0.get("note") or "").strip()
                scan = {
                    "verdict": _r0.get("verdict"),
                    "composite_score": int(_r0.get("composite_score") or 0),
                    "block_reason": _r0.get("block_reason"),
                    "reason": _r0.get("block_reason") or (_errs[0] if _errs else (_note or None)),
                }
                # 单股扫描异常不阻断整轮（position_builder 的既有约定），这里如实带回
                if _r0.get("scan_error"):
                    scan_error = str(_r0.get("scan_error"))[:200]
            else:
                scan_error = "扫描未返回结果（该码可能不在股池中）"
        except Exception as e:
            scan_error = str(e)[:200]

        # 走到这里说明该股会被人工盘表显示（auto 池未持有的已在上面被拒；
        # 持仓股即便 pool=auto 也被 _visible() 放行）
        return {"ok": True, "code": code, "name": name, "scan_date": scan_date,
                "visible_in_manual": True, "scan": scan, "scan_error": scan_error}

    def remove_from_watchlist(self, code):
        """从 watchlist_buy.json 删除股票。"""
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {})
        stocks = wl.get("stocks", {})
        if code in stocks:
            del stocks[code]
            try:
                tmp = fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(wl, f, ensure_ascii=False, indent=2)
                tmp.replace(fp)
                return {"ok": True, "code": code}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "股票不在股池中"}

    # ---------- W33 G3: 人工确认建仓（回写 signal_history，喂 forward_tracker） ----------
    def confirm_position(self, code, price=None, qty=None):
        """人工确认建仓 → signal_history 追加 {confirmed, confirm_price, confirm_time, confirm_qty}，
        状态置 confirmed（不再重复出建仓建议）。forward_tracker 以 confirm_price 为基准算前瞻收益。"""
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {})
        stocks = wl.get("stocks", {})
        if code not in stocks:
            return {"ok": False, "error": "股票不在股池中"}
        stock = stocks[code]
        hist = stock.setdefault("signal_history", [])
        confirm_price = float(price) if price else float(stock.get("suggested_price") or 0)
        confirm_qty = int(qty) if qty else int(stock.get("suggested_qty") or 0)
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "confirm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed": True,
            "confirm_price": confirm_price,
            "confirm_qty": confirm_qty,
            "source": "gui_confirm",
        }
        hist.append(entry)
        stock["status"] = "confirmed"
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "code": code, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_signal_condition_detail(self, code, date=None):
        """获取某支股票的详细条件检查报告（用于 GUI 折叠式面板）。
        返回 {conditions_met, conditions_total, conditions: [{name, status, message, detail}], blockers: [...]}"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        fp = TRACES / f"position_builder_{date}.jsonl"
        if not fp.exists():
            return {"available": False, "error": "扫描数据不可用"}

        # 查找该股票的最新扫描记录
        latest_record = None
        try:
            lines = open(fp, encoding="utf-8").read().splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    if r.get("code") == code:
                        latest_record = r
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if not latest_record:
            return {"available": False, "error": f"未找到 {code} 的扫描记录"}

        # 构建条件详情列表：先时机门控条件（现行判定，2026-08-27 对齐），再旧双通道（参考级）
        conditions_detail = []
        channels = latest_record.get("channels", {})
        conditions = latest_record.get("conditions", {})
        verdict = latest_record.get("verdict", "")
        blockers_raw = latest_record.get("blockers", [])
        timing = latest_record.get("timing") or {}

        _bk_by_key = {b.get("key"): b for b in blockers_raw if isinstance(b, dict)}

        # 1) 时机门控条件（trace 中 conditions 为 bool 映射；此前面板只显示旧双通道，
        #    真正的建仓条件不可见——"条件不符合没说清楚"的根源）
        for _k, _label, _required in (
                ("t_regime", "市场有方向", True),
                ("t_trend", "多头结构", True),
                ("t_drawdown", "回撤到位", True),
                ("t_golden", "MACD金叉(近5日)", False)):
            if _k not in conditions:
                continue
            _ok = bool(conditions.get(_k))
            _bk = _bk_by_key.get(_k)
            if _ok:
                _msg = "已满足"
            elif _bk:
                _msg = f"未满足：{_bk.get('gap_txt', '')}（需：{_bk.get('need', '')}）"
            else:
                _msg = "未满足"
            conditions_detail.append({
                "category": "timing",
                "name": _label if _required else f"{_label}（加分项，不影响判定）",
                "verdict": "pass" if _ok else "fail",
                "message": _msg,
            })
        # 否决因子（2026-08-27 因子挖掘：爆量/远离MA60，仅触发时存在于 conditions）
        if conditions.get("t_veto") is False:
            _bk = _bk_by_key.get("t_veto") or {}
            conditions_detail.append({
                "category": "timing",
                "name": "否决因子（爆量≥3倍 / 偏离MA60>+20%）",
                "verdict": "fail",
                "message": f"已触发否决：{_bk.get('gap_txt') or timing.get('reason') or ''}",
            })

        # 2) 旧双通道（参考级·未验收 W33 A4，不驱动判定）
        for ch_name in ("iceberg", "breakout"):
            ch = channels.get(ch_name, {})
            if not ch:
                continue
            conditions_detail.append({
                "category": ch_name,
                "name": f"（参考）{ch.get('name', ch_name)}",
                "verdict": ch.get("verdict", ""),
                "score": ch.get("score", 0),
                "message": f"得分 {ch.get('score', 0)}/100，状态: {ch.get('verdict', '未知')}（参考级，不影响判定）"
            })

        # 增强 blockers 信息：计算具体指标值
        blockers_detail = []
        for b in blockers_raw:
            blocker_info = {
                "key": b.get("key", ""),
                "label": b.get("label", ""),
                "current": b.get("cur", ""),
                "required": b.get("need", ""),
                "gap": b.get("gap_txt", ""),
                "message": f"【{b.get('label', '未知')}】{b.get('gap_txt', '条件未满足')}"
            }

            # 增强特定卡点的信息
            if b.get("key") == "t_regime":
                # 计算具体的指数目标值
                blocker_info["detail"] = self._compute_regime_targets(latest_record)
            elif b.get("key") == "t_drawdown":
                # 添加回撤逻辑说明
                blocker_info["detail"] = self._compute_drawdown_detail(latest_record)

            blockers_detail.append(blocker_info)

        # 计算满足的必要条件数（时机门控 3 必要条件；trace 中 conditions 为 bool 映射，
        # 此前按 dict.get('passed') 取值恒为 0——已修复）
        _NECESSARY = ("t_regime", "t_trend", "t_drawdown")
        if any(k in conditions for k in _NECESSARY):
            conditions_met = sum(1 for k in _NECESSARY if conditions.get(k))
            conditions_total = 3
        else:
            conditions_met = 0
            conditions_total = 0

        return {
            "available": True,
            "code": code,
            "name": latest_record.get("name", code),
            "verdict": verdict,
            "composite_score": latest_record.get("composite_score", 0),
            "scan_time": latest_record.get("scan_time", ""),
            "conditions_met": conditions_met,
            "conditions_total": conditions_total,
            "conditions_detail": conditions_detail,
            "blockers": blockers_detail,
            "divergence": latest_record.get("divergence_detail", {}),
        }

    def _compute_regime_targets(self, record: dict) -> dict:
        """从 blockers 或特征中计算市场方向条件的具体指数目标值"""
        # 2026-08-27: regime 改读 timing（trace 顶层无 regime 字段，此前恒为空导致详情错乱）
        regime = (record.get("timing") or {}).get("regime") or record.get("regime", "")
        blockers = record.get("blockers", [])

        # 尝试从 blockers 中提取指数信息
        regime_blocker = None
        for b in blockers:
            if b.get("key") == "t_regime":
                regime_blocker = b
                break

        detail = {
            "regime": regime,
            "raw_message": regime_blocker.get("gap_txt") if regime_blocker else "未知",
        }

        # 2026-08-28: 给出具体指数点位（当前值/多头线/空头线/各差多少）。
        # 优先读 trace timing.index（新格式）；旧 trace 无该字段时从指数缓存现算（口径同 timing_gate）。
        # A-4(2026-09-07): 旧 trace 兜底按 code 所属板读对应缓存——原恒读 index_sh000001，
        # 深主板/创业板/科创板个股（30x/00x/68x/588x）会错用上证点位。新 trace 的
        # timing.index.index_code 已带板指数，直接采用；缺时按 code resolve_index 回落。
        idx_info = (record.get("timing") or {}).get("index") or {}
        close, up_line, dn_line, ma60 = (idx_info.get("close"), idx_info.get("up_line"),
                                         idx_info.get("dn_line"), idx_info.get("ma60"))
        if not (close and up_line and dn_line):
            try:
                _idx_code = idx_info.get("index_code")
                if not _idx_code:
                    from core.board_index import resolve_index as _ri
                    _idx_code = _ri(str(record.get("code") or "sh000001"))[0]
                import json as _json
                _cache = TRACES.parent / "cache" / "daily_kline" / f"index_{_idx_code}.json"
                _rows = _json.loads(_cache.read_text(encoding="utf-8"))["rows"]
                import pandas as _pd
                _c = _pd.Series([float(x["close"]) for x in _rows])
                close = float(_c.iloc[-1])
                ma60 = float(_c.rolling(60).mean().iloc[-1])
                up_line = round(ma60 * 1.005, 1)   # 与 timing_gate._regime 同口径
                dn_line = round(ma60 * 0.97, 1)
            except Exception:
                close = up_line = dn_line = ma60 = None

        if close and up_line and dn_line:
            detail["action"] = "需要指数突破 MA60 缓冲带（确立方向）"
            detail["rule"] = (f"多头线：站上 {up_line:.1f}（MA60 {ma60:.1f} × 1.005）｜"
                              f"空头线：跌破 {dn_line:.1f}（MA60 × 0.97）")
            detail["message"] = (f"当前指数 {close:.2f}，位于缓冲带 {dn_line:.1f} ~ {up_line:.1f} 内（无方向）："
                                 f"距多头线 {(up_line / close - 1) * 100:+.2f}%，"
                                 f"距空头线 {(dn_line / close - 1) * 100:+.2f}%")
        elif regime_blocker:
            gap_txt = regime_blocker.get("gap_txt", "")
            detail["message"] = gap_txt
            if "MA60" in gap_txt:
                detail["action"] = "需要指数通过 MA60 缓冲带的检查"
                if regime == "trend_up":
                    detail["rule"] = "指数需站上 MA60×1.005（多头确认）"
                elif regime == "trend_dn":
                    detail["rule"] = "指数需跌破 MA60×0.97（空头确认）"
                else:
                    detail["rule"] = "指数需突破 MA60±缓冲带（确立方向）"

        return detail

    def _compute_drawdown_detail(self, record: dict) -> dict:
        """计算回撤到位条件的详细说明"""
        # 2026-08-27: regime/drawdown 改读 timing/timing_features（trace 顶层无 features 字段，
        # 此前 drawdown 恒 0.0 → 详情永远显示"已满足"，严重误导）
        regime = (record.get("timing") or {}).get("regime") or record.get("regime", "")
        feats = record.get("timing_features") or record.get("features") or {}
        dd = feats.get("drawdown")
        if dd is None:
            # 旧 trace 无 timing_features（2026-08-27 前落盘）：从回撤卡点的 cur 文本回补（如 "-5.0%"）
            for _b in record.get("blockers", []):
                if _b.get("key") == "t_drawdown" and _b.get("cur"):
                    try:
                        dd = float(str(_b["cur"]).replace("%", "")) / 100
                    except (ValueError, TypeError):
                        pass
                    break

        detail = {
            "regime": regime,
            "current_drawdown": None,
        }
        if dd is None:
            detail.update({
                "threshold": None, "type": "回撤", "rule": "个股日线数据不足",
                "explanation": "个股日线拉取失败或不足61根，回撤无法计算。\n请检查数据管道（缓存/网络/腾讯WAF拦截）后重扫。",
                "status": "数据不足",
            })
            return detail
        # 面板按百分数直接显示（toFixed(2)+"%"），这里换算成百分数（如 -5.0 表示 -5.0%）
        detail["current_drawdown"] = round(dd * 100, 2)

        if regime == "trend_up":
            detail["threshold"] = -0.03
            detail["type"] = "浅回撤"
            detail["rule"] = "多头趋势下，需要浅回撤≥-3%"
            detail["explanation"] = (
                "在多头趋势中，股价应该快速回撤到位后继续上升。\n"
                "浅回撤（-3% 以内）表示多头力度足，适合建仓。\n"
                "若回撤超过 -3%，说明多头动能不足，需要等待。"
            )
            detail["status"] = "已满足" if dd >= -0.03 else f"未满足（差 {((-0.03) - dd) * 100:.2f}pp）"
        elif regime == "trend_dn":
            detail["threshold"] = -0.10
            detail["type"] = "深回撤"
            detail["rule"] = "空头趋势下，需要深回撤<-10%"
            detail["explanation"] = (
                "在空头趋势中，股价应该深度回撤后再继续下跌。\n"
                "深回撤（< -10%）表示有充分的获利回吐机会，适合建仓。\n"
                "若回撤不足 -10%，说明跌幅还不够深，继续等待。"
            )
            detail["status"] = "已满足" if dd < -0.10 else f"未满足（差 {(dd - (-0.10)) * 100:.2f}pp）"
        else:  # range
            detail["threshold"] = -0.03
            detail["type"] = "浅回撤"
            detail["rule"] = "震荡市中，按浅回撤≥-3% 判断"
            detail["explanation"] = (
                "在震荡市中，没有明确的主方向，保守按浅回撤标准。\n"
                "等待指数破位后确立方向，然后再调整标准。"
            )
            detail["status"] = "已满足" if dd >= -0.03 else f"未满足（差 {((-0.03) - dd) * 100:.2f}pp）"

        return detail

    def get_high_confidence_signals(self, date):
        """获取高置信度建仓信号（仅 signal/approaching 且有连续背离或无背离数据）。
        筛选规则：排除所有 weak 信号 + 单次无效背离，只显示核心信号。"""
        result = self._agg_position_builder(date, filter_high_confidence=True)
        # 更新刷新时间和技术标签
        try:
            _pb_fp = TRACES / f"position_builder_{date}.jsonl"
            result["refreshed_at"] = datetime.fromtimestamp(_pb_fp.stat().st_mtime).strftime("%H:%M:%S")
        except Exception:
            result["refreshed_at"] = ""
        # 批量个股技术标签
        try:
            codes = [r.get("code") for r in result.get("rows", []) if r.get("code")]
            if codes:
                tag_res = self.load_stock_tags_batch(codes)
                tags_map = tag_res.get("tags", {})
                for r in result.get("rows", []):
                    code = r.get("code")
                    info = tags_map.get(code, {})
                    if info:
                        r["tags"] = info.get("tags", [])
                        r["trend"] = info.get("trend")
        except Exception:
            pass
        return result

    # ---------- 轻量 PB 刷新（盘中实时） ----------
    def refresh_pb(self, date):
        """仅重读 position_builder jsonl 并返回聚合结果 + 个股技术标签。"""
        result = self._agg_position_builder(date)
        # fix P0-14: refreshed_at 改为 jsonl 最后写入时间（不再用服务器当前时间冒充"实时"）
        try:
            _pb_fp = TRACES / f"position_builder_{date}.jsonl"
            result["refreshed_at"] = datetime.fromtimestamp(_pb_fp.stat().st_mtime).strftime("%H:%M:%S")
        except Exception:
            result["refreshed_at"] = ""
        # 批量个股技术标签
        try:
            codes = [r.get("code") for r in result.get("rows", []) if r.get("code")]
            if codes:
                tag_res = self.load_stock_tags_batch(codes)
                tags_map = tag_res.get("tags", {})
                for r in result.get("rows", []):
                    code = r.get("code")
                    info = tags_map.get(code, {})
                    if info:
                        r["tags"] = info.get("tags", [])
                        r["trend"] = info.get("trend")
        except Exception:
            pass
        return result

    def prewarm_stock_tags(self, date=None):
        """启动预热（2026-10-04）：后台算一次当日建仓池技术标签，避免首次进建仓表标签先空约 20s。
        无返回值；仅触发 load_stock_tags_batch 的后台分支。失败静默（预热不该影响启动）。"""
        try:
            date = date or datetime.now().strftime("%Y-%m-%d")
            rows = self._agg_position_builder(date).get("rows") or []
            codes = [r.get("code") for r in rows if r.get("code")]
            if codes:
                self.load_stock_tags_batch(codes)  # 无缓存 ⇒ 起后台算，立即返回
        except Exception:
            pass

    def prewarm_overview(self):
        """启动预热（2026-10-07）：后台**并发**跑一遍总览页的重端点（指数板/大盘成交额/成交额历史/
        指数背离），把冷启动成本（东财重试、多笔指数日线、12+ GM 调用）挪到后台，避免前端首帧在主
        线程上等十几秒（实测这几个端点冷启动合计 ~15s）。失败静默。"""
        from concurrent.futures import ThreadPoolExecutor

        def _safe(fn):
            try:
                fn()
            except Exception:
                pass
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(_safe, [self.load_indices, self.load_market_turnover,
                                    self.load_turnover_history, self.load_index_divergence]))
        except Exception:
            pass

    def prewarm_holdings_charts(self):
        """启动预热（2026-10-04）：后台**低并发**预热持仓的个股图表缓存。

        为什么：`load_ob_analysis`（持仓日线体检）逐只调 `load_stock_chart`，冷启动实测 **14.3s**
        阻塞在主线程（热路径只需 60ms）。预热把这段冷取数挪到后台，让前端首次调用即命中热缓存。
        并发压到 3：预热是「顺便」，不该和 UI/其它后台任务（如标签批算）抢 CPU/GIL 把整体拖涩。
        失败静默；已在缓存内的标的直接跳过。"""
        try:
            cur = _load_json(HOLDINGS_MANUAL, {})
            codes = []
            for code, info in cur.items():
                if not isinstance(info, dict) or code.startswith("_"):
                    continue
                if not (info.get("qty") or 0):
                    continue  # 已清仓不体检（与 load_ob_analysis 同口径）
                bc = str(code).split("_")[0]
                if bc and bc not in codes:
                    codes.append(bc)
            if not codes:
                return
            if not hasattr(self, "_stock_chart_cache"):
                self._stock_chart_cache = {}   # 先建，避免并发首次各建一份互相覆盖
            from concurrent.futures import ThreadPoolExecutor
            _today = datetime.now().strftime("%Y-%m-%d")

            def _one(c):
                # 建图（当日内存缓存命中则秒回）
                try:
                    self.load_stock_chart(c)
                except Exception:
                    pass
                # 2026-10-07：顺带预热**技术标签**（持仓体检表要显示标签）。`_stock_tags_from_df`
                # 里最贵的是 30min 趋势 `get_trend30`（~0.8s/只、有 per-code 缓存），预热后
                # `load_ob_analysis` 命中缓存 → 不卡主线程。
                try:
                    import core.chart_cache as _cc2
                    _df = _cc2.load_daily_display(c)
                    if _df is not None and not _df.empty:
                        self._stock_tags_from_df(_df, c)
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_one, codes))
        except Exception:
            pass

    def _build_and_save_payloads(self, codes):
        """盘后预下载（小池）：逐只（低并发 3）建图并落盘 `chart_payload/`，供次日**瞬开**。
        已有新鲜 payload 则跳过。返回统计。失败静默。"""
        try:
            import pandas as pd
            import core.chart_cache as _cc
            from concurrent.futures import ThreadPoolExecutor
            codes = [str(c).split("_")[0] for c in (codes or [])]
            stat = {"requested": len(codes), "built": 0, "skipped": 0, "miss": 0}

            def _one(c):
                try:
                    if _cc.load_payload(c):
                        return "skip"
                    df = _cc.load_daily_display(c)
                    if df is None or df.empty:
                        from core.position_builder import fetch_daily_kline
                        df = fetch_daily_kline(c)
                    if df is None or df.empty:
                        return "miss"
                    try:
                        from core.market_data import get_provider
                        df = get_provider().append_forming_bar(df, c)
                    except Exception:
                        pass
                    df = df.copy()
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.sort_values("date").reset_index(drop=True)
                    rows = [{"date": str(_r.date), "open": float(_r.open), "close": float(_r.close),
                             "high": float(_r.high), "low": float(_r.low), "volume": float(_r.volume)}
                            for _r in df.itertuples(index=False)]
                    out = {"code": c, "name": c, "available": False, "error": ""}
                    res = self._build_chart_from_df(df, out, c)
                    if res.get("available") and _cc.save_payload(c, res, rows):
                        return "built"
                except Exception:
                    return "err"
                return "miss"

            with ThreadPoolExecutor(max_workers=3) as ex:
                for r in ex.map(_one, codes):
                    if r == "built":
                        stat["built"] += 1
                    elif r == "skip":
                        stat["skipped"] += 1
                    else:
                        stat["miss"] += 1
            return stat
        except Exception as e:
            return {"error": str(e)[:120]}

    def recompute_pb(self, date):
        """盘后重跑建仓扫描 + 重算加仓观察。返回 {position_builder, add_watch, error?}。
        重跑用 eod 档、不推送飞书（避免重复打扰）；run_position_scan 会更新 watchlist_buy。
        fix 2026-08-27: 不再静默吞异常——失败记录并随返回带 error，前端可见。
        fix 2026-08-27(2): 重跑前热重载 config/timing_gate/position_builder——GUI 进程常驻，
        不重启时 import 缓存的是启动时的旧模块，"盘后重跑"会跑旧逻辑。重载顺序保证依赖新鲜：
        config → position_builder → timing_gate（timing_gate 顶部 from position_builder import）。
        重载失败回退普通 import（用当前内存模块），不阻断重跑。"""
        err = None
        # 1) 重跑建仓扫描（eod 档）
        try:
            import importlib
            try:
                import config as _cfg
                import core.position_builder as _pb_mod
                import core.timing_gate as _tg_mod
                importlib.reload(_cfg)
                importlib.reload(_pb_mod)
                importlib.reload(_tg_mod)
            except Exception as _re:
                print(f"⚠️ 模块热重载失败（回退当前内存模块）: {str(_re)[:120]}")
            from core.position_builder import run_position_scan
            run_position_scan(date_str=date, scan_type="eod", silent=True, no_feishu=True)
        except Exception as e:
            err = str(e)[:200]
            print(f"⚠️ 盘后重跑建仓扫描失败: {err}")
        # 2) 聚合新 trace（含技术标签）+ 重算加仓
        pb = self.refresh_pb(date)
        aw = self.compute_add_watch(date)
        out = {"position_builder": pb, "add_watch": aw}
        if err:
            out["error"] = err
        return _clean(out)

    # ---------- 内部聚合 ----------
    def _load_stage_board(self):
        sb = _load_json(OUT / "stage_board.json", {})
        return sb.get("stages", [])

    def _agg_position_builder(self, date, filter_high_confidence=False):
        fp = TRACES / f"position_builder_{date}.jsonl"
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
        wl_stocks = wl.get("stocks", {})
        holdings = _load_json(HOLDINGS_MANUAL, {})
        empty = {"has_data": True, "counts": {}, "by_code": {}, "rows": [],
                 "cond_labels": COND_LABELS, "note": "", "progress": {}}

        verdicts = Counter()
        by_code = {}
        scanned_codes = set()
        latest_by_code = {}  # fix P0-13/P0-14: 每 code 最新一条扫描记录
        if fp.exists():
            try:
                lines = open(fp, encoding="utf-8").read().splitlines()
            except Exception:
                lines = []
            for line in lines:
                line = line.strip()
                if not line: continue
                try: r = json.loads(line)
                except Exception: continue
                code = r.get("code")
                if not code: continue
                scanned_codes.add(code)
                # fix P0-14: 跟踪每 code 最新记录（按 scan_time）
                _prev = latest_by_code.get(code)
                if _prev is None or (r.get("scan_time") or "") > (_prev.get("scan_time") or ""):
                    latest_by_code[code] = r
                st = r.get("scan_type", "manual")
                bucket = by_code.setdefault(code, {}).setdefault(
                    st, {"latest": None, "best": None, "scans": 0})
                bucket["scans"] += 1
                score = r.get("composite_score") or 0
                if bucket["latest"] is None or (r.get("scan_time") or "") > (
                    bucket["latest"].get("scan_time") or ""):
                    bucket["latest"] = r
                if bucket["best"] is None or score > (bucket["best"].get("composite_score") or 0):
                    bucket["best"] = r

        # fix P0-13: counts 按 code 去重后统计各 code 最新 verdict 的股票数（不再是扫描记录行数）
        # fix 2026-08-25: 已从股池删除的（不在 watchlist 且非持仓）不再展示 → 删除按钮后 refresh_pb 不再"复活"
        def _is_holding(code: str) -> bool:
            return bool((holdings.get(code) or {}).get("qty") or 0) or \
                bool((holdings.get(code.split("_")[0]) or {}).get("qty") or 0)
        # 手动盘建仓表可见性（2026-09-20 owner 拍板调整）：
        # 持仓股 + **显式加进股池的都显示** —— 不再按池隐藏 auto 标的。
        # 人工盘/自动盘互不阻塞，同一只票两边都能加、都看得到；行的 pool 字段仍带
        # 「自动」徽章，可用池筛选器（全部/人工/自动）分开看。
        # （此前 2026-08-30 曾用 auto_pool.is_manual 把 auto 池+未持有的隐藏，
        #   导致「添加成功却看不到」——600584 长电科技实例。）
        def _visible(code: str) -> bool:
            return _is_holding(code) or code in wl_stocks

        for code, r in latest_by_code.items():
            if not _visible(code):
                continue
            verdicts[r.get("verdict", "")] += 1

        # 扫描过的：正常聚合
        rows = []
        for code, rec in by_code.items():
            if not _visible(code):
                continue
            # fix P0-14: 行选择改为最新一条扫描记录（不再取当日最高分快照）
            eod = (rec.get("eod") or {}).get("latest")
            intraday = (rec.get("intraday") or {}).get("latest")
            row = dict(latest_by_code.get(code) or eod or intraday or {})
            row["_eod_best_score"] = (eod or {}).get("composite_score")
            row["_intraday_best_score"] = (intraday or {}).get("composite_score")
            row["_scans"] = sum(v.get("scans", 0) for v in rec.values())
            row.setdefault("scan_time", "")  # fix P0-14: 每行确保带 scan_time 字段
            # fix 2026-08-20: in_holdings 实时对齐 holdings.json（qty>0 才算持仓，trace/watchlist 字段可能陈旧）
            row["in_holdings"] = _is_holding(code)
            # P3-2 池分管：pool 标注（优先 holdings 权威源，回退 watchlist，缺省 manual），供 GUI 池筛选
            row["pool"] = (holdings.get(code) or {}).get("pool") or \
                (wl_stocks.get(code) or {}).get("pool") or "manual"
            rows.append(row)

        # 未扫描的 watchlist 股票：monitoring/signal → "等待扫描"；archived → "已停用"（可见但不参与扫描）
        pending = 0
        archived_cnt = 0
        for code, info in wl_stocks.items():
            if not isinstance(info, dict): continue
            if not _visible(code): continue   # fix 2026-08-30: auto 池且未持有 → 隐藏（属自动盘 tab）
            if code in scanned_codes: continue
            status = info.get("status")
            if status not in ("monitoring", "signal", "archived", None): continue
            # fix 2026-08-20: qty>0 才算持仓（已清仓的 holdings 记录不算）
            in_hold = bool((holdings.get(code) or {}).get("qty") or 0) or \
                bool((holdings.get(code.split("_")[0]) or {}).get("qty") or 0)
            is_archived = status == "archived"
            rows.append({
                "code": code, "name": info.get("name", code),
                "verdict": "archived" if is_archived else "pending",
                "composite_score": 0,
                "conditions": {},
                "suggested_qty": 0, "suggested_price": 0, "capital_required": 0,
                "in_holdings": in_hold,
                "pool": (holdings.get(code) or {}).get("pool") or info.get("pool") or "manual",  # P3-2 池分管：GUI 池筛选（优先 holdings）
                "scan_type": "已停用" if is_archived else (
                    "自动盘(不手动扫)" if info.get("pool") == "auto" else "等待扫描"),
                "scan_time": "",  # fix P0-14: 每行确保带 scan_time 字段
                "_scans": 0,
            })
            if is_archived: archived_cnt += 1
            else: pending += 1

        rows.sort(key=lambda x: -(x.get("composite_score") or 0))
        verdicts["pending"] = pending
        verdicts["archived"] = archived_cnt
        no_data_count = verdicts.get("insufficient_data", 0)
        note_parts = []
        if no_data_count: note_parts.append(f"{no_data_count}只无快照")
        if pending: note_parts.append(f"{pending}只等待首次扫描")
        if archived_cnt: note_parts.append(f"{archived_cnt}只已停用")
        note = " · ".join(note_parts) if note_parts else ""

        total = len([c for c in wl_stocks if _visible(c)])  # fix 2026-08-30: 分母=手动盘可见候选数（auto 池未持有已隐藏）
        progress = {
            "total_candidates": total,
            "scanned": len(scanned_codes),
            # fix P0-13: online_fetched 字段名保留，语义改为"当日已扫描股票数"（前端改标签）
            "online_fetched": len(scanned_codes),
            "no_data": no_data_count,
            "pending": pending,
        }
        # 技术标签：仅当天附加（当天日线缓存新鲜，批量秒回；历史日缓存过期会走网络，跳过避免拖慢）
        if date == datetime.now().strftime("%Y-%m-%d"):
            try:
                codes = [r.get("code") for r in rows if r.get("code")]
                if codes:
                    tag_res = self.load_stock_tags_batch(codes)
                    tags_map = tag_res.get("tags", {})
                    for r in rows:
                        info = tags_map.get(r.get("code"), {})
                        if info:
                            r["tags"] = info.get("tags", [])
                            r["trend"] = info.get("trend")
            except Exception:
                pass

        # 持仓股背离兜底（2026-09-20 owner 要求"持仓股的背离也要显示"）：
        # 自动池由 _gm 侧扫描，那条路径**不产出背离**（_gm/signals/position_builder.py
        # 无背离逻辑，且其 docstring 声明"纯函数，无 IO"，不该塞网络调用）⇒ 自动池持仓
        # 的背离列恒为"—"。这里对**持仓且无背离数据**的行补算一次。
        # 与上面的技术标签同策略：仅当天、逐只 try、失败静默。
        # 成本受控：只补持仓（~17 只）× 每 (票,周期) 当日文件缓存 ⇒ 首次刷新后基本无网络。
        if date == datetime.now().strftime("%Y-%m-%d"):
            _need = [r for r in rows
                     if r.get("code") and r.get("in_holdings") and not r.get("divergence_detail")]
            if _need:
                _memo = getattr(self, "_div_memo", None)
                if _memo is None:
                    _memo = self._div_memo = {}
                try:
                    from analysis.divergence import detect_minute_divergence_detail as _det_div
                    for r in _need:
                        _c = r["code"]
                        if _c not in _memo:
                            try:
                                _memo[_c] = _det_div(_c) or {}
                            except Exception:
                                _memo[_c] = {}
                        _d = _memo[_c]
                        if _d:
                            r["divergence_detail"] = _d
                            r["divergence"] = {k: v["type"] for k, v in _d.items()}
                except Exception:
                    pass

        return {
            "has_data": True,
            "counts": dict(verdicts),
            "by_code": by_code,
            "rows": self._filter_high_confidence_signals(rows) if filter_high_confidence else rows,
            "cond_labels": COND_LABELS,
            "note": note,
            "progress": progress,
        }

    def _filter_high_confidence_signals(self, rows: list) -> list:
        """过滤仅保留有连续背离的信号。
        规则：必须有任何连续背离（m30 或 m60 的 consec=true），verdict 无限制。"""
        filtered = []
        for row in rows:
            div_detail = row.get("divergence_detail") or {}
            m60 = div_detail.get("m60", {})
            m30 = div_detail.get("m30", {})

            # 唯一条件：必须有连续背离
            has_consecutive_divergence = m60.get("consec") or m30.get("consec")

            if has_consecutive_divergence:
                # 计算优先级：60分钟连续底背离最优
                priority = 0
                if m60.get("type") == "底背离" and m60.get("consec"):
                    priority = 100
                elif m60.get("type") == "顶背离" and m60.get("consec"):
                    priority = 90
                elif m30.get("type") == "底背离" and m30.get("consec"):
                    priority = 80
                elif m30.get("type") == "顶背离" and m30.get("consec"):
                    priority = 70
                else:
                    priority = 50

                row["_priority"] = priority
                row["_divergence_summary"] = self._format_divergence_summary(div_detail)
                filtered.append(row)

        # 按优先级和 score 排序
        filtered.sort(key=lambda x: (
            -(x.get("_priority") or 0),
            -(x.get("composite_score") or 0)
        ))
        return filtered

    @staticmethod
    def _format_divergence_summary(div_detail: dict) -> str:
        """格式化背离简述，供前端显示"""
        if not div_detail:
            return ""
        parts = []
        for key in ("m30", "m60"):
            v = div_detail.get(key)
            if v:
                consec_mark = "✓" if v.get("consec") else ""
                parts.append(f"{key}:{v.get('type', '')}{consec_mark}")
        return " | ".join(parts) if parts else ""

    def _load_positions(self, date, kpi):
        current = _load_json(HOLDINGS_MANUAL, {})
        snap_today = {}
        snap_prev = {}
        prev_date = None

        # 2026-08-30: 旧格式 holdings_{date}.json 快照已清理删除，改读 GUI 每日快照
        # holdings_daily_{date}.json（{"holdings": [...]} 列表）→ 转 code 键 dict（下游口径不变）
        def _daily_to_map(snap):
            rows = snap.get("holdings") if isinstance(snap, dict) else None
            if isinstance(rows, list):
                return {r.get("code"): r for r in rows if isinstance(r, dict) and r.get("code")}
            return snap if isinstance(snap, dict) else {}

        fps = sorted(STATE_DIR.glob("holdings_daily_*.json"))
        for fp in fps:
            d = fp.stem.replace("holdings_daily_", "")
            if d == date:
                snap_today = _daily_to_map(_load_json(fp, {}))
            if d < date and (prev_date is None or d > prev_date):
                prev_date = d
        if prev_date:
            snap_prev = _daily_to_map(_load_json(STATE_DIR / f"holdings_daily_{prev_date}.json", {}))

        # 2026-09-14: manual 做T 下线——t_mode.json（正/反T）已随 manual 做T 删除，不再读入/返回。

        # 从独立配置文件读（不再依赖 holdings.json）
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        return {
            "current": current,
            "accounts": accounts,
            "snapshot_today": snap_today,
            "snapshot_prev": snap_prev,
            "prev_date": prev_date,
            "k2": (kpi or {}).get("K2_cost_change", {}),
            "k3": (kpi or {}).get("K3_base_drift", {}),
        }


def start_chart_prefetch_scheduler(api):
    """盘后 K线预下载调度（2026-10-04）：交易日 15:10 后触发一次 `chart_cache.run_prefetch`。

    小池日线+分钟+payload → 全池日线兜底。幂等由 `run_prefetch` 的跨进程锁 + 当日标记保证
    （本循环可反复调用，已跑过即空转）。守护线程，失败静默。仅 __main__ 显式启动。"""
    if _CHART_PREFETCH_STATE.get("started"):
        return
    _CHART_PREFETCH_STATE["started"] = True

    def _loop():
        while True:
            try:
                import core.chart_cache as _cc
                now = datetime.now()
                if now.weekday() < 5 and now.strftime("%H:%M") >= "15:10":
                    _cc.run_prefetch(payload_builder=lambda codes: api._build_and_save_payloads(codes))
            except Exception:
                pass
            _time_mod.sleep(300)

    _th.Thread(target=_loop, daemon=True).start()


def _start_gui_freeze_watchdog(threshold_s=12.0):
    """界面卡死看门狗（2026-10-08 需求①）：js_api 全在 pywebview 主线程串行执行，任何长阻塞都会冻界面。
    高频轮询（`load_console` 2s / `load_live` 10s）每次给 `_GUI_HB` 打点；本守护线程发现心跳停
    >threshold_s 秒即判「卡死」，把**全部线程栈**追加到 `t_io/logs/gui_freeze.log`
    （含主线程当前卡在哪一帧），供后续持续改进。单次卡死最多 30s 记一次，避免刷屏。"""
    import faulthandler
    log_fp = BASE / "t_io" / "logs" / "gui_freeze.log"

    def _loop():
        _last = 0.0
        while True:
            try:
                _time_mod.sleep(2.0)
                _ts = _GUI_HB.get("ts") or 0.0
                _now = _time_mod.time()
                if _ts and (_now - _ts) > threshold_s and (_now - _last) > 30:
                    _last = _now
                    try:
                        log_fp.parent.mkdir(parents=True, exist_ok=True)
                        with open(log_fp, "a", encoding="utf-8") as f:
                            f.write(f"\n===== 疑似界面卡死 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                    f"（主线程心跳停 {_now - _ts:.0f}s）线程栈 =====\n")
                            faulthandler.dump_traceback(file=f, all_threads=True)
                    except Exception:
                        pass
            except Exception:
                pass

    _th.Thread(target=_loop, name="gui-freeze-watchdog", daemon=True).start()


def start_hunter_autoscheduler(api):
    """启动「猎手定时自动运行」守护线程（2026-09-21 owner 需求）。

    开盘后按 HUNTER_AUTORUN_SLOTS（10:30/11:30/13:30/14:30）各跑一次「今日数据」；
    交易日按 weekday<5 近似（仓库既有口径，节假日空跑无害）。循环 20s 一跳，
    保证同一分钟内必中时点。定时运行走 auto=True → 建仓推送按板块当日去重。

    仅在 t_gui.py 的 __main__ 显式调用：测试/其他进程构造 Api() 不会起线程。
    """
    if _HUNTER_AUTORUN_STATE.get("started"):
        return
    _HUNTER_AUTORUN_STATE["started"] = True

    def _loop():
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                st = _HUNTER_AUTORUN_STATE
                if st.get("date") != today:       # 跨日重置已跑时点
                    st["date"] = today
                    st["done"] = set()
                if now.weekday() < 5:
                    hhmm = now.strftime("%H:%M")
                    if hhmm in HUNTER_AUTORUN_SLOTS and hhmm not in st["done"]:
                        st["done"].add(hhmm)
                        print(f"[猎手自动运行] {hhmm} 触发（{today}）")
                        api.run_hunter(today, auto=True)
            except Exception as e:
                print(f"[猎手自动运行] 异常（已忽略）: {str(e)[:150]}")
            _time_mod.sleep(20)

    _th.Thread(target=_loop, daemon=True).start()
    print(f"[猎手自动运行] 已启动：{' / '.join(HUNTER_AUTORUN_SLOTS)}（仅工作日，跳午休）")


if __name__ == "__main__":
    import webview

    # V8 预热(2026-09-08): akshare 的 py_mini_racer(V8) 首次初始化必须在主线程，见 core/v8guard.py
    try:
        from core.v8guard import prewarm_akshare_v8
        prewarm_akshare_v8()
    except Exception:
        pass

    api = Api()
    start_hunter_autoscheduler(api)   # 开盘后每小时自动跑「今日数据」
    _start_gui_freeze_watchdog()      # 卡死看门狗：心跳停 >12s → 落线程栈到 t_io/logs/gui_freeze.log
    start_chart_prefetch_scheduler(api)   # 盘后 15:10 预下载 K线缓存（小池 payload+分钟、全池日线）
    # 启动预热（均为后台 daemon 线程，不阻塞启动；失败静默）：
    #  · 持仓图表：低并发(3)，消除 load_ob_analysis 的 14s 冷启动阻塞
    #  · 技术标签：走其内置后台分支，消除首次进建仓表/破位表的标签冷算等待
    _th.Thread(target=api.prewarm_holdings_charts, daemon=True).start()
    _th.Thread(target=api.prewarm_stock_tags, daemon=True).start()
    _th.Thread(target=api.prewarm_overview, daemon=True).start()   # 总览页重端点（指数板/成交额/背离）
    # 预热「加仓观察」：消除首次 load_day 的 ~5.6s 冷算（看门狗抓到的主线程卡点）
    _th.Thread(target=lambda: api.compute_add_watch(datetime.now().strftime("%Y-%m-%d")),
               daemon=True).start()

    def _prewarm_chart_build():
        """建图冷启动预热（2026-10-06）：首次 `_build_chart_from_df` 含懒加载 import
        （analysis.indicators.wilder_rsi / pandas-numba 首次计算）≈1.9s，预热后降到 ~0.5s。
        后台线程，失败静默。"""
        try:
            import pandas as pd
            _d = pd.DataFrame({
                "date": pd.to_datetime(["2020-01-01"] * 400),
                "open": [1.0] * 400, "high": [1.1] * 400, "low": [0.9] * 400,
                "close": [1.0] * 400, "volume": [1.0] * 400,
            })
            _calc_ma_and_indicators(_d)
        except Exception:
            pass

    _th.Thread(target=_prewarm_chart_build, daemon=True).start()
    here = Path(__file__).parent
    entry = here / "web" / "index.html"

    # 前端开发调试: 设置 WEBVIEW_DEBUG=1 打开 devtools
    debug = sys.argv[1] == "--debug" if len(sys.argv) > 1 else False

    window = webview.create_window(
        "trader pannel",
        str(entry),
        js_api=api,
        width=1440,
        height=920,
        min_size=(1100, 700),
    )
    webview.start(gui="edgechromium", debug=debug)
