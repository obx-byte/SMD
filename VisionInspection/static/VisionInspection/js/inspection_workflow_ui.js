/**
 * VisionInspection — Airbag Stitch Inspection workflow UI (barcode → model →
 * submit → front → back → result → save).  Drives the progress panel, front/
 * back stitch tables, and overall result banner from
 * real backend state via AJAX polling (single timer, cleaned up on leave).
 *
 * Verdicts come only from the backend; nothing is fabricated here.
 */
(function () {
    "use strict";

    var cfg = window.__VI_WS__ || { urls: {} };
    var URL = cfg.urls || {};

    var st = {
        sessionId: null,
        partOk: false,
        modelOk: false,
        frontCompleted: false,
        backCompleted: false,
        overall: "PENDING",
        saved: false,
    };

    function $(id) { return document.getElementById(id); }
    function csrf() { return cfg.csrf || ""; }

    var E = {
        barcode: $("wfBarcode"), scanBtn: $("wfScanBtn"), validateBtn: $("wfValidateBtn"),
        submitBtn: $("wfSubmitBtn"), scanState: $("wfScanState"), modelState: $("wfModelState"),
        partInfo: $("wfPartInfo"), progress: $("wfProgress"),
        progressRows: $("wfProgressRows"),
        frontSide: $("wsFrontSide"), frontSideIcon: $("wsFrontSideIcon"), frontSideValue: $("wsFrontSideValue"),
        backSide: $("wsBackSide"), backSideIcon: $("wsBackSideIcon"), backSideValue: $("wsBackSideValue"),
        modelCard: $("wsModelCard"), modelCardTitle: $("wsModelCardTitle"),
        modelName: $("wsModelName"), modelClasses: $("wsModelClasses"),
        infoPart: $("wsInfoPart"), infoModel: $("wsInfoModel"), infoRevision: $("wsInfoRevision"),
        result: $("wfResult"), resultIcon: $("wfResultIcon"), resultState: $("wfResultState"),
        resultNote: $("wfResultNote"), detFront: $("wfDetFront"), detBack: $("wfDetBack"),
        backBtn: $("wfBackBtn"), saveBtn: $("wfSaveBtn"), cancelBtn: $("wfCancelBtn"),
        error: $("wsError"),
    };

    function setBadge(node, text, key) {
        if (!node) return;
        node.className = "vi-master-badge " + key;
        if (node.firstChild && node.firstChild.nodeType === 3) node.firstChild.textContent = text;
        else node.textContent = text;
    }

    /** Timestamp for a completed step (only real progress carries one). */
    function nowLabel() {
        var d = new Date();
        var pad = function (n) { return (n < 10 ? "0" : "") + n; };
        return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }

    /** Record when a step really completed, so the sidebar can show it. */
    function stampStep(step) {
        var label = nowLabel();
        var root = document.getElementById("wfProgressRows");
        if (root) {
            Array.prototype.forEach.call(
                root.querySelectorAll('[data-step="' + step + '"]'),
                function (el) { el.setAttribute("data-at", label); }
            );
        }
    }

    /** One side of the Inspection Result card — states come from the backend. */
    function setSide(el, iconEl, valueEl, state, label) {
        if (!el) return;
        el.setAttribute("data-state", state);
        if (iconEl) {
            iconEl.textContent = state === "OK" ? "\u2714"
                : state === "NOT_OK" ? "\u2716"
                : state === "IN_PROGRESS" ? "\u25CE"
                : state === "ERROR" ? "\u26A0"
                : state === "WAITING" ? "\u231B" : "\u23F3";
        }
        if (valueEl) valueEl.textContent = label;
    }

    function setFrontSide(state) {
        setSide(E.frontSide, E.frontSideIcon, E.frontSideValue, state, {
            WAITING: "Waiting", IN_PROGRESS: "Inspecting...", OK: "OK",
            NOT_OK: "NOT OK", ERROR: "Error",
        }[state] || "Waiting");
    }

    function setBackSide(state) {
        setSide(E.backSide, E.backSideIcon, E.backSideValue, state, {
            WAITING: "Waiting...", IN_PROGRESS: "Inspecting...", OK: "OK",
            NOT_OK: "NOT OK", ERROR: "Error",
        }[state] || "Waiting...");
    }

    /** Model status card: green only when the model is genuinely loaded. */
    function setModelCard(info) {
        info = info || {};
        var loaded = info.loaded === true;
        if (E.modelCard) {
            E.modelCard.className = "ws-model-status " +
                (loaded ? "is-loaded" : (info.error ? "is-error" : "is-idle"));
        }
        if (E.modelCardTitle) {
            E.modelCardTitle.textContent = loaded ? "Model Found & Loaded"
                : (info.error ? "Model Missing / Invalid" : "Model not validated");
        }
        if (E.modelName) E.modelName.textContent = info.name || "\u2014";
        if (E.modelVersion) E.modelVersion.textContent = info.version || "\u2014";
        if (E.modelClasses) E.modelClasses.textContent = info.num_classes != null ? String(info.num_classes) : "\u2014";
        if (E.infoModel) E.infoModel.textContent = info.name || "\u2014";
    }

    /** Run an async action with its button kept busy and clicks ignored. */
    function busy(btn, fn) {
        if (!btn) return Promise.resolve().then(fn);
        if (btn.classList.contains("is-loading")) return Promise.resolve();
        btn.classList.add("is-loading");
        var restore = function () { btn.classList.remove("is-loading"); };
        return Promise.resolve().then(fn)
            .then(function (r) { restore(); return r; }, function (e) { restore(); throw e; });
    }
    function showError(m) { if (E.error) E.error.textContent = m || ""; }

    function post(url, body) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" },
            body: JSON.stringify(body || {}),
        });
    }

    // ---------- Step 1: scan ----------
    async function scan() {
        showError("");
        var bc = (E.barcode && E.barcode.value || "").trim();
        if (!bc) { setBadge(E.scanState, "Waiting for scan", "STOPPED"); return; }
        setBadge(E.scanState, "Validating…", "CONNECTING");
        try {
            var res = await post(URL.wfScan, { barcode: bc });
            var data = await res.json();
            if (!data.ok) {
                st.partOk = false;
                setBadge(E.scanState, labelFor(data.state), "ERROR");
                if (E.partInfo) E.partInfo.textContent = data.message || "Invalid barcode.";
                E.validateBtn.disabled = true; E.submitBtn.disabled = true;
                return;
            }
            st.partOk = true;
            setBadge(E.scanState, "Part identified", "RUNNING");
            stampStep("scan");
            var p = data.part || {};
            if (E.partInfo) E.partInfo.textContent =
                (p.part_number || "") + (p.part_name ? " — " + p.part_name : "") +
                " · Front ROI: " + (data.front_roi ? "set" : "missing") +
                " · Back ROI: " + (data.back_roi ? "set" : "missing");
            if (E.infoPart) E.infoPart.textContent = p.part_number || "\u2014";
        } catch (e) {
            setBadge(E.scanState, "Scan error", "ERROR");
            showError(e.message);
        }
    }

    function labelFor(state) {
        return ({
            waiting: "Waiting for scan", part_not_found: "Part not found",
            invalid: "Invalid barcode", model_missing: "Model missing",
            model_invalid: "Model invalid", model_classes_missing: "Model classes missing",
            model_loaded: "Model loaded", duplicate_session: "Active session exists",
        })[state] || state;
    }

    // ---------- Step 2: validate model ----------
    async function validateModel() {
        showError("");
        setBadge(E.modelState, "Validating…", "CONNECTING");
        try {
            var res = await post(URL.wfValidate, {});
            var data = await res.json();
            if (!data.ok) {
                st.modelOk = false;
                setBadge(E.modelState, labelFor(data.state), "ERROR");
                E.submitBtn.disabled = true;
                showError(data.message || "Model validation failed.");
                return;
            }
            st.modelOk = true;
            setBadge(E.modelState, "Loaded (" + (data.num_classes || 0) + " cls)", "RUNNING");
            // The model card only turns green when the model is really loaded.
            setModelCard({
                loaded: true,
                name: data.model_name || cfg.modelName || "",
                num_classes: data.num_classes != null ? data.num_classes : null,
            });
            stampStep("model");
            E.submitBtn.disabled = false;
        } catch (e) {
            setBadge(E.modelState, "Error", "ERROR");
            showError(e.message);
        }
    }

    // ---------- Step 3: submit ----------
    async function submit() {
        showError("");
        var part = (E.barcode.value || "").trim();
        await busy(E.submitBtn, async function () {
            setBadge(E.scanState, "Submitting…", "CONNECTING");
            try {
                var res = await post(URL.wfSubmit, { part_number: part });
                var data = await res.json();
                if (!data.ok) {
                    setBadge(E.scanState, labelFor(data.state), "ERROR");
                    setPill(E.partBadge, "Part: " + (data.message || "submit failed"), "");
                    showError(data.message || "Submit failed.");
                    E.submitBtn.disabled = !st.modelOk;
                    return;
                }
                st.sessionId = data.session_id;
                st.saved = false;
                st.frontCompleted = false;
                setBadge(E.scanState, "Front inspection running", "RUNNING");
                setPill(E.partBadge, "Session open — front inspection", "ready");
                if (E.cancelBtn) E.cancelBtn.disabled = false;
                startBackBtn(false);
                startProgressPoll();
                // Run front inspection on the live frame.
                await inspectSide("front");
                pollNow();
            } catch (e) { showError(e.message); E.submitBtn.disabled = !st.modelOk; }
    })

    // ---------- Step 4/5/6: run side ----------
    async function inspectSide(side) {
        if (!st.sessionId) return;
        try {
            var res = await post(side === "front" ? URL.wfInspectFront : URL.wfInspectBack, { session_id: st.sessionId });
            var data = await res.json();
            if (!data.ok) {
                showError(data.message || (side + " inspection failed."));
                if (E.progress) E.progress.textContent = data.message || "Inspection error.";
                return;
            }
            if (side === "front") st.frontCompleted = true;
            else st.backCompleted = true;
            pollNow();
            // Automatically continue sequential flow.
            if (side === "front") {
                if (E.resultNote) E.resultNote.textContent = "Front inspection complete. Reposition the part, then capture the back.";
                startBackBtn(true);
            } else {
                await computeResult();
            }
        } catch (e) { showError(e.message); }
    }

    function startBackBtn(enabled) {
        if (!E.backBtn) return;
        E.backBtn.disabled = !enabled;
        E.backBtn.textContent = enabled ? "\u21BB Reposition & Capture Back" : "\u21BB Reposition & Capture Back (after front)";
    }

    async function computeResult() {
        try {
            var res = await post(URL.wfCompute, { session_id: st.sessionId });
            await res.json();
            pollNow();
        } catch (e) { showError(e.message); }
    }

    // ---------- Step 7/9: save ----------
    async function save() {
        if (!st.sessionId) return;
        E.saveBtn.disabled = true;
        try {
            var res = await post(URL.wfSave, { session_id: st.sessionId });
            var data = await res.json();
            if (!data.ok) { showError(data.message || "Save failed."); E.saveBtn.disabled = false; return; }
            st.saved = true;
            setBadge(E.scanState, "Saved", "RUNNING");
            if (E.resultNote) E.resultNote.textContent = "Inspection saved to database (record #" + (data.inspection_id || "?") + ").";
            pollNow();
        } catch (e) { showError(e.message); E.saveBtn.disabled = false; }
    }

    async function cancel() {
        if (!st.sessionId) return;
        try { await post(URL.wfCancel, { session_id: st.sessionId }); } catch (e) {}
        st.sessionId = null; st.frontCompleted = st.backCompleted = false; st.saved = false;
        stopProgressPoll();
        setBadge(E.scanState, "Cancelled", "STOPPED");
        renderProgress({ ok: true, stages: [], front_status: "PENDING", back_status: "PENDING", overall: "PENDING", front_results: [], back_results: [] });
    }


    // ---------- Progress polling ----------
    var timer = null, inFlight = false;
    function startProgressPoll() {
        stopProgressPoll();
        pollNow();
        timer = setInterval(function () {
            if (document.hidden) return;
            pollNow();
        }, 1500);
    }
    function stopProgressPoll() { if (timer) { clearInterval(timer); timer = null; } }

    async function pollNow() {
        if (!st.sessionId || inFlight) return;
        inFlight = true;
        try {
            var res = await fetch(cfg.urls.wfSession + st.sessionId + "/", { headers: { Accept: "application/json" } });
            var data = await res.json();
            if (data && data.ok) renderProgress(data);
        } catch (e) { /* keep last */ }
        finally { inFlight = false; }
    }

    function renderProgress(p) {
        st.overall = p.overall || "PENDING";
        st.frontCompleted = p.front_status === "COMPLETED";
        st.backCompleted = p.back_status === "COMPLETED";
        st.saved = !!p.is_saved;

        // Workflow stages reported by the backend session.
        var stages = p.stages || [];
        if (E.progress) {
            if (!stages.length) E.progress.textContent = "Scan a part, validate the model, then Submit.";
            else E.progress.textContent = stages.map(function (s) { return s.label + ": " + s.status; }).join(" · ");
        }

        // Stitch detection tables
        renderDet(E.detFront, p.front_results);
        renderDet(E.detBack, p.back_results);

        // Overall result banner
        var o = st.overall, cls, icon, note;
        if (o === "OK") { cls = "state-OK"; icon = "&#10003;"; note = "Front and back inspection passed."; }
        else if (o === "NOT_OK") { cls = "state-NOT_OK"; icon = "&#10007;"; note = "A stitch check failed."; }
        else if (o === "INCOMPLETE") { cls = "state-WAITING"; icon = "&#9888;"; note = "Inspection incomplete — no acceptance rule configured, or a side failed. Not eligible for OK."; }
        else { cls = "state-WAITING"; icon = "&#9711;"; note = p.is_saved ? "Saved." : "Awaiting completion."; }
        if (E.result) E.result.className = "ws-result " + cls;
        if (E.resultIcon) E.resultIcon.innerHTML = icon;
        if (E.resultState) E.resultState.textContent = (o === "INCOMPLETE" ? "INCOMPLETE" : o);
        if (E.resultNote && !st.saved) E.resultNote.textContent = note;

        // Inspection Result — per-side state
        setFrontSide(p.front_status === "IN_PROGRESS" ? "IN_PROGRESS"
            : p.front_status === "COMPLETED" ? (p.front_verdict === "OK" ? "OK" : "NOT_OK")
            : p.front_status === "ERROR" ? "ERROR" : "WAITING");
        setBackSide(p.back_status === "IN_PROGRESS" ? "IN_PROGRESS"
            : p.back_status === "COMPLETED" ? (p.back_verdict === "OK" ? "OK" : "NOT_OK")
            : p.back_status === "ERROR" ? "ERROR" : "WAITING");

        // Buttons
        if (E.saveBtn) E.saveBtn.disabled = !(st.frontCompleted && st.backCompleted && !st.saved && o !== "PENDING");
        if (E.backBtn) E.backBtn.disabled = !(st.frontCompleted && !st.backCompleted && !st.saved);

        // Stepper state for both the sidebar rows and the bottom flow cards.
        var stepState = {
            scan: (st.partOk || st.sessionId) ? "Completed" : (stages.length ? stages[0].status : "Pending"),
            model: "Pending",
            front: "Pending",
            back: "Pending",
            result: "Pending",
            saved: st.saved ? "Completed" : "Pending",
        };
        var map = { scan: "Part Scanned", model: "Model Loaded", front: "Front", back: "Back", result: "Result", saved: "Saved" };
        Object.keys(map).forEach(function (key) {
            var label = map[key];
            var found = stages.find(function (s) { return s.label.indexOf(label) === 0; });
            if (found) stepState[key] = found.status;
        });
        stepState.model = st.modelOk ? "Completed"
            : (st.sessionId ? "Completed" : stepState.model);
        stepState.result = (o === "PENDING") ? stepState.result : (o === "INCOMPLETE" ? "Pending" : "Completed");

        applySteps("wfProgressRows", "ws-progress-row", "ws-progress-state",
            { completed: nowLabel }, stepState);
    }

    /**
     * Paint the inspection-progress rows in the right sidebar.
     * Each row carries a ``data-step`` attribute matching the workflow state
     * map, so one real state map drives the whole stepper.
     */
    function applySteps(rootId, stepClass, stateClass, stateText, stepState) {
        var root = document.getElementById(rootId);
        if (!root) return;
        Array.prototype.forEach.call(root.querySelectorAll("." + stepClass), function (el) {
            var step = el.getAttribute("data-step");
            var state = stepState[step] || "Pending";
            el.className = stepClass + " is-" + state.replace(/\s/g, "");
            var stateEl = el.querySelector("." + stateClass);
            if (!stateEl) return;
            if (state === "Completed") {
                stateEl.textContent = el.getAttribute("data-at") || stateText.completed();
            } else if (state === "In Progress") {
                stateEl.textContent = "In Progress...";
            } else {
                stateEl.textContent = state;
            }
        });
    }

    function badgeClass(status) {
        if (status === "Completed") return "RUNNING";
        if (status === "In Progress") return "STARTING";
        if (status === "Failed") return "ERROR";
        return "STOPPED";
    }

    function renderDet(tbody, rows) {
        if (!tbody) return;
        if (!rows || !rows.length) { tbody.innerHTML = '<tr><td colspan="3" class="ws-empty">Waiting.</td></tr>'; return; }
        tbody.innerHTML = rows.map(function (r) {
            var conf = (r.confidence == null) ? "—" : (typeof r.confidence === "number" ? r.confidence.toFixed(2) : r.confidence);
            var statCls = r.status === "OK" ? "pass" : (r.status === "NOT_OK" ? "fail" : "neutral");
            return "<tr><td>" + esc(r.stitch) + "</td><td>" + conf + "</td>" +
                '<td><span class="ws-status-pill ' + statCls + '">' + esc(r.status || "—") + "</span></td></tr>";
        }).join("");
    }

    function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]; }); }

    function init() {
        if (E.barcode) E.barcode.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); scan(); } });
        if (E.scanBtn) E.scanBtn.addEventListener("click", function () { busy(E.scanBtn, scan); });
        if (E.validateBtn) E.validateBtn.addEventListener("click", function () { busy(E.validateBtn, validateModel); });
        if (E.submitBtn) E.submitBtn.addEventListener("click", submit);
        if (E.backBtn) E.backBtn.addEventListener("click", function () { busy(E.backBtn, function () { return inspectSide("back"); }); });
        if (E.saveBtn) E.saveBtn.addEventListener("click", function () { busy(E.saveBtn, save); });
        if (E.cancelBtn) E.cancelBtn.addEventListener("click", function () { busy(E.cancelBtn, cancel); });
        window.addEventListener("pagehide", function () { stopProgressPoll(); });
        // Nothing runs until an operator scans a part: Submit stays disabled.
        if (E.submitBtn) E.submitBtn.disabled = true;
        setFrontSide("WAITING");
        setBackSide("WAITING");
        // No model has been validated yet, so the card stays neutral.
        setModelCard({ loaded: false });
        renderProgress({ ok: true, stages: [], overall: "PENDING", front_results: [], back_results: [] });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
    }})();
