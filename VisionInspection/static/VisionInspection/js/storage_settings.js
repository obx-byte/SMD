(function () {
    "use strict";

    var cfg = window.__VI_STORAGE__ || { urls: {}, state: {} };
    var state = cfg.state || {};
    var form = document.getElementById("storageDestinationForm");
    var typeSelect = document.getElementById("storageType");
    var testButton = document.getElementById("storageTestBtn");
    var activateButton = document.getElementById("storageActivateBtn");
    var notice = document.getElementById("storageNotice");
    var testResult = document.getElementById("storageTestResult");
    var busy = false;

    function el(id) { return document.getElementById(id); }
    function csrf() { return cfg.csrf || ""; }
    function escapedText(value) { return value == null || value === "" ? "—" : String(value); }
    function formatBytes(value) {
        var bytes = Number(value || 0);
        if (bytes < 1024) return bytes + " B";
        var units = ["KB", "MB", "GB", "TB"];
        var index = -1;
        do { bytes /= 1024; index += 1; } while (bytes >= 1024 && index < units.length - 1);
        return bytes.toFixed(bytes >= 100 ? 0 : 1) + " " + units[index];
    }
    function destinationTypeLabel(type) {
        return ({
            WINDOWS: "Windows SMB/CIFS",
            LINUX: "Ubuntu Samba (SMB/CIFS)",
            SMB: "SMB/CIFS",
            LOCAL: "Local Storage"
        })[type] || String(type || "Unknown");
    }
    function setNotice(message, kind) {
        notice.textContent = message || "";
        notice.dataset.state = kind || "";
    }
    function setTest(stateName, title, detail, checkedAt) {
        testResult.dataset.state = stateName;
        el("storageTestStatus").textContent = title;
        el("storageTestDetails").textContent = detail;
        el("storageTestTime").textContent = checkedAt ? new Date(checkedAt).toLocaleString() : "—";
    }
    function toggleSmbFields() {
        var local = typeSelect.value === "LOCAL";
        document.querySelectorAll("[data-smb-only]").forEach(function (node) {
            node.hidden = local;
            var input = node.querySelector("input");
            if (input) input.disabled = local;
        });
    }
    function formPayload() {
        var result = {};
        new FormData(form).forEach(function (value, key) {
            if (key !== "csrfmiddlewaretoken") result[key] = value;
        });
        return result;
    }
    async function request(url, method, payload) {
        var response = await fetch(url, {
            method: method,
            headers: {
                Accept: "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": csrf()
            },
            body: payload ? JSON.stringify(payload) : undefined
        });
        var data;
        try { data = await response.json(); }
        catch (error) { throw new Error("The server returned an unreadable response."); }
        if (!response.ok || data.ok === false) throw new Error(data.message || "Storage request failed.");
        return data;
    }
    function appendCell(row, value) {
        var cell = document.createElement("td");
        cell.textContent = escapedText(value);
        row.appendChild(cell);
        return cell;
    }
    function renderDestinations(destinations) {
        var body = el("storageDestinationRows");
        body.replaceChildren();
        if (!destinations || !destinations.length) {
            var empty = document.createElement("tr");
            var cell = appendCell(empty, "No destinations configured.");
            cell.colSpan = 8;
            body.appendChild(empty);
            return;
        }
        destinations.forEach(function (destination) {
            var row = document.createElement("tr");
            appendCell(row, destination.id);
            appendCell(row, destinationTypeLabel(destination.destination_type));
            appendCell(row, destination.destination_type === "LOCAL" ? "Local filesystem" : destination.ip_address + " / " + destination.share_name);
            appendCell(row, destination.destination_directory || "—");
            var statusCell = document.createElement("td");
            var statusBadge = document.createElement("span");
            statusBadge.className = "storage-badge " + (destination.connection_status === "CONNECTED" ? "is-connected" : "");
            statusBadge.textContent = destination.connection_status || "UNKNOWN";
            statusCell.appendChild(statusBadge);
            row.appendChild(statusCell);
            var activeCell = document.createElement("td");
            var activeBadge = document.createElement("span");
            activeBadge.className = "storage-badge " + (destination.is_active ? "is-active" : "");
            activeBadge.textContent = destination.is_active ? "ACTIVE" : "INACTIVE";
            activeCell.appendChild(activeBadge);
            row.appendChild(activeCell);
            appendCell(row, destination.created_at ? new Date(destination.created_at).toLocaleString() : "—");
            var actionCell = document.createElement("td");
            var actionWrap = document.createElement("div");
            actionWrap.className = "storage-row-actions";
            var loadButton = document.createElement("button");
            loadButton.type = "button";
            loadButton.className = "storage-button storage-small";
            loadButton.textContent = "LOAD";
            loadButton.addEventListener("click", function () { loadDestination(destination); });
            actionWrap.appendChild(loadButton);
            actionCell.appendChild(actionWrap);
            row.appendChild(actionCell);
            body.appendChild(row);
        });
    }
    function loadDestination(destination) {
        typeSelect.value = destination.destination_type || "SMB";
        el("storageHost").value = destination.ip_address || "";
        el("storageShare").value = destination.share_name || "";
        el("storageDirectory").value = destination.destination_directory || "";
        el("storageUsername").value = destination.username || "";
        el("storagePassword").value = "";
        toggleSmbFields();
        setTest("untested", "Not Tested", "Loaded settings must be tested again before activation.", null);
        el("storageHost").focus();
    }
    function renderState(data, updateSettings) {
        state = data;
        var active = data.active;
        var badge = el("storageActiveBadge");
        badge.dataset.state = active ? "active" : "inactive";
        badge.textContent = active ? "ACTIVE · " + (active.name || active.destination_type) : "No destination active";
        el("summaryDestination").textContent = active
            ? (active.destination_type === "LOCAL" ? "Local Storage" : destinationTypeLabel(active.destination_type) + " · " + active.ip_address + " / " + active.share_name)
            : "Local fallback";
        el("summaryImageCount").textContent = String(data.summary.image_count || 0);
        el("summaryImageBytes").textContent = formatBytes(data.summary.bytes);
        el("summaryFreeSpace").textContent = data.summary.free_bytes == null
            ? "Not reported" : formatBytes(data.summary.free_bytes);
        if (updateSettings) {
            var automatic = data.automatic || {};
            el("automaticEnabled").checked = !!automatic.enabled;
            el("thresholdRange").value = automatic.threshold_percent || 50;
            el("thresholdNumber").value = automatic.threshold_percent || 50;
        }
        renderAutomatic(data);
        renderDestinations(data.destinations || []);
        renderMigrationSources(data);
    }
    function renderAutomatic(data) {
        var automatic = data.automatic || {};
        var summary = data.summary || {};
        var usage = summary.usage_percent;
        var threshold = Number(el("thresholdNumber").value || automatic.threshold_percent || 50);
        var destination = data.active;
        el("automaticDestination").textContent = destination
            ? (destination.destination_type === "LOCAL" ? "Local Storage" : destinationTypeLabel(destination.destination_type) + " · " + destination.ip_address + " / " + destination.share_name)
            : "No validated SMB destination";
        el("automaticUsage").textContent = usage == null
            ? "Not available"
            : Number(usage).toFixed(1) + "% · " + formatBytes(summary.used_bytes) + " / " + formatBytes(summary.total_bytes);
        el("automaticThresholdValue").textContent = threshold + "%";
        el("automaticDestinationStatus").textContent = destination
            ? (destination.connection_status || "UNKNOWN") + (destination.last_verified_at ? " · " + new Date(destination.last_verified_at).toLocaleString() : "")
            : "Not configured";
        el("automaticPending").textContent = String(automatic.pending_files || 0);
        el("automaticTransferred").textContent = String(automatic.transferred_files || 0);
        el("automaticLastError").textContent = automatic.last_transfer_error || "None";
        var bar = el("usageBar");
        var marker = el("thresholdMarker");
        var warning = el("automaticWarning");
        marker.style.left = Math.max(0, Math.min(100, threshold)) + "%";
        if (usage == null) {
            bar.style.width = "0%";
            bar.className = "";
            el("usageProgressText").textContent = "Usage not available";
            warning.textContent = "Local volume usage unavailable";
        } else {
            var percent = Math.max(0, Math.min(100, Number(usage)));
            bar.style.width = percent + "%";
            bar.className = percent >= threshold ? "is-warning" : "";
            if (percent >= 90) bar.classList.add("is-critical");
            el("usageProgressText").textContent = percent.toFixed(1) + "% used · transfer starts at " + threshold + "%";
            warning.textContent = automatic.enabled && percent >= threshold
                ? "Threshold reached; worker will transfer eligible files."
                : "";
        }
        if (automatic.enabled && (!destination || destination.destination_type === "LOCAL")) {
            warning.textContent = "Automatic transfer needs an active SMB destination.";
        }
    }
    function renderMigrationSources(data) {
        var select = el("migrationSource");
        select.replaceChildren();
        var activeId = data.active && data.active.id;
        (data.destinations || []).filter(function (destination) {
            return !destination.is_active && destination.id !== activeId;
        }).forEach(function (destination) {
            var option = document.createElement("option");
            option.value = String(destination.id);
            option.textContent = (destination.name || destination.destination_type) + " · " + destination.connection_status;
            select.appendChild(option);
        });
        if (Number(data.local_migration_count || 0) > 0) {
            var local = document.createElement("option");
            local.value = "LOCAL";
            local.textContent = "Local-only evidence · " + data.local_migration_count + " images";
            select.appendChild(local);
        }
        el("migrationStartBtn").disabled = !data.active || !select.options.length;
    }
    async function refreshState() {
        try {
            var data = await request(cfg.urls.state, "GET");
            renderState(data, false);
        } catch (error) { setNotice(error.message, "error"); }
    }
    async function saveAutomaticSettings() {
        if (busy) return;
        var threshold = Number(el("thresholdNumber").value);
        if (!Number.isInteger(threshold) || threshold < 10 || threshold > 95) {
            setNotice("Storage threshold must be a whole number from 10 to 95.", "error");
            return;
        }
        busy = true;
        el("saveAutomaticBtn").disabled = true;
        try {
            await request(cfg.urls.automaticSettings, "POST", {
                enabled: el("automaticEnabled").checked,
                threshold_percent: threshold
            });
            setNotice("Automatic transfer settings saved.", "success");
            await refreshState();
            renderState(state, true);
        } catch (error) {
            setNotice(error.message, "error");
        } finally {
            busy = false;
            el("saveAutomaticBtn").disabled = false;
        }
    }
    async function testConnection() {
        if (busy) return;
        busy = true;
        testButton.disabled = true;
        activateButton.disabled = true;
        setNotice("");
        setTest("testing", "Testing", "Checking connection and verifying temporary-file read/write access...", null);
        try {
            var data = await request(cfg.urls.test, "POST", formPayload());
            setTest("connected", "Connected", "Connection succeeded; test file was written, read back, and removed.", data.checked_at);
            setNotice("Connection test succeeded. The active destination was not changed.", "success");
        } catch (error) {
            setTest("failed", "Connection Failed", error.message, new Date().toISOString());
            setNotice(error.message, "error");
        } finally {
            busy = false;
            testButton.disabled = false;
            activateButton.disabled = false;
        }
    }
    async function activate(event) {
        event.preventDefault();
        if (busy) return;
        busy = true;
        testButton.disabled = true;
        activateButton.disabled = true;
        setNotice("Activation re-tests storage before changing the active destination.", "");
        try {
            var data = await request(cfg.urls.activate, "POST", formPayload());
            el("storagePassword").value = "";
            setTest("connected", "Connected", "Activation was persisted after a successful read/write test.", data.test.checked_at);
            setNotice(data.message, "success");
            await refreshState();
        } catch (error) {
            setNotice(error.message, "error");
        } finally {
            busy = false;
            testButton.disabled = false;
            activateButton.disabled = false;
        }
    }
    function setMigrationProgress(job) {
        var percent = job.total ? Math.min(100, Math.round((job.success + job.failed) / job.total * 100)) : 100;
        el("migrationProgressBar").style.width = percent + "%";
        el("migrationProgressText").textContent = job.status + " · " + (job.success || 0) + " copied · " + (job.failed || 0) + " failed · " + (job.total || 0) + " total";
        el("migrationFailureText").textContent = job.last_error || "";
    }
    async function startMigration() {
        var button = el("migrationStartBtn");
        if (busy || button.disabled) return;
        busy = true;
        button.disabled = true;
        try {
            var data = await request(cfg.urls.migrate, "POST", {
                source_destination_id: el("migrationSource").value,
                delete_originals: el("migrationDeleteOriginals").checked,
                delete_confirmation: el("migrationConfirmation").value
            });
            var job = data.job;
            setMigrationProgress(job);
            while (!job.done) {
                job = (await request(cfg.urls.batchPrefix + job.id + "/batch/", "POST", {})).job;
                setMigrationProgress(job);
            }
            setNotice("Migration finished. Originals were retained unless deletion was separately confirmed.", job.failed ? "error" : "success");
            await refreshState();
        } catch (error) {
            el("migrationFailureText").textContent = error.message;
            setNotice(error.message, "error");
        } finally {
            busy = false;
            button.disabled = false;
            renderMigrationSources(state);
        }
    }
    function toggleDeleteConfirmation() {
        var checked = el("migrationDeleteOriginals").checked;
        el("migrationConfirmationWrap").hidden = !checked;
        el("migrationConfirmation").disabled = !checked;
        if (!checked) el("migrationConfirmation").value = "";
    }

    typeSelect.addEventListener("change", toggleSmbFields);
    testButton.addEventListener("click", testConnection);
    form.addEventListener("submit", activate);
    el("storageRefreshBtn").addEventListener("click", refreshState);
    el("migrationDeleteOriginals").addEventListener("change", toggleDeleteConfirmation);
    el("migrationStartBtn").addEventListener("click", startMigration);
    window.setInterval(function () {
        if (!document.hidden && !busy) refreshState();
    }, 15000);
    toggleSmbFields();
    renderState(state, true);
    el("thresholdRange").addEventListener("input", function () {
        el("thresholdNumber").value = el("thresholdRange").value;
        renderAutomatic(state);
    });
    el("thresholdNumber").addEventListener("input", function () {
        var value = Number(el("thresholdNumber").value);
        if (Number.isFinite(value)) el("thresholdRange").value = Math.max(10, Math.min(95, value));
        renderAutomatic(state);
    });
    el("saveAutomaticBtn").addEventListener("click", saveAutomaticSettings);
    el("checkStorageBtn").addEventListener("click", refreshState);
})();
