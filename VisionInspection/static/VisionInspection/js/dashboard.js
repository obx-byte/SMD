/**
 * VisionInspection — Operations Dashboard
 *
 * Self-contained, dependency-free dashboard controller.
 *  • Polls a single summary endpoint on one timer (configurable, non-aggressive).
 *  • Renders all charts as inline SVG (no chart library → works offline).
 *  • Handles loading, empty and error states for every panel.
 *  • Click-through interactions navigate to existing filtered log pages.
 *
 * Configuration is read from data-* attributes on #viDashRoot (set in the
 * template) so nothing is hardcoded here.
 */
(function () {
    "use strict";

    // ------------------------------------------------------------------ #
    //  Config + state
    // ------------------------------------------------------------------ #

    const root = document.getElementById("viDashRoot");
    if (!root) return;

    const SUMMARY_URL = root.dataset.summaryUrl || "/vision-inspection/api/dashboard/summary/";
    const LOGS_URL = root.dataset.logsUrl || "/vision-inspection/logs/";
    const POLL_INTERVAL = Math.max(
        Number(root.dataset.pollMs || 30000),
        5000
    );

    let pollTimer = null;
    let abortController = null;
    let lastSuccessAt = null;
    let firstLoad = true;

    // ------------------------------------------------------------------ #
    //  DOM references
    // ------------------------------------------------------------------ #

    const el = {
        live: document.getElementById("viDashLive"),
        liveText: document.getElementById("viDashLiveText"),
        lastRefresh: document.getElementById("viDashLastRefresh"),
        refreshBtn: document.getElementById("viDashRefreshBtn"),
        // Flow card
        flowValue: document.getElementById("viDashFlowValue"),
        flowDelta: document.getElementById("viDashFlowDelta"),
        flowNote: document.getElementById("viDashFlowNote"),
        flowTrend: document.getElementById("viDashTrend"),
        flowByPart: document.getElementById("viDashByPart"),
        // Inspection card
        donut: document.getElementById("viDashDonut"),
        donutSvg: document.getElementById("viDashDonutSvg"),
        donutCenterVal: document.getElementById("viDashDonutCenterVal"),
        donutCenterLbl: document.getElementById("viDashDonutCenterLbl"),
        donutLegend: document.getElementById("viDashDonutLegend"),
        // Inventory card
        invBars: document.getElementById("viDashInvBars"),
        invNote: document.getElementById("viDashInvNote"),
        // Alerts card
        alerts: document.getElementById("viDashAlerts"),
        alertsCount: document.getElementById("viDashAlertsCount"),
        // Filters
        filterDays: document.getElementById("viDashFilterDays"),
        filterPart: document.getElementById("viDashFilterPart"),
        filterResult: document.getElementById("viDashFilterResult"),
    };

    // ------------------------------------------------------------------ #
    //  Utilities
    // ------------------------------------------------------------------ #

    const fmtInt = (n) => Number(n || 0).toLocaleString();

    const SVG_NS = "http://www.w3.org/2000/svg";
    function svgEl(tag, attrs) {
        const node = document.createElementNS(SVG_NS, tag);
        if (attrs) for (const k in attrs) node.setAttribute(k, attrs[k]);
        return node;
    }

    // Shared tooltip element
    let tooltipEl = document.createElement("div");
    tooltipEl.className = "vi-dash-tooltip";
    document.body.appendChild(tooltipEl);

    function showTooltip(html, x, y) {
        tooltipEl.innerHTML = html;
        tooltipEl.classList.add("show");
        const pad = 14;
        const rect = tooltipEl.getBoundingClientRect();
        let left = x + pad;
        let top = y + pad;
        if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
        if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
        tooltipEl.style.left = left + "px";
        tooltipEl.style.top = top + "px";
    }
    function hideTooltip() {
        tooltipEl.classList.remove("show");
    }
    function bindTooltip(node, getHtml) {
        node.addEventListener("mousemove", (e) =>
            showTooltip(getHtml(), e.clientX, e.clientY)
        );
        node.addEventListener("mouseleave", hideTooltip);
        node.addEventListener("mouseenter", () => {
            hideTooltip();
            const r = node.getBoundingClientRect();
            showTooltip(getHtml(), r.left + r.width / 2, r.top);
        });
    }

    function navigate(query) {
        const qs = Object.keys(query)
            .filter((k) => query[k] !== "" && query[k] != null)
            .map((k) => encodeURIComponent(k) + "=" + encodeURIComponent(query[k]))
            .join("&");
        if (!qs) { window.location.href = LOGS_URL; return; }
        window.location.href = LOGS_URL + (LOGS_URL.indexOf("?") >= 0 ? "&" : "?") + qs;
    }

    // ------------------------------------------------------------------ #
    //  Live indicator
    // ------------------------------------------------------------------ #

    function setLive(state, text) {
        el.live.className = "vi-dash-live" + (state ? " " + state : "");
        el.liveText.textContent = text;
    }
    function updateLastRefresh() {
        if (!lastSuccessAt) {
            el.lastRefresh.textContent = "";
            return;
        }
        el.lastRefresh.textContent =
            "Last refresh " + lastSuccessAt.toLocaleTimeString();
    }

    // ------------------------------------------------------------------ #
    //  States (loading / empty / error)
    // ------------------------------------------------------------------ #

    function loadingState(container, lines) {
        container.innerHTML =
            '<div class="vi-dash-skeleton line w40"></div>' +
            '<div class="vi-dash-skeleton block"></div>' +
            (lines || "");
    }
    function emptyState(container, title, text, icon) {
        container.innerHTML =
            '<div class="vi-dash-state">' +
            '<div class="vi-dash-state-icon">' + (icon || "◌") + "</div>" +
            '<div class="vi-dash-state-title">' + title + "</div>" +
            '<div class="vi-dash-state-text">' + text + "</div>" +
            "</div>";
    }

    // ------------------------------------------------------------------ #
    //  Chart: area / line trend
    // ------------------------------------------------------------------ #

    function renderTrend(trend) {
        el.flowTrend.innerHTML = "";
        const labels = trend.labels || [];
        const counts = trend.counts || [];
        const total = counts.reduce((a, b) => a + b, 0);

        if (!labels.length || total === 0) {
            emptyState(
                el.flowTrend,
                "No inspections in this period",
                "When the inspection engine records results they will appear here in real time.",
                "▤"
            );
            return;
        }

        const W = 640, H = 190, PL = 34, PR = 12, PT = 14, PB = 26;
        const iw = W - PL - PR, ih = H - PT - PB;
        const maxV = Math.max.apply(null, counts.concat([1]));
        // rounded ceiling for nicer gridlines
        const step = Math.max(1, Math.ceil(maxV / 4));
        const top = Math.ceil(maxV / step) * step;

        const x = (i) => PL + (labels.length <= 1 ? iw / 2 : (i * iw) / (labels.length - 1));
        const y = (v) => PT + ih - (v / top) * ih;

        const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });

        // gridlines + y labels
        for (let g = 0; g <= 4; g++) {
            const v = (top / 4) * g;
            svg.appendChild(
                svgEl("line", {
                    x1: PL, x2: W - PR, y1: y(v), y2: y(v),
                    class: "vi-dash-grid-line",
                })
            );
            const lbl = svgEl("text", {
                x: PL - 8, y: y(v) + 3, "text-anchor": "end", class: "vi-dash-axis-label",
            });
            lbl.textContent = Math.round(v);
            svg.appendChild(lbl);
        }

        // area + line path
        let lineD = "", areaD = "";
        counts.forEach((v, i) => {
            lineD += (i === 0 ? "M" : "L") + x(i) + " " + y(v);
        });
        areaD = lineD + " L" + x(counts.length - 1) + " " + y(0) + " L" + x(0) + " " + y(0) + " Z";

        const defs = svgEl("defs");
        const grad = svgEl("linearGradient", { id: "viDashTrendGrad", x1: 0, y1: 0, x2: 0, y2: 1 });
        const s1 = svgEl("stop", { offset: "0%", "stop-color": "#22d3ee", "stop-opacity": 0.35 });
        const s2 = svgEl("stop", { offset: "100%", "stop-color": "#22d3ee", "stop-opacity": 0 });
        grad.appendChild(s1); grad.appendChild(s2); defs.appendChild(grad);
        svg.appendChild(defs);

        svg.appendChild(svgEl("path", { d: areaD, fill: "url(#viDashTrendGrad)" }));
        svg.appendChild(
            svgEl("path", {
                d: lineD, fill: "none", stroke: "#22d3ee", "stroke-width": 2.5,
                "stroke-linejoin": "round", "stroke-linecap": "round",
            })
        );

        // x labels (every nth to avoid clutter)
        const everyN = Math.ceil(labels.length / 8);
        labels.forEach((lb, i) => {
            if (i % everyN === 0 || i === labels.length - 1) {
                const t = svgEl("text", {
                    x: x(i), y: H - 8, "text-anchor": "middle", class: "vi-dash-axis-label",
                });
                t.textContent = lb;
                svg.appendChild(t);
            }
        });

        // points (with tooltip)
        counts.forEach((v, i) => {
            const c = svgEl("circle", {
                cx: x(i), cy: y(v), r: 3.6, fill: "#0b1220",
                stroke: "#22d3ee", "stroke-width": 2, style: "cursor:pointer",
            });
            c.style.opacity = v === 0 ? "0.35" : "1";
            bindTooltip(c, () => '<div class="tt-title">' + labels[i] + "</div><div class='tt-row'><b>" + v + "</b> inspections</div>");
            svg.appendChild(c);
        });

        svg.style.cursor = "pointer";
        svg.addEventListener("click", () => navigate(currentQuery()));
        el.flowTrend.appendChild(svg);
    }

    function renderByPart(byPart) {
        el.flowByPart.innerHTML = "";
        if (!byPart || !byPart.length) {
            el.flowByPart.innerHTML =
                '<div class="vi-dash-legend-item" style="margin-top:10px;opacity:.7">No per-part breakdown</div>';
            return;
        }
        const maxC = Math.max.apply(null, byPart.map((p) => p.count));
        const wrap = document.createElement("div");
        wrap.className = "vi-dash-bars";
        byPart.forEach((p) => {
            const row = document.createElement("div");
            row.className = "vi-dash-bar-row";
            const id = "viByPart_" + p.part_number.replace(/[^a-z0-9]/gi, "_");
            row.innerHTML =
                '<div class="vi-dash-bar-meta"><span class="vi-dash-bar-name">' +
                escapeHtml(p.part_number) + "</span>" +
                '<span class="vi-dash-bar-qty">' + fmtInt(p.count) + "</span></div>" +
                '<div class="vi-dash-bar-track"><div class="vi-dash-bar-fill is-SAFE" style="width:' +
                (maxC ? (p.count / maxC) * 100 : 0) + '%"></div></div>';
            row.style.cursor = "pointer";
            row.setAttribute("role", "button");
            row.setAttribute("tabindex", "0");
            const go = () => navigate(currentQuery({ part_number: p.part_number }));
            row.addEventListener("click", go);
            row.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
            });
            bindTooltip(row, () => '<div class="tt-title">' + escapeHtml(p.part_number) + "</div><div class='tt-row'><b>" + p.count + "</b> inspections — click to view logs</div>");
            wrap.appendChild(row);
        });
        el.flowByPart.appendChild(wrap);
    }

    // ------------------------------------------------------------------ #
    //  Chart: donut (inspection results)
    // ------------------------------------------------------------------ #

    function renderInspection(inv) {
        const total = inv.total || 0;
        const segments = [
            { name: "Passed", value: inv.passed || 0, color: "#34d399" },
            { name: "Failed", value: inv.failed || 0, color: "#f87171" },
            { name: "Error", value: inv.error || 0, color: "#fbbf24" },
        ];

        // donut svg (target the dedicated svg node; center overlay is a sibling)
        el.donutSvg.innerHTML = "";
        const R = 15.9155, C = 100;
        const svg = el.donutSvg;
        svg.appendChild(
            svgEl("circle", {
                cx: 21, cy: 21, r: R, fill: "none",
                stroke: "#172033", "stroke-width": 5,
            })
        );
        let offset = 25; // start at top (12 o'clock)
        if (total > 0) {
            segments.forEach((seg) => {
                if (seg.value <= 0) return;
                const pct = (seg.value / total) * 100;
                const arc = svgEl("circle", {
                    cx: 21, cy: 21, r: R, fill: "none", stroke: seg.color,
                    "stroke-width": 5, class: "vi-dash-seg",
                    "stroke-dasharray": pct + " " + (C - pct),
                    "stroke-dashoffset": offset, style: "cursor:pointer",
                });
                bindTooltip(arc, () => '<div class="tt-title">' + seg.name + "</div><div class='tt-row'><b>" + seg.value + "</b> (" + Math.round((seg.value / total) * 100) + "%) — click to view logs</div>");
                arc.addEventListener("click", () =>
                    navigate(currentQuery({ result: segResultName(seg.name) }))
                );
                svg.appendChild(arc);
                offset -= pct;
            });
        } else {
            svg.appendChild(svgEl("circle", {
                cx: 21, cy: 21, r: R, fill: "none", stroke: "#233046", "stroke-width": 5,
            }));
        }
        // (svg node already lives in the DOM; nothing to re-append)

        // center
        if (total > 0) {
            el.donutCenterVal.textContent = Math.round(inv.pass_pct || 0) + "%";
            el.donutCenterLbl.textContent = "Pass rate";
        } else {
            el.donutCenterVal.textContent = "—";
            el.donutCenterLbl.textContent = "No data";
        }

        // legend
        el.donutLegend.innerHTML = "";
        segments.forEach((seg) => {
            const pct = total ? Math.round((seg.value / total) * 100) : 0;
            const row = document.createElement("div");
            row.className = "vi-dash-seg-row";
            row.style.cursor = total ? "pointer" : "default";
            row.innerHTML =
                '<span class="swatch" style="background:' + seg.color + '"></span>' +
                '<span class="name">' + seg.name + "</span>" +
                '<span class="spacer"></span>' +
                '<span class="num">' + fmtInt(seg.value) + "</span>" +
                '<span class="pct">' + pct + "%</span>";
            if (total) {
                row.addEventListener("click", () =>
                    navigate(currentQuery({ result: segResultName(seg.name) }))
                );
                bindTooltip(row, () => '<div class="tt-title">' + seg.name + "</div><div class='tt-row'>Click to view " + seg.name.toLowerCase() + " inspection logs</div>");
            }
            el.donutLegend.appendChild(row);
        });

        if (total === 0) {
            el.donutLegend.insertAdjacentHTML(
                "beforeend",
                '<div class="vi-dash-state" style="padding:6px 0"><div class="vi-dash-state-text">No inspections recorded in this period yet.</div></div>'
            );
        }
    }

    function segResultName(name) {
        if (name === "Passed") return "OK";
        if (name === "Failed") return "NOT_OK";
        return "ERROR";
    }

    // ------------------------------------------------------------------ #
    //  Inventory bars
    // ------------------------------------------------------------------ #

    function renderInventory(data) {
        const parts = (data && data.parts) || [];
        const threshold = (data && data.threshold) || { reorder: 60, critical: 20 };
        el.invBars.innerHTML = "";

        if (!parts.length) {
            emptyState(
                el.invBars,
                "No parts inspected yet",
                "Parts you inspect will appear here with configuration coverage and stock status.",
                "◫"
            );
            el.invNote.textContent = "";
            return;
        }

        const maxQty = Math.max.apply(null, parts.map((p) => p.quantity));
        const wrap = document.createElement("div");
        wrap.className = "vi-dash-bars";
        parts.forEach((p) => {
            const widthPct = maxQty ? (p.quantity / maxQty) * 100 : 0;
            const thresholdPct = (threshold.reorder / 100) * 100;
            const row = document.createElement("div");
            row.className = "vi-dash-bar-row";
            row.setAttribute("role", "button");
            row.setAttribute("tabindex", "0");
            row.innerHTML =
                '<div class="vi-dash-bar-meta">' +
                '<span class="vi-dash-bar-name">' + escapeHtml(p.part_number) + "</span>" +
                '<span class="vi-dash-bar-qty">' + fmtInt(p.quantity) + "</span>" +
                '<span class="vi-dash-bar-status vi-dash-status-' + p.health + '">' + p.health + "</span>" +
                "</div>" +
                '<div class="vi-dash-bar-track">' +
                '<div class="vi-dash-bar-fill is-' + p.health + '" style="width:' + widthPct + '%"></div>' +
                '<div class="vi-dash-bar-threshold" style="left:' + thresholdPct + '%" title="Reorder threshold"></div>' +
                "</div>";
            const go = () => navigate(currentQuery({ part_number: p.part_number, result: "" }));
            row.addEventListener("click", go);
            row.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
            });
            bindTooltip(row, () =>
                '<div class="tt-title">' + escapeHtml(p.part_number) + "</div>" +
                "<div class='tt-row'>Inspections today: <b>" + p.quantity + "</b></div>" +
                "<div class='tt-row'>Active presets: <b>" + p.active_presets + "</b> / " + p.total_presets + "</div>" +
                "<div class='tt-row'>Failures: <b>" + p.failures + "</b></div>" +
                "<div class='tt-row'>Status: <b>" + p.health + "</b></div>"
            );
            wrap.appendChild(row);
        });
        el.invBars.appendChild(wrap);
        el.invNote.textContent =
            "Showing top " + parts.length + " inspected part(s) · amber tick marks the reorder threshold";
    }

    // ------------------------------------------------------------------ #
    //  Alerts
    // ------------------------------------------------------------------ #

    function renderAlerts(data) {
        const alerts = (data && data.alerts) || [];
        el.alerts.innerHTML = "";
        if (el.alertsCount) {
            el.alertsCount.textContent = alerts.length
                ? alerts.length + (alerts.length === 1 ? " alert" : " alerts")
                : "";
        }

        if (!alerts.length) {
            emptyState(
                el.alerts,
                "All clear",
                "No anomalies detected. Inspection errors and failed training jobs will surface here.",
                "✓"
            );
            return;
        }

        const wrap = document.createElement("div");
        wrap.className = "vi-dash-alerts";
        alerts.forEach((a) => {
            const node = document.createElement(a.url ? "a" : "div");
            if (a.url) { node.href = a.url; } else { node.style.cursor = "default"; }
            node.className = "vi-dash-alert";
            const rel = a.timestamp ? relTime(a.timestamp) : "";
            node.innerHTML =
                '<span class="vi-dash-alert-sev ' + a.severity + '"></span>' +
                '<div class="vi-dash-alert-body">' +
                '<div class="vi-dash-alert-top">' +
                '<span class="vi-dash-alert-type">' + escapeHtml(a.type) + "</span>" +
                '<span class="vi-dash-alert-time">' + rel + "</span>" +
                "</div>" +
                '<div class="vi-dash-alert-msg">' + escapeHtml(a.message || "") + "</div>" +
                '<div class="vi-dash-alert-foot">' +
                '<span class="vi-dash-chip ' + a.severity + '">' + a.severity + "</span>" +
                '<span class="vi-dash-chip vi-dash-chip-location">⌖ ' + escapeHtml(a.location || "") + "</span>" +
                (a.status ? '<span class="vi-dash-chip">' + escapeHtml(a.status) + "</span>" : "") +
                "</div></div>";
            if (a.url) {
                bindTooltip(node, () => '<div class="tt-title">' + escapeHtml(a.type) + "</div><div class='tt-row'>' + fullTime(a.timestamp) + </div>");
             
            wrap.appendChild(node);
        };
        el.alerts.appendChild(wrap);
    })}

    // ------------------------------------------------------------------ #
    //  Helpers: escaping + time
    // ------------------------------------------------------------------ #

    function escapeHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }
    function fullTime(iso) {
        if (!iso) return "";
        return new Date(iso).toLocaleString();
    }
    function relTime(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        const diff = (Date.now() - d.getTime()) / 1000;
        if (diff < 60) return "just now";
        if (diff < 3600) return Math.floor(diff / 60) + "m ago";
        if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
        return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    // ------------------------------------------------------------------ #
    //  Filters + fetch
    // ------------------------------------------------------------------ #

    function currentQuery(extra) {
        const base = {
            days: el.filterDays ? el.filterDays.value : currentFilters.days,
            part_number: el.filterPart ? el.filterPart.value.trim() : currentFilters.part_number,
        };
        return Object.assign(base, extra || {});
    }

    async function fetchSummary(showSkeleton) {
        if (abortController) abortController.abort();
        abortController = new AbortController();

        if (showSkeleton || firstLoad) {
            loadingState(el.flowTrend);
            loadingState(el.invBars);
            loadingState(el.alerts);
        }
        setLive("polling", "Updating…");
        if (el.refreshBtn) el.refreshBtn.disabled = true;

        const q = new URLSearchParams();
        q.set("days", el.filterDays ? el.filterDays.value : 1);
        const partVal = el.filterPart ? el.filterPart.value.trim() : "";
        if (partVal) q.set("part_number", partVal);
        const resultVal = el.filterResult ? el.filterResult.value : "";
        if (resultVal) q.set("result", resultVal);

        try {
            const res = await fetch(SUMMARY_URL + "?" + q.toString(), {
                signal: abortController.signal,
                headers: { Accept: "application/json" },
            });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            if (!data || data.ok !== true) throw new Error("Malformed response");

            lastSuccessAt = new Date();
            firstLoad = false;
            currentFilters = {
                days: Number(q.get("days")) || 1,
                part_number: partVal,
                result: resultVal,
            };

            // KPI + delta
            const flow = data.flow || {};
            el.flowValue.textContent = fmtInt(flow.current);
            renderDelta(flow);
            el.flowNote.textContent =
                "vs " + fmtInt(flow.previous) + " in the previous " +
                (flow.label === "Today" ? "day" : flow.label.toLowerCase().replace("last ", "")) +
                " · " + (flow.label || "");

            renderTrend(flow.trend || {});
            renderByPart(flow.by_part || []);
            renderInspection(data.inspection || {});
            renderInventory(data.inventory || {});
            renderAlerts(data.alerts || {});

            setLive("", "Live");
            updateLastRefresh();
        } catch (err) {
            if (err.name === "AbortError") return;
            setLive("error", "Connection lost");
            if (firstLoad) {
                emptyState(
                    el.flowTrend,
                    "Couldn't load dashboard data",
                    "Check that the server is running, then use Refresh to try again.",
                    "⚠"
                );
                emptyState(el.invBars, "Unavailable", "Data could not be loaded.", "⚠");
                emptyState(el.alerts, "Unavailable", "Data could not be loaded.", "⚠");
            } else {
                // keep last good data, flag staleness
                setLive("stale", "Reconnecting… (" + (lastSuccessAt ? lastSuccessAt.toLocaleTimeString() : "no data") + ")");
            }
        } finally {
            if (el.refreshBtn) el.refreshBtn.disabled = false;
            abortController = null;
        }
    }

    function renderDelta(flow) {
        const pct = flow.change_pct;
        let cls = "flat", arrow = "→", txt = "no baseline";
        if (pct == null) {
            cls = "flat"; arrow = "→"; txt = "no baseline";
        } else if (pct > 0) {
            cls = "up"; arrow = "▲"; txt = "+" + pct + "%";
        } else if (pct < 0) {
            cls = "down"; arrow = "▼"; txt = pct + "%";
        } else {
            cls = "flat"; arrow = "→"; txt = "0%";
        }
        el.flowDelta.className = "vi-dash-delta " + cls;
        el.flowDelta.innerHTML = "<span>" + arrow + "</span> " + txt;
    }

    // ------------------------------------------------------------------ #
    //  Polling (single timer, cleaned up on pagehide)
    // ------------------------------------------------------------------ #

    function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(() => fetchSummary(false), POLL_INTERVAL);
    }
    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (abortController) { abortController.abort(); abortController = null; }
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #

    function init() {
        // populate part filter options from the existing parts-list API
        if (el.filterPart) loadPartOptions();

        if (el.refreshBtn) el.refreshBtn.addEventListener("click", () => fetchSummary(false));
        if (el.filterDays) el.filterDays.addEventListener("change", () => fetchSummary(false));
        if (el.filterResult) el.filterResult.addEventListener("change", () => fetchSummary(false));
        if (el.filterPart) {
            el.filterPart.addEventListener("change", () => fetchSummary(false));
        }

        // redraw charts on resize (debounced) so SVG stays crisp
        let rz;
        window.addEventListener("resize", () => {
            clearTimeout(rz);
            rz = setTimeout(() => {
                if (!firstLoad) fetchSummary(false);
            }, 250);
        });

        window.addEventListener("pagehide", stopPolling);

        fetchSummary(true);
        startPolling();
    }

    async function loadPartOptions() {
        try {
            const res = await fetch(
                (root.dataset.partsUrl || "/vision-inspection/api/parts/list/"),
                { headers: { Accept: "application/json" } }
            );
            if (!res.ok) return;
            const data = await res.json();
            const parts = (data && data.parts) || [];
            parts.forEach((p) => {
                const opt = document.createElement("option");
                opt.value = p.part_number;
                opt.textContent = p.part_number + (p.part_desc ? " — " + p.part_desc : "");
                el.filterPart.appendChild(opt);
            });
        } catch (e) {
            /* non-fatal: keep the manual text filter usable */
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
