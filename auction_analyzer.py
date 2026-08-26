# -*- coding: utf-8 -*-
"""
auction_analyzer.py — 集合竞价诊断分析引擎（Phase 1）
根据 doc/solutions/集合竞价决策方案.md 实现

功能：
  1. 持仓级别缺口分级与昨日联动分析
  2. 竞价价格轨迹形态识别（阶梯/V形/倒V/一字）
  3. 大盘指数竞价分析
  4. ETF 与跟踪指数背离检测
  5. 生成结构化诊断报告（JSON）

使用场景：9:24:45 执行一次，生成 auction_diagnosis_{date}.json，
后端飞书推送取消，改由 web UI 读取展示。
"""

import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Windows UTF-8 编码修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
PREOPEN_DIR = BASE / "t_io" / "preopen"
STATE_DIR = BASE / "t_io" / "state"
HOLDINGS_FP = BASE / "holdings.json"

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


# ============== 配置参数（对应 config.py 扩展） ==============

AUCTION_PARAMS = {
    "gap_levels": {
        "大幅高开": 2.0,
        "小幅高开": 0.5,
        "平开": 0.5,
        "小幅低开": -0.5,
        "大幅低开": -2.0,
    },
    "trend_shape_threshold": 0.3,  # 0.3% 价格变化视为有效趋势
    "yesterday_linkage": {
        "大涨_threshold": 0.03,
        "大跌_threshold": -0.03,
    },
    "index_codes": ["sh000001", "sh000688", "sz399001"],
}

# 指数显示名称（腾讯快照可能返回简体名，缺失时兜底用）
INDEX_META = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sh000688": "科创50",
}


# ============== 数据类定义 ==============

@dataclass
class HoldingAuctionDiagnosis:
    """单只持仓的竞价诊断"""
    code: str
    name: str = ""

    # 竞价缺口分析
    gap_pct: float = 0.0  # 高开幅度%
    gap_level: str = "平开"  # "大幅高开"/"小幅高开"/"平开"/"小幅低开"/"大幅低开"

    # 竞价价格轨迹形态
    prices_9_20: Optional[float] = None
    prices_9_22: Optional[float] = None
    prices_9_24_30: Optional[float] = None
    trend_shape: str = "无法判断"  # "阶梯上升"/"阶梯下降"/"V形翘尾"/"倒V砸尾"/"一字横盘"/"震荡不定"

    # 昨日走势联动
    yesterday_change_pct: float = 0.0
    yesterday_label: str = ""  # "大涨"/"小涨"/"横盘"/"小跌"/"大跌"
    linkage_label: str = ""  # "高危：获利盘兑现" / "机会：恐慌释放" 等

    # 大盘对比
    vs_index: str = "同步"  # "强于大盘"/"弱于大盘"/"同步"

    # 做T建议
    suggestion: str = ""  # "开盘先卖，等回落接回（反T）" 等
    confidence: str = "中"  # "高"/"中"/"低"

    # 风险标记
    risk_tag: str = ""  # "⚠️高危"/"💪强势"/"📉弱势" 等


@dataclass
class IndexAuctionDiagnosis:
    """大盘指数的竞价诊断"""
    code: str = ""
    name: str = ""
    gap_pct: float = 0.0
    gap_level: str = "平开"


@dataclass
class AuctionDiagnosisReport:
    """完整的竞价诊断报告"""
    date: str = ""
    generated_at: str = ""

    # 大盘竞价分析
    index_analysis: List[IndexAuctionDiagnosis] = field(default_factory=list)
    market_mood: str = "中性"  # "偏多"/"偏空"/"中性"
    market_summary: str = ""

    # 持仓竞价诊断
    holdings_diagnosis: List[HoldingAuctionDiagnosis] = field(default_factory=list)

    # 策略摘要
    bullish_count: int = 0  # 高开（>0.5%）个数
    bearish_count: int = 0  # 低开（<-0.5%）个数
    neutral_count: int = 0  # 平开个数
    suggested_action: str = ""  # "进攻"/"观察"/"回避"

    # 风险提示
    risk_alerts: List[str] = field(default_factory=list)


# ============== 分析函数 ==============

def _get_holdings() -> Dict[str, dict]:
    """加载持仓列表"""
    try:
        return json.loads(HOLDINGS_FP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_holdings_state(date: str) -> Dict[str, dict]:
    """加载指定日期的持仓状态（含昨日收盘价等）"""
    fp = STATE_DIR / f"holdings_{date}.json"
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _get_auction_data(date: str) -> dict:
    """加载当日竞价采集数据"""
    fp = PREOPEN_DIR / f"auction_{date}.json"
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"snapshots": {}}


def _get_yesterday_change(code: str, holdings_state: dict) -> float:
    """获取股票昨日涨跌幅（从 holdings_{date}.json）
    返回小数形式：0.032 = +3.2%
    """
    row = holdings_state.get(code, {})
    # 尝试多个可能的字段名
    for key in ["change_pct", "pct_change", "yesterday_change"]:
        if key in row:
            val = row[key]
            if isinstance(val, (int, float)):
                return float(val) / 100.0 if abs(val) > 1 else float(val)
    return 0.0


def _classify_gap_level(gap_pct: float) -> str:
    """根据缺口幅度分级"""
    if gap_pct > AUCTION_PARAMS["gap_levels"]["大幅高开"]:
        return "大幅高开"
    elif gap_pct > AUCTION_PARAMS["gap_levels"]["小幅高开"]:
        return "小幅高开"
    elif abs(gap_pct) <= AUCTION_PARAMS["gap_levels"]["平开"]:
        return "平开"
    elif gap_pct >= AUCTION_PARAMS["gap_levels"]["大幅低开"]:
        return "小幅低开"
    else:
        return "大幅低开"


def _classify_yesterday(change_pct: float) -> str:
    """分类昨日走势"""
    threshold_big = AUCTION_PARAMS["yesterday_linkage"]["大涨_threshold"]
    threshold_small = threshold_big / 3  # ~1%

    if change_pct > threshold_big:
        return "大涨"
    elif change_pct > threshold_small:
        return "小涨"
    elif change_pct < -threshold_big:
        return "大跌"
    elif change_pct < -threshold_small:
        return "小跌"
    else:
        return "横盘"


def _detect_trend_shape(price_9_20: Optional[float], price_9_22: Optional[float],
                        price_9_24_30: Optional[float]) -> str:
    """识别竞价价格形态

    规则：
    - 价格1 < 价格2 < 价格3 → "阶梯上升"
    - 价格1 > 价格2 > 价格3 → "阶梯下降"
    - 三个价格都接近（<0.1%）→ "一字横盘"
    - 低-低-高（最后拉升>0.3%）→ "V形翘尾"
    - 高-高-低（最后下杀>0.3%）→ "倒V砸尾"
    - 其他 → "震荡不定"
    """
    if None in [price_9_20, price_9_22, price_9_24_30]:
        return "无法判断"

    # 避免除零
    if price_9_20 == 0:
        return "无法判断"

    threshold = AUCTION_PARAMS["trend_shape_threshold"] / 100.0

    # 计算相对变化
    p1 = price_9_20
    p2 = price_9_22
    p3 = price_9_24_30

    # 相对变化幅度
    change_1_2 = (p2 - p1) / p1 if p1 != 0 else 0
    change_2_3 = (p3 - p2) / p2 if p2 != 0 else 0
    change_1_3 = (p3 - p1) / p1 if p1 != 0 else 0

    # 一字横盘（所有变化 < 0.1%）
    if abs(change_1_2) < 0.001 and abs(change_2_3) < 0.001:
        return "一字横盘"

    # 阶梯上升：p1 < p2 < p3
    if p1 < p2 < p3:
        return "阶梯上升"

    # 阶梯下降：p1 > p2 > p3
    if p1 > p2 > p3:
        return "阶梯下降"

    # V形翘尾：前两个低，最后拉升
    if p1 >= p2 and change_2_3 > threshold:
        return "V形翘尾"

    # 倒V砸尾：前两个高，最后下杀
    if p1 <= p2 and change_2_3 < -threshold:
        return "倒V砸尾"

    return "震荡不定"


def _analyze_linkage(yesterday_label: str, gap_level: str) -> tuple:
    """分析昨日走势与今日竞价缺口的联动关系
    返回：(linkage_label, suggestion, risk_tag, confidence)
    """
    # 联动规则表（来自方案 §4.3）
    rules = {
        ("大涨", "大幅高开"): ("高危：获利盘兑现", "开盘先卖等回落（反T）", "⚠️高危", "高"),
        ("大涨", "小幅高开"): ("强势整理", "观望不追", "", "中"),
        ("大涨", "平开"): ("强势整理", "不追，等企稳", "", "中"),
        ("大涨", "小幅低开"): ("强势整理", "不追，等企稳", "", "中"),
        ("大涨", "大幅低开"): ("回调走强", "等低吸", "", "中"),

        ("小涨", "大幅高开"): ("获利盘兑现", "开盘先卖", "⚠️警惕", "中"),
        ("小涨", "小幅高开"): ("惯性上冲", "开盘后5分钟不创新高则卖", "", "中"),
        ("小涨", "平开"): ("平衡整理", "按盘中信号做T", "", "低"),
        ("小涨", "小幅低开"): ("弱势修复", "观望，等企稳", "", "中"),
        ("小涨", "大幅低开"): ("回调", "低吸信号", "", "中"),

        ("横盘", "大幅高开"): ("突破信号", "持有等高抛", "💪强势", "中"),
        ("横盘", "小幅高开"): ("突破信号", "持有等高抛", "💪强势", "低"),
        ("横盘", "平开"): ("无方向", "按盘中信号执行", "", "低"),
        ("横盘", "小幅低开"): ("破位信号", "等反弹减仓", "📉弱势", "低"),
        ("横盘", "大幅低开"): ("破位信号", "等反弹减仓", "📉弱势", "中"),

        ("小跌", "大幅高开"): ("反弹信号", "持有等高抛", "💪强势", "中"),
        ("小跌", "小幅高开"): ("反弹信号", "持有等高抛", "", "中"),
        ("小跌", "平开"): ("平衡反弹", "按盘中信号做T", "", "低"),
        ("小跌", "小幅低开"): ("延续下跌", "等深跌低吸或止损", "", "中"),
        ("小跌", "大幅低开"): ("延续下跌", "等深跌低吸或止损", "📉弱势", "中"),

        ("大跌", "大幅高开"): ("强势反包", "持有，不急卖", "💪强势", "中"),
        ("大跌", "小幅高开"): ("反包试图", "持有观望", "", "低"),
        ("大跌", "平开"): ("平衡修复", "观望", "", "低"),
        ("大跌", "小幅低开"): ("恐慌释放", "等企稳后低吸（正T）", "💪机会", "高"),
        ("大跌", "大幅低开"): ("机会：恐慌释放", "等开盘企稳后低吸（正T）", "💪机会", "高"),
    }

    key = (yesterday_label, gap_level)
    if key in rules:
        return rules[key]
    else:
        return ("无特殊联动", "按盘中信号执行", "", "低")


# ============== 大盘指数竞价分析（方案 §2.3 / §4.4） ==============

def _fetch_index_auction_live() -> List[IndexAuctionDiagnosis]:
    """实时拉取大盘指数竞价快照（腾讯 qt.gtimg.cn，竞价时段字段[3]=虚拟匹配价）。
    仅用于当日盘中分析；历史日期应从已采集数据读取（见 _get_index_auction_from_data）。"""
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    codes = AUCTION_PARAMS["index_codes"]
    q = ",".join(codes)
    try:
        req = urllib.request.Request(
            f"http://qt.gtimg.cn/q={q}", headers={"User-Agent": "Mozilla/5.0"}
        )
        txt = urllib.request.urlopen(req, timeout=12).read().decode("gbk", errors="ignore")
    except Exception:
        return []

    out = []
    for part in txt.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, payload = part.partition("=")
        code = key.strip().lstrip("v_").lower()
        f = payload.strip().strip('"').split("~")
        if len(f) < 5:
            continue

        def _f(i):
            try:
                return float(f[i])
            except Exception:
                return None

        price, pre_close = _f(3), _f(4)
        if not price or not pre_close:
            continue
        gap_pct = (price - pre_close) / pre_close * 100
        out.append(IndexAuctionDiagnosis(
            code=code,
            name=(f[1] if len(f) > 1 and f[1] else INDEX_META.get(code, code)),
            gap_pct=round(gap_pct, 2),
            gap_level=_classify_gap_level(gap_pct),
        ))
    return out


def _get_index_auction_from_data(auction_data: dict) -> List[IndexAuctionDiagnosis]:
    """从已采集的 auction_{date}.json 读取指数竞价快照（优先最新 slot 的 index_rows 节点）"""
    snapshots = auction_data.get("snapshots") or {}
    for slot in ("09:22", "09:20", "09:25"):
        s = snapshots.get(slot) or {}
        rows = s.get("index_rows") or {}
        if not rows:
            continue
        out = []
        for code, r in rows.items():
            gap_pct = r.get("gap_pct")
            if gap_pct is None:
                price = r.get("auction_price") or r.get("price")
                pre_close = r.get("pre_close")
                gap_pct = (price - pre_close) / pre_close * 100 if price and pre_close else 0.0
            gap_pct = float(gap_pct)
            out.append(IndexAuctionDiagnosis(
                code=code,
                name=r.get("name") or INDEX_META.get(code, code),
                gap_pct=round(gap_pct, 2),
                gap_level=r.get("gap_level") or _classify_gap_level(gap_pct),
            ))
        return out
    return []


def analyze_index_auction(date: str, auction_data: dict) -> List[IndexAuctionDiagnosis]:
    """大盘指数竞价分析。
    优先使用已采集数据（保证历史日期口径正确）；当日且无采集数据时实时拉取。"""
    result = _get_index_auction_from_data(auction_data)
    if result:
        return result
    if date == datetime.now().strftime("%Y-%m-%d"):
        return _fetch_index_auction_live()
    return []


def _compute_market_mood(index_list: List[IndexAuctionDiagnosis]) -> tuple:
    """根据指数竞价缺口判断市场情绪 → (mood, summary)
    平均缺口 >+0.5% 偏多 / <-0.5% 偏空 / 否则中性。"""
    if not index_list:
        return "中性", "指数竞价数据缺失"
    gaps = [i.gap_pct for i in index_list if i.gap_pct is not None]
    if not gaps:
        return "中性", "指数竞价数据缺失"
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap > 0.5:
        mood = "偏多"
    elif mean_gap < -0.5:
        mood = "偏空"
    else:
        mood = "中性"
    direction = "高开" if mean_gap > 0.3 else ("低开" if mean_gap < -0.3 else "平开")
    summary = f"大盘{direction}{mean_gap:+.2f}%，情绪{mood}"
    return mood, summary


def _compute_vs_index(holding_gap_pct: float, index_list: List[IndexAuctionDiagnosis]) -> str:
    """个股缺口 vs 大盘（优先上证）缺口 → 强于大盘/弱于大盘/同步"""
    ref = None
    for i in index_list:
        if i.code in ("sh000001", "000001"):
            ref = i.gap_pct
            break
    if ref is None:
        refs = [i.gap_pct for i in index_list if i.gap_pct is not None]
        if refs:
            ref = sum(refs) / len(refs)
    if ref is None:
        return "同步"
    diff = holding_gap_pct - ref
    if diff > 0.5:
        return "强于大盘"
    if diff < -0.5:
        return "弱于大盘"
    return "同步"


def analyze_holdings_auction(date: str, auction_data: dict,
                            holdings_state: dict,
                            index_list: Optional[List[IndexAuctionDiagnosis]] = None) -> List[HoldingAuctionDiagnosis]:
    """分析所有持仓的竞价诊断"""
    results = []
    holdings = _get_holdings()

    # 获取 9:20 和 9:22 的快照
    snap_9_20 = auction_data.get("snapshots", {}).get("09:20", {}).get("rows", {})
    snap_9_22 = auction_data.get("snapshots", {}).get("09:22", {}).get("rows", {})

    for code, holding in holdings.items():
        code_clean = code.split("_")[0]  # 处理双账户 000988_B → 000988

        row_9_20 = snap_9_20.get(code_clean, {})
        row_9_22 = snap_9_22.get(code_clean, {})

        price_9_20 = row_9_20.get("auction_price")
        price_9_22 = row_9_22.get("auction_price")
        pre_close = row_9_20.get("pre_close") or holding.get("pre_close", 0)

        # 缺口计算（优先使用竞价数据中已计算的 pct_vs_preclose，否则重新计算）
        if "pct_vs_preclose" in row_9_20:
            gap_pct = row_9_20.get("pct_vs_preclose", 0.0) or 0.0
        elif pre_close and price_9_20:
            gap_pct = (price_9_20 - pre_close) / pre_close * 100
        else:
            gap_pct = 0.0

        gap_level = _classify_gap_level(gap_pct)

        # 昨日走势
        yesterday_change = _get_yesterday_change(code_clean, holdings_state)
        yesterday_label = _classify_yesterday(yesterday_change)

        # 形态识别（目前只有 9:20/9:22，9:24:30 由后续更新补充）
        trend_shape = _detect_trend_shape(price_9_20, price_9_22, None)

        # 昨日联动分析
        linkage_label, suggestion, risk_tag, confidence = _analyze_linkage(
            yesterday_label, gap_level
        )

        # 大盘对比（个股缺口 vs 指数缺口）
        vs_index = _compute_vs_index(gap_pct, index_list or [])

        diagnosis = HoldingAuctionDiagnosis(
            code=code_clean,
            name=holding.get("name", code_clean),
            gap_pct=round(gap_pct, 2),
            gap_level=gap_level,
            prices_9_20=price_9_20,
            prices_9_22=price_9_22,
            trend_shape=trend_shape,
            yesterday_change_pct=round(yesterday_change * 100, 2),
            yesterday_label=yesterday_label,
            linkage_label=linkage_label,
            vs_index=vs_index,
            suggestion=suggestion,
            confidence=confidence,
            risk_tag=risk_tag,
        )
        results.append(diagnosis)

    return results


def generate_diagnosis_report(date: str) -> AuctionDiagnosisReport:
    """生成完整的竞价诊断报告"""
    auction_data = _get_auction_data(date)
    holdings_state = _get_holdings_state(date)

    # 大盘指数竞价分析
    index_diag = analyze_index_auction(date, auction_data)
    market_mood, market_summary = _compute_market_mood(index_diag)

    # 分析持仓竞价（带大盘对比）
    holdings_diag = analyze_holdings_auction(date, auction_data, holdings_state, index_diag)

    # 统计分布
    bullish_count = sum(1 for d in holdings_diag if d.gap_pct > 0.5)
    bearish_count = sum(1 for d in holdings_diag if d.gap_pct < -0.5)
    neutral_count = len(holdings_diag) - bullish_count - bearish_count

    # 策略建议（多数高开→进攻，多数低开→回避，否则观察）
    if bullish_count > bearish_count + neutral_count / 2:
        suggested_action = "进攻"
    elif bearish_count > bullish_count + neutral_count / 2:
        suggested_action = "回避"
    else:
        suggested_action = "观察"

    # 风险提示
    risk_alerts = []
    high_risk = [d for d in holdings_diag if d.confidence == "高" and "高危" in d.risk_tag]
    if high_risk:
        risk_alerts.append(f"⚠️ {len(high_risk)} 只持仓发现高风险信号")

    opportunities = [d for d in holdings_diag if d.confidence == "高" and "机会" in d.risk_tag]
    if opportunities:
        risk_alerts.append(f"💪 {len(opportunities)} 只持仓发现低吸机会")

    report = AuctionDiagnosisReport(
        date=date,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        index_analysis=index_diag,
        market_mood=market_mood,
        market_summary=market_summary,
        holdings_diagnosis=holdings_diag,
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        neutral_count=neutral_count,
        suggested_action=suggested_action,
        risk_alerts=risk_alerts,
    )

    return report


def save_diagnosis_report(report: AuctionDiagnosisReport) -> Path:
    """保存诊断报告到 JSON 文件"""
    PREOPEN_DIR.mkdir(parents=True, exist_ok=True)
    fp = PREOPEN_DIR / f"auction_diagnosis_{report.date}.json"

    # 转换为字典（递归处理嵌套数据类）
    data = asdict(report)

    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def analyze_and_save(date: str) -> AuctionDiagnosisReport:
    """生成并保存诊断报告"""
    report = generate_diagnosis_report(date)
    save_diagnosis_report(report)
    return report


# ============== 主入口 ==============

if __name__ == "__main__":
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    report = analyze_and_save(today)

    print(f"✅ 竞价诊断报告已生成：{report.date}")
    for idx in report.index_analysis:
        print(f"   🌐 {idx.name}({idx.code}): {idx.gap_pct:+.2f}% [{idx.gap_level}]")
    print(f"   情绪: {report.market_mood} | {report.market_summary}")
    print(f"   持仓高开: {report.bullish_count} | 平开: {report.neutral_count} | 低开: {report.bearish_count}")
    print(f"   策略建议: {report.suggested_action}")
    for alert in report.risk_alerts:
        print(f"   {alert}")
