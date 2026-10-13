/**
 * VisionInspection — Configuration Screen JavaScript
 * Handles preset management, master image capture, and YOLO model management.
 */

(function () {
    "use strict";

    let currentPresetId = null;
    let masterWidth = 0;
    let masterHeight = 0;
    let cameraStreamActive = false;

    function getCsrfToken() {
        const cookie = document.cookie.split("; ").find(row => row.startsWith("csrftoken="));
        return cookie ? cookie.split("=")[1] : "";
    }

    function csrfHeaders() {
        return { "X-CSRFToken": getCsrfToken() };
    }

    // ------------------------------------------------------------------ #
    //  Tab Navigation
    // ------------------------------------------------------------------ #

    window.showTab = function (tabName) {
        const tabs = document.querySelectorAll(".vi-tab-content");
        tabs.forEach(t => t.classList.remove("active"));

        const navBtns = document.querySelectorAll(".vi-nav-btn");
        navBtns.forEach(b => b.classList.remove("active"));

        document.getElementById("tab-" + tabName).classList.add("active");
        event?.target.classList.add("active");
    };

    // ------------------------------------------------------------------ #
    //  Part selection
    // ------------------------------------------------------------------ #

    function loadParts() {
        fetch("/vision-inspection/api/parts/list/")
            .then(r => r.json())
            .then(data => {
                const select = document.getElementById("partSelect");
                if (!select) return;

                select.innerHTML = '<option value="">-- Select Part --</option>';
                data.parts.forEach(p => {
                    const opt = document.createElement("option");
                    opt.value = p.part_number;
                    opt.textContent = p.part_number + " — " + (p.part_desc || "");
                    select.appendChild(opt);
                });
            })
            .catch(err => console.error("Failed to load parts:", err));
    }

    window.loadPartConfig = function () {
        const select = document.getElementById("partSelect");
        const partNumber = select ? select.value : "";
        if (!partNumber) return;

        fetch("/vision-inspection/api/part/" + encodeURIComponent(partNumber) + "/configuration/")
            .then(r => r.json())
            .then(data => {
                if (data.has_config) {
                    currentPresetId = data.active_preset.id;
                    document.getElementById("presetName").value = data.active_preset.name;
                    document.getElementById("partClass").value = data.active_preset.expected_part_class || "";
                    document.getElementById("cameraIndex").value = data.active_preset.camera || 0;
                    document.getElementById("confidenceThreshold").value = data.active_preset.confidence_threshold || 0.5;
                    document.getElementById("stableFrames").value = data.active_preset.stable_frames || 3;
                    document.getElementById("triggerCooldown").value = (data.active_preset.inspection_rules?.trigger_cooldown) || 2.0;
                    masterWidth = data.master_width || 0;
                    masterHeight = data.master_height || 0;

                    if (data.master_image) {
                        const display = document.getElementById("masterImageDisplay");
                        display.innerHTML = '<img src="' + data.master_image + '" alt="Master Image">';
                    }
                } else {
                    currentPresetId = null;
                    document.getElementById("presetName").value = partNumber + " Preset";
                    document.getElementById("partClass").value = partNumber;
                }
            })
            .catch(err => console.error("Failed to load part config:", err));
    };

    // ------------------------------------------------------------------ #
    //  Preset save
    // ------------------------------------------------------------------ #

    window.savePreset = function () {
        const partNumber = document.getElementById("partSelect").value;
        const name = document.getElementById("presetName").value;
        const partClass = document.getElementById("partClass").value;
        const yoloModelId = document.getElementById("yoloModel").value;
        const cameraIndex = document.getElementById("cameraIndex").value;
        const confidence = document.getElementById("confidenceThreshold").value;
        const stableFrames = document.getElementById("stableFrames").value;
        const triggerCooldown = document.getElementById("triggerCooldown").value;

        if (!partNumber || !partClass) {
            alert("Part Number and Part Class are required");
            return;
        }

        const payload = {
            part_number: partNumber,
            name: name,
            part_class: partClass,
            yolo_model_id: yoloModelId ? parseInt(yoloModelId) : null,
            camera_index: parseInt(cameraIndex),
            confidence_threshold: parseFloat(confidence),
            stable_frames: parseInt(stableFrames),
            trigger_cooldown: parseFloat(triggerCooldown),
        };

        fetch("/vision-inspection/configuration/preset/create/", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(data => {
                if (data.preset_id) {
                    currentPresetId = data.preset_id;
                    alert("Preset saved! ID: " + data.preset_id);
                    loadPresets();
                }
            })
            .catch(err => console.error("Save preset error:", err));
    };

    window.activatePreset = function () {
        if (!currentPresetId) {
            alert("Save preset first");
            return;
        }
        fetch("/vision-inspection/configuration/preset/" + currentPresetId + "/activate/", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
        })
            .then(r => r.json())
            .then(data => {
                alert(data.message || "Preset activated");
            })
            .catch(err => console.error("Activate error:", err));
    };

    function loadPresets() {
        fetch("/vision-inspection/configuration/preset/list/")
            .then(r => r.json())
            .then(data => {
                const tbody = document.getElementById("presetsTableBody");
                if (!tbody) return;
                tbody.innerHTML = "";
                data.presets.forEach(p => {
                    const row = document.createElement("tr");
                    row.innerHTML =
                        "<td>" + p.part_number + "</td>" +
                        "<td>" + p.name + "</td>" +
                        "<td>v" + p.version + "</td>" +
                        "<td>" + (p.is_active ? "Active" : "Inactive") + "</td>" +
                        "<td>" +
                        '<a href="/vision-inspection/configuration/master-image/?preset_id=' + p.id + '">Master</a> ' +
                        '<a href="/vision-inspection/configuration/trigger-zone/' + p.id + '/">Trigger</a> ' +
                        '<a href="/vision-inspection/configuration/rois/' + p.id + '/">ROIs</a>' +
                        "</td>";
                    tbody.appendChild(row);
                });
            })
            .catch(err => console.error("Load presets error:", err));
    }

    // ------------------------------------------------------------------ #
    //  Camera control
    // ------------------------------------------------------------------ #

    window.startCameraConfig = function () {
        const cameraIndex = document.getElementById("cameraIndex").value;
        fetch("/vision-inspection/camera/start/?camera_index=" + cameraIndex)
            .then(r => r.json())
            .then(data => {
                cameraStreamActive = data.status === "ON";
                const feed = document.getElementById("liveFeedContainer");
                const img = document.getElementById("liveFeed");
                if (feed && img) {
                    feed.style.display = "block";
                    img.src = "/vision-inspection/camera/stream/";
                }
            })
            .catch(err => console.error("Camera start error:", err));
    };

    window.stopCameraConfig = function () {
        fetch("/vision-inspection/camera/stop/")
            .then(r => r.json())
            .then(data => {
                cameraStreamActive = false;
                const feed = document.getElementById("liveFeedContainer");
                if (feed) feed.style.display = "none";
            })
            .catch(err => console.error("Camera stop error:", err));
    };

    window.captureMasterImage = function () {
        if (!currentPresetId) {
            alert("Save a preset first");
            return;
        }
        fetch("/vision-inspection/camera/capture/")
            .then(r => r.json())
            .then(data => {
                if (data.image) {
                    const payload = {
                        preset_id: currentPresetId,
                        image: data.image,
                    };
                    fetch("/vision-inspection/configuration/master-image/save/", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRFToken": getCsrfToken(),
                        },
                        body: JSON.stringify(payload),
                    })
                        .then(r => r.json())
                        .then(result => {
                            alert(result.message || "Master image saved");
                            const display = document.getElementById("masterImageDisplay");
                            if (display) {
                                display.innerHTML = '<img src="' + result.master_image + '" alt="Master Image">';
                            }
                            masterWidth = result.width;
                            masterHeight = result.height;
                        });
                }
            })
            .catch(err => console.error("Capture error:", err));
    };

    // ------------------------------------------------------------------ #
    //  YOLO Model management
    // ------------------------------------------------------------------ #

    window.uploadModel = function () {
        const name = document.getElementById("modelName").value;
        const path = document.getElementById("modelPath").value;
        const version = document.getElementById("modelVersion").value;
        const partClass = document.getElementById("modelPartClass").value;
        const numClasses = document.getElementById("modelNumClasses").value;

        if (!name || !path) {
            alert("Name and Model Path are required");
            return;
        }

        fetch("/vision-inspection/yolo/model/upload/", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify({
                name: name,
                model_path: path,
                version: version,
                part_class: partClass,
                num_classes: parseInt(numClasses),
            }),
        })
            .then(r => r.json())
            .then(data => {
                alert(data.message + (data.loaded ? " (model loaded successfully)" : " (failed to load)"));
                loadModels();
            })
            .catch(err => console.error("Upload model error:", err));
    };

    window.testModel = function () {
        fetch("/vision-inspection/yolo/model/test/")
            .then(r => r.json())
            .then(data => {
                alert("Detections: " + data.count);
            })
            .catch(err => console.error("Test model error:", err));
    };

    function loadModels() {
        fetch("/vision-inspection/yolo/models/")
            .then(r => r.json())
            .then(data => {
                const tbody = document.getElementById("modelsTableBody");
                if (!tbody) return;
                tbody.innerHTML = "";
                data.forEach(m => {
                    const row = document.createElement("tr");
                    row.innerHTML =
                        "<td>" + m.name + "</td>" +
                        "<td>" + (m.version || "1.0") + "</td>" +
                        "<td>" + (m.part_class || "—") + "</td>" +
                        "<td>" + (m.is_active ? "Active" : "Inactive") + "</td>";
                    tbody.appendChild(row);
                });
            })
            .catch(err => console.error("Load models error:", err));
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #

    function initConfig() {
        loadParts();
        loadPresets();
        loadModels();

        if (window.presetId) {
            currentPresetId = window.presetId;
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initConfig);
    } else {
        initConfig();
    }

    // Expose loadModels for training.js
    window.loadModels = loadModels;
})();
