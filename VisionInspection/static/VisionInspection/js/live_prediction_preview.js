(function () {
    "use strict";

    const page = document.getElementById("pcLivePreview");
    if (!page || !window.__VI_PART_CONFIG__) return;

    const config = window.__VI_PART_CONFIG__;
    const toggle = document.getElementById("pcPreviewToggle");
    const sideSelect = document.getElementById("pcPreviewSide");
    const modelSelect = document.getElementById("pcPreviewModel");
    const confidence = document.getElementById("pcPreviewConfidence");
    const confidenceValue = document.getElementById("pcPreviewConfidenceValue");
    const showMasks = document.getElementById("pcPreviewMasks");
    const showBoxes = document.getElementById("pcPreviewBoxes");
    const status = document.getElementById("pcPreviewStatus");
    const image = document.getElementById("pcPreviewImage");
    const placeholder = document.getElementById("pcPreviewPlaceholder");
    const summary = document.getElementById("pcPreviewSummary");
    const inference = document.getElementById("pcPreviewInference");
    const detections = document.getElementById("pcPreviewDetections");

    let running = false;
    let currentConfiguration = null;
    let lastFrameSeq = null;

    function setStatus(message, isError) {
        status.textContent = message;
        status.classList.toggle("is-error", Boolean(isError));
    }

    function stop(message) {
        running = false;
        lastFrameSeq = null;
        toggle.textContent = "Start Preview";
        if (message) setStatus(message, false);
    }

    async function loadConfiguration() {
        const response = await fetch(config.urls.read, { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || "Could not load the Part Vision configuration.");
        }
        const configuration = payload.configuration;
        if (!configuration) throw new Error("Save a Part Vision configuration before starting preview.");
        currentConfiguration = configuration;

        const sides = [];
        if (["FRONT_ONLY", "FRONT_BACK"].includes(configuration.inspection_mode)) {
            sides.push(["FRONT", "Front"]);
        }
        if (["BACK_ONLY", "FRONT_BACK"].includes(configuration.inspection_mode)) {
            sides.push(["BACK", "Back"]);
        }
        sideSelect.replaceChildren(...sides.map(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            return option;
        }));

        const models = (configuration.models || []).filter(
            (model) => model.enabled && model.task_type === "segment"
        );
        modelSelect.replaceChildren();
        for (const model of models) {
            const option = document.createElement("option");
            option.value = String(model.id);
            option.textContent = `${model.name}${model.version ? ` (${model.version})` : ""}`;
            modelSelect.append(option);
        }
        if (!models.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = "No enabled segmentation model";
            modelSelect.append(option);
        }
        modelSelect.disabled = models.length === 0;
    }

    function renderDetections(payload) {
        detections.replaceChildren();
        if (!payload.detections.length) {
            summary.textContent = "No detections";
            return;
        }

        const grouped = new Map();
        for (const detection of payload.detections) {
            if (!grouped.has(detection.class)) grouped.set(detection.class, []);
            grouped.get(detection.class).push(detection.confidence_percent);
        }
        summary.textContent = `${payload.detection_count} detection${payload.detection_count === 1 ? "" : "s"}`;
        for (const [className, scores] of grouped) {
            const row = document.createElement("div");
            row.className = "pc-preview-detection";
            const label = document.createElement("strong");
            label.textContent = className;
            const scoreList = document.createElement("span");
            scoreList.textContent = scores.map((score) => `${Number(score).toFixed(1)}%`).join(", ");
            const count = document.createElement("span");
            count.className = "pc-preview-count";
            count.textContent = `Count: ${scores.length}`;
            row.append(label, scoreList, count);
            detections.append(row);
        }
    }

    async function poll() {
        const cameraWaitStarted = Date.now();
        while (running) {
            const query = new URLSearchParams({
                side: sideSelect.value,
                model_id: modelSelect.value,
                confidence: confidence.value,
                show_masks: showMasks.checked ? "1" : "0",
                show_boxes: showBoxes.checked ? "1" : "0"
            });
            if (Number.isInteger(lastFrameSeq) && lastFrameSeq > 0) {
                query.set("after_seq", String(lastFrameSeq));
            }
            try {
                const response = await fetch(`${config.urls.preview}?${query}`, { cache: "no-store" });
                const payload = await response.json();
                if (!response.ok || !payload.ok) {
                    const error = new Error(payload.error || "Preview request failed.");
                    error.retryable = response.status === 409 &&
                        /waiting for it to produce a frame|camera is starting|has not produced a frame/i.test(error.message);
                    throw error;
                }
                if (!running) return;
                if (payload.unchanged === true) {
                    if (Number(payload.frame_seq) !== lastFrameSeq ||
                            payload.side !== sideSelect.value ||
                            Number(payload.model_id) !== Number(modelSelect.value)) {
                        throw new Error("The unchanged preview response does not match the selected side and model.");
                    }
                    setStatus(
                        `Live ${payload.side.toLowerCase()} preview · frame ${payload.frame_seq}`,
                        false
                    );
                    await new Promise((resolve) => window.setTimeout(resolve, 250));
                    continue;
                }
                image.src = payload.image;
                lastFrameSeq = Number(payload.frame_seq);
                image.hidden = false;
                placeholder.hidden = true;
                inference.textContent = `${Number(payload.inference_ms).toFixed(1)} ms`;
                setStatus(
                    `Live ${payload.side.toLowerCase()} preview · frame ${payload.frame_seq}` +
                    (payload.roi_applied ? " · ROI applied" : " · full frame"),
                    false
                );
                renderDetections(payload);
            } catch (error) {
                if (running && error.retryable && Date.now() - cameraWaitStarted < 20000) {
                    setStatus("Waiting for the configured camera to produce a frame…", false);
                    await new Promise((resolve) => window.setTimeout(resolve, 350));
                    continue;
                }
                if (running) stop(error.message || "Preview failed.");
                return;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 250));
        }
    }

    confidence.addEventListener("input", () => {
        confidenceValue.textContent = `${Math.round(Number(confidence.value) * 100)}%`;
    });

    sideSelect.addEventListener("change", () => {
        if (running) stop("Side changed. Start preview again to use its configured camera.");
    });

    toggle.addEventListener("click", async () => {
        if (running) {
            stop("Preview stopped.");
            return;
        }
        toggle.disabled = true;
        setStatus("Loading configuration…", false);
        try {
            await loadConfiguration();
            if (!sideSelect.value) throw new Error("This configuration has no enabled inspection side.");
            if (!modelSelect.value) throw new Error("This configuration has no enabled segmentation model.");
            const source = sideSelect.value === "FRONT"
                ? currentConfiguration.front_camera_source
                : currentConfiguration.back_camera_source;
            const cameraIndex = sideSelect.value === "FRONT"
                ? currentConfiguration.front_camera_index
                : currentConfiguration.back_camera_index;
            const cameraQuery = new URLSearchParams({
                source: source === "RPI" ? "picamera" : "opencv",
                camera_index: String(cameraIndex || 0),
                reuse_if_active: "1"
            });
            setStatus("Starting or reusing the configured camera…", false);
            const cameraResponse = await fetch(`${config.urls.cameraStart}?${cameraQuery}`, {
                cache: "no-store"
            });
            const cameraPayload = await cameraResponse.json();
            if (!cameraResponse.ok || cameraPayload.state === "ERROR") {
                throw new Error(
                    cameraPayload.error ||
                    "The configured camera could not be started."
                );
            }
            running = true;
            toggle.textContent = "Stop Preview";
            setStatus(
                cameraPayload.started_by_request
                    ? "Camera started; waiting for the first frame…"
                    : "Reusing the shared camera; waiting for the latest frame…",
                false
            );
            poll();
        } catch (error) {
            setStatus(error.message || "Could not start preview.", true);
        } finally {
            toggle.disabled = false;
        }
    });

    window.addEventListener("pagehide", () => {
        running = false;
    });
})();
