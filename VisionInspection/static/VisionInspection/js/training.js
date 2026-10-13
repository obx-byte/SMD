/**
 * VisionInspection — Training Management JavaScript
 *
 * Handles YOLO model training workflow with Celery + Redis + AJAX polling.
 * Does NOT use WebSocket or Django Channels.
 */

(function () {
    "use strict";

    let pollingTimer = null;
    let currentJobId = null;
    let currentJobResult = null;

    function getCsrfToken() {
        const cookie = document.cookie.split("; ").find(row => row.startsWith("csrftoken="));
        return cookie ? cookie.split("=")[1] : "";
    }

    // ------------------------------------------------------------------ #
    //  DOM cache
    // ------------------------------------------------------------------ #

    const trainingSection = document.getElementById("trainingSection");
    const btnStartTraining = document.getElementById("btnStartTraining");
    const btnCancelTraining = document.getElementById("btnCancelTraining");
    const trainingProjectInput = document.getElementById("trainingProject");
    const trainingEpochsInput = document.getElementById("trainingEpochs");
    const datasetSelect = document.getElementById("datasetSelect");

    // Status display elements
    const elStatus = document.getElementById("trainingStatus");
    const elProgress = document.getElementById("trainingProgress");
    const elProgressText = document.getElementById("trainingProgressText");
    const elCurrentEpoch = document.getElementById("trainingCurrentEpoch");
    const elTotalEpochs = document.getElementById("trainingTotalEpochs");
    const elMessage = document.getElementById("trainingMessage");
    const elPrecision = document.getElementById("trainingPrecision");
    const elRecall = document.getElementById("trainingRecall");
    const elMap50 = document.getElementById("trainingMap50");
    const elMap5095 = document.getElementById("trainingMap5095");

    // ------------------------------------------------------------------ #
    //  Start training
    // ------------------------------------------------------------------ #

    if (btnStartTraining) {
        btnStartTraining.addEventListener("click", startTraining);
    }

    if (btnCancelTraining) {
        btnCancelTraining.addEventListener("click", cancelTraining);
    }

    function startTraining() {
        if (!trainingProjectInput || !trainingProjectInput.value) {
            alert("Please select a dataset/project");
            return;
        }

        const epochs = parseInt(trainingEpochsInput?.value || "100", 10);
        const project = trainingProjectInput.value;

        btnStartTraining.disabled = true;
        btnStartTraining.textContent = "Queued...";

        fetch("/vision-inspection/training/start/", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify({
                training_project: project,
                epochs: epochs,
            }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.job_id) {
                    currentJobId = data.job_id;
                    showTrainingUI(true);
                    updateTrainingStatus(data);
                    startPolling();
                } else {
                    alert(data.error || "Failed to start training");
                }
            })
            .catch(err => {
                console.error("Training start error:", err);
                alert("Failed to start training");
            })
            .finally(() => {
                btnStartTraining.disabled = false;
                btnStartTraining.textContent = "Start Training";
            });
    }

    function cancelTraining() {
        if (!currentJobId) return;

        fetch("/vision-inspection/training/cancel/" + currentJobId + "/", {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
        })
            .then(r => r.json())
            .then(data => {
                alert(data.message || "Training cancelled");
                stopPolling();
            })
            .catch(err => console.error("Cancel error:", err));
    }

    // ------------------------------------------------------------------ #
    //  Polling
    // ------------------------------------------------------------------ #

    function startPolling() {
        if (pollingTimer) clearInterval(pollingTimer);
        pollingTimer = setInterval(pollTrainingStatus, 1500);
    }

    function stopPolling() {
        if (pollingTimer) {
            clearInterval(pollingTimer);
            pollingTimer = null;
        }
    }

    function pollTrainingStatus() {
        if (!currentJobId) {
            stopPolling();
            return;
        }

        fetch("/vision-inspection/training/status/" + currentJobId + "/")
            .then(r => r.json())
            .then(data => {
                updateTrainingStatus(data);
                currentJobResult = data;

                if (data.status === "COMPLETED" ||
                    data.status === "FAILED" ||
                    data.status === "CANCELLED") {
                    stopPolling();
                    finalizeTraining(data);
                }
            })
            .catch(err => {
                console.error("Status poll error:", err);
            });
    }

    // ------------------------------------------------------------------ #
    //  UI updates
    // ------------------------------------------------------------------ #

    function updateTrainingStatus(data) {
        if (elStatus) elStatus.textContent = data.status || "—";
        if (elProgress) elProgress.value = data.progress || 0;
        if (elProgressText) elProgressText.textContent = (data.progress || 0) + "%";
        if (elCurrentEpoch) elCurrentEpoch.textContent = data.current_epoch || 0;
        if (elTotalEpochs) elTotalEpochs.textContent = data.total_epochs || 0;
        if (elMessage) elMessage.textContent = data.message || "";
        if (elPrecision) elPrecision.textContent = formatMetric(data.precision);
        if (elRecall) elRecall.textContent = formatMetric(data.recall);
        if (elMap50) elMap50.textContent = formatMetric(data.map50);
        if (elMap5095) elMap5095.textContent = formatMetric(data.map50_95);

        if (data.error_message && elMessage) {
            elMessage.textContent = data.error_message;
        }
    }

    function finalizeTraining(data) {
        if (elStatus) {
            elStatus.textContent = data.status;
        }

        if (btnStartTraining) {
            btnStartTraining.disabled = false;
            btnStartTraining.textContent = "Start Training";
        }

        if (btnCancelTraining) {
            btnCancelTraining.disabled = true;
        }

        if (data.status === "COMPLETED") {
            alert("✓ TRAINING COMPLETED\nModel: " + (data.model_version || "—"));
            loadModels();
        } else if (data.status === "FAILED") {
            alert("✗ TRAINING FAILED\n" + (data.error_message || "Unknown error"));
        } else if (data.status === "CANCELLED") {
            alert("Training was cancelled.");
        }

        // Reset after a moment
        setTimeout(() => {
            showTrainingUI(false);
            currentJobId = null;
        }, 3000);
    }

    function showTrainingUI(show) {
        if (trainingSection) {
            trainingSection.style.display = show ? "block" : "none";
        }
        if (btnStartTraining) {
            btnStartTraining.disabled = show;
        }
        if (btnCancelTraining) {
            btnCancelTraining.disabled = !show;
        }
    }

    function formatMetric(value) {
        if (value === undefined || value === null) return "—";
        return parseFloat(value).toFixed(4);
    }

    // ------------------------------------------------------------------ #
    //  Load datasets for the dropdown
    // ------------------------------------------------------------------ #

    function loadDatasets() {
        fetch("/vision-inspection/training/dataset-paths/")
            .then(r => r.json())
            .then(data => {
                if (datasetSelect) {
                    datasetSelect.innerHTML = "";
                    data.datasets.forEach(ds => {
                        const opt = document.createElement("option");
                        opt.value = ds.name;
                        opt.textContent = ds.name + " (" + ds.num_files + " files)";
                        if (!ds.has_yaml) {
                            opt.textContent += " ⚠";
                        }
                        datasetSelect.appendChild(opt);
                    });
                    if (data.datasets.length === 0) {
                        const opt = document.createElement("option");
                        opt.value = "default_project";
                        opt.textContent = "default_project (default)";
                        datasetSelect.appendChild(opt);
                    }

                    // Sync project input when dataset changes
                    if (datasetSelect && trainingProjectInput) {
                        datasetSelect.addEventListener("change", function () {
                            trainingProjectInput.value = this.value;
                        });
                    }
                }
                if (trainingProjectInput) {
                    trainingProjectInput.value = datasetSelect && datasetSelect.value
                        ? datasetSelect.value
                        : "default_project";
                }
            })
            .catch(err => console.error("Dataset load error:", err));
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #

    function initTraining() {
        loadDatasets();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initTraining);
    } else {
        initTraining();
    }

    // Expose for global access
    window.trainingManager = {
        startTraining,
        cancelTraining,
        pollTrainingStatus,
        loadDatasets,
    };
})();
