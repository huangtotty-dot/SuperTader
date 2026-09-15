# -*- coding: utf-8 -*-
"""test_b7_shadow_report.py — B7 影子日报/验收脚本测试套件（施工员_B7-2 · 2026-09-15）

合成 jsonl fixture 覆盖：
1.  正常配对（sell + buyback 同 chain_id → settled）
2.  跨日配对（卖在 09-16 文件、接回在 09-17 文件，chain_id 相同）
3.  缺接回（只有 sell → pending）
4.  坏行（非 JSON / 缺 event 键 → 跳过并计数）
5.  多票同日（两个 code 同日各自配对）
6.  统计数字（胜率 / 平均费后净收益 / 卖飞率 / skip reason 分布）
7.  legacy 兼容（无 chain_id 旧草案格式 → code+qty+sell_px 复合键配对）
8.  孤儿接回（buyback 无对应 sell → 计数）
9.  CLI 日报：exit 0 且 b7_shadow_report_{date}.md 落盘
10. CLI 坏目录：友好报错 exit 2
11. 验收模式：一致样本 exit 0（pass）
12. 验收模式：全亏样本 exit 1（alarm）

运行：python t_io/validation/b7_shadow/test_b7_shadow_report.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import b7_shadow_report as rep  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "b7_shadow_report.py"


# ── fixture 构造 ────────────────────────────────────────────────────────────
def _sell(code, date, cid=None, qty=1000, px=10.0, tail=1.5):
    ev = {"event": "b7_virtual_sell", "code": code, "qty": qty,
          "sell_px": px, "sell_date": date, "tail30_pct": tail,
          "ts": f"{date} 14:55:00"}
    if cid:
        ev["chain_id"] = cid
    return ev


def _buyback(code, buy_date, sell_date, cid=None, qty=1000, buy_px=9.9,
             sell_px=10.0, net=0.864, gap=-0.5, win=True):
    ev = {"event": "b7_virtual_buyback", "code": code, "qty": qty,
          "buy_px": buy_px, "buy_date": buy_date, "sell_px": sell_px,
          "sell_date": sell_date, "net_pct": net, "overnight_gap_pct": gap,
          "win": win, "ts": f"{buy_date} 09:30:05"}
    if cid:
        ev["chain_id"] = cid
    return ev


def _write_log(log_dir, date, events, extra_raw_lines=()):
    path = Path(log_dir) / f"b7_shadow_{date}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for raw in extra_raw_lines:
            f.write(raw + "\n")
    return path


def _mk_fixture(log_dir):
    """标准 fixture：正常配对 + 跨日 + 缺接回 + 坏行 + 多票同日 + skip + signal。"""
    _write_log(log_dir, "2026-09-16", [
        {"event": "b7_signal", "chain_id": "600481_2026-09-16",
         "sig": {"code": "600481", "action": "OVERNIGHT_REVERSE_T"},
         "pos_qty": 5000, "virtual_qty": 1000, "ts": "2026-09-16 14:55:00"},
        {"event": "b7_signal", "chain_id": "002639_2026-09-16",
         "sig": {"code": "002639", "action": "OVERNIGHT_REVERSE_T"},
         "pos_qty": 3000, "virtual_qty": 1000, "ts": "2026-09-16 14:55:00"},
        _sell("600481", "2026-09-16", cid="600481_2026-09-16", px=10.0),
        _sell("002639", "2026-09-16", cid="002639_2026-09-16", px=20.0),
        _sell("600000", "2026-09-16", cid="600000_2026-09-16", px=8.0),
        {"event": "b7_skip", "code": "600519", "reason": "circuit_tripped",
         "tail30_pct": 1.8, "ts": "2026-09-16 14:55:00"},
        {"event": "b7_skip", "code": "000988", "reason": "has_awaiting_buyback",
         "tail30_pct": None, "ts": "2026-09-16 14:55:00"},
    ], extra_raw_lines=["这不是JSON", '{"broken": true, no_event_key}'])
    _write_log(log_dir, "2026-09-17", [
        _buyback("600481", "2026-09-17", "2026-09-16",
                 cid="600481_2026-09-16", buy_px=9.9, net=0.864, gap=-0.5, win=True),
        _buyback("002639", "2026-09-17", "2026-09-16",
                 cid="002639_2026-09-16", buy_px=20.2, sell_px=20.0,
                 net=-0.336, gap=0.8, win=False),
        # 600000 故意不接回 → pending
    ])


def _mk_acceptance_events(n=16, n_win=10, win_net=1.02, loss_net=-0.001,
                          n_fly=5, date_sell="2026-09-16", date_buy="2026-09-17"):
    """构造与离线锚点一致的验收样本（默认应 pass）。"""
    sells, buys = [], []
    for i in range(n):
        cid = f"60{i:04d}_{date_sell}"
        win = i < n_win
        net = win_net if win else loss_net
        gap = 0.6 if i < n_fly else -0.7
        sells.append(_sell(f"60{i:04d}", date_sell, cid=cid))
        buys.append(_buyback(f"60{i:04d}", date_buy, date_sell, cid=cid,
                             net=net, gap=gap, win=win))
    return sells, buys


def _run_cli(*cli_args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *cli_args],
        capture_output=True, text=True, encoding="utf-8", env=env)


# ── 测试主体 ────────────────────────────────────────────────────────────────
def run():
    n = 0

    def check(name, cond):
        nonlocal n
        assert cond, f"FAILED: {name}"
        n += 1
        print(f"  ok {name}")

    with tempfile.TemporaryDirectory() as td:
        _mk_fixture(td)
        events, lst = rep.load_events(td)
        paired = rep.pair_chains(events)
        stats = rep.compute_stats(paired)

        # 4) 坏行：2 行坏行跳过并计数，正常事件不受影响
        check("坏行计数=2", lst["bad_lines"] == 2)
        check("坏行文件登记", lst["bad_files"].get("b7_shadow_2026-09-16.jsonl") == 2)
        check("坏行不影响加载", lst["files"] == 2 and len(events) == 9)

        # 1)+2) 正常配对 + 跨日配对（卖 09-16 / 接 09-17，chain_id 一致）
        check("已结算=2（600481/002639）", stats["n_settled"] == 2)
        r = next(x for x in paired["settled"] if x["chain_id"] == "600481_2026-09-16")
        check("跨日配对 sell_date", r["sell_date"] == "2026-09-16")
        check("跨日配对 buy_date", r["buy_date"] == "2026-09-17")
        check("配对字段合并 sell_px", r["sell_px"] == 10.0 and r["buy_px"] == 9.9)

        # 3) 缺接回 → pending
        check("待结算=1（600000）", stats["n_pending"] == 1)
        check("pending 是 600000", paired["pending"][0]["code"] == "600000")

        # 5) 多票同日：两只票同日卖出各自正确配对（不串 chain）
        r2 = next(x for x in paired["settled"] if x["chain_id"] == "002639_2026-09-16")
        check("多票同日不串 chain", r2["code"] == "002639" and r2["buy_px"] == 20.2)

        # 6) 统计数字
        check("信号数=2", stats["n_signals"] == 2)
        check("胜率=1/2", abs(stats["win_rate"] - 0.5) < 1e-9)
        check("平均费后净收益=(0.864-0.336)/2",
              abs(stats["mean_net_pct"] - (0.864 - 0.336) / 2) < 1e-9)
        check("卖飞率=1/2（gap>0 占比）", abs(stats["fly_rate"] - 0.5) < 1e-9)
        check("skip 分布 circuit_tripped=1",
              stats["skip_by_reason"].get("circuit_tripped") == 1)
        check("skip 分布 has_awaiting_buyback=1",
              stats["skip_by_reason"].get("has_awaiting_buyback") == 1)
        check("按日分布 09-16 卖出3", stats["per_day"]["2026-09-16"]["sells"] == 3)
        check("按日分布 09-17 接回2", stats["per_day"]["2026-09-17"]["buybacks"] == 2)

        # 日报渲染包含关键小节
        md = rep.render_report("2026-09-16 ~ 2026-09-17", stats, paired, lst)
        check("日报含逐笔明细", "逐笔明细" in md and "600481_2026-09-16" in md)
        check("日报含待结算", "待结算" in md and "600000" in md)
        check("日报含坏行警告", "坏行" in md)

        # 7) legacy 无 chain_id 兼容：code+qty+sell_px 复合键配对
        legacy_events = [
            _sell("600111", "2026-09-16", cid=None, qty=500, px=12.0),
            _buyback("600111", "2026-09-17", "2026-09-16", cid=None,
                     qty=500, sell_px=12.0, net=0.5, gap=-0.3, win=True),
        ]
        for i, e in enumerate(legacy_events):
            e["_file_date"] = "2026-09-16" if i == 0 else "2026-09-17"
        lp = rep.pair_chains(legacy_events)
        check("legacy 复合键配对 settled=1", len(lp["settled"]) == 1)
        check("legacy 无 pending", len(lp["pending"]) == 0)

        # 8) 孤儿接回计数
        op = rep.pair_chains([dict(_buyback("600222", "2026-09-17", "2026-09-16",
                                            cid="600222_2026-09-16"),
                                   _file_date="2026-09-17")])
        check("孤儿接回=1", len(op["orphan_buybacks"]) == 1)

        # 9) CLI 日报：exit 0 + md 落盘 + stdout 含关键指标
        p = _run_cli("--log-dir", td)
        check("CLI 日报 exit 0", p.returncode == 0)
        check("CLI stdout 含胜率", "胜率" in p.stdout)
        out_md = Path(td) / "b7_shadow_report_2026-09-17.md"
        check("日报 md 落盘", out_md.is_file())
        check("日报 md 内容含卖飞率", "卖飞率" in out_md.read_text(encoding="utf-8"))

        # --date 单日过滤
        p1 = _run_cli("--log-dir", td, "--date", "2026-09-16", "--no-write")
        check("CLI --date exit 0", p1.returncode == 0)
        out_md1 = Path(td) / "b7_shadow_report_2026-09-16.md"
        check("--date 单日文件名", not out_md1.exists())  # --no-write 不落盘

        # 10) 坏目录：友好报错 exit 2
        p2 = _run_cli("--log-dir", os.path.join(td, "no_such_dir"))
        check("坏目录 exit 2", p2.returncode == 2)
        check("坏目录友好报错", "目录不存在" in p2.stderr)

        # 11) 验收 pass：与离线锚点一致样本 → exit 0
        with tempfile.TemporaryDirectory() as ta:
            sells, buys = _mk_acceptance_events()
            _write_log(ta, "2026-09-16", sells)
            _write_log(ta, "2026-09-17", buys)
            pa = _run_cli("--log-dir", ta, "--acceptance",
                          "--pool-size", "39", "--trading-days", "10")
            check("验收一致样本 exit 0", pa.returncode == 0)
            check("验收一致样本输出 PASS", "PASS" in pa.stdout)

        # 12) 验收 alarm：全亏样本（净均转负）→ exit 1
        with tempfile.TemporaryDirectory() as tb:
            sells, buys = _mk_acceptance_events(n=16, n_win=0,
                                                win_net=-1.5, loss_net=-1.5)
            _write_log(tb, "2026-09-16", sells)
            _write_log(tb, "2026-09-17", buys)
            pb = _run_cli("--log-dir", tb, "--acceptance",
                          "--pool-size", "39", "--trading-days", "10")
            check("验收全亏样本 exit 1", pb.returncode == 1)
            check("验收全亏样本输出 ALARM", "ALARM" in pb.stdout
                  and "转负" in pb.stdout)

    print(f"\n测试完成: {n} 项全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
