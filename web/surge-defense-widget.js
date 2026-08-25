/**
 * surge-defense-widget.js - 日内冲高防御实时监控组件
 *
 * 功能：
 * - 实时显示持仓和监控代码的冲高风险
 * - 四个等级告警：SAFE | WARNING | AVOID | EXIT
 * - 支持自动刷新和手动查询
 */

class SurgeDefenseWidget {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.apiHandle = null;
        this.lastResult = null;
        this.autoRefreshInterval = null;
        this.init();
    }

    init() {
        if (!this.container) return;

        this.container.innerHTML = `
            <div class="surge-defense-panel">
                <div class="panel-header">
                    <h3>日内冲高防御监控</h3>
                    <div class="panel-controls">
                        <button id="surgeDef_refresh" class="mini-btn" title="立即刷新">刷新</button>
                        <label class="panel-check">
                            <input type="checkbox" id="surgeDef_auto" checked>
                            自动监控
                        </label>
                        <span id="surgeDef_time" class="panel-time">—</span>
                    </div>
                </div>

                <div class="surge-summary">
                    <div class="surge-stat safe">
                        <div class="stat-label">正常</div>
                        <div class="stat-value" id="surgeDef_safe">0</div>
                    </div>
                    <div class="surge-stat warning">
                        <div class="stat-label">警告</div>
                        <div class="stat-value" id="surgeDef_warning">0</div>
                    </div>
                    <div class="surge-stat avoid">
                        <div class="stat-label">回避</div>
                        <div class="stat-value" id="surgeDef_avoid">0</div>
                    </div>
                    <div class="surge-stat critical">
                        <div class="stat-label">止损</div>
                        <div class="stat-value" id="surgeDef_exit">0</div>
                    </div>
                </div>

                <div class="surge-critical" id="surgeDef_critical" style="display:none">
                    <div class="critical-title">⚠️ 立即处理</div>
                    <div id="surgeDef_criticalList"></div>
                </div>

                <div class="surge-alerts">
                    <div class="alert-section">
                        <div class="section-title">🏦 持仓风险</div>
                        <div id="surgeDef_holdings" class="alert-list"></div>
                    </div>
                    <div class="alert-section">
                        <div class="section-title">👀 监控风险</div>
                        <div id="surgeDef_watchlist" class="alert-list"></div>
                    </div>
                </div>
            </div>
        `;

        // 绑定事件
        document.getElementById("surgeDef_refresh").addEventListener("click", () => this.refresh());
        document.getElementById("surgeDef_auto").addEventListener("change", (e) => {
            if (e.target.checked) {
                this.startAutoRefresh();
            } else {
                this.stopAutoRefresh();
            }
        });

        // 自动刷新
        this.startAutoRefresh();
    }

    async refresh() {
        if (!window.pywebview) return;

        try {
            const result = await window.pywebview.api.load_intraday_surge_defense();
            this.lastResult = result;
            this.render(result);
        } catch (e) {
            console.error("冲高防御加载失败:", e);
        }
    }

    render(result) {
        if (!result.available) {
            this.container.querySelector(".surge-alerts").innerHTML =
                `<div class="alert-empty">数据加载中...</div>`;
            return;
        }

        // 更新时间戳
        const timeEl = document.getElementById("surgeDef_time");
        const time = result.timestamp.split(" ")[1] || result.timestamp;
        timeEl.textContent = time;

        // 更新统计
        const summary = result.summary || {};
        document.getElementById("surgeDef_safe").textContent = summary.safe_count || 0;
        document.getElementById("surgeDef_warning").textContent = summary.warning_count || 0;
        document.getElementById("surgeDef_avoid").textContent = summary.avoid_count || 0;
        document.getElementById("surgeDef_exit").textContent = summary.exit_count || 0;

        // 渲染立即处理告警
        const criticalDiv = document.getElementById("surgeDef_critical");
        const criticalList = document.getElementById("surgeDef_criticalList");
        if (result.critical_alerts && result.critical_alerts.length > 0) {
            criticalDiv.style.display = "block";
            criticalList.innerHTML = result.critical_alerts.map(alert => `
                <div class="critical-item ${alert.action.toLowerCase()}">
                    <span class="critical-code">${alert.code}</span>
                    <span class="critical-name">${alert.name}</span>
                    <span class="critical-action">[${alert.action}]</span>
                    <span class="critical-reason">${alert.reason}</span>
                </div>
            `).join("");
        } else {
            criticalDiv.style.display = "none";
        }

        // 渲染持仓告警
        const holdingsDiv = document.getElementById("surgeDef_holdings");
        const holdingsAlerts = result.holdings_alerts || [];
        holdingsDiv.innerHTML = holdingsAlerts.length > 0 ?
            holdingsAlerts.map(alert => this.renderAlert(alert)).join("") :
            '<div class="alert-empty">无告警</div>';

        // 渲染监控告警
        const watchlistDiv = document.getElementById("surgeDef_watchlist");
        const watchlistAlerts = result.watchlist_alerts || [];
        watchlistDiv.innerHTML = watchlistAlerts.length > 0 ?
            watchlistAlerts.slice(0, 5).map(alert => this.renderAlert(alert)).join("") :
            '<div class="alert-empty">无告警</div>';
    }

    renderAlert(alert) {
        const action = alert.action.toLowerCase();
        const actionText = {
            "safe": "✓ 正常",
            "warning": "⚠ 警告",
            "avoid": "✖ 回避",
            "exit": "🔴 止损"
        }[action] || alert.action;

        return `
            <div class="alert-item ${action}">
                <div class="alert-header">
                    <span class="alert-code">${alert.code}</span>
                    <span class="alert-name">${alert.name}</span>
                    <span class="alert-action">${actionText}</span>
                </div>
                <div class="alert-detail">
                    <span class="detail-item">高点: ${alert.high_reached.toFixed(2)} @ ${alert.high_time || "—"}</span>
                    <span class="detail-item">现价: ${alert.current_price.toFixed(2)}</span>
                    <span class="detail-item">回落: ${(alert.pullback_ratio * 100).toFixed(1)}%</span>
                </div>
                <div class="alert-reason">${alert.reason}</div>
            </div>
        `;
    }

    startAutoRefresh() {
        if (this.autoRefreshInterval) return;
        this.refresh();
        this.autoRefreshInterval = setInterval(() => this.refresh(), 30000); // 30秒
    }

    stopAutoRefresh() {
        if (this.autoRefreshInterval) {
            clearInterval(this.autoRefreshInterval);
            this.autoRefreshInterval = null;
        }
    }
}

// 页面加载时初始化
document.addEventListener("DOMContentLoaded", () => {
    window.surgeDefenseWidget = new SurgeDefenseWidget("surge-defense-container");
});
