(function () {
    "use strict";

    var INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000;
    var INSPECTION_REQUEST_TIMEOUT_MS = 120 * 1000;
    var CAMERA_READY_TIMEOUT_MS = 20000;
    var RESULT_DISPLAY_MS = 500;
    var MAX_PREVIEW_RETRIES = 3;
    var cfg = window.__VI_WS__ || { urls: {} };
    var URL = cfg.urls || {};
    var STAGE = {
        SCAN_PART: "SCAN_PART",
        FRONT_INSPECTION: "FRONT_INSPECTION",
        BACK_INSPECTION: "BACK_INSPECTION",
        SAVING_RESULTS: "SAVING_RESULTS",
        FINAL_RESULT: "FINAL_RESULT",
        ERROR: "ERROR"
    };
    var state = {
        stage: STAGE.SCAN_PART,
        sessionId: null,
        sessionActive: false,
        pendingStart: null,
        pendingStartResolve: null,
        cameraActivationKey: null,
        cameraActivationPromise: null,
        leaveTarget: null,
        partNumber: "",
        partId: null,
        configurationId: null,
        selectedBarcode: "",
        validatedModelId: null,
        predictionTimer: null,
        predictionInFlight: false,
        predictionController: null,
        predictionSide: null,
        predictionGeneration: 0,
        predictionFrameSeq: null,
        frontVerdict: "PENDING",
        backVerdict: "PENDING",
        savedRecordId: null,
        busy: false,
        cameraTimer: null,
        cameraReadyTimer: null,
        cameraPollInterval: null,
        cameraInFlight: false,
        cameraStatusController: null,
        cancellingPart: false,
        sessionPollTimer: null,
        sessionPollInFlight: false,
        sessionPollGeneration: 0,
        sessionPollFailures: 0,
        resumeRetryTimer: null,
        resumeRetryCount: 0,
        statusPollWarning: false,
        requestVersion: 0,
        validationController: null,
        validationPending: false,
        validatedPartNumber: "",
        cameraStatus: { Front: "OFF", Back: "OFF" },
        sideStatus: { Front: "PENDING", Back: "PENDING" },
        predictionReady: { Front: false, Back: false },
        predictionRetryCount: 0,
        inactivityTimer: null,
        countdownTimer: null,
        inactivityDeadline: null,
        screenSaverActive: false,
        activityMonitoring: false,
        finalTransitionTimer: null,
        finalTransitionScheduled: false,
        leaveInvoker: null,
        ready: false
    };
    var initialized = false;
    var previewTelemetry = {
        startedAt: performance.now(),
        requests: 0,
        fullResponses: 0,
        unchangedResponses: 0,
        responseBytes: 0,
        requestMs: 0,
        transferMs: 0,
        transferSamples: 0,
        transferBytes: 0,
        sequences: new Set()
    };

    function byId(id) { return document.getElementById(id); }
    function cancelFinalTransition() {
        if (state.finalTransitionTimer) window.clearTimeout(state.finalTransitionTimer);
        state.finalTransitionTimer = null;
        state.finalTransitionScheduled = false;
    }
    function csrf() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return cfg.csrf || (input && input.value) || "";
    }
    function stageNode(name) { return byId("wfStage" + name); }
    function showStage(name) {
        if (name !== "Final") cancelFinalTransition();
        ["Scan", "Front", "Back", "Saving", "Final"].forEach(function (item) {
            var node = stageNode(item);
            if (node) node.hidden = item !== name;
        });
        document.body.classList.toggle("vi-camera-fullscreen", name === "Front" || name === "Back");
        state.stage = ({ Scan: STAGE.SCAN_PART, Front: STAGE.FRONT_INSPECTION,
            Back: STAGE.BACK_INSPECTION, Saving: STAGE.SAVING_RESULTS,
            Final: STAGE.FINAL_RESULT })[name] || STAGE.ERROR;
        syncInactivityTimer();
    }
    function setText(id, value) {
        var node = byId(id);
        if (node) node.textContent = value == null || value === "" ? "—" : String(value);
    }
    function renderOperatorInstructions(instructions) {
        var panel = byId("wfOperatorGuidance");
        var list = byId("wfInstructionSides");
        var frontText = byId("wfFrontInstruction");
        var backText = byId("wfBackInstruction");
        if (!panel || !list || !instructions || !Array.isArray(instructions.required_sides)) {
            if (panel) panel.hidden = true;
            return;
        }
        list.replaceChildren();
        var sideSummaries = {};
        instructions.required_sides.forEach(function (side, index) {
            var item = document.createElement("li");
            var heading = document.createElement("strong");
            heading.textContent = (index + 1) + ". " + side.side.toUpperCase()
                + " camera (" + side.camera_source + " " + side.camera_index + ")";
            item.appendChild(heading);

            var classes = [];
            (side.required_classes || []).forEach(function (rule) {
                classes.push(rule.class_name + " required; expected "
                    + rule.expected_count + "; " + rule.acceptance_rule);
            });
            (side.optional_classes || []).forEach(function (rule) {
                classes.push(rule.class_name + " optional; expected "
                    + rule.expected_count + "; " + rule.acceptance_rule);
            });
            if (classes.length) {
                var detail = document.createElement("p");
                detail.textContent = classes.join(" | ");
                item.appendChild(detail);
            }

            var roi = side.roi;
            if (roi && [roi.x, roi.y, roi.w, roi.h].every(Number.isFinite)) {
                var roiDetail = document.createElement("p");
                roiDetail.textContent = "Configured ROI: x "
                    + Math.round(roi.x * 100) + "%, y "
                    + Math.round(roi.y * 100) + "%, width "
                    + Math.round(roi.w * 100) + "%, height "
                    + Math.round(roi.h * 100) + "%. Position the part within the camera frame.";
                item.appendChild(roiDetail);
            } else {
                var noRoi = document.createElement("p");
                noRoi.textContent = "No ROI guidance is stored for this configuration.";
                item.appendChild(noRoi);
            }
            list.appendChild(item);
            sideSummaries[side.side] = classes.join(" | ");
        });
        setText("wfInstructionPart", (instructions.part_number || "Part")
            + (instructions.part_name ? " — " + instructions.part_name : "")
            + " · " + (instructions.inspection_mode || "Inspection"));
        if (frontText) frontText.textContent = sideSummaries.front || "Front side is not required.";
        if (backText) backText.textContent = sideSummaries.back || "Back side is not required.";
        panel.hidden = instructions.required_sides.length === 0;
    }
    function setDot(id, status) {
        var node = byId(id);
        if (!node) return;
        node.className = "wf-indicator is-" + status;
    }
    function setCameraStatus(side, status, message) {
        var node = byId("wf" + side + "CameraStatus");
        var overlay = byId("ov" + side);
        var previousStatus = state.cameraStatus[side];
        state.cameraStatus[side] = status;
        if (node) {
            var cameraOn = status === "ON";
            node.className = "wf-camera-status " + (cameraOn ? "is-on" : "is-off");
            node.innerHTML = "<i></i> " + (cameraOn ? "ON" : "OFF");
        }
        if (overlay) {
            overlay.textContent = status === "ERROR" ? "Camera unavailable"
                : status === "CONNECTING" ? "Connecting camera..." : "Camera off";
            overlay.hidden = status === "ON";
        }
        var retry = byId("wf" + side + "RetryCamera");
        if (retry) retry.hidden = false;
        var button = byId(side === "Front" ? "wfFrontSubmitBtn" : "wfBackSubmitBtn");
        var previewReady = state.predictionReady[side] === true;
        var inspectionInProgress = state.sideStatus[side] === "IN_PROGRESS";
        if (button) {
            button.textContent = inspectionInProgress
                ? "INSPECTION IN PROGRESS"
                : status === "ON" && !previewReady
                    ? "PREPARING..."
                    : side.toUpperCase() + " SCAN";
        }
        if (button && !state.busy) {
            button.disabled = inspectionInProgress ||
                status !== "ON" ||
                !previewReady ||
                button.dataset.completed === "true";
        }
        if (status !== "CONNECTING" && state.cameraReadyTimer) {
            window.clearTimeout(state.cameraReadyTimer);
            state.cameraReadyTimer = null;
        }
        if (status === "ERROR") stopPredictionPreview();
        if (previousStatus !== status) syncInactivityTimer();
    }
    function setModelStatus(side, status, message) {
        var node = byId("wf" + side + "ModelStatus");
        if (node) {
            node.className = "wf-model-status is-" + status.toLowerCase();
            node.textContent = message;
        }
        var retry = byId("wf" + side + "RetryModel");
        if (retry) retry.hidden = status !== "error";
    }
    function activeCameraSide() {
        if (state.stage === STAGE.FRONT_INSPECTION) return "Front";
        if (state.stage === STAGE.BACK_INSPECTION) return "Back";
        return null;
    }
    function clearInactivityTimer() {
        if (state.inactivityTimer) window.clearTimeout(state.inactivityTimer);
        state.inactivityTimer = null;
    }
    function setScreenSaver(active) {
        state.screenSaverActive = active;
        var node = byId("wfScreenSaver");
        if (node) node.hidden = !active;
    }
    function updateInactivityCountdown() {
        var remaining = state.inactivityDeadline
            ? Math.max(0, state.inactivityDeadline - Date.now()) : INACTIVITY_TIMEOUT_MS;
        var seconds = Math.ceil(remaining / 1000);
        var text = String(Math.floor(seconds / 60)).padStart(2, "0") + ":" +
            String(seconds % 60).padStart(2, "0");
        document.querySelectorAll(".wf-inactivity-countdown").forEach(function (node) {
            node.textContent = text;
        });
    }
    function syncInactivityTimer() {
        var side = activeCameraSide();
        if (!side || state.cameraStatus[side] !== "ON") {
            clearInactivityTimer();
            if (state.countdownTimer) window.clearInterval(state.countdownTimer);
            state.countdownTimer = null;
            state.inactivityDeadline = null;
            updateInactivityCountdown();
            setScreenSaver(false);
            return;
        }
        if (!state.screenSaverActive) resetInactivityTimer();
    }
    function resetInactivityTimer() {
        clearInactivityTimer();
        var side = activeCameraSide();
        if (!side || state.cameraStatus[side] !== "ON") return;
        if (state.screenSaverActive) setScreenSaver(false);
        state.inactivityDeadline = Date.now() + INACTIVITY_TIMEOUT_MS;
        updateInactivityCountdown();
        if (!state.countdownTimer) {
            state.countdownTimer = window.setInterval(updateInactivityCountdown, 1000);
        }
        state.inactivityTimer = window.setTimeout(function () {
            state.inactivityTimer = null;
            var currentSide = activeCameraSide();
            if (currentSide && state.cameraStatus[currentSide] === "ON") {
                updateInactivityCountdown();
                setScreenSaver(true);
            }
        }, INACTIVITY_TIMEOUT_MS);
    }
    function handleInspectionActivity(event) {
        var side = activeCameraSide();
        if (!side || state.cameraStatus[side] !== "ON") return;
        if (state.screenSaverActive &&
                (event.type === "keydown" || event.type === "pointerdown" || event.type === "touchstart")) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
        resetInactivityTimer();
    }
    function startActivityMonitoring() {
        if (state.activityMonitoring) return;
        state.activityMonitoring = true;
        document.addEventListener("pointermove", handleInspectionActivity, true);
        document.addEventListener("pointerdown", handleInspectionActivity, true);
        document.addEventListener("keydown", handleInspectionActivity, true);
        document.addEventListener("touchstart", handleInspectionActivity, { capture: true, passive: false });
        syncInactivityTimer();
    }
    function stopActivityMonitoring() {
        clearInactivityTimer();
        if (state.countdownTimer) window.clearInterval(state.countdownTimer);
        state.countdownTimer = null;
        state.inactivityDeadline = null;
        updateInactivityCountdown();
        if (!state.activityMonitoring) return;
        state.activityMonitoring = false;
        document.removeEventListener("pointermove", handleInspectionActivity, true);
        document.removeEventListener("pointerdown", handleInspectionActivity, true);
        document.removeEventListener("keydown", handleInspectionActivity, true);
        document.removeEventListener("touchstart", handleInspectionActivity, true);
        setScreenSaver(false);
    }
    function showError(message) {
        var node = byId("wsError");
        if (node) node.textContent = message || "";
    }

function setBusy(button, value) {
    state.busy = value;

    if (button && button.currentTarget) {
        button = button.currentTarget;
    }

    if (button && button.jquery) {
        button = button[0];
    }

    if (!(button instanceof HTMLElement)) {
        console.error("setBusy received an invalid button:", button);
        return;
    }

    button.disabled = value;
    button.setAttribute("aria-busy", value ? "true" : "false");
}

    async function jsonRequest(url, method, body, timeoutMs, signal) {
        var controller = timeoutMs && typeof AbortController === "function"
            ? new AbortController() : null;
        var requestSignal = controller ? controller.signal : signal;
        var abortRequest = null;
        if (controller && signal) {
            abortRequest = function () { controller.abort(); };
            if (signal.aborted) controller.abort();
            else signal.addEventListener("abort", abortRequest, { once: true });
        }
        var didTimeout = false;
        var timeout = controller
            ? window.setTimeout(function () {
                didTimeout = true;
                controller.abort();
            }, timeoutMs)
            : null;
        var response;
        try {
            response = await fetch(url, {
                method: method || "GET",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrf()
                },
                body: body ? JSON.stringify(body) : undefined,
                signal: requestSignal
            });
        } catch (error) {
            if (didTimeout) {
                var timeoutError = new Error("Inspection timed out before the server returned a result.");
                timeoutError.code = "inspection_timeout";
                throw timeoutError;
            }
            throw error;
        } finally {
            if (timeout) window.clearTimeout(timeout);
            if (signal && abortRequest) {
                signal.removeEventListener("abort", abortRequest);
            }
        }
        var data;
        try { data = await response.json(); }
        catch (error) { throw new Error("The server returned an unreadable response."); }
        if (!response.ok || data.ok === false || data.error) {
            var failure = new Error(data.message || data.error || "Request failed.");
            failure.data = data;
            failure.status = response.status;
            throw failure;
        }
        return data;
    }
    function isSessionId(value) {
        return typeof value === "string" &&
            /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
    }
    async function cameraStatusRequest(sessionId, signal) {
        var url = URL.cameraStatus;
        if (sessionId) {
            url += (url.indexOf("?") === -1 ? "?" : "&") +
                "session_id=" + encodeURIComponent(sessionId);
        }
        var response = await fetch(url, {
            headers: { Accept: "application/json" },
            signal: signal
        });
        var data;
        try { data = await response.json(); }
        catch (error) {
            if (error.name === "AbortError") throw error;
            throw new Error("The camera status response was unreadable.");
        }
        if (!response.ok || !data || typeof data.status !== "string") {
            var failure = new Error(
                data && (data.message || data.error) || "Camera status is unavailable."
            );
            failure.data = data;
            failure.status = response.status;
            throw failure;
        }
        return data;
    }
    function progressUrl(sessionId) {
        return URL.wfSession + encodeURIComponent(sessionId) + "/";
    }
    function persistLocal() {
        try {
            sessionStorage.setItem("viInspectionWorkflow", JSON.stringify({
                sessionId: state.sessionId,
                partNumber: state.partNumber,
                partId: state.partId,
                configurationId: state.configurationId,
                validatedModelId: state.validatedModelId,
                savedRecordId: state.savedRecordId
            }));
        } catch (error) { /* Storage may be disabled on the station browser. */ }
    }
    function clearLocal() {
        try { sessionStorage.removeItem("viInspectionWorkflow"); }
        catch (error) { /* Storage may be disabled on the station browser. */ }
    }
    function clearTransientState() {
        cancelFinalTransition();
        if (state.resumeRetryTimer) window.clearTimeout(state.resumeRetryTimer);
        state.resumeRetryTimer = null;
        state.resumeRetryCount = 0;
        state.statusPollWarning = false;
        state.requestVersion += 1;
        state.busy = false;
        state.validationPending = false;
        state.cameraActivationKey = null;
        state.cameraActivationPromise = null;
        stopCameraPolling();
        stopSessionProgressPolling();
        clearFeeds();
        clearLocal();
        state.sessionId = null;
        state.sessionActive = false;
        state.partNumber = "";
        state.partId = null;
        state.configurationId = null;
        state.selectedBarcode = "";
        state.validatedModelId = null;
        state.validatedPartNumber = "";
        state.predictionReady = { Front: false, Back: false };
        state.sideStatus = { Front: "PENDING", Back: "PENDING" };
        state.predictionRetryCount = 0;
        state.frontVerdict = "PENDING";
        state.backVerdict = "PENDING";
        state.savedRecordId = null;
        state.stage = STAGE.SCAN_PART;
        clearInactivityTimer();
        if (state.countdownTimer) window.clearInterval(state.countdownTimer);
        state.countdownTimer = null;
        state.inactivityDeadline = null;
        updateInactivityCountdown();
        setScreenSaver(false);
        setResult("Front", "PENDING", "PENDING");
        setResult("Back", "PENDING", "PENDING");
        setText("wfFinalPart", "—");
        setText("wfFinalId", "—");
        setText("wfFinalTitle", "—");
        setText("wfFinalTime", "—");
        setText("wfFinalFront", "—");
        setText("wfFinalBack", "—");
        setText("wfFinalSaved", "—");
        setText("wfFinalMessage", "");
        byId("wfNewInspectionBtn").hidden = true;
        byId("wfChangePartBtn").hidden = true;
        byId("wfNewInspectionBtn").textContent = "NEXT INSPECTION CYCLE — SAME PART";
        setText("wfSavingTitle", "Saving results");
        setText("wfSavingMessage", "The backend is finalizing this inspection session.");
        byId("wfRetrySaveBtn").hidden = true;
        ["wfFrontSubmitBtn", "wfBackSubmitBtn"].forEach(function (id) {
            var button = byId(id);
            button.dataset.completed = "false";
        });
        setModelStatus("Front", "waiting", "MODEL WAITING");
        setModelStatus("Back", "waiting", "MODEL WAITING");
        byId("wfBarcode").value = "";
        setText("wfSelectedPart", "Select a part to continue.");
        setText("wfPartInfo", "Select a part to validate its configuration and model.");
        setText("wfScanState", "Select a part");
        setText("wfModelState", "Waiting for part");
        setText("wfModelDetail", "");
        byId("wfModelDetail").hidden = true;
        renderOperatorInstructions(null);
        setDot("wfScanDot", "idle");
        setDot("wfModelDot", "idle");
        showError("");
        byId("wfBarcode").disabled = false;
        byId("wfBarcode").removeAttribute("aria-busy");
        showStage("Scan");
    }
    function setFeed(side) {
        var image = byId("feed" + side);
        if (image) {
            delete image.dataset.inspectionResult;
            image.src = URL.cameraStream + "?session_id=" +
                encodeURIComponent(state.sessionId || "") + "&t=" + Date.now();
        }
    }
    function clearFeeds() {
        stopCameraPolling();
        stopPredictionPreview();
        ["Front", "Back"].forEach(function (side) {
            var image = byId("feed" + side);
            if (image) {
                image.removeAttribute("src");
                delete image.dataset.inspectionResult;
            }
            setCameraStatus(side, "OFF", "Camera stopped.");
        });
    }

    async function activateCamera(side) {
        var key = String(state.sessionId || "") + ":" + side;
        if (state.cameraActivationKey === key && state.cameraActivationPromise) {
            return state.cameraActivationPromise;
        }
        var label = side === "front" ? "Front" : "Back";
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        var expectedStage = side === "front"
            ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION;
        var activation = (async function () {
            state.predictionReady[label] = false;
            state.predictionRetryCount = 0;
            setCameraStatus(label, "CONNECTING", "Connecting to configured " + side + " camera...");
            setModelStatus(label, "waiting", "WAITING FOR FIRST VALID FRAME");
            await jsonRequest(URL.wfActivateSide, "POST", {
                session_id: sessionId,
                side: side
            });
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    state.stage !== expectedStage) return;
            if (state.cameraReadyTimer) window.clearTimeout(state.cameraReadyTimer);
            state.cameraReadyTimer = window.setTimeout(function () {
                if (state.cameraStatus[label] === "CONNECTING") {
                    setCameraStatus(
                        label,
                        "ERROR",
                        "Camera did not produce a valid frame within " +
                            (CAMERA_READY_TIMEOUT_MS / 1000) + " seconds."
                    );
                }
            }, CAMERA_READY_TIMEOUT_MS);
            startCameraPolling(500);
            await refreshCamera();
        })();
        state.cameraActivationKey = key;
        state.cameraActivationPromise = activation;
        try {
            await activation;
        } finally {
            if (state.cameraActivationPromise === activation) {
                state.cameraActivationPromise = null;
                state.cameraActivationKey = null;
            }
        }
    }

    function stopPredictionPreview() {
        state.predictionGeneration += 1;
        state.predictionSide = null;
        state.predictionFrameSeq = null;
        if (state.predictionTimer) window.clearTimeout(state.predictionTimer);
        state.predictionTimer = null;
        if (state.predictionController) {
            state.predictionController.abort();
            state.predictionController = null;
        }
    }

    function recordPreviewTelemetry(data, requestMs, transferMs, transferBytes) {
        previewTelemetry.requests += 1;
        previewTelemetry.requestMs += requestMs;
        previewTelemetry.responseBytes += new Blob([JSON.stringify(data)]).size;
        if (Number.isFinite(transferMs)) {
            previewTelemetry.transferMs += transferMs;
            previewTelemetry.transferSamples += 1;
        }
        if (Number.isFinite(transferBytes) && transferBytes > 0) {
            previewTelemetry.transferBytes += transferBytes;
        }
        if (data.unchanged) {
            previewTelemetry.unchangedResponses += 1;
        } else {
            previewTelemetry.fullResponses += 1;
            previewTelemetry.sequences.add(Number(data.frame_seq));
        }
        var elapsed = performance.now() - previewTelemetry.startedAt;
        if (elapsed < 30000) return;
        console.info("Live preview client metrics", {
            requests: previewTelemetry.requests,
            imageResponses: previewTelemetry.fullResponses,
            unchangedResponses: previewTelemetry.unchangedResponses,
            uniqueFrameSequences: previewTelemetry.sequences.size,
            averageResponseBytes: Math.round(
                previewTelemetry.responseBytes / Math.max(previewTelemetry.requests, 1)
            ),
            averageRequestMs: Number(
                (previewTelemetry.requestMs / Math.max(previewTelemetry.requests, 1)).toFixed(1)
            ),
            averageBodyTransferMs: previewTelemetry.transferSamples
                ? Number(
                    (previewTelemetry.transferMs / previewTelemetry.transferSamples).toFixed(1)
                )
                : "unavailable",
            resourceTransferBytes: previewTelemetry.transferBytes || "unavailable",
            previewFrameFps: Number(
                (previewTelemetry.sequences.size / (elapsed / 1000)).toFixed(2)
            )
        });
        previewTelemetry = {
            startedAt: performance.now(),
            requests: 0,
            fullResponses: 0,
            unchangedResponses: 0,
            responseBytes: 0,
            requestMs: 0,
            transferMs: 0,
            transferSamples: 0,
            transferBytes: 0,
            sequences: new Set()
        };
    }

    function stopSessionProgressPolling() {
        state.sessionPollGeneration += 1;
        if (state.sessionPollTimer) window.clearTimeout(state.sessionPollTimer);
        state.sessionPollTimer = null;
        state.sessionPollInFlight = false;
        state.sessionPollFailures = 0;
    }

    function startSessionProgressPolling(side, recoverProcessing) {
        stopSessionProgressPolling();
        var sessionId = state.sessionId;
        var generation = state.sessionPollGeneration;
        if (!sessionId) return;

        async function poll() {
            if (generation !== state.sessionPollGeneration || sessionId !== state.sessionId) return;
            if (state.sessionPollInFlight) {
                state.sessionPollTimer = window.setTimeout(poll, 500);
                return;
            }
            state.sessionPollInFlight = true;
            var nextDelay = 750;
            try {
                var progress = await loadProgress(sessionId);
                if (generation !== state.sessionPollGeneration || sessionId !== state.sessionId) return;
                if (progress.ok !== true ||
                        progress.session_id !== sessionId ||
                        progress.part_number !== state.partNumber ||
                        Number(progress.configuration_id) !== Number(state.configurationId)) {
                    throw new Error("The inspection status response does not match the active session.");
                }
                if (progress.is_saved) {
                    stopSessionProgressPolling();
                    showFinal(progress, {
                        inspection_id: progress.inspection_id || state.savedRecordId,
                        timestamp: progress.timestamp,
                        overall: progress.overall
                    });
                    return;
                }
                if (!progress.is_active) {
                    handleInactiveCameraSession(
                        sessionId,
                        state.requestVersion,
                        new Error("This inspection session is no longer active.")
                    );
                    return;
                }
                state.sessionPollFailures = 0;
                if (state.statusPollWarning) {
                    state.statusPollWarning = false;
                    showError("");
                }
                state.frontVerdict = progress.front_verdict || state.frontVerdict;
                state.backVerdict = progress.back_verdict || state.backVerdict;
                if (!(state.busy && state.sideStatus.Front === "IN_PROGRESS" &&
                        progress.front_status === "PENDING")) {
                    setResult("Front", progress.front_status, progress.front_verdict, "progress poll");
                }
                if (!(state.busy && state.sideStatus.Back === "IN_PROGRESS" &&
                        progress.back_status === "PENDING")) {
                    setResult("Back", progress.back_status, progress.back_verdict, "progress poll");
                }
                if (progress.inspection_status === "ERROR") {
                    showError(progress.error_message || "Inspection failed. Retry the failed side.");
                    console.error("Inspection session polling reported an error", {
                        session_id: sessionId,
                        side: side,
                        message: progress.error_message || ""
                    });
                    stopSessionProgressPolling();
                    return;
                }
                if (recoverProcessing) {
                    var recoveredSideStatus = progress[side + "_status"];
                    if (recoveredSideStatus === "COMPLETED" ||
                            recoveredSideStatus === "SKIPPED") {
                        stopSessionProgressPolling();
                        await enterFromProgress(progress, null, false);
                        return;
                    }
                }
            } catch (error) {
                if (generation === state.sessionPollGeneration) {
                    var errorState = error.data && error.data.state;
                    if (errorState === "session_inactive" ||
                            errorState === "session_expired") {
                        handleInactiveCameraSession(
                            sessionId,
                            state.requestVersion,
                            error
                        );
                        return;
                    }
                    state.sessionPollFailures += 1;
                    nextDelay = Math.min(
                        8000,
                        750 * Math.pow(2, state.sessionPollFailures - 1)
                    );
                    state.statusPollWarning = true;
                    showError(
                        "Connection to inspection status was lost. Retrying in " +
                        Math.ceil(nextDelay / 1000) + " seconds."
                    );
                    console.warn("Inspection session status poll failed", {
                        session_id: sessionId,
                        side: side,
                        message: error.message
                    });
                }
            } finally {
                state.sessionPollInFlight = false;
            }
            if (generation === state.sessionPollGeneration && sessionId === state.sessionId) {
                state.sessionPollTimer = window.setTimeout(poll, nextDelay);
            }
        }
        poll();
    }

    function startPredictionPreview(side) {
        if (!state.partId || !state.validatedModelId) return;
        if (state.predictionSide === side) return;
        stopPredictionPreview();
        state.predictionSide = side;
        pollPredictionPreview(side, state.predictionGeneration);
    }

    function schedulePredictionPreview(side, generation, delay) {
        if (generation !== state.predictionGeneration ||
                state.predictionSide !== side || state.busy) return;
        state.predictionTimer = window.setTimeout(function () {
            pollPredictionPreview(side, generation);
        }, delay);
    }

    async function pollPredictionPreview(side, generation) {
        if (state.predictionInFlight || generation !== state.predictionGeneration ||
                state.predictionSide !== side) return;
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        var image = byId(side === "FRONT" ? "feedFront" : "feedBack");
        if (!image) return;
        var label = side === "FRONT" ? "Front" : "Back";
        state.predictionInFlight = true;
        var controller = typeof AbortController === "function"
            ? new AbortController() : null;
        state.predictionController = controller;
        try {
            var query = new URLSearchParams({
                side: side,
                model_id: String(state.validatedModelId),
                session_id: String(state.sessionId),
                confidence: "0.30",
                show_masks: "1",
                show_boxes: "1"
            });
            if (Number.isInteger(state.predictionFrameSeq) &&
                    state.predictionFrameSeq > 0) {
                query.set("after_seq", String(state.predictionFrameSeq));
            }
            var url = URL.partVisionPreview.replace(
                "__PART_ID__",
                encodeURIComponent(state.partId)
            ) + "?" + query.toString();
            var requestStarted = performance.now();
            var data = await jsonRequest(
                url,
                "GET",
                null,
                INSPECTION_REQUEST_TIMEOUT_MS,
                controller ? controller.signal : null
            );
            var requestMs = performance.now() - requestStarted;
            var resourceEntries = performance.getEntriesByName(url);
            var resourceEntry = null;
            for (var resourceIndex = resourceEntries.length - 1;
                    resourceIndex >= 0; resourceIndex -= 1) {
                if (resourceEntries[resourceIndex].startTime >= requestStarted - 1) {
                    resourceEntry = resourceEntries[resourceIndex];
                    break;
                }
            }
            var transferMs = resourceEntry
                ? resourceEntry.responseEnd - resourceEntry.responseStart
                : null;
            var transferBytes = resourceEntry
                ? Number(resourceEntry.transferSize) || null
                : null;
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    generation !== state.predictionGeneration ||
                    state.predictionSide !== side ||
                    state.stage !== (side === "FRONT" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (data.unchanged === true) {
                if (Number(data.frame_seq) !== state.predictionFrameSeq ||
                        Number(data.part_id) !== Number(state.partId) ||
                        data.session_id !== sessionId ||
                        data.side !== side ||
                        Number(data.model_id) !== Number(state.validatedModelId) ||
                        !["OKAY", "NOT OKAY"].includes(data.live_status) ||
                        !["OK", "NOT_OK"].includes(data.live_verdict) ||
                        (data.live_status === "OKAY") !== (data.live_verdict === "OK") ||
                        (state.configurationId &&
                            Number(data.configuration_id) !== Number(state.configurationId))) {
                    throw new Error("The unchanged preview response does not match the active inspection.");
                }
                recordPreviewTelemetry(data, requestMs, transferMs, transferBytes);
                schedulePredictionPreview(side, generation, 300);
                return;
            }
            if (typeof data.image !== "string" ||
                    data.image.indexOf("data:image/jpeg;base64,") !== 0 ||
                    !Array.isArray(data.detections) ||
                    data.inference_status !== "complete" ||
                    !Number.isInteger(Number(data.frame_seq)) ||
                    Number(data.frame_seq) < 1 ||
                    Number(data.part_id) !== Number(state.partId) ||
                    data.session_id !== sessionId ||
                    data.side !== side ||
                    Number(data.model_id) !== Number(state.validatedModelId) ||
                    !Number.isInteger(Number(data.configuration_id)) ||
                    !["OKAY", "NOT OKAY"].includes(data.live_status) ||
                    !["OK", "NOT_OK"].includes(data.live_verdict) ||
                    (data.live_status === "OKAY") !== (data.live_verdict === "OK") ||
                    !Array.isArray(data.rule_results) ||
                    !data.rule_results.some(function (item) {
                        return item && item.required === true;
                    })) {
                throw new Error("The live prediction response did not include a complete, current configured inspection decision.");
            }
            recordPreviewTelemetry(data, requestMs, transferMs, transferBytes);
            if (state.configurationId &&
                    Number(data.configuration_id) !== Number(state.configurationId)) {
                throw new Error("The live prediction used a different Part Vision Configuration than the active session.");
            }
            state.configurationId = Number(data.configuration_id);
            state.predictionFrameSeq = Number(data.frame_seq);
            image.src = data.image;
            state.predictionReady[side === "FRONT" ? "Front" : "Back"] = true;
            state.predictionRetryCount = 0;
            setModelStatus(
                side === "FRONT" ? "Front" : "Back",
                "ready",
                "READY"
            );
            setCameraStatus(label, "ON", "");
            showError("");
            schedulePredictionPreview(side, generation, 300);
        } catch (error) {
            if (sessionId === state.sessionId &&
                    requestVersion === state.requestVersion &&
                    generation === state.predictionGeneration &&
                    state.predictionSide === side) {
                var errorState = error.data && error.data.state;
                if (errorState === "session_inactive" ||
                        errorState === "session_expired") {
                    handleInactiveCameraSession(
                        sessionId,
                        requestVersion,
                        error
                    );
                    return;
                }
                var message = error.message || "Live prediction preview failed.";
                var label = side === "FRONT" ? "Front" : "Back";
                var errorCode = error.data && error.data.error_code || "";
                state.predictionReady[label] = false;
                setFeed(label);
                showError(message);
                var cameraError = [
                    "camera_not_ready",
                    "camera_mismatch",
                    "camera_frame_missing",
                    "camera_frame_stale"
                ].indexOf(errorCode) !== -1;
                var roiError = errorCode === "roi_missing" || errorCode === "roi_invalid";
                var configurationError = [
                    "model_configuration_missing",
                    "model_missing",
                    "side_not_enabled"
                ].indexOf(errorCode) !== -1;
                if (cameraError) {
                    setResult(label, "ERROR", "ERROR");
                    var retryableCameraFrame = [
                        "camera_frame_missing",
                        "camera_frame_stale"
                    ].indexOf(errorCode) !== -1 ||
                        (errorCode === "camera_not_ready" &&
                            /camera is (connecting|starting)/i.test(message));
                    if (retryableCameraFrame &&
                            state.predictionRetryCount < MAX_PREVIEW_RETRIES) {
                        state.predictionRetryCount += 1;
                        setCameraStatus(label, "CONNECTING", "Waiting for a fresh camera frame...");
                        setModelStatus(label, "waiting", "WAITING FOR FIRST VALID FRAME");
                        schedulePredictionPreview(side, generation, 1500);
                    } else {
                        setCameraStatus(label, "ERROR", message);
                        setModelStatus(label, "waiting", "WAITING FOR CAMERA");
                    }
                    return;
                }
                setCameraStatus(label, state.cameraStatus[label], "");
                if (roiError) {
                    setResult(label, "ERROR", "ERROR");
                    setModelStatus(label, "roi-error", "ROI CONFIGURATION ERROR");
                    return;
                }
                if (configurationError || (error.status === 409 && !errorCode)) {
                    setResult(label, "ERROR", "ERROR");
                    setModelStatus(label, "configuration-error", "PREVIEW CONFIGURATION ERROR");
                    return;
                }
                setResult(label, "ERROR", "ERROR");
                setModelStatus(label, "error", "MODEL / INFERENCE ERROR");
                var retryableCameraWarmup = error.status === 409 &&
                    /configured camera is (connecting|starting); waiting for it to produce a frame|camera has not produced a frame yet|latest camera frame is stale/i
                        .test(message);
                var retryableInference = !error.status || error.status === 503;
                if ((retryableCameraWarmup || retryableInference) &&
                        state.predictionRetryCount < MAX_PREVIEW_RETRIES) {
                    state.predictionRetryCount += 1;
                    setModelStatus(label, "warming", "RETRYING MODEL INITIALIZATION");
                    schedulePredictionPreview(side, generation, 1500);
                } else {
                    stopPredictionPreview();
                }
            }
        } finally {
            if (state.predictionController === controller) {
                state.predictionController = null;
            }
            state.predictionInFlight = false;
            if (state.predictionSide &&
                    state.predictionGeneration !== generation) {
                pollPredictionPreview(state.predictionSide, state.predictionGeneration);
            }
        }
    }

    function retryPrediction(side) {
        if (state.busy || state.cameraStatus[side] !== "ON") return;
        state.predictionReady[side] = false;
        state.predictionRetryCount = 0;
        setModelStatus(side, "warming", "RETRYING MODEL INITIALIZATION");
        setCameraStatus(side, "ON", "");
        startPredictionPreview(side.toUpperCase());
    }

    async function resetPreviousSession() {
        if (!state.sessionId) return true;
        await jsonRequest(URL.wfCancel, "POST", { session_id: state.sessionId });
        clearTransientState();
        return true;
    }

    async function scan(event) {
        if (event) event.preventDefault();
        if (!state.ready || state.busy || state.stage !== STAGE.SCAN_PART) return;
        var partSelect = byId("wfBarcode");
        var selectedOption = partSelect.options[partSelect.selectedIndex];
        var selectedPartId = selectedOption && selectedOption.value
            ? Number(selectedOption.dataset.partId)
            : null;
        var barcode = selectedOption && selectedOption.value
            ? (selectedOption.dataset.partNumber || "").trim()
            : "";
        if (!barcode || !Number.isInteger(selectedPartId) || selectedPartId < 1) {
            setText("wfPartInfo", "Select a part before validating.");
            return;
        }
        if (state.validatedPartNumber === barcode &&
                state.validatedModelId && state.configurationId) return;
        var version = ++state.requestVersion;
        var controller = typeof AbortController === "function"
            ? new AbortController() : null;
        state.validationController = controller;
        state.validationPending = false;
        setBusy(partSelect, true);
        byId("wfModelDetail").hidden = true;
        setText("wfModelDetail", "");
        state.validatedModelId = null;
        state.configurationId = null;
        showError("");
        setText("wfScanState", "Validating part...");
        setDot("wfScanDot", "working");
        setText("wfModelState", "Validating configuration...");
        setDot("wfModelDot", "working");
        try {
            if (!await resetPreviousSession()) return;
            if (version !== state.requestVersion) return;
            var partData = await jsonRequest(
                URL.wfScan, "POST", { barcode: barcode }, null,
                controller ? controller.signal : null
            );
            if (version !== state.requestVersion) return;
            if (partData.ok !== true || !partData.part ||
                    typeof partData.part.part_number !== "string" ||
                    !partData.part.part_number.trim() ||
                    partData.part.part_number !== barcode ||
                    Number(partData.part.id) !== selectedPartId) {
                throw new Error(partData.message || "The server did not identify a valid part.");
            }
            if (partData.active_session) {
                var active = partData.active_session;
                var activeMatchesPart = Number(active.part_id) ===
                    Number(partData.part.id);
                if (activeMatchesPart && isSessionId(active.session_id) &&
                        Number(active.part_id) > 0 && Number(active.model_id) > 0) {
                    await resumeSession({
                        sessionId: active.session_id,
                        partNumber: active.part_number,
                        partId: Number(active.part_id),
                        validatedModelId: Number(active.model_id)
                    });
                    return;
                }
                throw new Error(
                    "An inspection is already active for this account. Continue that inspection in its original tab before scanning another part."
                );
            }
            var part = partData.part || {};
            var partNumber = part.part_number || barcode;
            state.partNumber = partNumber;
            state.partId = selectedPartId;
            if (!state.partId) {
                throw new Error("The selected part response did not include a valid database ID.");
            }
            state.selectedBarcode = barcode;
            setText("wfScanState", "Part valid: " + partNumber);
            setDot("wfScanDot", "ready");
            setText("wfPartInfo", partData.message || ("Part identified: " + partNumber + "."));

            setText("wfModelState", "Loading model...");
            setDot("wfModelDot", "working");
            byId("wfModelDetail").hidden = true;
            setText("wfModelDetail", "");
            showError("");
            try {
                var modelData = await jsonRequest(
                    URL.wfValidate, "POST", { part_id: selectedPartId },
                    INSPECTION_REQUEST_TIMEOUT_MS,
                    controller ? controller.signal : null
                );
                if (version !== state.requestVersion) return;
                if (modelData.ok !== true || modelData.state !== "model_loaded" ||
                        !Number.isInteger(Number(modelData.model_id)) ||
                        Number(modelData.model_id) < 1 ||
                        !Number.isInteger(Number(modelData.configuration_id)) ||
                        Number(modelData.configuration_id) < 1 ||
                        !Array.isArray(modelData.models) || !modelData.models.length) {
                    throw new Error(modelData.message || "The model validation response did not identify a valid model.");
                }
                state.validatedModelId = Number(modelData.model_id);
                state.configurationId = Number(modelData.configuration_id) || null;
                var configuredModels = Array.isArray(modelData.models)
                    ? modelData.models : [];
                var modelSummary = configuredModels.map(function (model) {
                    var name = String(model.name || "Configured model");
                    var modelVersion = String(model.version || "");
                    return name + (modelVersion ? " v" + modelVersion : "");
                }).join(", ");
                if (!modelSummary && modelData.model_name) {
                    modelSummary = String(modelData.model_name) +
                        (modelData.model_version
                            ? " v" + String(modelData.model_version) : "");
                }
                state.validatedPartNumber = barcode;
                setText("wfModelState", "Model ready");
                setDot("wfModelDot", "ready");
                setText("wfModelDetail", modelSummary);
                byId("wfModelDetail").hidden = !modelSummary;
                renderOperatorInstructions(modelData.operator_instructions);
                setText("wfPartInfo", "Configuration and model are ready. Starting Front Camera...");
                persistLocal();
                if (version !== state.requestVersion) return;
                setBusy(partSelect, false);
                await startInspection(version);
            } catch (error) {
                if (version !== state.requestVersion) return;
                var validationState = error.data && error.data.state || "";
                var configurationError = validationState === "configuration_missing" ||
                    validationState.indexOf("configuration") !== -1;
                setText("wfModelState", configurationError
                    ? "Configuration error" : "Model error");
                setDot("wfModelDot", "error");
                setText("wfModelDetail", error.message || "Model validation failed.");
                byId("wfModelDetail").hidden = false;
                state.validatedPartNumber = "";
                setText("wfPartInfo", "Part " + partNumber + " is valid, but its inspection model/configuration is not ready.");
                showError(error.message || "Model validation failed.");
                return;
            }
        } catch (error) {
            if (version !== state.requestVersion) return;
            setText("wfScanState", (error.data && error.data.state) || "Validation failed");
            setDot("wfScanDot", "error");
            setText("wfModelState", "Not validated");
            setDot("wfModelDot", "idle");
            setText("wfPartInfo", error.message || "Part validation failed. Correct the barcode or configuration and retry.");
            showError(error.message);
        } finally {
            if (state.validationController === controller) {
                state.validationController = null;
            }
            if (state.stage === STAGE.SCAN_PART) {
                state.busy = false;
                partSelect.disabled = false;
                partSelect.removeAttribute("aria-busy");
            }
            if (state.validationPending && state.stage === STAGE.SCAN_PART &&
                    partSelect.value) {
                state.validationPending = false;
                window.setTimeout(function () { scan(); }, 0);
            }
        }
    }

    function handlePartSelectionChange() {
        var select = byId("wfBarcode");
        var option = select.options[select.selectedIndex];
        var partNumber = option && option.value
            ? (option.dataset.partNumber || "").trim()
            : "";
        var partId = option && option.value
            ? Number(option.dataset.partId)
            : null;
        var partName = option && option.value ? (option.dataset.partName || "").trim() : "";

        state.requestVersion += 1;
        if (state.stage === STAGE.SCAN_PART) {
            if (state.validationController) state.validationController.abort();
            state.validationPending = Boolean(partNumber);
        }
        state.partNumber = "";
        state.partId = Number.isInteger(partId) && partId > 0 ? partId : null;
        state.configurationId = null;
        state.selectedBarcode = "";
        state.validatedModelId = null;
        state.validatedPartNumber = "";
        byId("wfModelDetail").hidden = true;
        setText("wfModelDetail", "");
        renderOperatorInstructions(null);
        showError("");

        if (!partNumber) {
            setText("wfSelectedPart", "Select a part to continue.");
            setText("wfScanResult", "SELECT PART");
            setText("wfScanState", "Select a part");
            setDot("wfScanDot", "idle");
            setText("wfModelState", "Waiting for part");
            setDot("wfModelDot", "idle");
            setText("wfPartInfo", "Select a part to validate its configuration and model.");
            return;
        }

        setText("wfSelectedPart", partNumber + (partName ? " — " + partName : ""));
        setText("wfScanResult", "PART SELECTED");
        setText("wfScanState", "Not validated");
        setDot("wfScanDot", "idle");
        setText("wfScanState", "Validating configuration...");
        setDot("wfScanDot", "working");
        setText("wfModelState", "Loading model...");
        setDot("wfModelDot", "working");
        setText("wfPartInfo", "Validating the selected part and loading its configured model.");
        if (state.busy) {
            state.validationPending = true;
            return;
        }
        scan();
    }

    async function waitForCameraReady(sessionId, side, requestVersion) {
        await jsonRequest(URL.wfActivateSide, "POST", {
            session_id: sessionId,
            side: side
        });
        var deadline = Date.now() + CAMERA_READY_TIMEOUT_MS;
        while (Date.now() < deadline) {
            if (requestVersion !== state.requestVersion ||
                    sessionId !== state.sessionId) {
                throw new Error("The selected part changed while the camera was starting.");
            }
            var camera = await cameraStatusRequest(sessionId);
            if (requestVersion !== state.requestVersion ||
                    sessionId !== state.sessionId) {
                throw new Error("The selected part changed while the camera was starting.");
            }
            if (camera.status === "ON" && camera.ready === true) return;
            if (camera.status === "ERROR" || camera.status === "OFF") {
                throw new Error(camera.error || "The configured camera could not be initialized.");
            }
            await new Promise(function (resolve) {
                window.setTimeout(resolve, 300);
            });
        }
        throw new Error(
            "The configured camera did not produce a fresh frame within " +
                (CAMERA_READY_TIMEOUT_MS / 1000) + " seconds."
        );
    }

    async function startInspection(validationVersion) {
        if (state.busy || !state.partNumber || !state.partId ||
                !state.validatedModelId || !state.configurationId ||
                (validationVersion !== undefined &&
                    validationVersion !== state.requestVersion)) return;

        var partSelect = byId("wfBarcode");
        var partNumber = state.partNumber;
        var version = ++state.requestVersion;
        setBusy(partSelect, true);
        setText("wfScanState", "Starting Front Camera...");
        setDot("wfScanDot", "working");
        setText("wfPartInfo", "Starting the inspection session and configured camera.");
        showError("");
        state.pendingStart = new Promise(function (resolve) {
            state.pendingStartResolve = resolve;
        });
        var startedSessionId = null;
        try {
            var sessionData = await jsonRequest(URL.wfSubmit, "POST", {
                part_number: partNumber,
                model_id: state.validatedModelId
            });
            if (!isSessionId(sessionData.session_id)) {
                throw new Error("The server did not return a valid inspection session ID.");
            }
            startedSessionId = sessionData.session_id;
            if (version !== state.requestVersion) {
                await jsonRequest(URL.wfCancel, "POST", {
                    session_id: startedSessionId
                });
                return;
            }
            state.sessionId = startedSessionId;
            state.sessionActive = true;
            persistLocal();
            var progress = sessionData.progress || await loadProgress();
            var startupSide = sessionData.next_side ||
                (progress.front_status === "SKIPPED" ? "back" : "front");
            await waitForCameraReady(startedSessionId, startupSide, version);
            if (version !== state.requestVersion) {
                await jsonRequest(URL.wfCancel, "POST", {
                    session_id: startedSessionId
                });
                return;
            }
            state.savedRecordId = null;
            state.frontVerdict = "PENDING";
            state.backVerdict = "PENDING";
            ["Front", "Back"].forEach(function (side) {
                var submitButton = byId("wf" + side + "SubmitBtn");
                submitButton.dataset.completed = "false";
                submitButton.disabled = true;
                setResult(side, "PENDING", "PENDING");
            });
            setText("wfPartInfo", sessionData.message || "Inspection session started.");
            setText(
                "wfScanState",
                startupSide === "back" ? "Starting Back Camera..." : "Camera ready."
            );
            setDot("wfScanDot", "ready");
            if (version !== state.requestVersion) {
                await jsonRequest(URL.wfCancel, "POST", {
                    session_id: startedSessionId
                });
                return;
            }
            await enterFromProgress(progress, sessionData.next_side, true);
        } catch (error) {
            var cleanupError = null;
            if (startedSessionId) {
                try {
                    await jsonRequest(URL.wfCancel, "POST", {
                        session_id: startedSessionId
                    });
                } catch (cancelError) {
                    cleanupError = cancelError;
                }
            }
            if (version !== state.requestVersion) return;
            var failureState = error.data && error.data.state || "";
            var cameraFailure = failureState.indexOf("camera") !== -1 ||
                /camera/i.test(error.message || "");
            var message = error.message || "Inspection could not start.";
            if (cleanupError) {
                message += " The server could not confirm cleanup of the failed session: " +
                    (cleanupError.message || "retry cancellation before starting another inspection.");
            } else if (startedSessionId) {
                clearTransientState();
                showStage("Scan");
            }
            setText("wfScanState", cameraFailure ? "Camera error" : "Inspection error");
            setDot("wfScanDot", "error");
            setText("wfModelState", "Model ready");
            setDot("wfModelDot", "ready");
            setText("wfPartInfo", message);
            showError(message);
        } finally {
            if (state.pendingStartResolve) state.pendingStartResolve();
            state.pendingStartResolve = null;
            state.pendingStart = null;
            state.busy = false;
            if (state.stage === STAGE.SCAN_PART) {
                partSelect.disabled = false;
                partSelect.removeAttribute("aria-busy");
            }
        }
    }

    async function changePart(button) {
        var canCancelSideScan = state.stage === STAGE.FRONT_INSPECTION ||
            state.stage === STAGE.BACK_INSPECTION;
        if (state.cancellingPart || (state.busy && !canCancelSideScan)) return;
        state.cancellingPart = true;
        setBusy(button, true);
        showError("");
        var previousSessionId = state.sessionId;
        var restoreActiveStage = false;
        state.requestVersion += 1;
        cancelFinalTransition();
        stopCameraPolling();
        stopSessionProgressPolling();
        stopPredictionPreview();
        try {
            if (previousSessionId) {
                await jsonRequest(URL.wfCancel, "POST", { session_id: previousSessionId });
            }
            clearTransientState();
            byId("wfBarcode").value = "";
            byId("wfBarcode").focus();
        } catch (error) {
            showError(error.message || "The current inspection could not be closed.");
            restoreActiveStage = Boolean(
                previousSessionId && previousSessionId === state.sessionId && activeCameraSide()
            );
        } finally {
            state.cancellingPart = false;
            setBusy(button, false);
            if (restoreActiveStage) refreshCamera();
        }
    }

    async function loadProgress(sessionId) {
        return jsonRequest(progressUrl(sessionId || state.sessionId), "GET");
    }

    async function enterFromProgress(progress, nextSide, propagateCameraError) {
        if (!progress || !progress.ok) progress = await loadProgress();
        state.frontVerdict = progress.front_verdict || state.frontVerdict;
        state.backVerdict = progress.back_verdict || state.backVerdict;
        setResult("Front", progress.front_status, progress.front_verdict, "session restore");
        setResult("Back", progress.back_status, progress.back_verdict, "session restore");
        if (progress.is_saved) {
            showFinal(progress, {
                inspection_id: progress.inspection_id || state.savedRecordId,
                timestamp: progress.timestamp,
                overall: progress.overall
            });
            return;
        }
        if ([progress.front_status, progress.back_status].every(function (status) {
            return status === "COMPLETED" || status === "SKIPPED";
        })) {
            await finalize();
            return;
        }
        var next = nextSide || (progress.front_status === "SKIPPED" ? "back"
            : progress.front_status === "IN_PROGRESS" ? "front"
            : progress.back_status === "IN_PROGRESS" || progress.front_status === "COMPLETED" ? "back" : "front");
        if (progress.front_status === "COMPLETED" && progress.back_status === "SKIPPED") {
            await finalize();
            return;
        }
        await showInspection(
            next === "back" ? "back" : "front",
            progress,
            propagateCameraError
        );
    }

    async function showInspection(side, progress, propagateCameraError) {
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        showError("");
        var isBack = side === "back";
        var label = isBack ? "Back" : "Front";
        var expectedStage = isBack ? STAGE.BACK_INSPECTION : STAGE.FRONT_INSPECTION;
        state.predictionReady[label] = false;
        state.predictionRetryCount = 0;
        setModelStatus(label, "waiting", "WAITING FOR FIRST VALID FRAME");
        showStage(isBack ? "Back" : "Front");
        clearFeeds();
        try {
            progress = progress || await loadProgress();
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    state.stage !== expectedStage) return;
            setResult(
                isBack ? "Back" : "Front",
                progress[side + "_status"],
                progress[side + "_verdict"],
                "session restore"
            );
        } catch (error) {
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    state.stage !== expectedStage) return;
            setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
            showError(error.message || "Could not load inspection status.");
            return;
        }
        try {
            if (propagateCameraError) {
                setFeed(label);
                setCameraStatus(label, "ON", "");
                startCameraPolling(1800);
                startPredictionPreview(side.toUpperCase());
            } else {
                await activateCamera(side);
            }
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    state.stage !== expectedStage) return;
            if (!propagateCameraError && state.cameraStatus[label] !== "ON") {
                setCameraStatus(label, "CONNECTING", "Waiting for camera frames...");
            }
            if (progress[side + "_status"] === "IN_PROGRESS") {
                var submitButton = byId(isBack ? "wfBackSubmitBtn" : "wfFrontSubmitBtn");
                if (submitButton) submitButton.disabled = true;
                startSessionProgressPolling(side, true);
            }
        } catch (error) {
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    state.stage !== expectedStage) return;
            if (propagateCameraError) throw error;
            setCameraStatus(isBack ? "Back" : "Front", "ERROR", error.message);
            setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
            showError(error.message + " Check the configured camera and retry the inspection stage.");
            if (progress[side + "_status"] === "IN_PROGRESS") {
                startSessionProgressPolling(side, true);
            }
        }
    }

    async function retryCamera(side, triggerButton) {
        if (state.busy) return;
        var button = triggerButton || byId("wf" + side + "RetryCamera");
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        stopPredictionPreview();
        state.predictionReady[side] = false;
        state.predictionRetryCount = 0;
        setFeed(side);
        setResult(side, "PENDING", "PENDING");
        setModelStatus(side, "waiting", "WAITING FOR FIRST VALID FRAME");
        setBusy(button, true);
        showError("");
        try {
            await activateCamera(side.toLowerCase());
        } catch (error) {
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setCameraStatus(side, "ERROR", error.message);
            showError(error.message + " Check the configured camera and retry.");
        } finally {
            if (requestVersion === state.requestVersion) {
                setBusy(button, false);
                if (state.cameraStatus[side] === "ON") {
                    startPredictionPreview(side.toUpperCase());
                }
            }
        }
    }

    function resultDisplayState(status, verdict) {
        var normalizedStatus = String(status || "PENDING").trim().toUpperCase();
        var normalizedVerdict = String(verdict || "PENDING").trim().toUpperCase();

        if (normalizedStatus === "ERROR" || normalizedVerdict === "ERROR") {
            return { text: "ERROR", className: "error" };
        }
        if (normalizedStatus === "SKIPPED" || normalizedVerdict === "NOT_REQUIRED") {
            return { text: "NOT REQUIRED", className: "not_required" };
        }
        if (normalizedStatus === "COMPLETED" && normalizedVerdict === "OK") {
            return { text: "OKAY", className: "ok" };
        }
        if (normalizedStatus === "COMPLETED" && normalizedVerdict === "NOT_OK") {
            return { text: "NOT OKAY", className: "not_ok" };
        }
        if (normalizedStatus === "COMPLETED" && normalizedVerdict === "INDETERMINATE") {
            return { text: "INCOMPLETE", className: "error" };
        }
        if (normalizedStatus === "COMPLETED") {
            return { text: "ERROR", className: "error" };
        }
        if (normalizedStatus === "IN_PROGRESS") {
            return { text: "INSPECTING", className: "inspecting" };
        }
        return { text: "WAITING FOR RESULT", className: "pending" };
    }

    function hasValidSideProgress(progress, side, sessionId) {
        var verdict = progress && progress[side + "_verdict"];
        return Boolean(progress && progress.ok === true &&
            progress.session_id === sessionId &&
            progress[side + "_status"] === "COMPLETED" &&
            ["OK", "NOT_OK", "INDETERMINATE"].includes(verdict));
    }

    function setResult(side, status, verdict, source) {
        var normalizedStatus = String(status || "PENDING").trim().toUpperCase();
        var normalizedVerdict = String(verdict || "PENDING").trim().toUpperCase();
        var previousStatus = state.sideStatus[side] || "UNINITIALIZED";
        var previousVerdict = state[side.toLowerCase() + "Verdict"] || "PENDING";
        state.sideStatus[side] = normalizedStatus;
        state[side.toLowerCase() + "Verdict"] = normalizedVerdict;
        var el = byId("wf" + side + "Result");
        if (el) {
            var currentSessionId = state.sessionId || "";
            var sameSession = el.dataset.workflowSessionId === currentSessionId;
            var renderedStatus = el.dataset.workflowStatus;
            var renderedVerdict = el.dataset.workflowVerdict;
            if (!sameSession ||
                    renderedStatus !== normalizedStatus ||
                    renderedVerdict !== normalizedVerdict) {
                console.info("Inspection result state changed", {
                    session_id: currentSessionId || null,
                    side: side.toLowerCase(),
                    previous_status: sameSession
                        ? renderedStatus || "UNINITIALIZED"
                        : "UNINITIALIZED",
                    status: normalizedStatus,
                    previous_verdict: sameSession
                        ? renderedVerdict || "PENDING"
                        : "PENDING",
                    verdict: normalizedVerdict,
                    source: source || "workflow"
                });
                el.dataset.workflowSessionId = currentSessionId;
                el.dataset.workflowStatus = normalizedStatus;
                el.dataset.workflowVerdict = normalizedVerdict;
            }
        }
        var displayState = resultDisplayState(status, verdict);
        var scanStatus = byId("wfScanResult");
        if (scanStatus) {
            scanStatus.textContent = displayState.text;
            scanStatus.className = "wf-scan-result wf-side-result is-" + displayState.className;
        }
        if (!el) return;
        el.textContent = displayState.text;
        el.className = "wf-side-result wf-prediction-status is-" + displayState.className;
        var resultPanel = el.closest(".wf-camera-result");
        if (resultPanel) {
            resultPanel.className = "wf-stage-result wf-camera-result is-" + displayState.className;
        }
    }

    function renderInspectionOutput(side, data) {
        var image = byId("feed" + side);
        var output = byId("wf" + side + "Detections");
        var detections = Array.isArray(data.detections) ? data.detections : [];
        var masks = Number(data.mask_count) || detections.filter(function (item) {
            return Array.isArray(item.polygon) && item.polygon.length >= 3;
        }).length;
        var details = detections.map(function (item) {
            return (item.class || "Class " + item.class_id) + " " +
                Math.round((Number(item.confidence) || 0) * 100) + "%";
        });
        if (image && typeof data.annotated_frame === "string" &&
                data.annotated_frame.indexOf("data:image/jpeg;base64,") === 0) {
            image.src = data.annotated_frame;
            image.dataset.inspectionResult = "true";
        }
        if (output) {
            var summary = masks + " segmentation mask(s), " + detections.length + " detection(s)";
            if (data.already_completed) {
                summary = data.message || "This inspection side was already completed.";
            }
            output.textContent = details.length ? summary + ": " + details.join(", ") : summary;
        }
    }

    async function submitSide(side) {
        if (state.busy) return;
        if (!isSessionId(state.sessionId)) {
            showError("The active inspection session ID is missing or invalid. Scan the part again.");
            return;
        }
        stopPredictionPreview();
        var isBack = side === "back";
        var button = byId(isBack ? "wfBackSubmitBtn" : "wfFrontSubmitBtn");
        var label = isBack ? "Back" : "Front";
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        var keepProgressPolling = false;
        setBusy(button, true);
        showError("");
        setModelStatus(label, "running", "INFERENCE RUNNING");
        setResult(isBack ? "Back" : "Front", "IN_PROGRESS", "PENDING");
        console.info("Inspection submitted", {
            session_id: state.sessionId,
            side: side
        });
        startSessionProgressPolling(side);
        try {
            var data = await jsonRequest(isBack ? URL.wfInspectBack : URL.wfInspectFront, "POST", {
                session_id: sessionId
            }, INSPECTION_REQUEST_TIMEOUT_MS);
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            var progress = data.progress;
            if (!hasValidSideProgress(progress, side, sessionId)) {
                progress = await loadProgress(sessionId);
            }
            if (!hasValidSideProgress(progress, side, sessionId)) {
                throw new Error("The server completed the request without a valid inspection status. Check the session before retrying.");
            }
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            console.info("Inspection response received", {
                session_id: state.sessionId,
                side: side,
                status: progress.inspection_status || progress[side + "_status"]
            });
            renderInspectionOutput(isBack ? "Back" : "Front", data);
            if (button && progress[side + "_status"] === "COMPLETED") {
                button.dataset.completed = "true";
            }
            if (isBack) state.backVerdict = progress.back_verdict;
            else state.frontVerdict = progress.front_verdict;
            persistLocal();
            setResult(
                isBack ? "Back" : "Front",
                progress[side + "_status"],
                isBack ? state.backVerdict : state.frontVerdict
            );
            if (progress.inspection_status === "OKAY PART" ||
                    progress.inspection_status === "NOT OKAY PART") {
                console.info("Inspection result received and rendered", {
                    session_id: state.sessionId,
                    side: side,
                    status: progress.inspection_status
                });
            }

            if (!isBack && progress.back_status !== "SKIPPED") {
                if (!["OK", "NOT_OK"].includes(state.frontVerdict)) {
                    if (button) button.dataset.completed = "true";
                    setResult("Front", progress.front_status, state.frontVerdict);
                    showError("Front inspection is complete but indeterminate. Configure at least one required front stitch class, then start a new inspection.");
                    return;
                }
                await showInspection("back", progress);
            } else {
                await finalize();
            }
        } catch (error) {
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            if (error.code === "inspection_timeout") {
                setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
                try {
                    await jsonRequest(URL.wfCancel, "POST", {
                        session_id: sessionId
                    });
                    state.sessionId = null;
                    state.sessionActive = false;
                    state.savedRecordId = null;
                    clearLocal();
                    stopCameraPolling();
                    stopPredictionPreview();
                    clearFeeds();
                    showStage("Scan");
                    setText("wfPartInfo", "The timed-out session was cancelled safely. Start a new inspection to retry.");
                } catch (cancelError) {
                    if (button) button.disabled = true;
                    showError(error.message + " Cancellation could not be confirmed; this session will not be resubmitted.");
                }
                return;
            }
            if (error.data && error.data.state === "side_in_progress") {
                setResult(isBack ? "Back" : "Front", "IN_PROGRESS", "PENDING");
                keepProgressPolling = true;
                startSessionProgressPolling(side, true);
                if (button) button.disabled = true;
                showError(error.message || "Inspection is still in progress.");
                return;
            }
            if (error.data && error.data.state === "side_not_required") {
                setResult(isBack ? "Back" : "Front", "SKIPPED", "NOT_REQUIRED");
            } else {
                setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
                setModelStatus(label, "error", "INFERENCE ERROR");
            }
            showError(error.message || "Inspection failed. Reset this session before retrying.");
            if (button) button.disabled = false;
        } finally {
            if (requestVersion === state.requestVersion) {
                if (!keepProgressPolling) stopSessionProgressPolling();
                state.busy = false;
                if (button) {
                    button.setAttribute("aria-busy", "false");
                    if (button.dataset.completed === "true") {
                        button.disabled = true;
                    } else if (state.stage === (isBack ? STAGE.BACK_INSPECTION : STAGE.FRONT_INSPECTION)) {
                        button.disabled = state.sideStatus[label] === "IN_PROGRESS" ||
                            state.cameraStatus[label] !== "ON" ||
                            state.predictionReady[label] !== true;
                        button.textContent = button.disabled
                            ? state.sideStatus[label] === "IN_PROGRESS"
                                ? "INSPECTION IN PROGRESS"
                                : "PREPARING..."
                            : (isBack ? "BACK SCAN" : "FRONT SCAN");
                    }
                }
            }
        }
    }

    async function finalize() {
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        showStage("Saving");
        showError("");
        var retry = byId("wfRetrySaveBtn");
        if (retry) retry.hidden = true;
        setText("wfSavingTitle", "Saving results");
        setText("wfSavingMessage", "Computing the configured verdict and persisting the inspection record...");
        try {
            var resultData = await jsonRequest(URL.wfCompute, "POST", { session_id: sessionId });
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            if (!resultData.progress || !resultData.progress.ok) throw new Error("The backend did not confirm the computed inspection state.");
            state.frontVerdict = resultData.front_verdict || resultData.progress.front_verdict;
            state.backVerdict = resultData.back_verdict || resultData.progress.back_verdict;
            var saved = await jsonRequest(URL.wfSave, "POST", { session_id: sessionId });
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            if (!saved.inspection_id) throw new Error("The backend did not return a persisted inspection ID.");
            state.savedRecordId = saved.inspection_id;
            state.sessionActive = false;
            clearLocal();
            persistLocal();
            var progress = saved.progress || resultData.progress;
            progress.front_verdict = state.frontVerdict;
            progress.back_verdict = state.backVerdict;
            progress.overall = saved.overall || resultData.overall || progress.overall;
            showFinal(progress, saved);
        } catch (error) {
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setText("wfSavingTitle", "Save failed");
            setText("wfSavingMessage", error.message + " The session is retained; retry is safe.");
            if (retry) retry.hidden = false;
            showError(error.message);
            state.stage = STAGE.SAVING_RESULTS;
        }
    }

    function showFinal(progress, saved) {
        progress = progress || {};
        saved = saved || {};
        var inspectionId = saved.inspection_id || progress.inspection_id || state.savedRecordId;
        if (!inspectionId) {
            showStage("Saving");
            setText("wfSavingTitle", "Inspection reference unavailable");
            setText("wfSavingMessage", "The backend did not return a persisted inspection ID. Do not rescan this part; ask a supervisor to verify the inspection record.");
            byId("wfRetrySaveBtn").hidden = true;
            return;
        }
        var verdict = saved.overall || progress.overall || "INCOMPLETE";
        var title = byId("wfFinalTitle");
        if (title) {
            title.textContent = verdict === "OK" ? "OKAY PART"
                : verdict === "NOT_OK" ? "NOT OKAY PART" : "INCOMPLETE / ERROR";
            title.className = "wf-verdict " + (verdict === "OK" ? "is-ok"
                : verdict === "NOT_OK" ? "is-not-ok" : "is-error");
        }
        setText("wfFinalPart", state.partNumber || progress.part_number);
        setText("wfFinalId", inspectionId);
        var persistedTimestamp = saved.timestamp || progress.timestamp;
        var parsedTimestamp = persistedTimestamp ? new Date(persistedTimestamp) : null;
        setText("wfFinalTime", parsedTimestamp && !Number.isNaN(parsedTimestamp.getTime())
            ? parsedTimestamp.toLocaleString() : "Not recorded");
        setText("wfFinalFront", progress.front_verdict || state.frontVerdict);
        setText("wfFinalBack", progress.back_status === "SKIPPED" ? "NOT REQUIRED" : (progress.back_verdict || state.backVerdict));
        setText("wfFinalSaved", "SAVED");
        setText("wfFinalMessage", "Inspection record persisted successfully.");
        state.sessionActive = false;
        clearLocal();
        showStage("Final");
        stopCameraPolling();
        clearFeeds();
        byId("wfNewInspectionBtn").hidden = true;
        byId("wfChangePartBtn").hidden = true;
        if (!state.finalTransitionScheduled) {
            state.finalTransitionScheduled = true;
            var completedSession = state.sessionId;
            var requestVersion = state.requestVersion;
            state.finalTransitionTimer = window.setTimeout(function () {
                state.finalTransitionTimer = null;
                if (state.stage !== STAGE.FINAL_RESULT ||
                        state.sessionId !== completedSession ||
                        state.requestVersion !== requestVersion) return;
                newInspection();
            }, RESULT_DISPLAY_MS);
        }
    }

    async function retrySave() {
        if (state.busy) return;
        var button = byId("wfRetrySaveBtn");
        setBusy(button, true);
        await finalize();
        if (button) button.disabled = false;
    }

    async function newInspection() {
        if (state.busy || !state.partNumber) return;
        var button = byId("wfNewInspectionBtn");
        var requestVersion = state.requestVersion;
        setBusy(button, true);
        showError("");
        state.validatedModelId = null;
        try {
            var validation = await jsonRequest(URL.wfValidate, "POST", {
                part_number: state.partNumber
            });
            if (requestVersion !== state.requestVersion ||
                    state.stage !== STAGE.FINAL_RESULT) return;
            if (!validation.model_id) {
                throw new Error("The selected part no longer has a valid inspection model.");
            }
            state.validatedModelId = validation.model_id;
            setText("wfScanState", "Part valid: " + state.partNumber);
            setDot("wfScanDot", "ready");
            setText("wfModelState", validation.message || "Model and configuration valid");
            setDot("wfModelDot", "ready");
            setBusy(button, false);
            await startInspection(button);
        } catch (error) {
            if (requestVersion !== state.requestVersion ||
                    state.stage !== STAGE.FINAL_RESULT) return;
            showError(error.message || "The selected part could not be revalidated.");
            setText("wfFinalMessage", "The result was saved, but the next cycle could not start. " +
                (error.message || "Revalidate the part before continuing."));
            byId("wfNewInspectionBtn").hidden = false;
            byId("wfNewInspectionBtn").textContent = "RETRY NEXT CYCLE";
            byId("wfChangePartBtn").hidden = false;
            state.finalTransitionScheduled = false;
        } finally {
            setBusy(button, false);
        }
    }

    async function refreshCamera() {
        if (state.cameraInFlight || !state.sessionId) return;
        var side = state.stage === STAGE.FRONT_INSPECTION ? "Front"
            : state.stage === STAGE.BACK_INSPECTION ? "Back" : null;
        if (!side) return;
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        var previewReadyAtRequest = state.predictionReady[side] === true;
        state.cameraInFlight = true;
        var controller = typeof AbortController === "function"
            ? new AbortController() : null;
        state.cameraStatusController = controller;
        try {
            var data = await cameraStatusRequest(
                sessionId,
                controller ? controller.signal : null
            );
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion || state.stage !== (side === "Front" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (!previewReadyAtRequest &&
                    state.predictionReady[side] === true) return;
            if (data.status === "ON" && data.ready === true) {
                if (state.cameraStatus[side] !== "ON") setFeed(side);
                setCameraStatus(side, "ON", "");
                startCameraPolling(1800);
                if (!state.busy) startPredictionPreview(side.toUpperCase());
            } else if (data.status === "ERROR") {
                setCameraStatus(side, "ERROR", data.error || "Configured camera is not connected.");
                setResult(side, "ERROR", "ERROR");
                setModelStatus(side, "waiting", "WAITING FOR CAMERA");
            } else if (data.status === "CONNECTING" ||
                    (data.status === "ON" && data.ready !== true)) {
                setCameraStatus(side, "CONNECTING", "Waiting for camera frames...");
                setModelStatus(side, "waiting", "WAITING FOR FIRST VALID FRAME");
            } else if (data.status === "STOPPING") {
                setCameraStatus(side, "CONNECTING", "Waiting for the camera to stop safely...");
                setModelStatus(side, "waiting", "WAITING FOR CAMERA");
            } else {
                setCameraStatus(side, "ERROR", data.error || "Configured camera is stopped.");
                setResult(side, "ERROR", "ERROR");
                setModelStatus(side, "waiting", "WAITING FOR CAMERA");
            }
        } catch (error) {
            if (error.name !== "AbortError" &&
                    sessionId === state.sessionId &&
                    requestVersion === state.requestVersion &&
                    state.stage === (side === "Front"
                        ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) {
                var errorState = error.data && error.data.state;
                if (errorState === "session_inactive" ||
                        errorState === "session_expired") {
                    handleInactiveCameraSession(
                        sessionId,
                        requestVersion,
                        error
                    );
                    return;
                }
                if (error.status === 409) {
                    stopCameraPolling();
                    stopPredictionPreview();
                }
                setCameraStatus(
                    side,
                    "ERROR",
                    error.message || "Camera status is unavailable."
                );
                setResult(side, "ERROR", "ERROR");
            }
        } finally {
            if (state.cameraStatusController === controller) {
                state.cameraStatusController = null;
            }
            state.cameraInFlight = false;
        }
    }
    function startCameraPolling(interval) {
        interval = interval || 1800;
        if (state.cameraTimer && state.cameraPollInterval === interval) return;
        if (state.cameraTimer) window.clearInterval(state.cameraTimer);
        state.cameraPollInterval = interval;
        state.cameraTimer = window.setInterval(refreshCamera, interval);
    }
    function stopCameraPolling() {
        if (state.cameraTimer) window.clearInterval(state.cameraTimer);
        state.cameraTimer = null;
        state.cameraPollInterval = null;
        if (state.cameraReadyTimer) window.clearTimeout(state.cameraReadyTimer);
        state.cameraReadyTimer = null;
        if (state.cameraStatusController) {
            state.cameraStatusController.abort();
            state.cameraStatusController = null;
        }
    }

    function handleInactiveCameraSession(sessionId, requestVersion, error) {
        if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
        state.requestVersion += 1;
        stopCameraPolling();
        stopPredictionPreview();
        stopSessionProgressPolling();
        clearFeeds();
        state.sessionId = null;
        state.sessionActive = false;
        state.partNumber = "";
        state.partId = null;
        state.configurationId = null;
        state.validatedModelId = null;
        state.predictionReady = { Front: false, Back: false };
        state.sideStatus = { Front: "PENDING", Back: "PENDING" };
        state.predictionRetryCount = 0;
        state.busy = false;
        clearLocal();
        ["wfFrontSubmitBtn", "wfBackSubmitBtn"].forEach(function (id) {
            var button = byId(id);
            button.disabled = true;
            button.dataset.completed = "false";
        });
        byId("wfBarcode").disabled = false;
        byId("wfBarcode").removeAttribute("aria-busy");
        showStage("Scan");
        setText("wfScanState", "Inspection session ended");
        setDot("wfScanDot", "error");
        setText("wfModelState", "Scan again to start a new session");
        setDot("wfModelDot", "idle");
        setText("wfPartInfo", error.message);
        showError(error.message);
    }

    async function resumeSession(saved) {
        var progress = await loadProgress(saved.sessionId);
        var expectedConfigurationId = Number(saved.configurationId) || null;
        if (progress.ok !== true ||
                progress.session_id !== saved.sessionId ||
                progress.part_number !== saved.partNumber ||
                (expectedConfigurationId &&
                    Number(progress.configuration_id) !== expectedConfigurationId)) {
            var invalidProgressError = new Error(
                "The saved inspection status does not match its session, part, or configuration."
            );
            invalidProgressError.recoverable = false;
            throw invalidProgressError;
        }
        if (state.resumeRetryTimer) window.clearTimeout(state.resumeRetryTimer);
        state.resumeRetryTimer = null;
        state.resumeRetryCount = 0;
        if (progress.is_saved) {
            state.sessionId = saved.sessionId;
            state.partNumber = saved.partNumber;
            state.partId = saved.partId;
            state.configurationId = progress.configuration_id || saved.configurationId || null;
            state.validatedModelId = saved.validatedModelId;
            state.savedRecordId = saved.savedRecordId || saved.sessionId;
            state.sessionActive = false;
            setResult("Front", progress.front_status, progress.front_verdict);
            setResult("Back", progress.back_status, progress.back_verdict);
            showFinal(progress, {
                inspection_id: progress.inspection_id || state.savedRecordId,
                timestamp: progress.timestamp,
                overall: progress.overall
            });
            return;
        }
        if (!progress.is_active) {
            clearLocal();
            clearTransientState();
            var message = progress.state === "session_expired"
                ? progress.message || "This inspection session expired. Scan the part again."
                : "This inspection session is no longer active. Scan the part again.";
            setText("wfPartInfo", message);
            showError(message);
            return;
        }
        if (!saved.partNumber || !Number(saved.partId) || !Number(saved.validatedModelId)) {
            throw new Error(
                "The active inspection is valid, but this browser cannot restore its part/model details. Scan the part again to continue safely."
            );
        }

        state.requestVersion += 1;
        state.sessionId = saved.sessionId;
        state.sessionActive = true;
        state.partNumber = saved.partNumber;
        state.partId = Number(saved.partId);
        state.configurationId = progress.configuration_id || saved.configurationId || null;
        state.validatedModelId = Number(saved.validatedModelId);
        state.savedRecordId = saved.savedRecordId || null;
        state.frontVerdict = progress.front_verdict || "PENDING";
        state.backVerdict = progress.back_verdict || "PENDING";
        persistLocal();
        var side = progress.stage === "back" ||
            progress.front_status === "COMPLETED" ||
            progress.front_status === "SKIPPED" ? "back" : "front";
        setText(
            "wfPartInfo",
            "Resuming the active inspection for " + state.partNumber + "."
        );
        await enterFromProgress(progress, side);
    }

    function scheduleSessionRestoreRetry(saved, error) {
        var errorState = error.data && error.data.state;
        if (error.recoverable === false ||
                errorState === "session_inactive" ||
                errorState === "session_expired" ||
                errorState === "session_not_found") {
            clearTransientState();
            var terminalMessage = error.message ||
                "The saved inspection session is no longer available.";
            setText("wfPartInfo", terminalMessage);
            showError(terminalMessage);
            return;
        }
        if (state.resumeRetryCount >= 5) {
            var retryMessage = "Could not reconnect to the saved inspection. " +
                "The session is retained; reload the page when the connection is restored.";
            setText("wfPartInfo", retryMessage);
            showError(retryMessage);
            return;
        }
        state.resumeRetryCount += 1;
        var delay = Math.min(10000, 1000 * Math.pow(2, state.resumeRetryCount - 1));
        var message = "Unable to refresh inspection status. Retrying in " +
            Math.ceil(delay / 1000) + " seconds.";
        setText("wfPartInfo", message);
        showError(error.message ? message + " " + error.message : message);
        if (state.resumeRetryTimer) window.clearTimeout(state.resumeRetryTimer);
        state.resumeRetryTimer = window.setTimeout(function () {
            state.resumeRetryTimer = null;
            resumeSession(saved).catch(function (retryError) {
                scheduleSessionRestoreRetry(saved, retryError);
            });
        }, delay);
    }

    async function prepareFreshScan() {
        var raw;
        try { raw = sessionStorage.getItem("viInspectionWorkflow"); }
        catch (error) { raw = null; }
        if (!raw) return;
        var saved;
        try {
            saved = JSON.parse(raw);
        } catch (error) {
            clearLocal();
            return;
        }
        if (!saved || !isSessionId(saved.sessionId)) {
            clearLocal();
            return;
        }
        try {
            await resumeSession(saved);
        } catch (error) {
            scheduleSessionRestoreRetry(saved, error);
        }
    }

    function hasUnfinishedSession() {
        return Boolean(state.sessionActive || state.pendingStart);
    }

    function handleBeforeUnload(event) {
        if (state.stage === STAGE.FINAL_RESULT) {
            cancelFinalTransition();
            state.requestVersion += 1;
        }
        if (!hasUnfinishedSession()) return;
        event.preventDefault();
        event.returnValue = "";
    }

    function pauseInspectionPage() {
        cancelFinalTransition();
        if (state.resumeRetryTimer) window.clearTimeout(state.resumeRetryTimer);
        state.resumeRetryTimer = null;
        state.requestVersion += 1;
        stopActivityMonitoring();
        stopCameraPolling();
        stopSessionProgressPolling();
        stopPredictionPreview();
        clearFeeds();
    }

    function openLeaveDialog(targetUrl, trigger) {
        state.leaveTarget = targetUrl;
        state.leaveInvoker = trigger;
        var dialog = byId("wfLeaveDialog");
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
            byId("wfStayInspectionBtn").focus();
            return;
        }
        if (window.confirm(
            "Cancel this inspection and leave? You will need to scan and validate the part again."
        )) {
            leaveInspection();
        }
    }

    async function leaveInspection() {
        var button = byId("wfCancelLeaveBtn");
        if (button) button.disabled = true;
        try {
            if (state.pendingStart) await state.pendingStart;
            if (state.sessionId && state.sessionActive) {
                await jsonRequest(URL.wfCancel, "POST", {
                    session_id: state.sessionId
                });
            }
            var targetUrl = state.leaveTarget;
            clearTransientState();
            byId("wfLeaveDialog").close();
            if (targetUrl) window.location.assign(targetUrl);
        } catch (error) {
            showError(error.message || "The inspection could not be cancelled. Stay on this page and retry.");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function interceptNavigation(event) {
        if (event.button !== 0 || event.metaKey || event.ctrlKey ||
                event.shiftKey || event.altKey) return;
        var link = event.target && event.target.closest
            ? event.target.closest("a[href]") : null;
        if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
        var target;
        try { target = new URL(link.href, window.location.href); }
        catch (error) { return; }
        if (target.origin !== window.location.origin ||
                target.href === window.location.href) return;
        if (state.stage === STAGE.FINAL_RESULT) {
            cancelFinalTransition();
            state.requestVersion += 1;
            return;
        }
        if (!hasUnfinishedSession()) return;
        event.preventDefault();
        event.stopPropagation();
        openLeaveDialog(target.href, link);
    }

    function closeLeaveDialog() {
        var dialog = byId("wfLeaveDialog");
        if (dialog.open) dialog.close();
        if (state.leaveInvoker && document.contains(state.leaveInvoker)) {
            state.leaveInvoker.focus();
        }
        state.leaveTarget = null;
        state.leaveInvoker = null;
    }

    function init() {
        if (initialized) return;
        initialized = true;
        byId("wfBarcode").addEventListener("change", handlePartSelectionChange);
        if (byId("wfBarcode").value) handlePartSelectionChange();
        byId("wfFrontSubmitBtn").addEventListener("click", function () { submitSide("front"); });
        byId("wfBackSubmitBtn").addEventListener("click", function () { submitSide("back"); });
        byId("wfFrontChangePartBtn").addEventListener("click", function () { changePart(this); });
        byId("wfBackChangePartBtn").addEventListener("click", function () { changePart(this); });
        byId("wfFrontRetryCamera").addEventListener("click", function () { retryCamera("Front", this); });
        byId("wfBackRetryCamera").addEventListener("click", function () { retryCamera("Back", this); });
        byId("wfFrontRetryModel").addEventListener("click", function () { retryPrediction("Front"); });
        byId("wfBackRetryModel").addEventListener("click", function () { retryPrediction("Back"); });
        byId("wfRetrySaveBtn").addEventListener("click", retrySave);
        byId("wfNewInspectionBtn").addEventListener("click", newInspection);
        byId("wfChangePartBtn").addEventListener("click", function () {
            changePart(byId("wfChangePartBtn"));
        });
        byId("wfStayInspectionBtn").addEventListener("click", closeLeaveDialog);
        byId("wfCancelLeaveBtn").addEventListener("click", leaveInspection);
        document.addEventListener("click", interceptNavigation, true);
        startActivityMonitoring();
        window.addEventListener("beforeunload", handleBeforeUnload);
        window.addEventListener("pagehide", pauseInspectionPage);
        window.addEventListener("pageshow", function (event) {
            startActivityMonitoring();
            if (event.persisted) {
                prepareFreshScan().finally(function () {
                    state.ready = true;
                });
            }
        });
        window.addEventListener("vi:signout", function () {
            state.requestVersion += 1;
            clearTransientState();
            stopActivityMonitoring();
        });
        showStage("Scan");
        prepareFreshScan().finally(function () {
            state.ready = true;
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
