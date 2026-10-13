/**
 * VisionInspection — Airbag Stitch Inspection workspace.
 *
 * Single-camera hardware is handled honestly: only the ACTIVE view (front/back)
 * shows the live feed; the inactive view is clearly marked not connected.
 * ROI drawing uses the same object-fit/letterbox mapping as the Inspection
 * Master so coordinates always map to the original camera frame.  Results and
 * detections come only from the real continuous-inspection status; nothing is
 * fabricated.
 */
(function () {
    "use strict";

    var cfg = window.__VI_WS__ || { urls: {} };
    var URL = cfg.urls;
    var CAPS = cfg.capabilities || {};

    var state = {
        view: "front",
        cameraOn: false,
        running: false,
        refW: 0,
        refH: 0,
        rois: { front: null, back: null },
        hasSaved: { front: false, back: false },
        drawing: false,
        start: null,
        pending: null,
    };

    function id(v) { return v.charAt(0).toUpperCase() + v.slice(1); }
    function $(s) { return document.querySelector(s); }
    function rid(v, s) { return document.getElementById("ro" + id(v) + s); }

    var VIEWS = ["front", "back"];
    var E = {};
    VIEWS.forEach(function (v) {
        E[v] = {
            card: document.querySelector('.ws-view[data-view="' + v + '"]'),
            cam: document.getElementById("cam" + id(v)),
            feed: document.getElementById("feed" + id(v)),
            canvas: document.getElementById("draw" + id(v)),
            overlay: document.getElementById("ov" + id(v)),
            off: document.getElementById("offFront" == "" ? "" : "off" + id(v)),
            badge: document.getElementById("badge" + id(v)),
            x: rid(v, "X"), y: rid(v, "Y"), w: rid(v, "W"), h: rid(v, "H"),
            ref: rid(v, "Ref"),
            save: rid(v, "Save"), edit: rid(v, "Edit"), reset: rid(v, "Reset"),
        };
    });

    var G = {
        part: document.getElementById("wsPart"),
        camSrc: document.getElementById("wsCamSrc"),
        camIdx: document.getElementById("wsCamIdx"),
        camIdxField: document.getElementById("wsCamIdxField"),
        startCam: document.getElementById("wsStartCam"),
        stopCam: document.getElementById("wsStopCam"),
        capture: document.getElementById("wsCapture"),
        lightUp: document.getElementById("wsLightUp"),
        lightDn: document.getElementById("wsLightDn"),
        fs: document.getElementById("wsFullscreen"),
        startInsp: document.getElementById("wsStartInsp"),
        stopInsp: document.getElementById("wsStopInsp"),
        inspState: document.getElementById("wsInspState"),
        camState: document.getElementById("wsCamState"),
        toolbarCam: document.getElementById("wsToolbarCam"),
        modelState: document.getElementById("wsModelState"),
        modelCard: document.getElementById("wsModelCard"),
        modelCardTitle: document.getElementById("wsModelCardTitle"),
        modelName: document.getElementById("wsModelName"),
        modelVersion: document.getElementById("wsModelVersion"),
        modelClasses: document.getElementById("wsModelClasses"),
        infoPart: document.getElementById("wsInfoPart"),
        infoModel: document.getElementById("wsInfoModel"),
        infoRevision: document.getElementById("wsInfoRevision"),
        roiFront: document.getElementById("wsRoiFront"),
        roiBack: document.getElementById("wsRoiBack"),
        fps: document.getElementById("wsFps"),
        latency: document.getElementById("wsLatency"),
        e2e: document.getElementById("wsE2E"),
        frameAge: document.getElementById("wsFrameAge"),
        result: document.getElementById("wsResult"),
        resultIcon: document.getElementById("wsResultIcon"),
        resultState: document.getElementById("wsResultState"),
        resultNote: document.getElementById("wsResultNote"),
        detBody: document.getElementById("wsDetBody"),
        hint: document.getElementById("wsHint"),
        error: document.getElementById("wsError"),
        clock: document.getElementById("wsClock"),
        workspace: document.getElementById("wsWorkspace"),
    };

    function csrf() { return cfg.csrf || ""; }
    function setError(m) { if (G.error) G.error.textContent = m || ""; }
    function showError(m) { setError(m); }

    // ---------------- helpers ---------------- //

    /** Mark a button as busy (spinner, clicks ignored) until the promise settles. */
    function withBusy(btn, fn) {
        if (!btn) return Promise.resolve().then(fn);
        if (btn.classList.contains("is-loading")) return Promise.resolve(); // in-flight: ignore repeat clicks
        btn.classList.add("is-loading");
        var restore = function () { btn.classList.remove("is-loading"); };
        return Promise.resolve()
            .then(fn)
            .then(function (r) { restore(); return r; }, function (e) { restore(); throw e; });
    }
    function imageRect(canvas) {
        var rect = canvas.getBoundingClientRect();
        var cw = rect.width || 1, ch = rect.height || 1;
        var fw = state.refW || cw, fh = state.refH || ch;
        var boxAR = cw / ch, imgAR = fw / fh, dw, dh, dx, dy;
        if (imgAR > boxAR) { dw = cw; dh = cw / imgAR; dx = 0; dy = (ch - dh) / 2; }
        else { dh = ch; dw = ch * imgAR; dy = 0; dx = (cw - dw) / 2; }
        return { dx: dx, dy: dy, dw: dw, dh: dh, rect: rect };
    }

    function sizeCanvas(v) {
        var c = E[v].canvas; if (!c) return;
        var rect = E[v].cam.getBoundingClientRect();
        var dpr = window.devicePixelRatio || 1;
        c.width = Math.max(1, Math.floor(rect.width * dpr));
        c.height = Math.max(1, Math.floor(rect.height * dpr));
        drawRoi(v);
    }

    function drawRoi(v) {
        var c = E[v].canvas; if (!c || !c.getContext) return;
        var ctx = c.getContext("2d");
        var dpr = window.devicePixelRatio || 1;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, c.width, c.height);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        var roi = state.rois[v] || state.pending;
        if (!roi || v !== state.view) return;
        var r = imageRect(c);
        var X = function (px) { return r.dx + px / (state.refW || 1) * r.dw; };
        var Y = function (py) { return r.dy + py / (state.refH || 1) * r.dh; };
        var x = X(roi.x), y = Y(roi.y), w = X(roi.x + roi.w) - x, h = Y(roi.y + roi.h) - y;
        var saved = roi === state.rois[v];
        ctx.fillStyle = "rgba(255,77,94,0.14)";
        ctx.fillRect(x, y, w, h);
        ctx.lineWidth = saved ? 2.5 : 2;
        ctx.strokeStyle = "#ff4d5e";
        if (!saved) ctx.setLineDash([6, 4]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
        var hs = Math.max(9, Math.min(14, w * 0.18));
        ctx.fillStyle = "#fff"; ctx.strokeStyle = "#ff4d5e"; ctx.lineWidth = 2;
        [[x, y], [x + w - hs, y], [x, y + h - hs], [x + w - hs, y + h - hs]]
            .forEach(function (cn) { ctx.strokeRect(cn[0], cn[1], hs, hs); });
    }

    function canvasPoint(v, evt) {
        var c = E[v].canvas; var r = imageRect(c); var rect = r.rect;
        var px = evt.clientX - rect.left - r.dx;
        var py = evt.clientY - rect.top - r.dy;
        return {
            nx: Math.min(1, Math.max(0, px / (r.dw || 1))),
            ny: Math.min(1, Math.max(0, py / (r.dh || 1))),
        };
    }

    function updateReadout(v) {
        var roi = state.pending || state.rois[v];
        var set = function (el, val) { if (el) el.textContent = val; };
        if (roi && v === state.view) {
            set(E[v].x, Math.round(roi.x)); set(E[v].y, Math.round(roi.y));
            set(E[v].w, Math.round(roi.w)); set(E[v].h, Math.round(roi.h));
        } else { set(E[v].x, "—"); set(E[v].y, "—"); set(E[v].w, "—"); set(E[v].h, "—"); }
        set(E[v].ref, state.refW && state.refH ? (state.refW + "×" + state.refH) : "—");
    }

    /** Sidebar readout for one view's saved ROI (empty until one is really saved). */
    function setRoiReadout(v) {
        var el = v === "front" ? G.roiFront : G.roiBack;
        if (!el) return;
        var roi = state.rois[v];
        el.textContent = roi ? (Math.round(roi.w) + "×" + Math.round(roi.h) + " px") : "not set";
    }
    function onDown(v, e) {
        if (v !== state.view || !state.cameraOn || state.running) return;
        e.preventDefault();
        if (E[v].canvas.setPointerCapture) E[v].canvas.setPointerCapture(e.pointerId);
        state.drawing = true; state.start = canvasPoint(v, e); state.pending = null;
    }
    function onMove(v, e) {
        if (!state.drawing || v !== state.view) return;
        var s = state.start, cur = canvasPoint(v, e);
        state.pending = {
            x: Math.min(s.nx, cur.nx) * (state.refW || 0),
            y: Math.min(s.ny, cur.ny) * (state.refH || 0),
            w: Math.abs(cur.nx - s.nx) * (state.refW || 0),
            h: Math.abs(cur.ny - s.ny) * (state.refH || 0),
        };
        drawRoi(v); updateReadout(v);
    }
    function onUp(v) {
        if (!state.drawing) return;
        state.drawing = false;
        var roi = state.pending;
        if (!roi || roi.w < 4 || roi.h < 4) { state.pending = null; drawRoi(v); updateReadout(v); return; }
        if (E[v].save) E[v].save.disabled = false;
    }

    // ---------------- view toggle (single-camera fallback) ---------------- //
    function setActiveView(v) {
        state.view = v;
        VIEWS.forEach(function (vv) {
            var active = vv === v;
            var card = E[vv].card; if (card) card.style.opacity = active ? "1" : "";
            if (card) card.classList.toggle("ws-inactive", !active);
            if (E[vv].canvas) E[vv].canvas.style.pointerEvents = active ? "auto" : "none";
            if (E[vv].off) E[vv].off.style.display = (active || state.cameraOn) ? "none" : "grid";
            if (!active) { if (E[vv].feed) { E[vv].feed.removeAttribute("src"); E[vv].feed.style.display = "none"; } }
        });
        document.querySelectorAll(".ws-view-toggle button").forEach(function (b) {
            b.classList.toggle("active", b.getAttribute("data-view") === v);
        });
        // Attach the single live feed to the active view.
        if (state.cameraOn && E[v].feed) {
            E[v].feed.style.display = "block";
            E[v].feed.src = URL.cameraStream + "?t=" + Date.now();
            if (E[v].overlay) E[v].overlay.classList.add("hidden");
        }
        reloadView(v);
        refreshControlState();
        updateReadout(v);
        sizeCanvas(v);
    }

    async function reloadView(v) {
        var part = G.part && G.part.value;
        if (!part) { state.rois[v] = null; state.hasSaved[v] = false; drawRoi(v); updateReadout(v); return; }
        if (state.rois[v]) { drawRoi(v); updateReadout(v); return; }
        try {
            var res = await fetch(URL.roiGet + "?part_number=" + encodeURIComponent(part) + "&camera_view=" + v, { headers: { Accept: "application/json" } });
            var data = await res.json();
            if (data && data.saved) {
                state.refW = data.ref_width || state.refW;
                state.refH = data.ref_height || state.refH;
                state.rois[v] = { x: data.x, y: data.y, w: data.width, h: data.height };
                state.hasSaved[v] = true;
                setRoiReadout(v);
            } else { state.rois[v] = null; state.hasSaved[v] = false; }
        } catch (e) { /* ignore */ }
        drawRoi(v); updateReadout(v);
        refreshControlState();
    }

    // ---------------- camera ---------------- //
    function camOpt(src) { return (CAPS.sources || []).find(function (s) { return s.id === src; }); }

    async function startCamera() {
        setError("");
        var src = G.camSrc ? G.camSrc.value : "opencv";
        var opt = camOpt(src);
        if (opt && opt.available === false) { showError((opt.label || "Camera") + " is not available on this system."); return; }
        var idx = G.camIdx ? G.camIdx.value : 0;
        var data = {};
        try {
            var res = await fetch(URL.cameraStart + "?source=" + encodeURIComponent(src) + "&camera_index=" + encodeURIComponent(idx), { headers: { "X-CSRFToken": csrf() } });
            try { data = await res.json(); } catch (e) { data = {}; }
        } catch (e) { /* poll reflects */ }
        if (data.status === "ERROR") {
            state.cameraOn = false;
            if (E[state.view].overlay) { E[state.view].overlay.textContent = data.error || "Camera could not be started."; E[state.view].overlay.classList.remove("hidden"); }
            showError(data.error || "Camera could not be started.");
            refreshControlState();
            return;
        }
        state.cameraOn = true;
        setViewLive();
        await refreshResolution();
        VIEWS.forEach(function (v) { sizeCanvas(v); });
        refreshControlState();
    }

    function setViewLive() {
        var v = state.view;
        if (E[v].feed) { E[v].feed.style.display = "block"; E[v].feed.src = URL.cameraStream + "?t=" + Date.now(); }
        if (E[v].overlay) { E[v].overlay.textContent = "Connecting…"; E[v].overlay.classList.add("hidden"); }
        if (E[v].off) E[v].off.style.display = "none";
        // Backend must confirm frames before showing ON (handled by poll).
        setBadge(E[v].badge, "CONNECTING", "CONNECTING");
        var other = v === "front" ? "back" : "front";
        if (E[other].badge) setBadge(E[other].badge, "N/A", "OFF");
        if (E[other].off) E[other].off.style.display = "grid";
    }
    // Reflect the backend-confirmed camera state into both view cards.
    function applyCameraBadges(cam) {
        var on = cam === "ON";
        var key = on ? "ON" : (cam === "CONNECTING" ? "CONNECTING" : (cam === "ERROR" ? "ERROR" : "OFF"));
        var v = state.view, other = v === "front" ? "back" : "front";
        setBadge(E[v].badge, key, key);
        // Toolbar status always shows the real device state (never a fake ON).
        setBadge(G.toolbarCam, on ? "CAMERA ON" : (cam === "CONNECTING" ? "CONNECTING" : cam), key);
        if (E[v].overlay) {
            if (on) E[v].overlay.classList.add("hidden");
            else {
                E[v].overlay.textContent = cam === "ERROR" ? "Camera error — check the camera source."
                    : cam === "CONNECTING" ? "Connecting…" : "Camera off — press Start";
                E[v].overlay.classList.remove("hidden");
            }
        }
        if (E[v].off && on) E[v].off.style.display = "none";
        if (E[other].badge) setBadge(E[other].badge, "N/A", "OFF");  // single-camera
    }

    function stopCamera() {
        fetch(URL.cameraStop, { headers: { "X-CSRFToken": csrf() } }).catch(function () {});
        state.cameraOn = false;
        VIEWS.forEach(function (v) {
            if (E[v].feed) { E[v].feed.removeAttribute("src"); E[v].feed.style.display = "none"; }
            if (E[v].overlay) E[v].overlay.classList.remove("hidden");
        });
        refreshControlState();
    }

    async function refreshResolution() {
        try {
            var res = await fetch(URL.resolution);
            var d = await res.json();
            if (d.width && d.height) { state.refW = d.width; state.refH = d.height; }
        } catch (e) { /* ignore */ }
        if (!state.refW || !state.refH) { state.refW = 1280; state.refH = 720; }
        VIEWS.forEach(function (v) { updateReadout(v); drawRoi(v); });
    }

    // ---------------- inspection ---------------- //
    async function saveRoi(v) {
        setError("");
        var part = G.part && G.part.value;
        if (!part) { showError("Select a part number first."); return; }
        if (!state.cameraOn) { showError("Start the camera first."); return; }
        var roi = state.pending || state.rois[v];
        if (!roi) { showError("Draw an ROI on the live preview first."); return; }
        var body = { part_number: part, camera_view: v, x: Math.round(roi.x), y: Math.round(roi.y), width: Math.round(roi.w), height: Math.round(roi.h), ref_width: state.refW, ref_height: state.refH };
        if (E[v].save) E[v].save.disabled = true;
        try {
            var res = await fetch(URL.roiSave, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" }, body: JSON.stringify(body) });
            var data = await res.json();
            if (data && data.error) throw new Error(data.error);
            state.rois[v] = { x: Math.round(roi.x), y: Math.round(roi.y), w: Math.round(roi.w), h: Math.round(roi.h) };
            state.pending = null;
            state.hasSaved[v] = true;
            setRoiReadout(v);
            showError("");
            if (E[v].edit) E[v].edit.disabled = false;
            drawRoi(v); updateReadout(v);
            poll(true);
        } catch (e) { showError(e.message); if (E[v].save) E[v].save.disabled = false; }
    }

    async function startInspection() {
        setError("");
        var part = G.part && G.part.value;
        if (!part) { showError("Select a part number first."); return; }
        if (!state.cameraOn) { showError("Start the camera first."); return; }
        if (!state.hasSaved[state.view]) { showError("Draw and Save an ROI for this view first."); return; }
        try {
            var res = await fetch(URL.start, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" }, body: JSON.stringify({ part_number: part, camera_view: state.view }) });
            var data = await res.json();
            if (data && data.error) throw new Error(data.error);
            showError("");
            poll(true);
        } catch (e) { showError(e.message); }
    }

    async function stopInspection() {
        try {
            var res = await fetch(URL.stop, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" }, body: "{}" });
            poll(true);
        } catch (e) { showError(e.message); }
    }

    async function captureFrame() {
        setError("");
        if (!state.cameraOn) { showError("Start the camera before capturing."); return; }
        try {
            var res = await fetch(URL.cameraCapture, { method: "POST", headers: { "X-CSRFToken": csrf(), Accept: "application/json" }, body: "{}" });
            var data = await res.json();
            if (data && data.error) throw new Error(data.error);
            // Evidence (front+back frame, timestamp, inspection id) is written by
            // the configured inspection workflow; here we grab the current frame.
            if (data.image) {
                state.lastCapture = data.image;
                if (G.resultNote) G.resultNote.textContent = "Frame captured (" + (data.width || "?") + "×" + (data.height || "?") + "). Persist an inspection record via the configured inspection workflow to add traceability evidence.";
            }
            showHint("Frame captured. Evidence is stored by the configured inspection workflow.");
        } catch (e) { showError(e.message); }
    }

    function resetRoi(v) {
        if (state.running) { showError("Stop inspection before resetting the ROI."); return; }
        state.pending = null; state.rois[v] = null; state.hasSaved[v] = false;
        if (E[v].save) E[v].save.disabled = true;
        if (E[v].edit) E[v].edit.disabled = true;
        setRoiReadout(v);
        drawRoi(v); updateReadout(v); refreshControlState();
    }
    function editRoi(v) {
        if (state.running) { showError("Stop inspection before editing the ROI."); return; }
        state.pending = null; setError(""); drawRoi(v); updateReadout(v); showHint("Drag on the preview to redraw the ROI, then Save.");
    }

    // ---------------- status polling ---------------- //
    var inFlight = null, timer = null;
    function setBadge(node, text, key) {
        if (!node) return;
        node.className = "vi-master-badge " + key;
        node.lastChild.textContent = text;
    }

    async function poll(force) {
        if (inFlight && !force) return;
        inFlight = fetch(URL.status, { headers: { Accept: "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (s) {
                var st = s.state || "STOPPED";
                var running = s.running === true;
                state.running = running;
                setBadge(G.inspState, st, st);
                var cam = s.camera_status || "OFF";
                state.cameraOn = cam === "ON";
                setBadge(G.camState, cam, cam);
                applyCameraBadges(cam);  // per-view cards reflect backend-confirmed state
                if (s.model_configured) { setBadge(G.modelState, "CONFIGURED", "RUNNING"); }
                else { setBadge(G.modelState, "NOT CONFIGURED", "STOPPED"); }

                var m = s.metrics || {};
                if (G.fps) G.fps.textContent = (m.fps != null ? m.fps : "—") + " fps";
                if (G.latency) G.latency.textContent = m.inference_ms != null ? m.inference_ms + " ms" : "—";
                if (G.e2e) G.e2e.textContent = m.e2e_ms != null ? m.e2e_ms + " ms" : "—";
                if (G.frameAge) G.frameAge.textContent = m.frame_age_ms != null ? m.frame_age_ms + " ms" : "—";

                // Per-view ROI readouts (the active view's ROI is the live one).
                setRoiReadout("front"); setRoiReadout("back");
                if (s.roi) {
                    state.hasSaved[s.active_view || state.view] = true;
                    setRoiReadout("front"); setRoiReadout("back");
                }

                renderResult(s, st, running, cam);
                renderDetections(s);
                refreshControlState();
                updateHint();
            })
            .catch(function () { /* keep last good */ })
            .finally(function () { inFlight = null; });
        return inFlight;
    }

    function renderResult(s, st, running, cam) {
        if (!G.result || !G.resultState) return; // workflow owns the overall result
        var label = (s.result && s.result.label) || "";
        var cls = "state-WAITING", icon = "&#9711;", stateTxt = "WAITING", note = "Select a part and start the camera.";
        if (st === "ERROR") { cls = "state-NOT_OK"; icon = "&#9888;"; stateTxt = "ERROR"; note = s.error || "Inspection error."; }
        else if (cam !== "ON") { cls = "state-WAITING"; icon = "&#9711;"; stateTxt = "WAITING"; note = "Waiting for camera."; }
        else if (!G.part || !G.part.value) { cls = "state-WAITING"; icon = "&#9711;"; stateTxt = "WAITING"; note = "Select a part."; }
        else if (!s.model_configured) { cls = "state-INFO"; icon = "&#9881;"; stateTxt = "MODEL NOT CONFIGURED"; note = "ROI READY — no inspection model configured. Configure a segmentation model to get OK / NOT OK results."; }
        else if (running) { cls = "state-INFO"; icon = "&#9678;"; stateTxt = "INSPECTION RUNNING"; note = label || "Processing cropped ROI."; }
        else { cls = "state-WAITING"; icon = "&#9635;"; stateTxt = "READY"; note = "Press Start Inspection."; }
        G.result.className = "ws-result " + cls;
        G.resultIcon.innerHTML = icon;
        G.resultState.textContent = stateTxt;
        G.resultNote.textContent = note;
    }

    function renderDetections(s) {
        if (!G.detBody) return; // workflow owns the stitch detection tables
        var dets = (s.result && s.result.detections) || [];
        if (!dets.length) { G.detBody.innerHTML = '<tr><td colspan="3" class="ws-empty">No inspection result yet. Capture or run inspection to populate.</td></tr>'; return; }
        G.detBody.innerHTML = dets.map(function (d) {
            var conf = (d.confidence != null) ? d.confidence.toFixed ? d.confidence.toFixed(2) : d.confidence : "—";
            var cls = d["class"] || d.class_name || "—";
            return "<tr><td>" + esc(cls) + "</td><td>" + conf + "</td><td><span class=\"ws-status-pill neutral\">—</span></td></tr>";
        }).join("");
    }

    function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]; }); }

    function refreshControlState() {
        var part = G.part && G.part.value;
        if (G.startCam) G.startCam.disabled = state.running || state.cameraOn;
        if (G.stopCam) G.stopCam.disabled = !state.cameraOn;
        if (G.capture) G.capture.disabled = !state.cameraOn || state.running;
        if (G.startInsp) G.startInsp.disabled = state.running || !part || !state.cameraOn || !state.hasSaved[state.view];
        if (G.stopInsp) G.stopInsp.disabled = !state.running;
        VIEWS.forEach(function (v) {
            if (E[v].edit) E[v].edit.disabled = state.running || !state.hasSaved[v];
            if (E[v].save) E[v].save.disabled = state.running;
        });
        updateHint();
    }

    function updateHint() {
        var part = G.part && G.part.value;
        var t;
        if (!part) t = "Step 1 — Select a part number.";
        else if (!state.cameraOn) t = "Step 2 — Press Start to open the camera.";
        else if (state.running) t = "Inspection running — processing the saved ROI.";
        else if (!state.rois[state.view] && !state.pending) t = "Step 3 — Choose Front/Back, draw an ROI, then Save.";
        else if (!state.hasSaved[state.view]) t = "Step 4 — Save the ROI to start continuous inspection.";
        else t = "ROI ready — press Start Inspection. (No model configured yet.)";
        if (G.hint) G.hint.textContent = t;
    }
    function showHint(m) { if (G.hint) G.hint.textContent = m; }

    // Recalculate camera canvas/coordinate mapping after any layout change.
    var recalcRaf = null;
    function recalcCanvases() {
        if (recalcRaf) cancelAnimationFrame(recalcRaf);
        recalcRaf = requestAnimationFrame(function () {
            VIEWS.forEach(function (v) { sizeCanvas(v); });
            refreshResolution();
        });
    }

    function isFullscreen() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }

    function toggleFullscreen() {
        var root = document.getElementById("wsApp") || document.documentElement;
        try {
            if (!isFullscreen()) {
                (root.requestFullscreen || root.webkitRequestFullscreen || function () {}).call(root);
            } else {
                (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
            }
        } catch (e) { showError("Fullscreen was blocked by the browser."); }
    }

    /** Fullscreen a single camera card (front or back). */
    function fullscreenView(v) {
        var card = E[v] && E[v].card;
        if (!card) return;
        try {
            if (document.fullscreenElement === card) {
                (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
            } else {
                if (document.fullscreenElement) {
                    (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
                }
                (card.requestFullscreen || card.webkitRequestFullscreen || function () {}).call(card);
            }
        } catch (e) { showError("Fullscreen was blocked by the browser."); }
    }

    /** Model status card + part-information rows, filled from real state only. */
    function renderModelCard(info) {
        if (!G.modelCard) return;
        var loaded = !!(info && info.loaded);
        G.modelCard.className = "ws-model-status " + (loaded ? "is-loaded" : (info && info.error ? "is-error" : "is-idle"));
        if (G.modelCardTitle) {
            G.modelCardTitle.textContent = loaded ? "Model Found & Loaded"
                : (info && info.error ? "Model Missing / Invalid" : "Model not validated");
        }
        if (G.modelName) G.modelName.textContent = (info && info.name) || "—";
        if (G.modelVersion) G.modelVersion.textContent = (info && info.version) || "—";
        if (G.modelClasses) {
            G.modelClasses.textContent = (info && info.num_classes != null)
                ? String(info.num_classes)
                : "—";
        }
        if (G.infoModel) G.infoModel.textContent = (info && info.name) || "—";
    }

    function onFullscreenChange() {
        // ROI/size must be recomputed after entering/exiting and while resizing.
        recalcCanvases();
        if (G.fs) {
            G.fs.innerHTML = isFullscreen() ? "&#9974; Exit Fullscreen" : "&#9974; Fullscreen";
        }
    }

    function startClock() {
        function tick() {
            var d = new Date();
            var pad = function (n) { return (n < 10 ? "0" : "") + n; };
            if (G.clock) G.clock.textContent = d.toLocaleDateString() + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
        }
        tick(); setInterval(tick, 1000);
    }

    function toggleCamIdx() {
        var src = G.camSrc ? G.camSrc.value : "opencv";
        var opt = camOpt(src);
        var needs = opt ? opt.needs_index !== false : src === "opencv";
        if (G.camIdxField) G.camIdxField.style.display = needs ? "" : "none";
    }

    function init() {
        // View toggle buttons
        document.querySelectorAll(".ws-view-toggle button").forEach(function (b) {
            b.addEventListener("click", function () { setActiveView(b.getAttribute("data-view")); });
        });
        // Per-camera fullscreen buttons in the navy camera headers.
        document.querySelectorAll(".ws-cam-fs").forEach(function (b) {
            b.addEventListener("click", function () { fullscreenView(b.getAttribute("data-view")); });
        });
        // Canvas drawing on active view
        VIEWS.forEach(function (v) {
            if (!E[v].canvas) return;
            E[v].canvas.addEventListener("pointerdown", function (e) { onDown(v, e); });
            E[v].canvas.addEventListener("pointermove", function (e) { onMove(v, e); });
            E[v].canvas.addEventListener("pointerup", function () { onUp(v); });
            E[v].canvas.addEventListener("pointercancel", function () { onUp(v); });
        });

        if (G.startCam) G.startCam.addEventListener("click", function () { withBusy(G.startCam, startCamera); });
        if (G.stopCam) G.stopCam.addEventListener("click", function () { withBusy(G.stopCam, stopCamera); });
        if (G.capture) G.capture.addEventListener("click", function () { withBusy(G.capture, captureFrame); });
        if (G.startInsp) G.startInsp.addEventListener("click", function () { withBusy(G.startInsp, startInspection); });
        if (G.stopInsp) G.stopInsp.addEventListener("click", function () { withBusy(G.stopInsp, stopInspection); });
        if (G.fs) G.fs.addEventListener("click", toggleFullscreen);
        if (G.part) G.part.addEventListener("change", function () { VIEWS.forEach(function (v) { state.rois[v] = null; state.hasSaved[v] = false; setRoiReadout(v); reloadView(v); }); refreshControlState(); });
        if (G.camSrc) G.camSrc.addEventListener("change", toggleCamIdx);
        VIEWS.forEach(function (v) {
            if (E[v].save) E[v].save.addEventListener("click", function () { withBusy(E[v].save, function () { return saveRoi(v); }); });
            if (E[v].reset) E[v].reset.addEventListener("click", resetRoi);
            if (E[v].edit) E[v].edit.addEventListener("click", editRoi);
        });

        var rz;
        window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(recalcCanvases, 120); });
        document.addEventListener("fullscreenchange", onFullscreenChange);
        document.addEventListener("webkitfullscreenchange", onFullscreenChange);
        window.addEventListener("pagehide", function () { if (timer) clearInterval(timer); });

        toggleCamIdx();
        setActiveView("front");
        startClock();
        refreshControlState();
        poll(true);
        if (timer) clearInterval(timer);
        timer = setInterval(function () { poll(false); }, 1500);
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
