/**
 * VisionInspection — Inspection Screen JavaScript
 *
 * Provides real-time polling for camera status, state updates,
 * live MJPEG camera feed, and operator scan handling (Employee ID, Part Number).
 */

(function () {
    "use strict";

    const STATE_POLL_INTERVAL = 2000;
    const CAMERA_POLL_INTERVAL = 2000;

    let statePollTimer = null;
    let cameraPollTimer = null;
    let currentPresetId = null;

    function getCsrfToken() {
        const cookie = document.cookie.split("; ").find(row => row.startsWith("csrftoken="));
        return cookie ? cookie.split("=")[1] : "";
    }

    function csrfHeaders() {
        return { "X-CSRFToken": getCsrfToken() };
    }

    // ------------------------------------------------------------------ #
    //  DOM Elements
    // ------------------------------------------------------------------ #

    const cameraStatusText = document.getElementById("cameraStatusText");
    const cameraStatusDot = document.getElementById("cameraStatusDot");
    const triggerInfo = document.getElementById("triggerInfo");
    const employeeInput = document.getElementById("employeeId");
    const partInput = document.getElementById("partNumber");
    const employeeMsg = document.getElementById("employeeValidationMsg");
    const partMsg = document.getElementById("partValidationMsg");
    const cameraFeed = document.getElementById("cameraFeed");
    const roiResultsContainer = document.getElementById("roiResults");
    const finalResultValue = document.getElementById("finalResultValue");

    // ------------------------------------------------------------------ #
    //  Camera feed control
    // ------------------------------------------------------------------ #

    function updateCameraStatus(status, autoTrigger) {
        if (!cameraStatusText || !cameraStatusDot) return;

        cameraStatusText.textContent = "CAMERA " + status;
        cameraStatusDot.className = "vi-status-dot";

        if (status === "ON") {
            cameraStatusDot.classList.add("on");
            cameraFeed.src = "/vision-inspection/camera/stream/";
            cameraFeed.alt = "Live Camera Feed";
        } else if (status === "CONNECTING") {
            cameraStatusDot.classList.add("connecting");
            cameraFeed.src = "";
            cameraFeed.alt = "Camera Connecting...";
        } else if (status === "ERROR") {
            cameraStatusDot.classList.add("error");
            cameraFeed.src = "";
            cameraFeed.alt = "Camera Error";
        } else {
            cameraFeed.src = "";
            cameraFeed.alt = "Camera OFF";
        }

        // Trigger zone info display
        if (triggerInfo) {
            if (status !== "ON" || !autoTrigger) {
                triggerInfo.style.display = "block";
                triggerInfo.innerHTML = "<span>AUTO TRIGGER DISABLED</span>";
            } else {
                triggerInfo.style.display = "none";
            }
        }
    }

    function startCamera() {
        fetch("/vision-inspection/camera/start/")
            .then(r => r.json())
            .then(data => updateCameraStatus(data.status, true))
            .catch(err => console.error("Camera start error:", err));
    }

    function stopCamera() {
        fetch("/vision-inspection/camera/stop/")
            .then(r => r.json())
            .then(data => updateCameraStatus(data.status, false))
            .catch(err => console.error("Camera stop error:", err));
    }

    function pollCameraStatus() {
        fetch("/vision-inspection/camera/status/")
            .then(r => r.json())
            .then(data => {
                // Only update if the state machine has a preset loaded
                if (currentPresetId) {
                    updateCameraStatus(data.status, true);
                }
            })
            .catch(err => console.error("Camera status error:", err));
    }

    // ------------------------------------------------------------------ #
    //  State polling
    // ------------------------------------------------------------------ #

    function pollState() {
        fetch("/vision-inspection/api/state/")
            .then(r => r.json())
            .then(data => updateState(data))
            .catch(err => console.error("State poll error:", err));
    }

    function updateState(state) {
        // Camera status
        if (state.camera_status) {
            updateCameraStatus(state.camera_status, state.trigger_auto);
        }

        // Employee input
        if (state.employee_code) {
            if (employeeInput) employeeInput.value = state.employee_code;
        }

        // Part input
        if (state.part_number) {
            if (partInput) partInput.value = state.part_number;
        }

        // ROI results
        if (state.roi_results && state.roi_results.length > 0) {
            renderRoiResults(state.roi_results);
        }

        // Final result
        if (finalResultValue) {
            finalResultValue.textContent = state.final_result || "PENDING";
            finalResultValue.className = getResultClass(state.final_result);
        }

        // Error message
        if (state.error) {
            showError(state.error);
        }

        // Handle part validation flow
        if (state.part_number && !state.part_valid) {
            if (partMsg) {
                partMsg.textContent = "NO ACTIVE INSPECTION CONFIGURATION";
                partMsg.className = "vi-msg-invalid";
            }
            if (partInput) partInput.disabled = true;
        }

        if (state.part_valid) {
            if (partMsg) {
                partMsg.textContent = "PART VALID";
                partMsg.className = "vi-msg-valid";
            }
            if (partInput) partInput.disabled = true;
            currentPresetId = state.preset_version;
        }

        // Auto-start camera when part is valid
        if (state.part_valid && state.camera_status !== "ON" && state.camera_status !== "CONNECTING") {
            startCamera();
        }
    }

    function getResultClass(result) {
        if (result === "OK") return "vi-result-ok";
        if (result === "NOT OK") return "vi-result-not-ok";
        if (result === "INSPECTION ERROR") return "vi-result-error";
        return "vi-result-pending";
    }

    function renderRoiResults(roiResults) {
        if (!roiResultsContainer) return;

        roiResultsContainer.innerHTML = "";
        roiResults.forEach(function (item) {
            const card = document.createElement("div");
            card.className = "vi-roi-card";

            const nameEl = document.createElement("div");
            nameEl.className = "vi-roi-name";
            nameEl.textContent = item.roi_name || "—";

            const resultEl = document.createElement("div");
            resultEl.className = "vi-roi-result " + getRoiResultClass(item.result);
            resultEl.textContent = item.result || "—";

            card.appendChild(nameEl);
            card.appendChild(resultEl);
            roiResultsContainer.appendChild(card);
        });
    }

    function getRoiResultClass(result) {
        switch (result) {
            case "MATCH": return "vi-result-match";
            case "NOT_MATCH": return "vi-result-not-match";
            case "ERROR": return "vi-result-error";
            case "SKIPPED": return "vi-result-skipped";
            default: return "";
        }
    }

    function showError(message) {
        if (finalResultValue) {
            finalResultValue.textContent = "INSPECTION ERROR";
            finalResultValue.className = "vi-result-error";
        }
    }

    // ------------------------------------------------------------------ #
    //  Employee scan handling
    // ------------------------------------------------------------------ #

    function initEmployeeScan() {
        if (!employeeInput) return;

        let buffer = "";
        let lastKeystroke = 0;

        employeeInput.addEventListener("keydown", function (e) {
            const now = Date.now();
            if (now - lastKeystroke > 500) {
                buffer = "";
            }
            lastKeystroke = now;

            if (e.key === "Enter") {
                e.preventDefault();
                const code = buffer.trim();
                if (code) validateEmployee(code);
                buffer = "";
            } else if (e.key === "Backspace") {
                buffer = buffer.slice(0, -1);
            } else if (e.key.length === 1) {
                buffer += e.key;
            }
        });
    }

    function validateEmployee(code) {
        console.log("Validating employee:", code);
        const formData = new FormData();
        formData.append("employee_code", code);

        fetch("/vision-inspection/api/employee/validate/", {
            method: "POST",
            body: JSON.stringify({ employee_code: code }),
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
        })
            .then(r => r.json())
            .then(data => {
                if (data.valid) {
                    employeeMsg.textContent = "EMPLOYEE VALID";
                    employeeMsg.className = "vi-msg-valid";
                    if (partInput) partInput.disabled = false;
                    startCamera();
                } else {
                    employeeMsg.textContent = "INVALID EMPLOYEE";
                    employeeMsg.className = "vi-msg-invalid";
                    if (partInput) partInput.disabled = true;
                }
            })
            .catch(err => {
                employeeMsg.textContent = "Error validating employee";
                employeeMsg.className = "vi-msg-invalid";
                console.error(err);
            });
    }

    // ------------------------------------------------------------------ #
    //  Part scan handling
    // ------------------------------------------------------------------ #

    function initPartScan() {
        if (!partInput) return;

        let buffer = "";
        let lastKeystroke = 0;

        partInput.addEventListener("keydown", function (e) {
            const now = Date.now();
            if (now - lastKeystroke > 500) {
                buffer = "";
            }
            lastKeystroke = now;

            if (e.key === "Enter") {
                e.preventDefault();
                const part = buffer.trim();
                if (part) validatePart(part);
                buffer = "";
            } else if (e.key === "Backspace") {
                buffer = buffer.slice(0, -1);
            } else if (e.key.length === 1) {
                buffer += e.key;
            }
        });
    }

    function validatePart(partNumber) {
        console.log("Validating part:", partNumber);
        fetch("/vision-inspection/api/part/validate/", {
            method: "POST",
            body: JSON.stringify({ part_number: partNumber }),
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
        })
            .then(r => r.json())
            .then(data => {
                if (data.valid) {
                    if (data.inspection_enabled) {
                        partMsg.textContent = "PART VALID";
                        partMsg.className = "vi-msg-valid";
                        currentPresetId = data.active_preset?.id || null;
                        // Camera will auto-start via state polling
                    } else {
                        partMsg.textContent = "NO ACTIVE INSPECTION CONFIGURATION";
                        partMsg.className = "vi-msg-invalid";
                    }
                } else {
                    partMsg.textContent = "INVALID PART NUMBER";
                    partMsg.className = "vi-msg-invalid";
                }
            })
            .catch(err => {
                partMsg.textContent = "Error validating part";
                partMsg.className = "vi-msg-invalid";
                console.error(err);
            });
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #

    function init() {
        initEmployeeScan();
        initPartScan();

        // Start polling
        statePollTimer = setInterval(pollState, STATE_POLL_INTERVAL);
        cameraPollTimer = setInterval(pollCameraStatus, CAMERA_POLL_INTERVAL);

        // Initial poll
        pollState();

        console.log("Vision Inspection UI initialized");
    }

    // Auto-start camera if a preset is already loaded
    function checkInitialCamera() {
        fetch("/vision-inspection/camera/status/")
            .then(r => r.json())
            .then(data => updateCameraStatus(data.status, true))
            .catch(() => {});
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            checkInitialCamera();
            init();
        });
    } else {
        checkInitialCamera();
        init();
    }

})();
