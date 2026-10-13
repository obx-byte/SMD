/**
 * VisionInspection — Inspection Master Controller
 *
 * Live camera + draw-ROI + continuous inspection UI.  Reuses the existing
 * camera endpoints (start/stop/MJPEG stream) and the model-independent
 * continuous-inspection status/preview endpoints.
 */
(function () {
    "use strict";

    const cfg = window.__VI_MASTER__ || { urls: {} };
    const URL = cfg.urls;

    const el = {
        part: document.getElementById("mxPartSelect"),
        startCam: document.getElementById("mxStartCamBtn"),
        stopCam: document.getElementById("mxStopCamBtn"),
        startInsp: document.getElementById("mxStartInspBtn"),
        stopInsp: document.getElementById("mxStopInspBtn"),
        saveRoi: document.getElementById("mxSaveRoiBtn"),
        editRoi: document.getElementById("mxEditRoiBtn"),
        camWrap: document.getElementById("mxCamWrap"),
        camFeed: document.getElementById("mxCamFeed"),
        camOverlay: document.getElementById("mxCamOverlay"),
        canvas: document.getElementById("mxDrawCanvas"),
        roiX: document.getElementById("mxRoiX"),
        roiY: document.getElementById("mxRoiY"),
        roiW: document.getElementById("mxRoiW"),
        roiH: document.getElementById("mxRoiH"),
        refDims: document.getElementById("mxRefDims"),
        roiSaved: document.getElementById("mxRoiSaved"),
        error: document.getElementById("mxError"),
        stateBadge: document.getElementById("mxInspStateBadge"),
        stateText: document.getElementById("mxInspStateText"),
        stateInline: document.getElementById("mxStateInline"),
        camInline: document.getElementById("mxCamInline"),
        modelInline: document.getElementById("mxModelInline"),
        hint: document.getElementById("mxHint"),
        fps: document.getElementById("mxFps"),
        latency: document.getElementById("mxLatency"),
        e2e: document.getElementById("mxE2E"),
        frameAge: document.getElementById("mxFrameAge"),
        dropped: document.getElementById("mxDropped"),
        prediction: document.getElementById("mxPrediction"),
        predictionHint: document.getElementById("mxPredictionHint"),
        camSource: document.getElementById("mxCamSource"),
        camIndex: document.getElementById("mxCamIndex"),
        camIndexField: document.getElementById("mxCamIndexField"),
        resetRoi: document.getElementById("mxResetRoiBtn"),
    };

    // Local runtime state
    const state = {
        cameraOn: false,
        running: false,
        refW: 0,
        refH: 0,
        roi: null,            // {x,y,w,h} in pixels on ref frame
        hasSaved: false,
        drawing: false,
        dragStart: null,
        pendingRoi: null,
    };

    const POLL_MS = 1000;
    let pollTimer = null;
    let statusInFlight = null;
    let resizeRaf = null;

    // ------------------------------------------------------------------ #
    //  Helpers
    // ------------------------------------------------------------------ #
    const csrf = () => URL.csrfToken || (window.__VI_MASTER__ && window.__VI_MASTER__.csrfToken) || cfg.csrfToken || "";

    async function post(url, body) {
        const res = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrf(),
                "Accept": "application/json",
            },
            body: JSON.stringify(body || {}),
        });
        let data = {};
        try { data = await res.json(); } catch (e) { data = { error: "Bad response" }; }
        if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
        return data;
    }

    function setError(msg) { el.error.textContent = msg || ""; }
    function showError(msg) { setError(msg); }

    // ------------------------------------------------------------------ #
    //  Canvas & ROI drawing
    // ------------------------------------------------------------------ #
    function sizeCanvas() {
        const rect = el.camWrap.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        el.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
        el.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
        drawRoi();
    }

    // The <img> uses object-fit: contain, so the displayed image may be
    // letterboxed inside the 16/9 box.  This returns the actual CSS rectangle
    // of the visible image, accounting for scaling + letterbox offsets, so ROI
    // mapping stays accurate at any preview size and camera aspect ratio.
    function imageRectCss() {
        const rect = el.canvas.getBoundingClientRect();
        const cw = rect.width || 1;
        const ch = rect.height || 1;
        const fw = state.refW || cw;
        const fh = state.refH || ch;
        const boxAR = cw / ch;
        const imgAR = fw / fh;
        let dw, dh, dx, dy;
        if (imgAR > boxAR) {          // fit to width, letterbox top/bottom
            dw = cw; dh = cw / imgAR; dx = 0; dy = (ch - dh) / 2;
        } else {                      // fit to height, letterbox left/right
            dh = ch; dw = ch * imgAR; dy = 0; dx = (cw - dw) / 2;
        }
        return { dx: dx, dy: dy, dw: dw, dh: dh };
    }

    function canvasPoint(evt) {
        const rect = el.canvas.getBoundingClientRect();
        const r = imageRectCss();
        const px = evt.clientX - rect.left - r.dx;
        const py = evt.clientY - rect.top - r.dy;
        return {
            nx: Math.min(1, Math.max(0, px / (r.dw || 1))),
            ny: Math.min(1, Math.max(0, py / (r.dh || 1))),
        };
    }

    function drawRoi() {
        const ctx = el.canvas.getContext("2d");
        if (!ctx) return;
        const dpr = window.devicePixelRatio || 1;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, el.canvas.width, el.canvas.height);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        const r = imageRectCss();
        const X = (px) => r.dx + px / (state.refW || 1) * r.dw;
        const Y = (py) => r.dy + py / (state.refH || 1) * r.dh;

        [state.pendingRoi, state.roi].filter(Boolean).forEach((roi) => {
            const x = X(roi.x), y = Y(roi.y);
            const w = X(roi.x + roi.w) - x, h = Y(roi.y + roi.h) - y;
            const saved = roi === state.roi;

            ctx.fillStyle = "rgba(255,77,94,0.14)";
            ctx.fillRect(x, y, w, h);
            ctx.lineWidth = saved ? 2.5 : 2;
            ctx.strokeStyle = "#ff4d5e";                       // red ROI boundary
            if (!saved) ctx.setLineDash([6, 4]);
            ctx.strokeRect(x, y, w, h);
            ctx.setLineDash([]);

            // Corner resize handles (editable affordance)
            const hs = Math.max(9, Math.min(14, w * 0.18));
            ctx.fillStyle = "#ffffff";
            ctx.strokeStyle = "#ff4d5e";
            ctx.lineWidth = 2;
            [[x, y], [x + w - hs, y], [x, y + h - hs], [x + w - hs, y + h - hs]]
                .forEach((c) => ctx.strokeRect(c[0], c[1], hs, hs));
        });
    }

    function updateReadout() {
        const roi = state.pendingRoi || state.roi;
        if (roi) {
            el.roiX.textContent = Math.round(roi.x);
            el.roiY.textContent = Math.round(roi.y);
            el.roiW.textContent = Math.round(roi.w);
            el.roiH.textContent = Math.round(roi.h);
        } else {
            el.roiX.textContent = el.roiY.textContent = el.roiW.textContent = el.roiH.textContent = "—";
        }
    }

    function onPointerDown(e) {
        if (!state.cameraOn || state.running) return; // lock ROI while running
        e.preventDefault();
        el.canvas.setPointerCapture && el.canvas.setPointerCapture(e.pointerId);
        state.drawing = true;
        state.dragStart = canvasPoint(e);
        state.pendingRoi = null;
    }

    function onPointerMove(e) {
        if (!state.drawing) return;
        const cur = canvasPoint(e);
        const s = state.dragStart;
        const x = Math.min(s.nx, cur.nx) * (state.refW || 0);
        const y = Math.min(s.ny, cur.ny) * (state.refH || 0);
        const w = Math.abs(cur.nx - s.nx) * (state.refW || 0);
        const h = Math.abs(cur.ny - s.ny) * (state.refH || 0);
        state.pendingRoi = { x, y, w, h };
        drawRoi();
        updateReadout();
    }

    function onPointerUp() {
        if (!state.drawing) return;
        state.drawing = false;
        const roi = state.pendingRoi;
        if (!roi || roi.w < 4 || roi.h < 4) {
            state.pendingRoi = null;
            drawRoi();
            updateReadout();
            return;
        }
        el.saveRoi.disabled = false; // a valid ROI is ready to save
    }

    // ------------------------------------------------------------------ #
    //  Camera
    // ------------------------------------------------------------------ #
    async function startCamera() {
        setError("");
        // Hide any previous stream while we attempt (re)connect.
        el.camFeed.style.display = "none";
        if (!el.camSource || !el.camSource.value) { showError("Select a camera source."); return; }
        const source = el.camSource.value;
        const camOpt = (cfg.capabilities && cfg.capabilities.sources || []).find((s) => s.id === source);
        if (camOpt && camOpt.available === false) {
            showError((camOpt.label || "Camera") + " is not available on this system.");
            return;
        }
        const index = el.camIndex ? el.camIndex.value : 0;
        let data = {};
        try {
            const res = await fetch(
                URL.cameraStart + "?source=" + encodeURIComponent(source) + "&camera_index=" + encodeURIComponent(index),
                { headers: { "X-CSRFToken": csrf() } }
            );
            try { data = await res.json(); } catch (e) { data = {}; }
        } catch (e) { /* status poll will reflect it */ }

        if (data.status === "ERROR") {
            state.cameraOn = false;
            el.camOverlay.textContent = (data.error || "Camera could not be started.");
            el.camOverlay.classList.remove("hidden");
            showError(data.error || "Camera could not be started.");
            el.startCam.disabled = false;
            el.stopCam.disabled = true;
            return;
        }

        state.cameraOn = true;
        el.camFeed.src = URL.cameraStream + "?t=" + Date.now();
        el.camFeed.style.display = "block";
        el.camOverlay.textContent = "Camera stopped";
        el.camOverlay.classList.add("hidden");
        el.startCam.disabled = true;
        el.stopCam.disabled = false;
        await refreshResolution();
        sizeCanvas();
        applyRunningState(state.running);
    }

    function toggleCamIndexField() {
        if (!el.camSource || !el.camIndexField) return;
        const source = el.camSource.value;
        const camOpt = (cfg.capabilities && cfg.capabilities.sources || []).find((s) => s.id === source);
        const needsIndex = camOpt ? camOpt.needs_index !== false : source === "opencv";
        el.camIndexField.style.display = needsIndex ? "" : "none";
    }

    function resetRoi() {
        if (state.running) { showError("Stop inspection before resetting the ROI."); return; }
        state.pendingRoi = null;
        state.roi = null;
        state.hasSaved = false;
        setError("");
        el.saveRoi.disabled = true;
        el.roiSaved.textContent = "—";
        drawRoi();
        updateReadout();
    }

    function stopCamera() {
        fetch(URL.cameraStop, { headers: { "X-CSRFToken": csrf() } }).catch(() => {});
        state.cameraOn = false;
        // Remove src AND hide so the browser never paints a broken-image icon.
        el.camFeed.removeAttribute("src");
        el.camFeed.style.display = "none";
        el.camOverlay.classList.remove("hidden");
        el.startCam.disabled = false;
        el.stopCam.disabled = true;
        applyRunningState(state.running);
    }

    async function refreshResolution() {
        try {
            const res = await fetch(URL.cameraResolution);
            const data = await res.json();
            if (data.width && data.height) {
                state.refW = data.width;
                state.refH = data.height;
            }
        } catch (e) { /* ignore */ }
        if (!state.refW || !state.refH) { state.refW = 1280; state.refH = 720; }
        el.refDims.textContent = state.refW + "×" + state.refH;
        drawRoi();
        updateReadout();
    }

    // ------------------------------------------------------------------ #
    //  Save ROI / start-stop / part switch
    // ------------------------------------------------------------------ #
    async function saveRoi() {
        setError("");
        const part = el.part.value;
        if (!part) { showError("Select a part number first."); return; }
        if (!state.refW || !state.refH) { showError("Camera resolution unknown — start the camera first."); return; }
        const roi = state.pendingRoi || state.roi;
        if (!roi) { showError("Draw an ROI on the live preview first."); return; }
        if (roi.x + roi.w > state.refW + 1 || roi.y + roi.h > state.refH + 1) {
            showError("ROI extends outside the camera frame.");
            return;
        }
        el.saveRoi.disabled = true;
        try {
            const status = await post(URL.saveRoi, {
                part_number: part,
                camera_view: state.view === "back" ? "back" : "front",
                x: roi.x, y: roi.y, width: roi.w, height: roi.h,
                ref_width: state.refW, ref_height: state.refH,
                camera_source: el.camSource ? el.camSource.value : "opencv",
                camera_index: el.camIndex ? el.camIndex.value : 0,
            });
            if (status.error) throw new Error(status.error);
            state.roi = { x: roi.x, y: roi.y, w: roi.w, h: roi.h };
            state.pendingRoi = null;
            state.hasSaved = true;
            if (status.state === "ERROR") {
                showError(status.error || "Inspection could not start.");
            } else {
                showError("");
            }
            drawRoi();
            updateReadout();
            el.roiSaved.textContent = Math.round(roi.w) + "×" + Math.round(roi.h) + " px";
            applyRunningState(status.running === true || status.state === "RUNNING" || status.state === "STARTING");
            refreshStatus();
        } catch (e) {
            showError(e.message);
            el.saveRoi.disabled = false;
        }
    }

    async function loadSavedRoi(part) {
        if (!part) { state.roi = null; state.hasSaved = false; drawRoi(); updateReadout(); return; }
        try {
            const res = await fetch(URL.roiGet + "?part_number=" + encodeURIComponent(part));
            const data = await res.json();
            if (data.saved) {
                state.refW = data.ref_width || state.refW;
                state.refH = data.ref_height || state.refH;
                state.roi = { x: data.x, y: data.y, w: data.width, h: data.height };
                state.hasSaved = true;
                el.roiSaved.textContent = data.width + "×" + data.height + " px";
            } else {
                state.roi = null; state.hasSaved = false; el.roiSaved.textContent = "—";
            }
        } catch (e) { /* ignore */ }
        el.refDims.textContent = state.refW + "×" + state.refH;
        drawRoi();
        updateReadout();
        applyRunningState(state.running);  // refresh Edit/Start enablement + hint
    }

    async function startInspection() {
        setError("");
        const part = el.part.value;
        if (!part) { showError("Select a part number first."); return; }
        if (!state.cameraOn) { showError("Start the camera first."); return; }
        try {
            const status = await post(URL.start, { part_number: part });
            if (status.error) throw new Error(status.error);
            applyRunningState(status.running === true);
            showError("");
            refreshStatus();
        } catch (e) { showError(e.message); }
    }

    async function stopInspection() {
        try {
            const status = await post(URL.stop, {});
            applyRunningState(status.running === true);
        } catch (e) { showError(e.message); }
    }

    async function onPartChange() {
        const part = el.part.value;
        setError("");
        // Stop any running loop for the previous part, then prefill the new part.
        if (state.running) { try { await post(URL.stop, {}); } catch (e) {} applyRunningState(false); }
        await loadSavedRoi(part);
    }

    // ------------------------------------------------------------------ #
    //  Status polling (single timer; no overlap)
    // ------------------------------------------------------------------ #
    function applyRunningState(running) {
        state.running = running;
        el.startInsp.disabled = running;
        el.stopInsp.disabled = !running;
        el.saveRoi.disabled = running;
        el.editRoi.disabled = running || !state.hasSaved;  // edit only once an ROI exists
        el.startCam.disabled = running || state.cameraOn;
        updateHint();
    }

    // Workflow feedback — reflects the actual current state.
    function updateHint() {
        if (!el.hint) return;
        var part = el.part && el.part.value;
        var text;
        if (!part) text = "Step 1 — Select a part number.";
        else if (!state.cameraOn) text = "Step 2 — Start the camera.";
        else if (state.running) text = "Inspection running — processing the saved ROI.";
        else if (!state.hasSaved && !state.roi) text = "Step 3 — Drag on the preview to draw, then Save ROI.";
        else if (!state.hasSaved) text = "Step 4 — Save the ROI to start continuous inspection.";
        else text = "ROI ready — start inspection. No model configured yet (results unavailable).";
        if (state.running) text = "Inspection running — processing the saved ROI.";
        el.hint.textContent = text;
    }

    function setBadge(node, text, key) {
        node.classList.remove("STOPPED", "STARTING", "RUNNING", "ERROR", "ON", "OFF", "CONNECTING");
        node.classList.add(key);
        node.textContent = text;
    }

    async function refreshStatus() {
        if (statusInFlight) return;
        statusInFlight = fetch(URL.status, { headers: { Accept: "application/json" } })
            .then((r) => r.json())
            .then((s) => {
                const st = s.state || "STOPPED";
                const running = s.running === true;
                var part = el.part && el.part.value;
                // header + inline status
                el.stateText.textContent = st;
                setBadge(el.stateBadge, st, st);
                setBadge(el.stateInline, st, st);
                const cam = s.camera_status || "OFF";
                setBadge(el.camInline, cam, cam);
                state.cameraOn = cam === "ON";
                el.startCam.disabled = running || state.cameraOn;
                el.stopCam.disabled = !state.cameraOn;

                if (s.model_configured) {
                    el.modelInline.classList.remove("STOPPED");
                    el.modelInline.classList.add("RUNNING");
                    el.modelInline.textContent = "CONFIGURED";
                } else {
                    setBadge(el.modelInline, "NOT CONFIGURED", "STOPPED");
                }

                const m = s.metrics || {};
                el.fps.textContent = (m.fps != null ? m.fps : "—") + " fps";
                el.latency.textContent = m.inference_ms != null ? m.inference_ms + " ms" : "—";
                el.e2e.textContent = m.e2e_ms != null ? m.e2e_ms + " ms" : "—";
                el.frameAge.textContent = m.frame_age_ms != null ? m.frame_age_ms + " ms" : "—";
                el.dropped.textContent = m.dropped != null ? m.dropped : "—";

                if (s.roi) {
                    el.roiSaved.textContent = s.roi.width + "×" + s.roi.height + " px";
                    state.hasSaved = true;
                    if (!state.roi) state.roi = { x: s.roi.x, y: s.roi.y, w: s.roi.width, h: s.roi.height };
                }

                const res = s.result || {};
                el.prediction.textContent = res.label || "—";
                el.predictionHint.textContent = s.model_configured
                    ? "Live inference on the cropped ROI."
                    : "A YOLO model will be attached later. No predictions are fabricated.";

                if (s.error) setError(s.error);
                if (running !== state.running) applyRunningState(running);
                // keep Edit + hint in sync with live ROI/camera state
                el.editRoi.disabled = running || !state.hasSaved;
                el.startInsp.disabled = running || !part || !state.cameraOn;
                updateHint();
            })
            .catch(() => { /* keep last good status */ })
            .finally(() => { statusInFlight = null; });
        return statusInFlight;
    }

    function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(refreshStatus, POLL_MS);
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #
    function init() {
        // Never paint a broken-image icon before a stream is attached.
        el.camFeed.style.display = "none";
        el.camFeed.addEventListener("error", () => {
            el.camFeed.style.display = "none";
            el.camOverlay.textContent = "Camera stream could not be loaded. Check the camera endpoint.";
            el.camOverlay.classList.remove("hidden");
        });
        el.camFeed.addEventListener("load", () => {
            if (state.cameraOn) el.camOverlay.classList.add("hidden");
        });

        el.startCam.addEventListener("click", startCamera);
        el.stopCam.addEventListener("click", stopCamera);
        el.saveRoi.addEventListener("click", saveRoi);
        el.startInsp.addEventListener("click", startInspection);
        el.stopInsp.addEventListener("click", stopInspection);
        el.part.addEventListener("change", onPartChange);
        if (el.camSource) {
            el.camSource.addEventListener("change", toggleCamIndexField);
            toggleCamIndexField();
        }
        if (el.resetRoi) el.resetRoi.addEventListener("click", resetRoi);

        el.canvas.addEventListener("pointerdown", onPointerDown);
        el.canvas.addEventListener("pointermove", onPointerMove);
        el.canvas.addEventListener("pointerup", onPointerUp);
        el.canvas.addEventListener("pointercancel", onPointerUp);

        window.addEventListener("resize", () => {
            cancelAnimationFrame(resizeRaf);
            resizeRaf = requestAnimationFrame(sizeCanvas);
        });
        window.addEventListener("pagehide", () => { if (pollTimer) clearInterval(pollTimer); });

        sizeCanvas();
        // Prefill saved ROI if a part is preselected via URL.
        if (el.part.value) loadSavedRoi(el.part.value);
        refreshStatus();
        startPolling();
    }

    // "Edit ROI" returns to editing mode (allow drawing again while not running)
    if (el.editRoi) {
        el.editRoi.addEventListener("click", () => {
            if (state.running) { showError("Stop inspection before editing the ROI."); return; }
            state.pendingRoi = null;
            setError("");
            drawRoi();
            updateReadout();
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
