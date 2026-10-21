(function () {
    "use strict";

    var INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000;
    var INSPECTION_REQUEST_TIMEOUT_MS = 120 * 1000;
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
        cameraInFlight: false,
        sessionPollTimer: null,
        sessionPollInFlight: false,
        sessionPollGeneration: 0,
        requestVersion: 0,
        cameraStatus: { Front: "OFF", Back: "OFF" },
        inactivityTimer: null,
        countdownTimer: null,
        inactivityDeadline: null,
        screenSaverActive: false,
        activityMonitoring: false
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
            button.disabled = status !== "ON" || button.dataset.completed === "true";
        }
        if (status === "ERROR") {
            setResult(side, "ERROR", "ERROR");
        }
        if (previousStatus !== status) syncInactivityTimer();
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
        if (button) {
            button.disabled = value;
            button.setAttribute("aria-busy", value ? "true" : "false");
        }
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
    async function cameraStatusRequest() {
        var response = await fetch(URL.cameraStatus, {
            headers: { Accept: "application/json" }
        });
        var data;
        try { data = await response.json(); }
        catch (error) { throw new Error("The camera status response was unreadable."); }
        if (!response.ok || !data || typeof data.status !== "string") {
            throw new Error(data && data.error || "Camera status is unavailable.");
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
        stopCameraPolling();
        clearFeeds();
        clearLocal();
        state.sessionId = null;
        state.partNumber = "";
        state.partId = null;
        state.selectedBarcode = "";
        state.validatedModelId = null;
        state.frontVerdict = "PENDING";
        state.backVerdict = "PENDING";
        state.savedRecordId = null;
        state.stage = STAGE.SCAN_PART;
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
        byId("wfReturnToInspectionBtn").hidden = true;
        showStage("Scan");
    }
    function setFeed(side) {
        var image = byId("feed" + side);
        if (image) {
            delete image.dataset.inspectionResult;
            image.src = URL.cameraStream + "?t=" + Date.now();
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
        var label = side === "front" ? "Front" : "Back";
        setCameraStatus(label, "CONNECTING", "Connecting to configured " + side + " camera...");
        await jsonRequest(URL.wfActivateSide, "POST", { session_id: state.sessionId, side: side });
        setFeed(label);
        await refreshCamera();
        startCameraPolling();
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
        var image = byId(side === "FRONT" ? "feedFront" : "feedBack");
        if (!image) return;
        state.predictionInFlight = true;
        try {
            var url = URL.partVisionPreview.replace(
                "__PART_ID__",
                encodeURIComponent(state.partId)
            ) + "?side=" + encodeURIComponent(side) +
                "&model_id=" + encodeURIComponent(state.validatedModelId) +
                "&confidence=0.30&show_masks=1&show_boxes=1";
            var data = await jsonRequest(url, "GET");
            if (generation !== state.predictionGeneration ||
                    state.predictionSide !== side ||
                    state.stage !== (side === "FRONT" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (typeof data.image !== "string" ||
                    data.image.indexOf("data:image/jpeg;base64,") !== 0 ||
                    !Array.isArray(data.detections)) {
                throw new Error("The live prediction response did not contain a valid annotated frame.");
            }
            image.src = data.image;
            showError("");
            schedulePredictionPreview(side, generation, 300);
        } catch (error) {
            if (generation === state.predictionGeneration && state.predictionSide === side) {
                var message = error.message || "Live prediction preview failed.";
                showError(message);
                var retryableCameraWarmup = error.status === 409 &&
                    /configured camera is (connecting|starting); waiting for it to produce a frame|camera has not produced a frame yet/i
                        .test(message);
                if (!error.status || retryableCameraWarmup) {
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

    function normalizeBarcode(value) {
        return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
    }

    async function resetPreviousSession(scannedBarcode) {
        if (!state.sessionId) return true;
        var sameBarcode = normalizeBarcode(scannedBarcode) === normalizeBarcode(state.selectedBarcode || state.partNumber);
        if (sameBarcode) {
            var progress = await loadProgress();
            var errored = progress.front_status === "ERROR" || progress.back_status === "ERROR";
            if (progress.is_active && !errored) {
                setText("wfScanState", "Active inspection");
                setDot("wfScanDot", "error");
                setText("wfPartInfo", "An inspection for this part is already active. Return to it or scan a different part to safely cancel it.");
                byId("wfReturnToInspectionBtn").hidden = false;
                showError("The active inspection was not cancelled.");
                return false;
            }
        }

        setText("wfPartInfo", "Safely closing the previous unfinished inspection...");
        byId("wfReturnToInspectionBtn").hidden = false;
        var result = await jsonRequest(URL.wfCancel, "POST", { session_id: state.sessionId });
        if (result.state === "completed") {
            throw new Error(result.message || "The previous inspection is complete. Save its results before scanning another part.");
        }
        clearTransientState();
        return true;
    }

    async function scan(event) {
        if (event) event.preventDefault();
        if (state.busy || state.stage !== STAGE.SCAN_PART) return;
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
            if (!await resetPreviousSession(barcode) || version !== state.requestVersion) return;
            var partData = await jsonRequest(URL.wfScan, "POST", { barcode: barcode });
            if (version !== state.requestVersion) return;
            if (partData.active_session &&
                    !isSessionId(partData.active_session.session_id)) {
                throw new Error("The scan response contained an invalid active session ID.");
            }
            if (partData.ok !== true || !partData.part ||
                    typeof partData.part.part_number !== "string" ||
                    !partData.part.part_number.trim()) {
                throw new Error(partData.message || "The server did not identify a valid part.");
            }
            var part = partData.part || {};
            var partNumber = part.part_number || barcode;
            if (partData.active_session &&
                normalizeBarcode(partData.active_session.part_number) !== normalizeBarcode(partNumber)) {
                state.sessionId = partData.active_session.session_id;
                state.partNumber = partData.active_session.part_number;
                state.selectedBarcode = partData.active_session.part_number;
                persistLocal();
                byId("wfReturnToInspectionBtn").hidden = false;
                setText("wfPartInfo", "Safely closing the previous unfinished inspection...");
                var cancelled = await jsonRequest(URL.wfCancel, "POST", { session_id: state.sessionId });
                if (version !== state.requestVersion) return;
                if (cancelled.state === "completed") {
                    throw new Error(cancelled.message || "The previous inspection is complete and cannot be cancelled.");
                }
                clearTransientState();
                partData = await jsonRequest(URL.wfScan, "POST", { barcode: barcode });
                if (version !== state.requestVersion) return;
                if (partData.active_session &&
                        !isSessionId(partData.active_session.session_id)) {
                    throw new Error("The scan response contained an invalid active session ID.");
                }
                if (partData.ok !== true || !partData.part ||
                        typeof partData.part.part_number !== "string" ||
                        !partData.part.part_number.trim()) {
                    throw new Error(partData.message || "The server did not identify a valid part.");
                }
                part = partData.part || {};
                partNumber = part.part_number || barcode;
            }
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

            if (partData.active_session) {
                if (normalizeBarcode(partData.active_session.part_number) !== normalizeBarcode(partNumber)) {
                    state.sessionId = partData.active_session.session_id;
                    state.partNumber = partData.active_session.part_number;
                    state.selectedBarcode = partData.active_session.part_number;
                    persistLocal();
                    byId("wfReturnToInspectionBtn").hidden = false;
                    setText("wfScanState", "Another active inspection exists");
                    setDot("wfScanDot", "error");
                    setText("wfPartInfo", "Another part still has an active inspection. Return to the station's current inspection or resolve it before continuing.");
                    showError("Another active inspection session is still running.");
                    return;
                }
                state.sessionId = partData.active_session.session_id;
                persistLocal();
                byId("wfReturnToInspectionBtn").hidden = false;
                setText("wfScanState", "Active inspection exists");
                setDot("wfScanDot", "error");
                setText("wfPartInfo", "This part already has an active inspection. Return to that inspection or safely cancel it before starting another.");
                showError("An active inspection session already exists for this part.");
                return;
            }

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
        button = button || byId("wfStartInspectionBtn");
        var version = ++state.requestVersion;
        setBusy(button, true);
        showError("");
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
            if (error.data && error.data.state === "duplicate_session" && error.data.session_id) {
                if (!isSessionId(error.data.session_id)) {
                    showError("The server returned an invalid active inspection session ID.");
                    return;
                }
                state.sessionId = error.data.session_id;
                state.savedRecordId = null;
                persistLocal();
                try {
                    var activeProgress = await loadProgress();
                    if (version === state.requestVersion) {
                        await enterFromProgress(activeProgress);
                    }
                    return;
                } catch (progressError) {
                    showError(progressError.message || "The active inspection could not be restored.");
                    return;
                }
            }
            setText("wfScanState", (error.data && error.data.state) || "Inspection could not start");
            setDot("wfScanDot", "error");
            setText("wfPartInfo", error.message || "Inspection could not start.");
            showError(error.message);
        } finally {
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

    async function returnToInspection() {
        if (state.busy || !state.sessionId) return;
        var button = byId("wfReturnToInspectionBtn");
        setBusy(button, true);
        try {
            var progress = await loadProgress();
            await enterFromProgress(progress);
        } catch (error) {
            showError(error.message || "The active inspection could not be restored.");
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
        showError("");
        var isBack = side === "back";
        showStage(isBack ? "Back" : "Front");
        clearFeeds();
        try {
            progress = progress || await loadProgress();
            setResult(
                isBack ? "Back" : "Front",
                progress[side + "_status"],
                progress[side + "_verdict"],
                progress.inspection_status
            );
        } catch (error) {
            setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
            showError(error.message || "Could not load inspection status.");
            return;
        }
        try {
            await activateCamera(side);
            setCameraStatus(isBack ? "Back" : "Front", "CONNECTING", "Waiting for camera frames...");
        } catch (error) {
            setCameraStatus(isBack ? "Back" : "Front", "ERROR", error.message);
            showError(error.message + " Check the configured camera and retry the inspection stage.");
        }
    }

    async function retryCamera(side) {
        if (state.busy) return;
        var button = byId("wf" + side + "RetryCamera");
        setBusy(button, true);
        showError("");
        try {
            await activateCamera(side.toLowerCase());
        } catch (error) {
            setCameraStatus(side, "ERROR", error.message);
            showError(error.message + " Check the configured camera and retry.");
        } finally {
            state.busy = false;
            if (button) button.disabled = false;
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
                session_id: state.sessionId
            }, INSPECTION_REQUEST_TIMEOUT_MS);
            var progress = data.progress || await loadProgress();
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
            if (error.code === "inspection_timeout") {
                setResult(isBack ? "Back" : "Front", "ERROR", "ERROR");
                try {
                    await jsonRequest(URL.wfCancel, "POST", {
                        session_id: state.sessionId
                    });
                    state.sessionId = null;
                    state.savedRecordId = null;
                    clearLocal();
                    stopCameraPolling();
                    stopPredictionPreview();
                    clearFeeds();
                    showStage("Scan");
                    setText("wfPartInfo", "The timed-out session was cancelled safely. Start a new inspection to retry.");
                } catch (cancelError) {
                    if (cancelError.data && cancelError.data.state === "completed") {
                        try {
                            await enterFromProgress(await loadProgress());
                            return;
                        } catch (progressError) {
                            showError(progressError.message || error.message);
                        }
                    } else {
                        if (button) button.disabled = true;
                        showError(error.message + " Cancellation could not be confirmed; this session will not be resubmitted.");
                    }
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

    async function finalize() {
        showStage("Saving");
        showError("");
        var retry = byId("wfRetrySaveBtn");
        if (retry) retry.hidden = true;
        setText("wfSavingTitle", "Saving results");
        setText("wfSavingMessage", "Computing the configured verdict and persisting the inspection record...");
        try {
            var resultData = await jsonRequest(URL.wfCompute, "POST", { session_id: state.sessionId });
            if (!resultData.progress || !resultData.progress.ok) throw new Error("The backend did not confirm the computed inspection state.");
            state.frontVerdict = resultData.front_verdict || resultData.progress.front_verdict;
            state.backVerdict = resultData.back_verdict || resultData.progress.back_verdict;
            var saved = await jsonRequest(URL.wfSave, "POST", { session_id: state.sessionId });
            if (!saved.inspection_id && !saved.session_id) throw new Error("The backend did not confirm persistence of this inspection.");
            state.savedRecordId = saved.inspection_id || saved.session_id;
            persistLocal();
            var progress = saved.progress || resultData.progress;
            progress.front_verdict = state.frontVerdict;
            progress.back_verdict = state.backVerdict;
            progress.overall = saved.overall || resultData.overall || progress.overall;
            showFinal(progress, saved);
        } catch (error) {
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
            var data = await cameraStatusRequest();
            if (sessionId !== state.sessionId || requestVersion !== state.requestVersion || state.stage !== (side === "Front" ? STAGE.FRONT_INSPECTION : STAGE.BACK_INSPECTION)) return;
            if (data.status === "ON") {
                setCameraStatus(side, "ON", "");
                startPredictionPreview(side.toUpperCase());
            } else if (data.status === "ERROR") {
                setCameraStatus(side, "ERROR", data.error || "Configured camera is not connected.");
            } else if (data.status === "CONNECTING") {
                setCameraStatus(side, "CONNECTING", "Waiting for camera frames...");
            } else {
                setCameraStatus(side, "ERROR", data.error || "Configured camera is stopped.");
            }
        } catch (error) {
            if (sessionId === state.sessionId && requestVersion === state.requestVersion) {
                setCameraStatus(side, "ERROR", "Camera status unavailable.");
            }
        } finally { state.cameraInFlight = false; }
    }
    function startCameraPolling() {
        stopCameraPolling();
        state.cameraTimer = window.setInterval(refreshCamera, 1800);
    }
    function stopCameraPolling() {
        if (state.cameraTimer) window.clearInterval(state.cameraTimer);
        state.cameraTimer = null;
    }

    async function restoreSession() {
        var raw;
        try { raw = sessionStorage.getItem("viInspectionWorkflow"); }
        catch (error) { return; }
        if (!raw) return;
        var requestVersion = state.requestVersion;
        try {
            var saved = JSON.parse(raw);
            if (!isSessionId(saved.sessionId)) {
                clearLocal();
                return;
            }
            state.sessionId = saved.sessionId;
            state.partNumber = saved.partNumber || "";
            state.partId = Number.isInteger(Number(saved.partId)) && Number(saved.partId) > 0
                ? Number(saved.partId) : null;
            state.validatedModelId = Number.isInteger(Number(saved.validatedModelId)) &&
                Number(saved.validatedModelId) > 0 ? Number(saved.validatedModelId) : null;
            state.selectedBarcode = state.partNumber;
            state.savedRecordId = saved.savedRecordId || null;
            var progress = await loadProgress();
            if (requestVersion !== state.requestVersion || state.sessionId !== saved.sessionId) return;
            state.frontVerdict = progress.front_verdict || "PENDING";
            state.backVerdict = progress.back_verdict || "PENDING";
            await enterFromProgress(progress);
        } catch (error) {
            if (requestVersion !== state.requestVersion) return;
            clearLocal();
            state.sessionId = null;
            state.partNumber = "";
        }
    }

    function init() {
        if (initialized) return;
        initialized = true;
        byId("wfScanForm").addEventListener("submit", scan);
        byId("wfStartInspectionBtn").addEventListener("click", startInspection);
        byId("wfReturnToInspectionBtn").addEventListener("click", returnToInspection);
        byId("wfFrontSubmitBtn").addEventListener("click", function () { submitSide("front"); });
        byId("wfBackSubmitBtn").addEventListener("click", function () { submitSide("back"); });
        byId("wfFrontRetryCamera").addEventListener("click", function () { retryCamera("Front"); });
        byId("wfBackRetryCamera").addEventListener("click", function () { retryCamera("Back"); });
        byId("wfRetrySaveBtn").addEventListener("click", retrySave);
        byId("wfNewInspectionBtn").addEventListener("click", newInspection);
        byId("wfChangePartBtn").addEventListener("click", function () {
            changePart(byId("wfChangePartBtn"));
        });
        startActivityMonitoring();
        window.addEventListener("pagehide", stopActivityMonitoring);
        window.addEventListener("pageshow", startActivityMonitoring);
        showStage("Scan");
        restoreSession();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
