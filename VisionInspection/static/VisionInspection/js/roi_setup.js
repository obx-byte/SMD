/**
 * VisionInspection — dedicated ROI Setup controller.
 *
 * This is the ONLY screen that can draw or change the common ROI of a part.
 * Pointer coordinates are mapped from the rendered image (which is scaled by
 * CSS to fit the stage) back into the camera's source-frame pixel system, so
 * the stored rectangle always matches the real image it was drawn on.
 *
 * The camera is never started automatically: the operator presses Start.
 */
(function () {
    "use strict";

    const cfg = window.__VI_ROI_SETUP__;
    if (!cfg) return;

    const el = (id) => document.getElementById(id);
    const ui = {
        start: el("rsStartCam"), stop: el("rsStopCam"), source: el("rsCameraSource"),
        index: el("rsCameraIndex"), side: el("rsSide"), feed: el("rsFeed"),
        canvas: el("rsCanvas"), stage: el("rsStage"), overlay: el("rsOverlay"),
        state: el("rsCamState"), draw: el("rsDraw"), preview: el("rsPreview"),
        save: el("rsSave"), reset: el("rsReset"), coords: el("rsCoords"),
        cancel: el("rsCancel"),
        x: el("rsX"), y: el("rsY"), w: el("rsW"), h: el("rsH"), ref: el("rsRef"),
        cropWrap: el("rsCropWrap"), crop: el("rsCrop"), message: el("rsMessage"),
        savedBody: el("rsSavedBody"), savedEmpty: el("rsSavedEmpty"),
        hint: el("rsCameraHint"), backConfig: el("rsBackConfig"),
        configStatus: el("rsConfigStatus"),
    };

    const state = {
        running: false,
        frameReady: false,
        frameWidth: 0,
        frameHeight: 0,
        draft: null,
        saved: null,
        dirty: false,
        saving: false,
        cameraGeneration: 0,
        ownsCamera: false,
        activeSide: ui.side.value,
        drag: null,
        poll: null,
        display: { width: 0, height: 0, offsetX: 0, offsetY: 0, dpr: 1 },
    };

    function csrf() { return cfg.csrf || ""; }

    function message(text, tone) {
        ui.message.textContent = text || "";
        ui.message.className = "rs-message" + (tone ? " " + tone : "");
    }

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    async function post(url, payload) {
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify(payload || {}),
        });
        let data = {};
        try { data = await res.json(); } catch (e) { data = {}; }
        return { ok: res.ok, status: res.status, data };
    }

    async function get(url) {
        const res = await fetch(url, { headers: { Accept: "application/json" } });
        let data = {};
        try { data = await res.json(); } catch (e) { data = {}; }
        if (!res.ok) {
            throw new Error(data.error || data.message || "Request failed (" + res.status + ").");
        }
        if (data.ok === false) {
            throw new Error(data.error || data.message || "The request was rejected.");
        }
        return { ok: true, data };
    }

    // ------------------------------------------------------------------ #
    //  Camera
    // ------------------------------------------------------------------ #
    function sourceId() {
        const option = ui.source.options[ui.source.selectedIndex];
        return option ? option.value : "opencv";
    }

    function syncCameraRole() {
        const back = ui.side.value === "back";
        ui.source.value = back ? cfg.backCameraSource : cfg.frontCameraSource;
        ui.index.value = back ? cfg.backCameraIndex : cfg.frontCameraIndex;
        state.activeSide = ui.side.value;
    }

    function setCameraUi(status, error) {
        state.running = status === "ON";
        ui.state.textContent = status || "OFF";
        ui.state.className = "rs-badge " + (status === "ON" ? "on" : status === "ERROR" ? "off" : "off");
        const starting = status === "CONNECTING";
        ui.start.disabled = state.running || starting || !cfg.cameraCompatible;
        ui.stop.disabled = !(state.running || starting);
        ui.stop.textContent = state.ownsCamera
            ? "■ Stop Camera"
            : (state.running || starting ? "■ Stop Preview" : "■ Stop Camera");
        ui.draw.disabled = !(state.frameReady && cfg.cameraCompatible);
        ui.overlay.style.display = state.running && state.frameReady ? "none" : "grid";
        ui.feed.style.display = state.running ? "block" : "none";
        if (!state.running || !state.frameReady) {
            ui.overlay.textContent = error || "Camera stopped";
        }
        showCoords(state.draft || state.saved);
    }

    async function startCamera() {
        if (!cfg.cameraCompatible) {
            message(cfg.cameraCompatibilityError, "error");
            return;
        }
        message("Starting the camera…");
        const generation = ++state.cameraGeneration;
        state.frameReady = false;
        setCameraUi("CONNECTING", "Waiting for a valid camera frame…");
        const params = new URLSearchParams({
            source: sourceId(),
            camera_index: ui.index.value || "0",
            reuse_if_active: "1",
        });
        try {
            const res = await fetch(cfg.urls.cameraStart + "?" + params.toString());
            const data = await res.json();
            if (!res.ok || data.status === "ERROR") {
                throw new Error(data.error || "The camera could not be started.");
            }
            state.ownsCamera = data.started_by_request === true;
            ui.feed.src = cfg.urls.cameraStream + "?source=" + encodeURIComponent(sourceId());
            startPolling(generation);
            const ready = await waitForFrame(generation);
            if (!ready) return;
            message("Camera frame is ready. Drag inside the ROI to move it, use a handle to resize, or choose Draw ROI.");
        } catch (e) {
            if (generation !== state.cameraGeneration) return;
            state.frameReady = false;
            setCameraUi("ERROR", "Camera error: " + e.message);
            message("Camera error: " + e.message, "error");
        }
    }

    async function stopCamera() {
        state.cameraGeneration += 1;
        stopPolling();
        if (!state.ownsCamera) {
            const sharedCameraRunning = state.running || ui.state.textContent === "CONNECTING";
            state.frameReady = false;
            state.drag = null;
            ui.feed.removeAttribute("src");
            ui.feed.style.display = "none";
            setCameraUi("OFF");
            message(sharedCameraRunning
                ? "Preview stopped. The shared camera remains active for its current workflow."
                : "Camera preview stopped.");
            return;
        }
        const res = await fetch(cfg.urls.cameraStop);
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.error || "The camera could not be stopped.");
        }
        const data = await res.json();
        if (data.status === "ERROR") {
            throw new Error("The camera service reported an error while stopping.");
        }
        state.frameReady = false;
        state.ownsCamera = false;
        state.drag = null;
        ui.feed.removeAttribute("src");
        ui.feed.style.display = "none";
        setCameraUi("OFF");
        message("Camera stopped.");
    }

    async function refreshResolution() {
        const res = await get(cfg.urls.resolution);
        if (res.data.width > 0 && res.data.height > 0) {
            setFrameSize(res.data.width, res.data.height);
            state.frameReady = true;
            setCameraUi("ON");
            showCoords(state.draft || state.saved);
            return true;
        }
        return false;
    }

    function delay(milliseconds) {
        return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    async function waitForFrame(generation) {
        const deadline = Date.now() + 20000;
        let lastStatus = "CONNECTING";
        while (Date.now() < deadline) {
            if (generation !== state.cameraGeneration) return false;
            const result = await get(cfg.urls.cameraStatus);
            const data = result.data || {};
            lastStatus = data.status || lastStatus;
            if (lastStatus === "ERROR") {
                throw new Error(data.error || "The camera could not provide a frame.");
            }
            if (lastStatus === "ON" && data.width > 0 && data.height > 0) {
                setFrameSize(data.width, data.height);
                state.frameReady = true;
                setCameraUi("ON");
                showCoords(state.draft || state.saved);
                return true;
            }
            await delay(400);
        }
        throw new Error(lastStatus === "ON"
            ? "The camera connected but has not produced a valid frame yet. Stop it and retry."
            : "Camera connection timed out. Check the camera source and try again.");
    }

    function startPolling(generation) {
        if (state.poll) return;
        state.poll = setInterval(async () => {
            if (document.hidden || generation !== state.cameraGeneration) return;
            try {
                const res = await get(cfg.urls.cameraStatus);
                if (generation !== state.cameraGeneration) return;
                const data = res.data;
                if (data.status !== "ON") {
                    stopPolling();
                    state.frameReady = false;
                    setCameraUi(data.status || "OFF", data.error || "The camera stopped.");
                    message(data.error || "The camera stopped.", "error");
                    return;
                }
                if (data.width > 0 && data.height > 0) {
                    if (data.width !== state.frameWidth || data.height !== state.frameHeight) {
                        setFrameSize(data.width, data.height);
                    }
                    state.frameReady = true;
                    setCameraUi("ON");
                    showCoords(state.draft || state.saved);
                }
            } catch (e) {
                if (generation === state.cameraGeneration) {
                    message("Could not check camera status: " + e.message, "error");
                }
            }
        }, 1500);
    }

    function stopPolling() {
        if (state.poll) { clearInterval(state.poll); state.poll = null; }
    }

    // ------------------------------------------------------------------ #
    //  Coordinate mapping: display pixels  <->  source-frame pixels
    // ------------------------------------------------------------------ #
    function setFrameSize(width, height) {
        width = Math.round(Number(width));
        height = Math.round(Number(height));
        if (width <= 0 || height <= 0) return;
        const oldWidth = state.frameWidth;
        const oldHeight = state.frameHeight;
        if (oldWidth > 0 && oldHeight > 0 && (oldWidth !== width || oldHeight !== height)) {
            const scaleRect = (rect) => rect && ({
                x: rect.x * width / oldWidth,
                y: rect.y * height / oldHeight,
                width: rect.width * width / oldWidth,
                height: rect.height * height / oldHeight,
            });
            state.saved = scaleRect(state.saved);
            state.draft = scaleRect(state.draft);
        }
        state.frameWidth = width;
        state.frameHeight = height;
        ui.ref.textContent = width + "x" + height;
        resizeCanvas();
        showCoords(state.draft || state.saved);
    }

    function resizeCanvas() {
        const stageWidth = ui.stage.clientWidth;
        const stageHeight = ui.stage.clientHeight;
        if (!stageWidth || !stageHeight || !state.frameWidth || !state.frameHeight) return;
        const scale = Math.min(stageWidth / state.frameWidth, stageHeight / state.frameHeight);
        const displayWidth = Math.round(state.frameWidth * scale);
        const displayHeight = Math.round(state.frameHeight * scale);
        const dpr = window.devicePixelRatio || 1;
        ui.canvas.width = Math.round(displayWidth * dpr);
        ui.canvas.height = Math.round(displayHeight * dpr);
        ui.canvas.style.width = displayWidth + "px";
        ui.canvas.style.height = displayHeight + "px";
        ui.feed.style.width = displayWidth + "px";
        ui.feed.style.height = displayHeight + "px";
        state.display = { width: displayWidth, height: displayHeight, offsetX: 0, offsetY: 0, dpr: dpr };
        redraw();
    }

    function pointerToCanvas(event) {
        const rect = ui.canvas.getBoundingClientRect();
        const scaleX = ui.canvas.width / rect.width;
        const scaleY = ui.canvas.height / rect.height;
        return {
            x: (event.clientX - rect.left) * scaleX,
            y: (event.clientY - rect.top) * scaleY,
        };
    }

    function canvasToSource(point) {
        const scale = state.frameWidth / (ui.canvas.width || 1);
        return { x: Math.round(point.x * scale), y: Math.round(point.y * scale) };
    }

    function sourceToCanvas(point) {
        const scale = (ui.canvas.width || 1) / state.frameWidth;
        return { x: point.x * scale, y: point.y * scale };
    }

    function clampRect(rect, minimumSize) {
        const minimum = minimumSize || 0;
        const x = Math.max(0, Math.min(rect.x, state.frameWidth));
        const y = Math.max(0, Math.min(rect.y, state.frameHeight));
        return {
            x: x,
            y: y,
            width: Math.max(minimum, Math.min(rect.width, state.frameWidth - x)),
            height: Math.max(minimum, Math.min(rect.height, state.frameHeight - y)),
        };
    }

    function redraw() {
        const ctx = ui.canvas.getContext("2d");
        if (!ctx) return;
        ctx.clearRect(0, 0, ui.canvas.width, ui.canvas.height);
        const rect = state.draft || state.saved;
        if (!rect || !state.frameWidth) return;
        const topLeft = sourceToCanvas({ x: rect.x, y: rect.y });
        const bottomRight = sourceToCanvas({ x: rect.x + rect.width, y: rect.y + rect.height });
        ctx.save();
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y);
        ctx.setLineDash([]);
        const middleX = (topLeft.x + bottomRight.x) / 2;
        const middleY = (topLeft.y + bottomRight.y) / 2;
        const handles = [
            [topLeft.x, topLeft.y], [middleX, topLeft.y], [bottomRight.x, topLeft.y],
            [topLeft.x, middleY], [bottomRight.x, middleY],
            [topLeft.x, bottomRight.y], [middleX, bottomRight.y], [bottomRight.x, bottomRight.y],
        ];
        ctx.fillStyle = "#ffffff";
        ctx.strokeStyle = "#0284c7";
        handles.forEach((point) => {
            ctx.beginPath();
            ctx.arc(point[0], point[1], 5 * state.display.dpr, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
        });
        ctx.restore();
    }

    function showCoords(rect) {
        if (!rect) {
            ui.x.textContent = ui.y.textContent = ui.w.textContent = ui.h.textContent = "—";
            ui.preview.disabled = true;
            ui.save.disabled = true;
            return;
        }
        ui.x.textContent = Math.round(rect.x);
        ui.y.textContent = Math.round(rect.y);
        ui.w.textContent = Math.round(rect.width);
        ui.h.textContent = Math.round(rect.height);
        ui.preview.disabled = !(state.frameReady && rect.width > 2 && rect.height > 2);
        ui.save.disabled = !(
            state.frameReady && cfg.cameraCompatible && state.dirty &&
            rect.width > 2 && rect.height > 2 && !state.saving
        );
    }

    // ------------------------------------------------------------------ #
    //  Drawing
    // ------------------------------------------------------------------ #
    function onPointerDown(event) {
        if (!state.frameReady || !cfg.cameraCompatible) return;
        event.preventDefault();
        const point = canvasToSource(pointerToCanvas(event));
        if (ui.draw.dataset.mode === "on") {
            state.drag = { mode: "draw", start: point };
            state.draft = null;
            ui.canvas.style.cursor = "crosshair";
        } else {
            const rect = state.draft || state.saved;
            if (!rect) return;
            const handle = hitHandle(point, rect);
            if (handle) {
                state.drag = { mode: "resize", handle: handle, start: point, rect: Object.assign({}, rect) };
                ui.canvas.style.cursor = resizeCursor(handle);
            } else if (point.x >= rect.x && point.x <= rect.x + rect.width &&
                point.y >= rect.y && point.y <= rect.y + rect.height) {
                state.drag = { mode: "move", start: point, rect: Object.assign({}, rect) };
                ui.canvas.style.cursor = "move";
            } else {
                return;
            }
            state.draft = Object.assign({}, rect);
            state.dirty = true;
        }
        ui.canvas.setPointerCapture && ui.canvas.setPointerCapture(event.pointerId);
        showCoords(state.draft || state.saved);
    }

    function onPointerMove(event) {
        if (!state.drag) return;
        const point = canvasToSource(pointerToCanvas(event));
        const drag = state.drag;
        if (drag.mode === "draw") {
            state.draft = clampRect({
                x: Math.min(drag.start.x, point.x),
                y: Math.min(drag.start.y, point.y),
                width: Math.abs(point.x - drag.start.x),
                height: Math.abs(point.y - drag.start.y),
            });
            state.dirty = true;
        } else if (drag.mode === "move") {
            const rect = drag.rect;
            state.draft = clampRect({
                x: Math.max(0, Math.min(state.frameWidth - rect.width, rect.x + point.x - drag.start.x)),
                y: Math.max(0, Math.min(state.frameHeight - rect.height, rect.y + point.y - drag.start.y)),
                width: rect.width,
                height: rect.height,
            });
        } else {
            state.draft = resizeRect(drag.rect, drag.handle, point);
        }
        showCoords(state.draft);
        redraw();
    }

    function onPointerUp() {
        if (!state.drag) return;
        const mode = state.drag.mode;
        state.drag = null;
        ui.canvas.style.cursor = ui.draw.dataset.mode === "on" ? "crosshair" : "move";
        if (mode === "draw") {
            ui.draw.dataset.mode = "off";
            ui.draw.textContent = "✎ Draw ROI";
            if (!state.draft || state.draft.width <= 2 || state.draft.height <= 2) {
                state.draft = null;
                state.dirty = false;
                message("The ROI is too small. Draw a larger rectangle.", "error");
            } else {
                message("ROI drawn. Drag inside it to move, use a handle to resize, or save it.");
            }
        }
        showCoords(state.draft || state.saved);
        redraw();
    }

    function handlePoints(rect) {
        const left = rect.x;
        const right = rect.x + rect.width;
        const top = rect.y;
        const bottom = rect.y + rect.height;
        const middleX = (left + right) / 2;
        const middleY = (top + bottom) / 2;
        return {
            nw: { x: left, y: top }, n: { x: middleX, y: top }, ne: { x: right, y: top },
            w: { x: left, y: middleY }, e: { x: right, y: middleY },
            sw: { x: left, y: bottom }, s: { x: middleX, y: bottom }, se: { x: right, y: bottom },
        };
    }

    function hitHandle(point, rect) {
        const threshold = 14 * state.frameWidth / Math.max(1, state.display.width);
        let closest = null;
        let distance = threshold;
        Object.keys(handlePoints(rect)).forEach((name) => {
            const handle = handlePoints(rect)[name];
            const current = Math.hypot(point.x - handle.x, point.y - handle.y);
            if (current <= distance) {
                closest = name;
                distance = current;
            }
        });
        return closest;
    }

    function resizeCursor(handle) {
        return ({ n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize",
            ne: "nesw-resize", sw: "nesw-resize", nw: "nwse-resize", se: "nwse-resize" })[handle] || "move";
    }

    function resizeRect(rect, handle, point) {
        let left = rect.x;
        let right = rect.x + rect.width;
        let top = rect.y;
        let bottom = rect.y + rect.height;
        if (handle.includes("w")) left = Math.min(Math.max(0, point.x), right - 3);
        if (handle.includes("e")) right = Math.max(Math.min(state.frameWidth, point.x), left + 3);
        if (handle.includes("n")) top = Math.min(Math.max(0, point.y), bottom - 3);
        if (handle.includes("s")) bottom = Math.max(Math.min(state.frameHeight, point.y), top + 3);
        return { x: left, y: top, width: right - left, height: bottom - top };
    }

    async function previewCrop() {
        if ((!state.draft && !state.saved) || !state.frameReady) return;
        const rect = state.draft || state.saved;
        message("Rendering the ROI preview…");
        try {
            const result = await post(cfg.urls.cameraCapture, {});
            const data = result.data || {};
            if (!result.ok || !data.image) {
                throw new Error(data.error || "The camera has no frame to preview yet.");
            }
            const image = new Image();
            image.onload = function () {
                const scaleX = image.width / state.frameWidth;
                const scaleY = image.height / state.frameHeight;
                const canvas = document.createElement("canvas");
                canvas.width = Math.max(1, Math.round(rect.width * scaleX));
                canvas.height = Math.max(1, Math.round(rect.height * scaleY));
                const ctx = canvas.getContext("2d");
                ctx.drawImage(
                    image,
                    Math.round(rect.x * scaleX), Math.round(rect.y * scaleY),
                    canvas.width, canvas.height,
                    0, 0, canvas.width, canvas.height
                );
                ui.crop.src = canvas.toDataURL("image/jpeg", 0.9);
                ui.cropWrap.style.display = "block";
                message("ROI preview rendered.");
            };
            image.src = data.image;
        } catch (e) {
            message("Could not render the ROI preview: " + e.message, "error");
        }
    }

    // ------------------------------------------------------------------ #
    //  Persistence
    // ------------------------------------------------------------------ #
    async function loadSaved() {
        try {
            const res = await get(cfg.urls.roiGet);
            const data = res.data || {};
            if (data.saved && data.roi) {
                state.saved = {
                    x: data.roi.x, y: data.roi.y,
                    width: data.roi.width, height: data.roi.height,
                };
                if (!state.frameWidth || !state.frameHeight) {
                    state.frameWidth = data.roi.ref_width;
                    state.frameHeight = data.roi.ref_height;
                }
                renderSaved(data.roi);
                resizeCanvas();
                showCoords(state.saved);
                redraw();
            } else {
                renderSaved(null);
                if (data.message) message(data.message);
            }
        } catch (e) {
            renderSaved(null);
            message("Could not load the saved ROI: " + e.message, "error");
        }
    }

    function renderSaved(roi) {
        if (!roi) {
            ui.savedBody.innerHTML =
                '<p class="rs-empty" id="rsSavedEmpty">No common ROI has been saved for this part yet.</p>';
            return;
        }
        ui.savedBody.innerHTML =
            '<table class="rs-saved-table">' +
            "<tr><th>Version</th><td>v" + esc(roi.version) + "</td></tr>" +
            "<tr><th>Position</th><td>x " + esc(roi.x) + ", y " + esc(roi.y) + "</td></tr>" +
            "<tr><th>Size</th><td>" + esc(roi.width) + " x " + esc(roi.height) + " px</td></tr>" +
            "<tr><th>Source frame</th><td>" + esc(roi.ref_width) + " x " + esc(roi.ref_height) + " px</td></tr>" +
            "<tr><th>Saved by</th><td>" + esc(roi.configured_by || "—") + "</td></tr>" +
            "<tr><th>Updated</th><td>" + (roi.updated_at ? new Date(roi.updated_at).toLocaleString() : "—") + "</td></tr>" +
            "</table>";
    }

    async function saveRoi() {
        const rect = state.draft || state.saved;
        if (!rect) return;
        if (!cfg.cameraCompatible) {
            message(cfg.cameraCompatibilityError, "error");
            return;
        }
        if (!state.frameReady || !state.frameWidth || !state.frameHeight) {
            message("Start the camera first so the source frame size is known.", "error");
            return;
        }
        if (state.saving) return;
        state.saving = true;
        showCoords(rect);
        message("Saving the common ROI…");
        try {
            const result = await post(cfg.urls.roiSave, {
                configuration_id: cfg.configurationId,
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                ref_width: state.frameWidth,
                ref_height: state.frameHeight,
            });
            if (!result.ok || !result.data.ok) {
                throw new Error((result.data && result.data.error) || "The ROI could not be saved.");
            }
            state.saved = {
                x: result.data.roi.x, y: result.data.roi.y,
                width: result.data.roi.width, height: result.data.roi.height,
            };
            state.draft = null;
            state.dirty = false;
            renderSaved(result.data.roi);
            if (ui.configStatus && result.data.configuration_status) {
                ui.configStatus.textContent = result.data.configuration_status.replaceAll("_", " ");
                ui.configStatus.dataset.status = result.data.configuration_status;
                ui.configStatus.className = "rs-tag " + result.data.configuration_status.toLowerCase();
            }
            redraw();
            message(result.data.message || "ROI saved.", "ok");
        } catch (e) {
            message("Could not save the ROI: " + e.message, "error");
        } finally {
            state.saving = false;
            showCoords(state.draft || state.saved);
        }
    }

    function resetRoi() {
        state.draft = null;
        state.dirty = false;
        redraw();
        showCoords(state.saved);
        ui.cropWrap.style.display = "none";
        message(state.saved ? "Showing the saved ROI." : "ROI cleared.");
    }

    async function cancelSetup() {
        ui.cancel.disabled = true;
        try {
            await stopCamera();
            window.location.assign(cfg.urls.configuration);
        } catch (e) {
            ui.cancel.disabled = false;
            message("Could not stop the camera before leaving: " + e.message, "error");
        }
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #
    function init() {
        ui.start.addEventListener("click", startCamera);
        ui.stop.addEventListener("click", function () {
            stopCamera().catch((error) => message("Could not stop the camera: " + error.message, "error"));
        });
        ui.draw.addEventListener("click", function () {
            if (!state.frameReady || !cfg.cameraCompatible) return;
            const on = ui.draw.dataset.mode === "on";
            ui.draw.dataset.mode = on ? "off" : "on";
            ui.draw.textContent = on ? "✎ Draw ROI" : "✎ Drawing…";
            ui.canvas.style.cursor = on ? "move" : "crosshair";
            if (!on) {
                state.draft = null;
                state.dirty = true;
                showCoords(null);
                redraw();
            }
            message(on ? "Draw mode off." : "Drag on the preview to draw a new ROI.");
        });
        ui.preview.addEventListener("click", previewCrop);
        ui.save.addEventListener("click", saveRoi);
        ui.reset.addEventListener("click", resetRoi);
        ui.cancel.addEventListener("click", cancelSetup);
        ui.backConfig.addEventListener("click", function (event) {
            event.preventDefault();
            cancelSetup();
        });
        ui.side.addEventListener("change", function () {
            if (state.running) {
                ui.side.value = state.activeSide;
                message("Stop the camera before switching the inspection side.", "error");
                return;
            }
            syncCameraRole();
        });
        ui.canvas.addEventListener("pointerdown", onPointerDown);
        ui.canvas.addEventListener("pointermove", onPointerMove);
        ui.canvas.addEventListener("pointerup", onPointerUp);
        ui.canvas.addEventListener("pointerleave", onPointerUp);
        window.addEventListener("resize", resizeCanvas);
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) stopPolling();
            else if (state.running) startPolling(state.cameraGeneration);
        });
        ui.feed.addEventListener("load", function () {
            if (ui.feed.naturalWidth && ui.feed.naturalHeight) {
                setFrameSize(ui.feed.naturalWidth, ui.feed.naturalHeight);
                state.frameReady = true;
                setCameraUi("ON");
                showCoords(state.draft || state.saved);
            }
        });
        setCameraUi("OFF");
        showCoords(null);
        if (cfg.cameraCompatibilityError) {
            message(cfg.cameraCompatibilityError + " Configure matching cameras before saving a common ROI.", "error");
        }
        syncCameraRole();
        loadSaved();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
