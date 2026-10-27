# -*- coding: utf-8 -*-
"""两市成交额 · 桌面浮窗（2026-10-27 owner：独立于 GUI、可自由拖动，main.py 启动时拉起）。

为什么单独一个进程 / 用 tkinter：
  · 它要能拖到屏幕任意位置、**置顶常驻**，不受 GUI 窗口边界约束 —— DOM 浮层做不到；
  · 与 t_gui 的窗口完全无关：GUI 关掉、最小化、切到别的页面，它都在；
  · 只是几行数字，不值得再起一套 WebView2（内存/CPU），tkinter 是标准库、零依赖。

更新频率 = **10 秒**（owner 2026-10-27）。这带来一个必须说清楚的取舍：

  · GUI 卡片那条链（`load_market_turnover`）是**5 分钟粒度** —— 两腿的分钟线取
    `freq="300s"`/`scale=5`，实测同一分钟内多次计算 amount 完全不变（as_of 停在 :00/:05/…）。
    所以"把轮询改成 10 秒"对那张卡片毫无意义，数字要 5 分钟才动一次。
  · 因此浮窗的大数字改走**腾讯指数实时快照**（`qt.gtimg.cn` 三腿：沪/深/北，字段 37 = 万元），
    10 秒一跳是真的在跳。主数字因此按**亿**显示（`11,742亿`）：若写「1.17万亿」，
    两位小数 = 100 亿一格，10 秒才成交十几亿，看着仍像没动。
  · 对比基准（昨日同期累计）仍然来自 `t_gui.Api.load_market_turnover()`（每 60s 取一次，
    它本身就是 5 分钟口径），所以浮窗底部会**同时标出两个时刻**：
    `实时 14:21:05 · 基准 14:20` —— 不把"混了两个口径"藏起来。
  · 任一条腿取不到 ⇒ **立刻退回卡片口径**并在底部写明原因（绝不静默少算北交所那 0.8%）。

启动方式：
  · main.py 启动时自动拉起（`--no-widget` 可禁用；命名互斥量防重复，已经在跑就不会再开）；
  · 也可以单独运行：`python scripts/turnover_widget.py`

交互：左键拖到任意位置（位置会记住）；右键 = 置顶开关 / 关闭；右上角 × 关闭。
"""

import ctypes
import json
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

STATE_FP = BASE / "t_io" / "state" / "turnover_widget.json"
MUTEX_NAME = "Global\\SuperTraderTurnoverWidget"

# 与 GUI 同一套配色（暗色），免得挂件看着像另一个产品
BG = "#0d1117"
BORDER = "#30363d"
TEXT = "#c9d1d9"
DIM = "#8b949e"
UP = "#f85149"        # A 股：涨红
DOWN = "#3fb950"      # 跌绿
WARN = "#d29922"

W = 320               # 固定宽度（物理像素）：数字变化时窗口不会左右抽动
LIVE_EVERY_S = 10     # 实时快照节拍（owner 2026-10-27：10 秒更新一次）
BASE_EVERY_S = 60     # 对比基准（昨日同期/预计全日）是 5 分钟口径，1 分钟取一次足够
TICK_MS = 1000

# 三腿 = 沪(上证指数) + 深(深证综指) + 北(北证50) —— 与 t_gui 的 _market_turnover 同口径。
# ⚠️ 深度用**深证综指 sz399106**（不是深证成指 sz399001）：后者的分钟 amount 只有成分股。
TENCENT_URL = "https://qt.gtimg.cn/q=sh000001,sz399106,bj899050"
LEG_CODES = ("sh000001", "sz399106", "bj899050")


def fmt_amount(v):
    """大额读数（预计全日用）：≥1 万亿给"万亿"，否则给"亿"。"""
    try:
        v = float(v)
    except Exception:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    return f"{round(v / 1e8):,}亿"


def fmt_liang(v):
    """**实时主数字**一律用"亿"+千分位。

    为什么不跟卡片一样写"1.17万亿"：万亿保留两位小数 = 100 亿一格，盘中 10 秒才成交
    几亿到十几亿，**数字会看着一动不动**。用亿显示（11,742亿）才能看出 10 秒一跳。
    """
    try:
        v = float(v)
    except Exception:
        return "—"
    return f"{round(v / 1e8):,}亿"


def _enable_dpi():
    """高 DPI 屏幕上不让字糊（本机 GUI 是 150% 缩放）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _acquire_single_instance():
    """命名互斥量：main.py 每次启动都会拉一次，已经在跑就别开第二个。

    返回句柄（必须保持引用，否则会被回收）；已有实例时返回 None。
    守卫本身失败时**照常显示**——不能因为防重复而让浮窗不出现。
    """
    try:
        _k32 = ctypes.windll.kernel32
        handle = _k32.CreateMutexW(None, False, MUTEX_NAME)
        if _k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        return True


def fetch_live_amount():
    """三腿的**实时累计**成交额（元）。任一条腿缺失 ⇒ (None, {...})，绝不静默少算。

    腾讯指数快照：`f[37]` = 成交额（万元，指数口径 = 该市场全部成交额）。
    实测 2026-10-27 14:2x：沪 5,256亿 + 深 6,313亿 + 北 95亿 = 1.166万亿，
    与卡片（5 分钟口径，14:10 = 1.159万亿）量级一致，差的就是最近几分钟。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕开系统代理
    req = urllib.request.Request(TENCENT_URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = opener.open(req, timeout=3).read().decode("gbk", "ignore")
    got = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        code = line.split("=", 1)[0][2:]
        body = line.split('="', 1)[1].rstrip('"')
        f = body.split("~")
        if len(f) < 38:
            continue
        try:
            got[code] = float(f[37]) * 1e4        # 万元 → 元
        except Exception:
            continue
    if all(got.get(c, 0) > 0 for c in LEG_CODES):
        return sum(got[c] for c in LEG_CODES), got
    return None, got


class Feed(threading.Thread):
    """数据线程：10 秒取一次**实时**三腿；60 秒取一次**基准**（t_gui 的 5 分钟口径）。"""

    def __init__(self):
        super().__init__(daemon=True, name="turnover-feed")
        self._lock = threading.Lock()
        self.payload = {}          # t_gui 的载荷（基准/口径/快照标记）
        self.live = None           # 实时三腿合计（元）；None = 实时源不可用
        self.live_at = 0.0
        self.error = ""
        self.at = 0.0

    def snapshot(self):
        with self._lock:
            return (dict(self.payload or {}), self.live, self.live_at,
                    self.error, self.at)

    def run(self):
        api = None
        base_at = 0.0
        while True:
            t0 = time.time()
            # ① 实时三腿（10s）
            live, live_err = None, ""
            try:
                live, _legs = fetch_live_amount()
                if live is None:
                    live_err = "三腿快照不全"
            except Exception as e:
                live_err = f"{type(e).__name__}: {str(e)[:40]}"
            # ② 基准（60s）：昨日同期累计 / 预计全日比例 —— 本来就是 5 分钟口径
            if time.time() - base_at >= BASE_EVERY_S or not self.payload:
                try:
                    if api is None:
                        import t_gui        # 顶层 import 很轻（实测 0.13s / 24MB）
                        api = t_gui.Api()
                    self.payload = api.load_market_turnover() or {}
                    base_at = time.time()
                except Exception as e:
                    self.payload = {"available": False,
                                    "error": f"{type(e).__name__}: {str(e)[:60]}"}
            with self._lock:
                self.live = live
                self.live_at = time.time() if live else 0.0
                self.error = live_err
                self.at = time.time()
            # 收盘后（basis=全日）金额不再变 ⇒ 降到 60s 一次，别 24 小时每 10 秒打腾讯
            period = 60.0 if (self.payload or {}).get("basis") == "全日" else LIVE_EVERY_S
            time.sleep(max(2.0, period - (time.time() - t0)))


class Widget:
    def __init__(self, root: tk.Tk, feed: Feed):
        self.root = root
        self.feed = feed
        self._drag = None
        self._sig = None
        self._dot = None

        root.overrideredirect(True)          # 无系统标题栏：整个窗就是内容
        root.attributes("-topmost", True)
        root.configure(bg=BORDER)            # 外框 1px

        box = tk.Frame(root, bg=BG, padx=12, pady=8)
        box.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(box, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="📊 两市成交额", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", -13)).pack(side="left")
        x_btn = tk.Label(head, text="×", bg=BG, fg=DIM,
                         font=("Microsoft YaHei UI", -15, "bold"), cursor="hand2")
        x_btn.pack(side="right")
        x_btn.bind("<Button-1>", lambda e: (self.close(), "break")[1])

        self.lb_amount = tk.Label(box, text="—", bg=BG, fg=TEXT, anchor="w",
                                  font=("Microsoft YaHei UI", -30, "bold"))
        self.lb_amount.pack(fill="x")
        self.lb_pct = tk.Label(box, text="—", bg=BG, fg=DIM, anchor="w",
                               font=("Microsoft YaHei UI", -15))
        self.lb_pct.pack(fill="x")
        self.lb_basis = tk.Label(box, text="", bg=BG, fg=DIM, anchor="w",
                                 font=("Microsoft YaHei UI", -12))
        self.lb_basis.pack(fill="x")
        self.lb_proj = tk.Label(box, text="", bg=BG, fg=DIM, anchor="w",
                                font=("Microsoft YaHei UI", -12))
        self.lb_proj.pack(fill="x")
        self.lb_foot = tk.Label(box, text="取数中…", bg=BG, fg=DIM, anchor="w",
                                font=("Microsoft YaHei UI", -12))
        self.lb_foot.pack(fill="x")

        # 右键菜单：置顶开关 / 关闭
        self._top_var = tk.BooleanVar(value=True)
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="关闭", command=self.close)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="窗口置顶", variable=self._top_var,
                                  command=self._toggle_top)
        root.bind("<Button-3>", self._popup)

        # 左键拖动（整窗都能拖：Tk 的 bindtags 会把子控件的点击冒泡到 toplevel）
        root.bind("<Button-1>", self._drag_start)
        root.bind("<B1-Motion>", self._drag_move)
        root.bind("<ButtonRelease-1>", self._drag_end)

        self.pos = self._load_pos()
        self._place()
        self.root.after(200, self._tick)

    # ---------- 位置 ----------
    def _load_pos(self):
        try:
            d = json.loads(STATE_FP.read_text(encoding="utf-8"))
            return int(d["x"]), int(d["y"])
        except Exception:
            return None

    def _save_pos(self):
        try:
            STATE_FP.parent.mkdir(parents=True, exist_ok=True)
            STATE_FP.write_text(json.dumps({"x": self.root.winfo_x(),
                                            "y": self.root.winfo_y()}),
                                encoding="utf-8")
        except Exception:
            pass

    def _place(self):
        self.root.update_idletasks()
        h = max(96, self.root.winfo_reqheight())
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.pos:
            x, y = self.pos
        else:
            x, y = sw - W - 24, 96            # 默认：右上角（不挡交易软件主区）
        # 夹回可见范围：换过显示器/分辨率后别把它丢在屏幕外
        x = max(-W + 60, min(sw - 60, x))
        y = max(0, min(sh - 40, y))
        self.root.geometry(f"{W}x{h}+{x}+{y}")

    # ---------- 拖动 / 菜单 ----------
    def _drag_start(self, ev):
        self._drag = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())

    def _drag_move(self, ev):
        if not self._drag:
            return
        self.root.geometry(f"+{ev.x_root - self._drag[0]}+{ev.y_root - self._drag[1]}")

    def _drag_end(self, _ev):
        self._drag = None
        self._save_pos()

    def _popup(self, ev):
        try:
            self.menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_top(self):
        self.root.attributes("-topmost", bool(self._top_var.get()))

    def close(self):
        self._save_pos()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 渲染 ----------
    def _tick(self):
        t, live, live_at, err, at = self.feed.snapshot()
        try:
            self._render(t, live, live_at, err, at)
        except Exception:
            pass
        self.root.after(TICK_MS, self._tick)

    def _render(self, t, live, live_at, err, at):
        av = bool(t.get("available"))
        base = t.get("amount_prev")
        basis = "vs昨日同期" if t.get("basis") == "同期" else "vs昨日全日"
        # 实时优先：分子是 10 秒级快照，分母是卡片那条链给出的"昨日同期"基准
        use_live = bool(live and av and isinstance(base, (int, float)) and base > 0)

        # 盘中叫"实时"，收盘后（basis=全日）金额不再变，写"实时"会误导
        when = "实时" if t.get("basis") == "同期" else "收盘"
        if use_live:
            amount = float(live)
            pct = (amount - base) / base * 100.0
            delta = amount - base
            frac = t.get("proj_frac")
            projected = (amount / frac) if isinstance(frac, (int, float)) and frac > 0 else None
            foot = (f"{when} {time.strftime('%H:%M:%S', time.localtime(live_at))}"
                    f" · 基准 {t.get('as_of') or '—'}")
        elif live:
            # 实时拿到了但基准（昨日同期）缺 ⇒ 照样显示实时总额，只是不给对比，底部写明
            amount, pct, delta, projected = float(live), None, None, None
            basis = ""
            foot = f"{when} {time.strftime('%H:%M:%S', time.localtime(live_at))}"
        elif av:
            # 实时源不可用 ⇒ 退回卡片口径（5 分钟），并在底部写明，绝不假装是实时
            amount, pct = t.get("amount"), t.get("pct")
            delta, projected = t.get("delta"), t.get("projected")
            foot = f"数据 {t.get('as_of') or '—'}"
        else:
            amount = pct = delta = projected = None
            foot = t.get("error") or ("取数中…" if t.get("warming") else "不可用")

        notes = []
        if use_live:
            pass          # 实时数字本身最新；基准时刻已经写在 footer 里，不再叠"快照"噪音
        elif live:
            notes.append(f"基准不可用：{t.get('error') or '—'}")
        elif av:
            notes.append(f"实时源不可用：{err}" if err else "实时源不可用")
            if t.get("stale"):
                notes.append("快照（后台刷新中）")
            if t.get("bse_included") is False:
                notes.append("缺北交所")
        elif err:
            notes.append(f"实时源：{err}")
        if notes:
            foot = f"{foot}　· " + "　· ".join(notes)

        if amount is None:
            amt, pct_txt, color = "—", "", DIM
            basis, delta_txt, proj = "", "", ""
        else:
            amt = fmt_liang(amount)
            pct_txt = "—" if pct is None else f"{pct:+.2f}%"
            color = UP if (pct or 0) > 0.05 else (DOWN if (pct or 0) < -0.05 else DIM)
            delta_txt = ""
            if delta is not None:
                delta_txt = f"　增量 {'+' if delta >= 0 else '−'}{abs(round(delta / 1e8)):,}亿"
            proj = f"预计全日 {fmt_amount(projected)}" if projected is not None else ""

        # 只在内容真的变了才刷控件（每秒一次全量刷会闪）
        sig = (amt, pct_txt, color, basis, delta_txt, proj, foot, bool(notes))
        if sig == self._sig:
            return
        self._sig = sig
        self.lb_amount.config(text=amt)
        self.lb_pct.config(text=pct_txt, fg=color)
        self.lb_basis.config(text=basis + delta_txt)
        self.lb_proj.config(text=proj)
        self.lb_foot.config(text=foot, fg=(WARN if notes else DIM))


def main():
    _enable_dpi()
    guard = _acquire_single_instance()
    if guard is None:
        return                                  # 已有实例在跑
    feed = Feed()
    feed.start()
    root = tk.Tk()
    Widget(root, feed)
    try:
        root.mainloop()
    finally:
        try:
            del guard
        except Exception:
            pass


if __name__ == "__main__":
    main()
