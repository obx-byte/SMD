(function () {
    "use strict";

    var INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000;
    var INSPECTION_REQUEST_TIMEOUT_MS = 120 * 1000;
    var CAMERA_READY_TIMEOUT_MS = 20000;
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
        selectedBarcode: "",
        validatedModelId: null,
        predictionTimer: null,
        predictionInFlight: false,
        predictionSide: null,
        predictionGeneration: 0,
        frontVerdict: "PENDING",
        backVerdict: "PENDING",
        savedRecordId: null,
        busy: false,
        cameraTimer: null,
        cameraReadyTimer: null,
        cameraPollInterval: null,
        cameraInFlight: false,
        sessionPollTimer: null,
        sessionPollInFlight: false,
        sessionPollGeneration: 0,
        requestVersion: 0,
        cameraStatus: { Front: "OFF", Back: "OFF" },
        predictionReady: { Front: false, Back: false },
        predictionRetryCount: 0,
        inactivityTimer: null,
        countdownTimer: null,
        inactivityDeadline: null,
        screenSaverActive: false,
        activityMonitoring: false,
        leaveInvoker: null,
        ready: false
    };
    var initialized = false;

    function byId(id) { return document.getElementById(id); }
    function csrf() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return cfg.csrf || (input && input.value) || "";
    }
    function stageNode(name) { return byId("wfStage" + name); }
    function showStage(name) {
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
            node.className = "wf-camera-status is-" + status.toLowerCase();
            node.innerHTML = "<i></i> " + status;
        }
        if (overlay) {
            overlay.textContent = message || (status === "ON" ? "" : "Camera " + status.toLowerCase());
            overlay.hidden = status === "ON";
        }
        var retry = byId("wf" + side + "RetryCamera");
        if (retry) retry.hidden = status !== "ERROR";
        var button = byId(side === "Front" ? "wfFrontSubmitBtn" : "wfBackSubmitBtn");
        if (button && !state.busy) {
            button.disabled = status !== "ON" ||
                state.predictionReady[side] !== true ||
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

    async function jsonRequest(url, method, body, timeoutMs) {
        var controller = timeoutMs && typeof AbortController === "function"
            ? new AbortController() : null;
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
                signal: controller ? controller.signal : undefined
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
    async function cameraStatusRequest(sessionId) {
        var url = URL.cameraStatus;
        if (sessionId) {
            url += (url.indexOf("?") === -1 ? "?" : "&") +
                "session_id=" + encodeURIComponent(sessionId);
        }
        var response = await fetch(url, {
            headers: { Accept: "application/json" }
        });
        var data;
        try { data = await response.json(); }
        catch (error) { throw new Error("The camera status response was unreadable."); }
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
        state.requestVersion += 1;
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
        state.selectedBarcode = "";
        state.validatedModelId = null;
        state.predictionReady = { Front: false, Back: false };
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
        setText("wfSavingTitle", "Saving results");
        setText("wfSavingMessage", "The backend is finalizing this inspection session.");
        byId("wfRetrySaveBtn").hidden = true;
        ["wfFrontSubmitBtn", "wfBackSubmitBtn"].forEach(function (id) {
            var button = byId(id);
            button.dataset.completed = "false";
        });
        setModelStatus("Front", "waiting", "MODEL WAITING");
        setModelStatus("Back", "waiting", "MODEL WAITING");
        setText("wfPartInfo", "Ready to scan.");
        setText("wfScanState", "Waiting for scan");
        setText("wfModelState", "Waiting for part");
        setText("wfModelDetail", "");
        byId("wfModelDetail").hidden = true;
        byId("wfPartConfigLink").hidden = true;
        byId("wfStartInspectionBtn").disabled = true;
        renderOperatorInstructions(null);
        setDot("wfScanDot", "idle");
        setDot("wfModelDot", "idle");
        showError("");
        byId("wfStartInspectionBtn").disabled = true;
        byId("wfScanBtn").disabled = false;
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
        if (state.predictionTimer) window.clearTimeout(state.predictionTimer);
        state.predictionTimer = null;
    }

    function stopSessionProgressPolling() {
        state.sessionPollGeneration += 1;
        if (state.sessionPollTimer) window.clearTimeout(state.sessionPollTimer);
        state.sessionPollTimer = null;
        state.sessionPollInFlight = false;
    }

    function startSessionProgressPolling(side) {
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
            try {
                var progress = await loadProgress(sessionId);
                if (generation !== state.sessionPollGeneration || sessionId !== state.sessionId) return;
                state.frontVerdict = progress.front_verdict || state.frontVerdict;
                state.backVerdict = progress.back_verdict || state.backVerdict;
                setResult("Front", progress.front_status, progress.front_verdict, progress.inspection_status);
                setResult("Back", progress.back_status, progress.back_verdict, progress.inspection_status);
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
                if (progress.inspection_status === "OKAY PART" ||
                        progress.inspection_status === "NOT OKAY PART") {
                    console.info("Inspection result received and rendered", {
                        session_id: sessionId,
                        side: side,
                        status: progress.inspection_status
                    });
                    stopSessionProgressPolling();
                    return;
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
                state.sessionPollTimer = window.setTimeout(poll, 750);
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
        state.predictionInFlight = true;
        try {
            var url = URL.partVisionPreview.replace(
                "__PART_ID__",
                encodeURIComponent(state.partId)
            ) + "?side=" + encodeURIComponent(side) +
                "&model_id=" + encodeURIComponent(state.validatedModelId) +
                "&session_id=" + encodeURIComponent(state.sessionId) +
                "&confidence=0.30&show_masks=1&show_boxes=1";
            var data = await jsonRequest(url, "GET", null, INSPECTION_REQUEST_TIMEOUT_MS);
            if (sessionId !== state.sessionId ||
                    requestVersion !== state.requestVersion ||
                    generation !== state.predictionGeneration ||
                    state.predictionSide !== side ||
                    state.stage !== (side === "FRONT" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (typeof data.image !== "string" ||
                    data.image.indexOf("data:image/jpeg;base64,") !== 0 ||
                    !Array.isArray(data.detections) ||
                    data.inference_status !== "complete" ||
                    !Number.isInteger(Number(data.frame_seq)) ||
                    Number(data.frame_seq) < 1) {
                throw new Error("The live prediction response did not confirm a completed real-frame inference.");
            }
            image.src = data.image;
            state.predictionReady[side === "FRONT" ? "Front" : "Back"] = true;
            state.predictionRetryCount = 0;
            setModelStatus(
                side === "FRONT" ? "Front" : "Back",
                "ready",
                "INFERENCE READY · FRAME " + data.frame_seq
            );
            var label = side === "FRONT" ? "Front" : "Back";
            if (state.cameraStatus[label] === "ON") setCameraStatus(label, "ON", "");
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
                    setModelStatus(label, "roi-error", "ROI CONFIGURATION ERROR");
                    return;
                }
                if (configurationError || (error.status === 409 && !errorCode)) {
                    setModelStatus(label, "configuration-error", "PREVIEW CONFIGURATION ERROR");
                    return;
                }
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
        var barcode = (byId("wfBarcode").value || "").trim();
        if (!barcode) {
            setText("wfPartInfo", "Scan or enter a part barcode.");
            return;
        }
        var button = byId("wfScanBtn");
        var version = ++state.requestVersion;
        setBusy(button, true);
        byId("wfStartInspectionBtn").disabled = true;
        byId("wfPartConfigLink").hidden = true;
        byId("wfModelDetail").hidden = true;
        setText("wfModelDetail", "");
        state.validatedModelId = null;
        showError("");
        setText("wfScanState", "Validating part...");
        setDot("wfScanDot", "working");
        setText("wfModelState", "Waiting for part");
        setDot("wfModelDot", "idle");
        try {
            if (!await resetPreviousSession()) return;
            version = state.requestVersion;
            var partData = await jsonRequest(URL.wfScan, "POST", { barcode: barcode });
            if (version !== state.requestVersion) return;
            if (partData.ok !== true || !partData.part ||
                    typeof partData.part.part_number !== "string" ||
                    !partData.part.part_number.trim()) {
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
            state.partId = Number.isInteger(Number(part.id)) && Number(part.id) > 0
                ? Number(part.id) : null;
            state.selectedBarcode = barcode;
            var configLink = byId("wfPartConfigLink");
            if (part.id && configLink && URL.partVisionConfig) {
                configLink.href = URL.partVisionConfig.replace(
                    "__PART_ID__",
                    encodeURIComponent(part.id)
                );
                configLink.hidden = false;
            }
            setText("wfScanState", "Part valid: " + partNumber);
            setDot("wfScanDot", "ready");
            setText("wfPartInfo", partData.message || ("Part identified: " + partNumber + "."));

            setText("wfModelState", "Validating configured model...");
            setDot("wfModelDot", "working");
            byId("wfModelDetail").hidden = true;
            setText("wfModelDetail", "");
            showError("");
            try {
                var modelData = await jsonRequest(URL.wfValidate, "POST", { part_number: partNumber });
                if (version !== state.requestVersion) return;
                if (modelData.ok !== true || !Number.isInteger(Number(modelData.model_id)) ||
                        Number(modelData.model_id) < 1) {
                    throw new Error(modelData.message || "The model validation response did not identify a valid model.");
                }
                state.validatedModelId = Number(modelData.model_id);
                setText("wfModelState", modelData.message || "Model and configuration valid");
                setDot("wfModelDot", "ready");
                renderOperatorInstructions(modelData.operator_instructions);
                byId("wfModelDetail").hidden = true;
                setText("wfPartInfo", "Review the configured sides and inspection rules, then start the inspection.");
                persistLocal();
                setBusy(button, false);
                byId("wfStartInspectionBtn").disabled = false;
            } catch (error) {
                if (version !== state.requestVersion) return;
                var validationState = error.data && error.data.state;
                setText("wfModelState", validationState
                    ? validationState.replaceAll("_", " ")
                    : "Validation failed");
                setDot("wfModelDot", "error");
                setText("wfModelDetail", error.message || "Model validation failed.");
                byId("wfModelDetail").hidden = false;
                setText("wfPartInfo", "Part " + partNumber + " is valid, but its inspection model/configuration is not ready.");
                showError("Model validation failed. Correct the saved Part Vision settings, then scan again.");
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
            setBusy(button, false);
        }
    }

    async function startInspection(button) {
         if (state.busy || !state.partNumber || !state.validatedModelId) return;

    // Resolve the actual button from the click event.
    var button = event && event.currentTarget
        ? event.currentTarget
        : byId("wfStartInspectionBtn");

    var version = ++state.requestVersion;
    setBusy(button, true);
    showError("");
        state.pendingStart = new Promise(function (resolve) {
            state.pendingStartResolve = resolve;
        });
        try {
            var sessionData = await jsonRequest(URL.wfSubmit, "POST", {
                part_number: state.partNumber,
                model_id: state.validatedModelId
            });
            if (version !== state.requestVersion) return;
            if (!isSessionId(sessionData.session_id)) {
                throw new Error("The server did not return a valid inspection session ID.");
            }
            state.sessionId = sessionData.session_id;
            state.sessionActive = true;
            state.savedRecordId = null;
            state.frontVerdict = "PENDING";
            state.backVerdict = "PENDING";
            ["Front", "Back"].forEach(function (side) {
                var submitButton = byId("wf" + side + "SubmitBtn");
                submitButton.dataset.completed = "false";
                submitButton.disabled = true;
                setResult(side, "PENDING", "PENDING");
            });
            persistLocal();
            setText("wfPartInfo", sessionData.message || "Inspection session started.");
            var progress = sessionData.progress || await loadProgress();
            if (version === state.requestVersion) await enterFromProgress(progress, sessionData.next_side);
        } catch (error) {
            if (version !== state.requestVersion) return;
            setText("wfScanState", (error.data && error.data.state) || "Inspection could not start");
            setDot("wfScanDot", "error");
            setText("wfPartInfo", error.message || "Inspection could not start.");
            showError(error.message);
        } finally {
            if (state.pendingStartResolve) state.pendingStartResolve();
            state.pendingStartResolve = null;
            state.pendingStart = null;
            setBusy(button, false);
        }
    }

    async function changePart(button) {
        if (state.busy) return;
        setBusy(button, true);
        showError("");
        try {
            if (state.sessionId) {
                await jsonRequest(URL.wfCancel, "POST", { session_id: state.sessionId });
            }
            state.requestVersion += 1;
            clearTransientState();
            byId("wfBarcode").value = "";
            byId("wfBarcode").focus();
        } catch (error) {
            showError(error.message || "The current inspection could not be closed.");
        } finally {
            setBusy(button, false);
        }
    }

    async function loadProgress(sessionId) {
        return jsonRequest(progressUrl(sessionId || state.sessionId), "GET");
    }

    async function enterFromProgress(progress, nextSide) {
        if (!progress || !progress.ok) progress = await loadProgress();
        state.frontVerdict = progress.front_verdict || state.frontVerdict;
        state.backVerdict = progress.back_verdict || state.backVerdict;
        setResult("Front", progress.front_status, progress.front_verdict, progress.inspection_status);
        setResult("Back", progress.back_status, progress.back_verdict, progress.inspection_status);
        if (progress.is_saved) {
            showFinal(progress, { inspection_id: state.savedRecordId || state.sessionId, overall: progress.overall });
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
        await showInspection(next === "back" ? "back" : "front", progress);
    }

    async function showInspection(side, progress) {
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        showError("");
        var isBack = side === "back";
        var label = isBack ? "Back" : "Front";
        state.predictionReady[label] = false;
        state.predictionRetryCount = 0;
        setModelStatus(label, "waiting", "WAITING FOR FIRST VALID FRAME");
        showStage(isBack ? "Back" : "Front");
        clearFeeds();
        try {
            progress = progress || await loadProgress();
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setResult(
                isBack ? "Back" : "Front",
                progress[side + "_status"],
                progress[side + "_verdict"],
                progress.inspection_status
            );
        } catch (error) {
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
            showError(error.message || "Could not load inspection status.");
            return;
        }
        try {
            await activateCamera(side);
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setCameraStatus(isBack ? "Back" : "Front", "CONNECTING", "Waiting for camera frames...");
        } catch (error) {
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion) return;
            setCameraStatus(isBack ? "Back" : "Front", "ERROR", error.message);
            showError(error.message + " Check the configured camera and retry the inspection stage.");
        }
    }

    async function retryCamera(side) {
        if (state.busy) return;
        var button = byId("wf" + side + "RetryCamera");
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
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
                state.busy = false;
                if (button) button.disabled = false;
            }
        }
    }

    function resultDisplayState(inspectionStatus, status, verdict) {
        var normalizedStatus = String(status || "PENDING").trim().toUpperCase();
        var normalizedVerdict = String(verdict || "PENDING").trim().toUpperCase();
        var normalizedOverall = String(inspectionStatus || "").trim().toUpperCase();
        if (normalizedStatus === "ERROR" || normalizedVerdict === "ERROR" || normalizedOverall === "ERROR") {
            return { text: "ERROR", className: "error" };
        }
        if (normalizedOverall === "INSPECTING" || normalizedStatus === "IN_PROGRESS") {
            return { text: "INSPECTING", className: "inspecting" };
        }
        if (normalizedOverall === "OKAY PART") return { text: "OKAY PART", className: "ok" };
        if (normalizedOverall === "NOT OKAY PART") return { text: "NOT OKAY PART", className: "not_ok" };
        return { text: "REQUEST PENDING", className: "pending" };
    }

    function setResult(side, status, verdict, inspectionStatus) {
        var displayState = resultDisplayState(inspectionStatus, status, verdict);
        var el = byId("wf" + side + "Result");
        var scanStatus = byId("wfScanResult");
        if (scanStatus) {
            scanStatus.textContent = displayState.text;
            scanStatus.className = "wf-scan-result wf-side-result is-" + displayState.className;
        }
        if (!el) return;
        el.textContent = displayState.text;
        el.className = "wf-side-result is-" + displayState.className;
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
        var sessionId = state.sessionId;
        var requestVersion = state.requestVersion;
        setBusy(button, true);
        showError("");
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
            var progress = data.progress || await loadProgress(sessionId);
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
                isBack ? state.backVerdict : state.frontVerdict,
                progress.inspection_status
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
                if (button) button.disabled = true;
                showError(error.message || "Inspection is still in progress.");
                return;
            }
            if (error.data && error.data.state === "side_not_required") {
                setResult(isBack ? "Back" : "Front", "SKIPPED", "NOT_REQUIRED");
            } else {
                setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
            }
            showError(error.message || "Inspection failed. Reset this session before retrying.");
            if (button) button.disabled = false;
        } finally {
            if (requestVersion === state.requestVersion) {
                stopSessionProgressPolling();
                state.busy = false;
                if (button) {
                    button.setAttribute("aria-busy", "false");
                    if (button.dataset.completed === "true") {
                        button.disabled = true;
                    } else if (state.stage === (isBack ? STAGE.BACK_INSPECTION : STAGE.FRONT_INSPECTION)) {
                        button.disabled = false;
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
            if (!saved.inspection_id && !saved.session_id) throw new Error("The backend did not confirm persistence of this inspection.");
            state.savedRecordId = saved.inspection_id || saved.session_id;
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
        var verdict = saved.overall || progress.overall || "INCOMPLETE";
        var title = byId("wfFinalTitle");
        if (title) {
            title.textContent = verdict === "OK" ? "OKAY PART" : "NOT OKAY PART";
            title.className = "wf-verdict " + (verdict === "OK" ? "is-ok" : "is-not-ok");
        }
        setText("wfFinalPart", state.partNumber || progress.part_number);
        setText("wfFinalId", saved.inspection_id || state.sessionId);
        setText("wfFinalTime", new Date().toLocaleString());
        setText("wfFinalFront", progress.front_verdict || state.frontVerdict);
        setText("wfFinalBack", progress.back_status === "SKIPPED" ? "NOT REQUIRED" : (progress.back_verdict || state.backVerdict));
        setText("wfFinalSaved", "SAVED");
        setText("wfFinalMessage", "Inspection record persisted successfully.");
        state.sessionActive = false;
        clearLocal();
        showStage("Final");
        stopCameraPolling();
        clearFeeds();
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
        setBusy(button, true);
        showError("");
        state.validatedModelId = null;
        try {
            var validation = await jsonRequest(URL.wfValidate, "POST", {
                part_number: state.partNumber
            });
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
            showError(error.message || "The selected part could not be revalidated.");
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
        state.cameraInFlight = true;
        try {
            var data = await cameraStatusRequest(sessionId);
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion || state.stage !== (side === "Front" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (data.status === "ON" && data.ready === true) {
                if (state.cameraStatus[side] !== "ON") setFeed(side);
                setCameraStatus(side, "ON", "");
                startCameraPolling(1800);
                startPredictionPreview(side.toUpperCase());
            } else if (data.status === "ERROR") {
                setCameraStatus(side, "ERROR", data.error || "Configured camera is not connected.");
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
                setModelStatus(side, "waiting", "WAITING FOR CAMERA");
            }
        } catch (error) {
            if (sessionId === state.sessionId && requestVersion === state.requestVersion) {
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
            }
        } finally { state.cameraInFlight = false; }
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
        state.validatedModelId = null;
        state.predictionReady = { Front: false, Back: false };
        state.predictionRetryCount = 0;
        state.busy = false;
        clearLocal();
        ["wfFrontSubmitBtn", "wfBackSubmitBtn"].forEach(function (id) {
            var button = byId(id);
            button.disabled = true;
            button.dataset.completed = "false";
        });
        byId("wfStartInspectionBtn").disabled = true;
        byId("wfScanBtn").disabled = false;
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
        if (progress.is_saved) {
            state.sessionId = saved.sessionId;
            state.partNumber = saved.partNumber;
            state.partId = saved.partId;
            state.validatedModelId = saved.validatedModelId;
            state.savedRecordId = saved.savedRecordId || saved.sessionId;
            state.sessionActive = false;
            setResult("Front", progress.front_status, progress.front_verdict, progress.inspection_status);
            setResult("Back", progress.back_status, progress.back_verdict, progress.inspection_status);
            showFinal(progress, { inspection_id: state.savedRecordId, overall: progress.overall });
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
        var resumedProgress = await loadProgress(saved.sessionId);
        if (resumedProgress[side + "_status"] === "ERROR" &&
                resumedProgress.error_message) {
            setResult(
                side === "back" ? "Back" : "Front",
                resumedProgress[side + "_status"],
                resumedProgress[side + "_verdict"],
                resumedProgress.inspection_status
            );
            setText("wfPartInfo", resumedProgress.error_message);
            showError(resumedProgress.error_message);
        }
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
            clearLocal();
            clearTransientState();
            setText("wfPartInfo", error.message || "The saved inspection could not be restored.");
            showError(error.message || "The saved inspection could not be restored.");
        }
    }

    function hasUnfinishedSession() {
        return Boolean(state.sessionActive || state.pendingStart);
    }

    function handleBeforeUnload(event) {
        if (!hasUnfinishedSession()) return;
        event.preventDefault();
        event.returnValue = "";
    }

    function pauseInspectionPage() {
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
        if (!hasUnfinishedSession()) return;
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
        byId("wfScanForm").addEventListener("submit", scan);
        byId("wfStartInspectionBtn").addEventListener("click", startInspection);
        byId("wfFrontSubmitBtn").addEventListener("click", function () { submitSide("front"); });
        byId("wfBackSubmitBtn").addEventListener("click", function () { submitSide("back"); });
        byId("wfFrontRetryCamera").addEventListener("click", function () { retryCamera("Front"); });
        byId("wfBackRetryCamera").addEventListener("click", function () { retryCamera("Back"); });
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
