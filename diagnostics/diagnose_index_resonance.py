import sys
sys.path.insert(0, '.')

print("=" * 70)
print("🔍 排查 index_resonance 数据采集链路")
print("=" * 70)

# 1. 检查 index_regime_intraday 模块是否能正常导入
print("\n【Step 1】模块导入检查")
try:
    from index_regime_intraday import fetch_index_minutes_live
    print("  ✅ index_regime_intraday 导入成功")
except Exception as e:
    print(f"  ❌ 导入失败: {e}")
    sys.exit(1)

# 2. 尝试调用 akshare 获取科创50分钟数据
print("\n【Step 2】akshare 新浪源 — sh000688（科创50）")
try:
    import akshare as ak
    df = ak.stock_zh_a_minute(symbol="sh000688", period="1")
    if df is not None and not df.empty:
        print(f"  ✅ 返回 {len(df)} 行")
        print(f"  列: {list(df.columns)}")
        print(f"  前3行:\n{df.head(3)}")
        print(f"  后3行:\n{df.tail(3)}")
        # 检查是否有今日数据
        if 'day' in df.columns:
            last_day = df['day'].max()
            print(f"  最新日期: {last_day}")
    else:
        print("  ❌ 返回空数据")
except Exception as e:
    print(f"  ❌ 异常: {type(e).__name__}: {e}")

# 3. 尝试调用腾讯接口
print("\n【Step 3】腾讯当日分时 — sh000688（科创50）")
try:
    import urllib.request
    import json
    url = "https://ifzq.gtimg.cn/appstock/app/minute/query?code=sh000688"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    print(f"  ✅ HTTP 200, keys={list(data.keys())[:5]}")
    node = (data.get("data", {}) or {}).get("sh000688") or {}
    print(f"  data.sh000688 keys={list(node.keys()) if isinstance(node, dict) else type(node)}")
    pack = node.get("data") if isinstance(node, dict) else {}
    if isinstance(pack, dict):
        rows = pack.get("data") or []
        day_str = str(pack.get("date") or "")
        print(f"  日期: {day_str}, 行数: {len(rows)}")
        if rows:
            print(f"  前3行: {rows[:3]}")
    elif isinstance(pack, list):
        print(f"  list形态, 行数: {len(pack)}")
        if pack:
            print(f"  前3行: {pack[:3]}")
    else:
        print(f"  pack类型: {type(pack)}, 值: {pack}")
except Exception as e:
    print(f"  ❌ 异常: {type(e).__name__}: {e}")

# 4. 尝试完整调用 fetch_index_minutes_live
print("\n【Step 4】完整链路 — fetch_index_minutes_live('sh000688')")
try:
    df = fetch_index_minutes_live("sh000688")
    print(f"  ✅ 成功, {len(df)} 行")
    print(f"  source={df.attrs.get('iri_source')}, degraded={df.attrs.get('iri_degraded')}")
    print(f"  列: {list(df.columns)}")
    print(f"  前3行:\n{df.head(3)}")
except Exception as e:
    print(f"  ❌ 异常: {type(e).__name__}: {e}")

# 5. 再试上证指数
print("\n【Step 5】完整链路 — fetch_index_minutes_live('sh000001')")
try:
    df = fetch_index_minutes_live("sh000001")
    print(f"  ✅ 成功, {len(df)} 行")
    print(f"  source={df.attrs.get('iri_source')}, degraded={df.attrs.get('iri_degraded')}")
except Exception as e:
    print(f"  ❌ 异常: {type(e).__name__}: {e}")

# 6. 检查 _5MIN_CACHE 状态
print("\n【Step 6】检查 5MIN 缓存状态")
try:
    import index_resonance as ir
    print(f"  _5MIN_CACHE keys: {list(ir._5MIN_CACHE.keys())}")
    for k, v in ir._5MIN_CACHE.items():
        print(f"    {k}: boundary={v[0]}, df_rows={len(v[1]) if v[1] is not None else 'None'}")
except Exception as e:
    print(f"  读取缓存异常: {e}")

print("\n" + "=" * 70)
print("排查完成")
print("=" * 70)
