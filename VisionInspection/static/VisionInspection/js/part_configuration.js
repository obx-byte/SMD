(function () {
    "use strict";

    const cfg = window.__VI_PART_CONFIG__;
    const root = document.getElementById("partConfigPage");
    if (!cfg || !root) return;

    const form = document.getElementById("partConfigForm");
    const picker = document.getElementById("pcModelPicker");
    const modelFile = document.getElementById("pcModelFile");
    const addButton = document.getElementById("pcAddModel");
    const addRegisteredButton = document.getElementById("pcAddRegisteredModel");
    const uploadVersion = document.getElementById("pcUploadVersion");
    const modelAvailability = document.getElementById("pcModelAvailability");
    const modelsRoot = document.getElementById("pcModels");
    const message = document.getElementById("pcMessage");
    let availableModels = [];
    let currentConfiguration = null;

    function csrfToken() {
        const field = form.querySelector('[name="csrfmiddlewaretoken"]');
        return field ? field.value : "";
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[char]));
    }

    async function requestJson(url, options) {
        const response = await fetch(url, options || { headers: { Accept: "application/json" } });
        let data = {};
        try { data = await response.json(); } catch (error) { /* handled below */ }
        if (!response.ok) throw new Error(data.error || "Request failed (" + response.status + ").");
        return data;
    }

    function setMessage(text, success) {
        message.textContent = text || "";
        message.classList.toggle("is-ok", !!success);
    }

    function updatePicker() {
        const selected = new Set(Array.from(modelsRoot.querySelectorAll(".pc-model-card"), (card) => card.dataset.modelId));
        const remaining = availableModels.filter((model) => !selected.has(String(model.id)));
        const prompt = availableModels.length
            ? (remaining.length ? "Select registered model" : "All registered models added")
            : "No registered models";
        picker.innerHTML = '<option value="">' + prompt + '</option>' + remaining
            .map((model) => '<option value="' + model.id + '">' + escapeHtml(model.name) + " v" + escapeHtml(model.version) + "</option>")
            .join("");
        picker.disabled = remaining.length === 0;
        addRegisteredButton.disabled = !picker.value || picker.disabled;
        modelAvailability.textContent = availableModels.length
            ? "Registered models are available to all users for draft configuration."
            : "No models are registered yet. Once a model is registered, it will be available to all users here.";
    }

    function rowMarkup(modelId, cls, saved, priority) {
        const side = saved ? saved.side : (document.getElementById("pcMode").value === "BACK_ONLY" ? "BACK" : "FRONT");
        const rule = saved ? saved.acceptance_rule : "";
        return '<tr class="pc-class-row" data-class-id="' + cls.id + '" data-class-name="' + escapeHtml(cls.name) + '">' +
            '<td><label><input class="pc-enabled" type="checkbox" ' + (!saved || saved.enabled ? "checked" : "") + '> ' + escapeHtml(cls.name) + '</label></td>' +
            '<td><select class="pc-side"><option value="FRONT" ' + (side === "FRONT" ? "selected" : "") + '>Front</option><option value="BACK" ' + (side === "BACK" ? "selected" : "") + '>Back</option><option value="BOTH" ' + (side === "BOTH" ? "selected" : "") + '>Both</option></select></td>' +
            '<td><input class="pc-front-count" type="number" min="0" step="1" value="' + (saved ? saved.expected_front : 0) + '" aria-label="Expected Front count"></td>' +
            '<td><input class="pc-back-count" type="number" min="0" step="1" value="' + (saved ? saved.expected_back : 0) + '" aria-label="Expected Back count"></td>' +
            '<td><input class="pc-front-threshold" type="number" min="0" max="1" step="0.01" value="' + (saved ? saved.front_threshold : 0.5) + '" aria-label="Front confidence threshold"></td>' +
            '<td><input class="pc-back-threshold" type="number" min="0" max="1" step="0.01" value="' + (saved ? saved.back_threshold : 0.5) + '" aria-label="Back confidence threshold"></td>' +
            '<td><input class="pc-priority" type="number" min="1" step="1" value="' + (saved ? saved.priority : priority) + '" aria-label="Priority"></td>' +
            '<td><input class="pc-required" type="checkbox" ' + (saved ? (saved.required ? "checked" : "") : "") + ' aria-label="Required class"></td>' +
            '<td><select class="pc-class-rule"><option value="" ' + (!rule ? "selected" : "") + '>Default</option><option value="count" ' + (rule === "count" ? "selected" : "") + '>Count</option><option value="presence" ' + (rule === "presence" ? "selected" : "") + '>Presence</option><option value="confidence" ' + (rule === "confidence" ? "selected" : "") + '>Confidence</option><option value="count_confidence" ' + (rule === "count_confidence" ? "selected" : "") + '>Count + confidence</option></select></td>' +
            '</tr>';
    }

    function addModelCard(model, classes, savedRules, modelEnabled) {
        if (modelsRoot.querySelector('[data-model-id="' + model.id + '"]')) return;
        const rulesById = new Map((savedRules || []).map((rule) => [Number(rule.class_id), rule]));
        const card = document.createElement("section");
        card.className = "pc-model-card";
        card.dataset.modelId = model.id;
        card.innerHTML = '<div class="pc-model-head"><label><input class="pc-model-enabled" type="checkbox" ' + (modelEnabled !== false ? "checked" : "") + '> Enable ' + escapeHtml(model.name) + ' v' + escapeHtml(model.version || "1.0") + '</label>' +
            '<button class="vi-btn pc-remove-model" type="button">Remove</button></div>' +
            '<div class="pc-table-wrap"><table class="pc-class-table"><thead><tr><th>Class</th><th>Side</th><th>Front count</th><th>Back count</th><th>Front threshold</th><th>Back threshold</th><th>Priority</th><th>Required</th><th>Rule</th></tr></thead><tbody>' +
            classes.map((cls, index) => rowMarkup(model.id, cls, rulesById.get(Number(cls.id)), modelsRoot.querySelectorAll(".pc-class-row").length + index + 1)).join("") +
            '</tbody></table></div>';
        card.querySelector(".pc-remove-model").addEventListener("click", () => {
            card.remove();
            updatePicker();
        });
        modelsRoot.appendChild(card);
        updatePicker();
    }

    async function addRegisteredModel() {
        const modelId = Number(picker.value);
        if (!modelId) {
            setMessage("Select a registered model before clicking Add Model.", false);
            return;
        }
        const model = availableModels.find((item) => item.id === modelId);
        if (!model) {
            setMessage("That model is no longer available. Refresh the page and try again.", false);
            return;
        }
        addButton.disabled = true;
        setMessage("Discovering classes from the selected model…", false);
        try {
            const data = await requestJson(cfg.urls.classes.replace("__ID__", modelId));
            addModelCard(model, data.classes || [], []);
            setMessage("Model classes loaded from the registered model.", true);
        } catch (error) {
            setMessage(error.message, false);
        } finally {
            updatePicker();
        }
    }

    async function uploadModel() {
        const file = modelFile.files && modelFile.files[0];
        if (!file) return;
        const ext = file.name.split(".").pop().toLowerCase();
        if (ext !== "pt" && ext !== "onnx") {
            setMessage("Choose a .pt or .onnx model file.", false);
            modelFile.value = "";
            return;
        }
        addButton.disabled = true;
        setMessage("Uploading and validating the model…", false);
        try {
            const formData = new FormData();
            formData.append("model_file", file);
            formData.append("version", uploadVersion.value.trim());
            const data = await requestJson(cfg.urls.upload, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken(), Accept: "application/json" },
                body: formData,
            });
            availableModels.push(data.model);
            addModelCard(data.model, data.classes || [], [], true);
            uploadVersion.value = "";
            setMessage(data.message || "Model uploaded and validated.", true);
        } catch (error) {
            setMessage(error.message, false);
        } finally {
            modelFile.value = "";
            addButton.disabled = false;
            updatePicker();
        }
    }

    function loadConfiguration(data) {
        availableModels = data.available_models || [];
        currentConfiguration = data.configuration || null;
        const config = currentConfiguration;
        document.getElementById("pcStatus").textContent = config ? config.status.replaceAll("_", " ") : "Not Configured";
        document.getElementById("pcVersion").textContent = config ? "Version " + config.version : "";
        document.getElementById("pcRoi").textContent = "Common ROI: " + (config && config.roi_configured ? "configured" : "not configured");
        if (config) {
            document.getElementById("pcMode").value = config.inspection_mode;
            document.getElementById("pcRule").value = config.acceptance_rule;
            document.getElementById("pcFrontSource").value = config.front_camera_source;
            document.getElementById("pcFrontIndex").value = config.front_camera_index;
            document.getElementById("pcBackSource").value = config.back_camera_source;
            document.getElementById("pcBackIndex").value = config.back_camera_index;
            config.models.forEach((savedModel) => {
                const registered = availableModels.find((item) => item.id === savedModel.id) || savedModel;
                const classes = (savedModel.discovered_classes || []).map((item) => ({ id: item.id, name: item.name }));
                if (classes.length) {
                    addModelCard(registered, classes, savedModel.classes || [], savedModel.enabled);
                } else {
                    setMessage("Saved model class metadata is incomplete. Re-add the model to rediscover its classes.", false);
                }
            });
        }
        updatePicker();
    }

    function collectModels() {
        return Array.from(modelsRoot.querySelectorAll(".pc-model-card"), (card) => ({
            model_id: Number(card.dataset.modelId),
            enabled: card.querySelector(".pc-model-enabled").checked,
            classes: Array.from(card.querySelectorAll(".pc-class-row"), (row) => ({
                class_id: Number(row.dataset.classId),
                class_name: row.dataset.className,
                enabled: row.querySelector(".pc-enabled").checked,
                side: row.querySelector(".pc-side").value,
                expected_front: Number(row.querySelector(".pc-front-count").value),
                expected_back: Number(row.querySelector(".pc-back-count").value),
                front_threshold: Number(row.querySelector(".pc-front-threshold").value),
                back_threshold: Number(row.querySelector(".pc-back-threshold").value),
                priority: Number(row.querySelector(".pc-priority").value),
                required: row.querySelector(".pc-required").checked,
                acceptance_rule: row.querySelector(".pc-class-rule").value,
            })),
        }));
    }

    async function saveConfiguration(event) {
        event.preventDefault();
        if (!form.reportValidity()) return;
        const body = {
            confirm_changes: document.getElementById("pcConfirm").checked,
            inspection_mode: document.getElementById("pcMode").value,
            acceptance_rule: document.getElementById("pcRule").value,
            front_camera_source: document.getElementById("pcFrontSource").value,
            front_camera_index: Number(document.getElementById("pcFrontIndex").value),
            back_camera_source: document.getElementById("pcBackSource").value,
            back_camera_index: Number(document.getElementById("pcBackIndex").value),
            models: collectModels(),
        };
        if (currentConfiguration && !currentConfiguration.is_active) {
            body.configuration_id = currentConfiguration.id;
        }
        const button = document.getElementById("pcSave");
        button.disabled = true;
        setMessage("Saving model configuration…", false);
        try {
            const result = await requestJson(cfg.urls.save, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                    Accept: "application/json",
                },
                body: JSON.stringify(body),
            });
            const validationErrors = Array.isArray(result.validation_errors) ? result.validation_errors : [];
            const ready = result.status === "READY" && result.is_active === true && validationErrors.length === 0;
            const statusMessage = result.message + " Status: " + String(result.status || "unknown").replaceAll("_", " ") + ".";
            setMessage(statusMessage + (validationErrors.length ? " " + validationErrors.join(" ") : ""), ready);
            await refresh();
        } catch (error) {
            setMessage(error.message, false);
        } finally {
            button.disabled = false;
        }
    }

    async function refresh() {
        try {
            const data = await requestJson(cfg.urls.read);
            loadConfiguration(data);
        } catch (error) {
            setMessage(error.message, false);
        }
    }

    addButton.addEventListener("click", () => modelFile.click());
    addRegisteredButton.addEventListener("click", addRegisteredModel);
    modelFile.addEventListener("change", uploadModel);
    form.addEventListener("submit", saveConfiguration);
    refresh();
})();