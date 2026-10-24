"""2026-10-24 AI应用·微软生态（二级概念）解散：owner 反馈该概念"很鸡肋"，要求按实际业务重新归类。

逐家核查（公开信息 + t_io/cache/company_profile.json）：
  300182 捷成股份 —— 影视版权运营/新媒体发行/AIGC视频+短剧；2023-06 互动易明确否认与微软合作，
                       2025/2026 年投资者关系口径的合作伙伴是华为云、字节跳动。微软生态无依据。
  688327 云从科技-UW —— 人机协同操作系统 + 智慧金融/治理/出行解决方案；算力侧是华为昇腾。
                       与微软无公开合作（"签过协议"仅见股吧/自媒体）。
  688111 金山办公 —— WPS Office / WPS 365，国产办公软件；与微软是**替代竞争**关系（对标 Microsoft 365），
                       不是微软生态成员。
  002230 科大讯飞 —— 智能语音 + 星火大模型（训练/推理侧走华为昇腾）；微软是 AI 竞争对手。

⇒ 四家各自已有正确二级概念：AI影视短剧 / AI视觉 / AI办公 / AI大模型，仅删除 "微软生态" 一项，
  分类（AI应用）与其它概念均不变，猎手池规模不变。

约定：成对写（categoryN/conceptN）+ legacy 由 pairs 重建；幂等。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_ms_eco.json')
NOW = '2026-10-24 16:10:00'

obj = json.loads(WL.read_text(encoding='utf-8'))
BK.parent.mkdir(parents=True, exist_ok=True)
if not BK.exists():
    shutil.copy2(WL, BK)

def get_pairs(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            p = e.get(f'jiuyan_concept{k}', '')
            if isinstance(p, (list, tuple)):
                p = '|'.join(map(str, p))
            out.append((c, str(p or '').strip()))
    if out:
        return out
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(map(str, lp))
    return [(t.strip(), str(lp or '').strip()) for t in lc.split('|') if t.strip()]

def put_pairs(e, pairs):
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        if p:
            e[f'jiuyan_concept{k}'] = p
    e['jiuyan_category'] = '|'.join(c for c, _ in pairs)
    e['jiuyan_concept'] = '|'.join(p for _, p in pairs if p)
    e['updated_at'] = NOW

TARGET = '微软生态'
codes = ['300182', '688327', '688111', '002230']
log = []

for code in codes:
    e = obj.get(code)
    if not e:
        log.append(f'{code} 不在池，跳过')
        continue
    pairs = get_pairs(e)
    new_pairs, changed = [], False
    for cat, con in pairs:
        toks = [t.strip() for t in con.split('|') if t.strip()]
        kept = [t for t in toks if t != TARGET]
        if len(kept) != len(toks):
            changed = True
        new_pairs.append((cat, '|'.join(kept)))
    if not changed:
        log.append(f'{code} {e.get("name")} 无 {TARGET}，跳过（幂等）')
        continue
    # 安全闸：不允许把某个 AI应用 配对的概念删空
    bad = [(c, p) for c, p in new_pairs if c == 'AI应用' and not p]
    if bad:
        raise SystemExit(f'{code} 删除后 AI应用 概念为空，中止：{bad}')
    put_pairs(e, new_pairs)
    log.append(f'{code} {e.get("name")} 删除 {TARGET} → ' +
               ' / '.join(f'{c}:{p or "(空)"}' for c, p in new_pairs))

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print('\n'.join(log))

# ── 重载验证 ──
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def rows_of(e):
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            p = e.get(f'jiuyan_concept{k}', '')
            if isinstance(p, (list, tuple)):
                p = '|'.join(map(str, p))
            pairs.append((c, str(p or '')))
    if pairs:
        return pairs
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(map(str, lp))
    return [(t.strip(), str(lp or '')) for t in lc.split('|') if t.strip()] if lc else []

from collections import Counter
rows = Counter()
ai_pool = 0
left = []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = rows_of(e)
    for c, p in rs:
        rows[c] += 1
    if any(c == 'AI应用' and p.strip() for c, p in rs):
        ai_pool += 1
    if any(TARGET in p for _, p in rs):
        left.append(code)

print(f'\n残留 {TARGET}: {left or "无"}')
print(f'AI应用 面板行数={rows.get("AI应用")}  带概念的 AI应用 标的数={ai_pool}')
for code in codes:
    print(code, obj2.get(code, {}).get('name'), rows_of(obj2[code]))
