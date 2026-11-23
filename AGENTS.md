# AGENTS.md

## GUI 性能三条铁律（2026-11-22）

背景：`doc/solutions/2026-11-22_UI实时性深度诊断与架构优化.md` 第五节。11 月的 UI 卡顿病全是"新代码绕过老纪律"，故把纪律从注释变成闸门。守卫测试：`tests/phase3/test_architecture_guards.py`（静态源码断言，离线秒级）。

1. **TTL ≠ 轮询周期**：新端点必须声明服务周期（TTL 或 SWR 语义），并注册进守卫测试的 `_TTL_REGISTRY`（端点 / gui.py TTL 定位 / app.js 轮询定位 / 豁免理由）。TTL 落在危险带 `(0.8×周期, 周期]` 即相位病——每拍穿透缓存打网络（同型已复发 3 次：P1-9 → mw_head_stats → sentiment_live）。
2. **js_api 调用线程禁止同步网络与秒级重算**：统一 SWR 模板——旧值立返 + 后台单飞刷新。裸 `urlopen` 必须为 0（出网一律 `core.market_data.http_pool.pooled_get`，keep-alive 复用）；`requests.get/post` 新增会被基线拦下。
3. **行情消费必走 QuoteBus**：新增 `TencentProvider()` 直实例化会被守卫测试拦下。合法位置仅限 bus 本体与已登记的 fail-open 回退（同函数体必须带 `quote_bus` 引用）。

### 新端点 checklist

1. 声明服务周期，登记进 `tests/phase3/test_architecture_guards.py` 的 `_TTL_REGISTRY`（含前端轮询周期定位）。
2. TTL 与前端轮询满足：`TTL > 周期`（隔拍命中）或 `TTL ≤ 0.8×周期`（拍内去重）或登记 SWR 豁免理由。
3. 调用线程零同步网络、零秒级重算：旧值立返 + 后台单飞；出网走 `pooled_get`。
4. 实时行情走 `core.market_data.quote_bus.get_quotes`，不新增 `TencentProvider()` 直实例化。
5. 合并前跑 `python scripts/run_regression.py --fast`（静态守卫层五件套，秒级），全绿才合；等价替代：`python -m unittest tests.phase3.test_architecture_guards tests.phase3.test_http_pool_shared`。全量回归用 `python scripts/run_regression.py`（失败集 ⊆ 脚本顶部既存基线 ⇒ 退出码 0，基线外新失败 ⇒ 1）。
