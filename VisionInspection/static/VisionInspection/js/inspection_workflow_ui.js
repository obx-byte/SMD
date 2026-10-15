(function () {
    "use strict";

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
        requestVersion: 0
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
        state.stage = ({ Scan: STAGE.SCAN_PART, Front: STAGE.FRONT_INSPECTION,
            Back: STAGE.BACK_INSPECTION, Saving: STAGE.SAVING_RESULTS,
            Final: STAGE.FINAL_RESULT })[name] || STAGE.ERROR;
    }
    function setText(id, value) {
        var node = byId(id);
        if (node) node.textContent = value == null || value === "" ? "—" : String(value);
    }
    function setDot(id, status) {
        var node = byId(id);
        if (!node) return;
        node.className = "wf-indicator is-" + status;
    }
    function setCameraStatus(side, status, message) {
        var node = byId("wf" + side + "CameraStatus");
        var overlay = byId("ov" + side);
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
    async function jsonRequest(url, method, body) {
        var response = await fetch(url, {
            method: method || "GET",
            headers: {
                Accept: "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": csrf()
            },
            body: body ? JSON.stringify(body) : undefined
        });
        var data;
        try { data = await response.json(); }
        catch (error) { throw new Error("The server returned an unreadable response."); }
        if (!response.ok || data.ok === false || data.error) {
            var failure = new Error(data.message || data.error || "Request failed.");
            failure.data = data;
            throw failure;
        }
        return data;
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
        setText("wfFrontResult", "RESULT PENDING");
        setText("wfFrontMessage", "Live camera ready for front inspection.");
        setText("wfFrontDetections", "Segmentation results will appear after inspection.");
        setText("wfBackResult", "RESULT PENDING");
        setText("wfBackMessage", "Live camera ready for back inspection.");
        setText("wfBackDetections", "Segmentation results will appear after inspection.");
        setText("wfFrontPart", "—");
        setText("wfBackPart", "—");
        setText("wfFrontSession", "—");
        setText("wfBackSession", "—");
        setText("wfBackFrontResult", "—");
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
        setDot("wfScanDot", "idle");
        setDot("wfModelDot", "idle");
        showError("");
        byId("wfStartInspectionBtn").disabled = true;
        byId("wfReturnToInspectionBtn").hidden = true;
        showStage("Scan");
    }
    function setSessionHeader() {
        ["Front", "Back"].forEach(function (side) {
            setText("wf" + side + "Part", state.partNumber);
            setText("wf" + side + "Session", state.sessionId);
        });
        setText("wfBackFrontResult", state.frontVerdict);
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
        var output = byId(side === "FRONT" ? "wfFrontDetections" : "wfBackDetections");
        if (!image || !output) return;
        state.predictionInFlight = true;
        try {
            var url = URL.partVisionPreview.replace(
                "__PART_ID__",
                encodeURIComponent(state.partId)
            ) + "?side=" + encodeURIComponent(side) +
                "&model_id=" + encodeURIComponent(state.validatedModelId) +
                "&confidence=0.50&show_masks=1&show_boxes=1";
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
            var names = data.detections.map(function (detection) {
                return detection.class + " — " +
                    Number(detection.confidence_percent).toFixed(1) + "%";
            });
            var counts = Object.keys(data.counts_by_class || {}).map(function (name) {
                return name + ": " + data.counts_by_class[name];
            });
            output.textContent = data.detections.length
                ? names.join(", ") + " | Counts: " + counts.join(", ") +
                    " | Total: " + data.detection_count +
                    " | Inference: " + Number(data.inference_ms).toFixed(1) + " ms"
                : "No detections | Total: 0 | Inference: " +
                    Number(data.inference_ms).toFixed(1) + " ms";
            schedulePredictionPreview(side, generation, 300);
        } catch (error) {
            if (generation === state.predictionGeneration && state.predictionSide === side) {
                output.textContent = "Live prediction unavailable: " +
                    (error.message || "Inference failed.");
                schedulePredictionPreview(side, generation, 1500);
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
                byId("wfModelDetail").hidden = true;
                setText("wfPartInfo", partData.message || ("Part identified: " + partNumber + "."));
                persistLocal();
                setBusy(button, false);
                await startInspection(byId("wfStartInspectionBtn"));
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
            state.sessionId = sessionData.session_id;
            state.savedRecordId = null;
            state.frontVerdict = "PENDING";
            state.backVerdict = "PENDING";
            ["Front", "Back"].forEach(function (side) {
                var submitButton = byId("wf" + side + "SubmitBtn");
                submitButton.dataset.completed = "false";
                submitButton.disabled = true;
                setResult(side, "PENDING", "Live camera ready for " + side.toLowerCase() + " inspection.");
                setText("wf" + side + "Detections", "Segmentation results will appear after inspection.");
            });
            persistLocal();
            setSessionHeader();
            setText("wfPartInfo", sessionData.message || "Inspection session started.");
            var progress = sessionData.progress || await loadProgress();
            if (version === state.requestVersion) await enterFromProgress(progress, sessionData.next_side);
        } catch (error) {
            if (version !== state.requestVersion) return;
            if (error.data && error.data.state === "duplicate_session" && error.data.session_id) {
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
        var next = nextSide || (progress.front_status === "IN_PROGRESS" ? "front"
            : progress.back_status === "IN_PROGRESS" || progress.front_status === "COMPLETED" ? "back" : "front");
        if (progress.front_status === "COMPLETED" && progress.back_status === "SKIPPED") {
            await finalize();
            return;
        }
        await showInspection(next === "back" ? "back" : "front");
    }

    async function showInspection(side) {
        showError("");
        setSessionHeader();
        var isBack = side === "back";
        showStage(isBack ? "Back" : "Front");
        clearFeeds();
        var result = isBack ? state.backVerdict : state.frontVerdict;
        setResult(isBack ? "Back" : "Front", result);
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

    function setResult(side, verdict, message) {
        var el = byId("wf" + side + "Result");
        if (!el) return;
        el.textContent = "RESULT " + (verdict || "PENDING").replace("NOT_OK", "NOT OK");
        el.className = "wf-side-result is-" + String(verdict || "pending").toLowerCase();
        var note = byId("wf" + side + "Message");
        if (note && message) note.textContent = message;
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
        if (state.busy || !state.sessionId) return;
        stopPredictionPreview();
        var isBack = side === "back";
        var button = byId(isBack ? "wfBackSubmitBtn" : "wfFrontSubmitBtn");
        setBusy(button, true);
        showError("");
        setResult(isBack ? "Back" : "Front", "IN_PROGRESS", "Running configured inspection rules...");
        try {
            var data = await jsonRequest(isBack ? URL.wfInspectBack : URL.wfInspectFront, "POST", {
                session_id: state.sessionId
            });
            var progress = data.progress || await loadProgress();
            renderInspectionOutput(isBack ? "Back" : "Front", data);
            if (button && progress[side + "_status"] === "COMPLETED") {
                button.dataset.completed = "true";
            }
            if (isBack) state.backVerdict = progress.back_verdict;
            else state.frontVerdict = progress.front_verdict;
            persistLocal();
            setResult(isBack ? "Back" : "Front", isBack ? state.backVerdict : state.frontVerdict,
                "Configured stitch acceptance rules evaluated by the backend.");

            if (!isBack && progress.back_status !== "SKIPPED") {
                if (!["OK", "NOT_OK"].includes(state.frontVerdict)) {
                    if (button) button.dataset.completed = "true";
                    setResult("Front", state.frontVerdict,
                        "Inference completed, but no required front class rule produced a verdict.");
                    showError("Front inspection is complete but indeterminate. Configure at least one required front stitch class, then start a new inspection.");
                    return;
                }
                await showInspection("back");
            } else {
                await finalize();
            }
        } catch (error) {
            setResult(isBack ? "Back" : "Front", "ERROR", "Inspection did not complete. Correct the issue and retry.");
            showError(error.message || "Inspection failed. Reset this session before retrying.");
            if (button) button.disabled = false;
        } finally {
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
            title.textContent = verdict === "OK" ? "OK" : "NOT OK";
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
            if (!saved.sessionId) return;
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
        byId("wfFrontNextPartBtn").addEventListener("click", function () {
            changePart(byId("wfFrontNextPartBtn"));
        });
        byId("wfBackNextPartBtn").addEventListener("click", function () {
            changePart(byId("wfBackNextPartBtn"));
        });
        byId("wfFrontRetryCamera").addEventListener("click", function () { retryCamera("Front"); });
        byId("wfBackRetryCamera").addEventListener("click", function () { retryCamera("Back"); });
        byId("wfRetrySaveBtn").addEventListener("click", retrySave);
        byId("wfNewInspectionBtn").addEventListener("click", newInspection);
        byId("wfChangePartBtn").addEventListener("click", function () {
            changePart(byId("wfChangePartBtn"));
        });
        window.addEventListener("pagehide", stopCameraPolling);
        showStage("Scan");
        restoreSession();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
})();
