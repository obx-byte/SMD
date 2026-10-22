(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        var root = document.getElementById("storageManager");
        if (!root || root.dataset.canBrowse !== "true") return;

        var rootSelect = document.getElementById("storageRoot");
        var browseButton = document.getElementById("storageBrowse");
        var refreshButton = document.getElementById("storageRefresh");
        var parentButton = document.getElementById("storageParent");
        var entries = document.getElementById("storageEntries");
        var location = document.getElementById("storageLocation");
        var status = document.getElementById("storageStatus");
        var loading = document.getElementById("storageLoading");
        var empty = document.getElementById("storageEmpty");
        var current = null;
        var source = null;
        var destination = null;
        var requestNumber = 0;

        function csrfToken() {
            var field = document.querySelector("#storageCsrf input[name=csrfmiddlewaretoken]");
            if (field) return field.value;
            var field = document.querySelector("#storageAddRoot input[name=csrfmiddlewaretoken]");
            if (!field) field = document.querySelector("[data-deactivate-root] input[name=csrfmiddlewaretoken]");
            return field ? field.value : "";
        }

        function showStatus(message, state) {
            status.textContent = message || "";
            status.dataset.state = state || "";
        }

        function pathText(rootPath, relative) {
            return relative ? rootPath.replace(/[\\/]+$/, "") + "/" + relative : rootPath;
        }

        function selectedPath(selection) {
            return selection ? pathText(current.root.path, selection.path) : "";
        }

        function updateSelection() {
            var sourceLabel = document.getElementById("storageSourceLabel");
            var destinationLabel = document.getElementById("storageDestinationLabel");
            var merge = document.getElementById("storageMerge");
            if (sourceLabel) sourceLabel.textContent = source ? selectedPath(source) : "Not selected";
            if (destinationLabel) destinationLabel.textContent = destination ? selectedPath(destination) : "Not selected";
            if (merge) {
                var sameRoot = source && destination && source.rootId === destination.rootId;
                var same = sameRoot && source.path === destination.path;
                function isNested(parentPath, childPath) {
                    return parentPath === ""
                        ? childPath !== ""
                        : childPath.indexOf(parentPath + "/") === 0;
                }
                var nested = sameRoot && (
                    isNested(source.path, destination.path)
                    || isNested(destination.path, source.path)
                );
                merge.disabled = !source || !destination || !sameRoot || same || nested;
            }
        }

        function formatSize(value) {
            if (value === null || value === undefined) return "—";
            if (value < 1024) return value + " B";
            if (value < 1048576) return (value / 1024).toFixed(1) + " KB";
            return (value / 1048576).toFixed(1) + " MB";
        }

        function renderListing(data) {
            current = data;
            location.textContent = pathText(data.root.path, data.path);
            entries.replaceChildren();
            empty.hidden = data.entries.length !== 0;
            data.entries.forEach(function (entry) {
                var row = document.createElement("tr");
                var nameCell = document.createElement("td");
                var typeCell = document.createElement("td");
                var sizeCell = document.createElement("td");
                if (entry.type === "directory") {
                    var open = document.createElement("button");
                    open.type = "button";
                    open.className = "storage-entry-open";
                    open.textContent = "📁 " + entry.name;
                    open.addEventListener("click", function () {
                        loadFolder(data.root.id, entry.path);
                    });
                    nameCell.appendChild(open);
                } else {
                    var fileName = document.createElement("span");
                    fileName.className = "storage-entry-file";
                    fileName.textContent = "📄 " + entry.name;
                    nameCell.appendChild(fileName);
                }
                typeCell.textContent = entry.type === "directory" ? "Folder" : "File";
                sizeCell.textContent = formatSize(entry.size);
                row.append(nameCell, typeCell, sizeCell);
                entries.appendChild(row);
            });
            if (!data.entries.length) {
                var row = document.createElement("tr");
                var cell = document.createElement("td");
                cell.colSpan = 3;
                cell.className = "storage-empty";
                cell.textContent = "No folders or image files in this location.";
                row.appendChild(cell);
                entries.appendChild(row);
            }
            browseButton.disabled = false;
            refreshButton.disabled = false;
            parentButton.disabled = !data.path;
            ["storageNewFolder", "storageSetSource", "storageSetDestination"].forEach(function (id) {
                var button = document.getElementById(id);
                if (button) button.disabled = false;
            });
            showStatus(data.truncated ? "Showing the first 500 items." : "", data.truncated ? "warning" : "");
            updateSelection();
        }

        function loadFolder(rootId, relativePath, completionMessage) {
            if (!rootId) return;
            var sequence = ++requestNumber;
            loading.hidden = false;
            browseButton.disabled = true;
            refreshButton.disabled = true;
            var params = new URLSearchParams({ root_id: rootId, path: relativePath || "" });
            fetch(root.dataset.browseUrl + "?" + params.toString(), {
                credentials: "same-origin",
                headers: { "Accept": "application/json" }
            }).then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok || !data.ok) throw new Error(data.message || "Unable to browse this folder.");
                    return data;
                });
            }).then(function (data) {
                if (sequence === requestNumber) {
                    renderListing(data);
                    if (completionMessage) showStatus(completionMessage, "success");
                }
            }).catch(function (error) {
                if (sequence === requestNumber) showStatus(error.message, "error");
            }).finally(function () {
                if (sequence === requestNumber) {
                    loading.hidden = true;
                    browseButton.disabled = !rootSelect.value;
                    refreshButton.disabled = !current;
                }
            });
        }

        function postJson(url, payload) {
            return fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                    "Accept": "application/json"
                },
                body: JSON.stringify(payload)
            }).then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok || !data.ok) throw new Error(data.message || "The request could not be completed.");
                    return data;
                });
            });
        }

        rootSelect.addEventListener("change", function () {
            current = null;
            source = destination = null;
            showStatus("", "");
            location.textContent = "Loading authorized location…";
            updateSelection();
            browseButton.disabled = !rootSelect.value;
            refreshButton.disabled = true;
            parentButton.disabled = true;
            ["storageNewFolder", "storageSetSource", "storageSetDestination"].forEach(function (id) {
                var button = document.getElementById(id);
                if (button) button.disabled = true;
            });
            if (rootSelect.value) loadFolder(rootSelect.value, "");
            else location.textContent = "Choose an authorized location to browse.";
        });
        browseButton.addEventListener("click", function () {
            if (rootSelect.value) loadFolder(rootSelect.value, current ? current.path : "");
        });
        refreshButton.addEventListener("click", function () {
            if (current) loadFolder(current.root.id, current.path);
        });
        parentButton.addEventListener("click", function () {
            if (!current || !current.path) return;
            var parent = current.path.split("/").slice(0, -1).join("/");
            loadFolder(current.root.id, parent);
        });

        var newFolder = document.getElementById("storageNewFolder");
        if (newFolder) {
            newFolder.addEventListener("click", function () {
                var name = window.prompt("Enter a name for the new folder:");
                if (!name || !current) return;
                newFolder.disabled = true;
                postJson(root.dataset.createUrl, {
                    root_id: current.root.id,
                    parent_path: current.path,
                    name: name
                }).then(function () {
                    loadFolder(current.root.id, current.path, "Folder created.");
                }).catch(function (error) {
                    showStatus(error.message, "error");
                }).finally(function () {
                    newFolder.disabled = false;
                });
            });
        }

        var setSource = document.getElementById("storageSetSource");
        if (setSource) setSource.addEventListener("click", function () {
            if (!current) return;
            source = { rootId: current.root.id, path: current.path };
            updateSelection();
        });
        var setDestination = document.getElementById("storageSetDestination");
        if (setDestination) setDestination.addEventListener("click", function () {
            if (!current) return;
            destination = { rootId: current.root.id, path: current.path };
            updateSelection();
        });

        var mergeButton = document.getElementById("storageMerge");
        if (mergeButton) mergeButton.addEventListener("click", function () {
            if (!source || !destination || source.rootId !== destination.rootId) return;
            var sourceDisplay = selectedPath(source);
            var destinationDisplay = selectedPath(destination);
            if (!window.confirm("Merge inspection image files from:\n" + sourceDisplay + "\n\nInto:\n" + destinationDisplay + "\n\nThe source folder will be retained. Continue?")) return;
            mergeButton.disabled = true;
            showStatus("Merging files and updating image references…", "");
            postJson(root.dataset.mergeUrl, {
                root_id: source.rootId,
                source_path: source.path,
                destination_path: destination.path
            }).then(function (data) {
                var message = "Merge completed. Processed " + data.file_count + " image file(s); updated "
                    + data.metadata_updated + " image reference(s). Source folder retained at "
                    + sourceDisplay + ". " + data.source_files_retained
                    + " source file(s) remain because they were identical or could not be removed.";
                if (data.source_files_retained_paths && data.source_files_retained_paths.length) {
                    message += " Retained: " + data.source_files_retained_paths.slice(0, 3).join(", ");
                    if (data.source_files_retained_paths.length > 3) {
                        message += " (and " + (data.source_files_retained_paths.length - 3) + " more)";
                    }
                }
                loadFolder(current.root.id, current.path, message);
            }).catch(function (error) {
                showStatus(error.message, "error");
                updateSelection();
            });
        });

        var addRootForm = document.getElementById("storageAddRoot");
        if (addRootForm) addRootForm.addEventListener("submit", function (event) {
            event.preventDefault();
            var values = new FormData(addRootForm);
            postJson(root.dataset.rootsUrl, {
                name: values.get("name"),
                path: values.get("path")
            }).then(function () {
                showStatus("Authorized location added.", "success");
                window.location.reload();
            }).catch(function (error) {
                showStatus(error.message, "error");
            });
        });

        var deactivateTemplate = root.dataset.deactivateTemplate;
        document.querySelectorAll("[data-deactivate-root]").forEach(function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                var rootId = form.dataset.deactivateRoot;
                if (!window.confirm("Deactivate this authorized location? Existing files will not be changed.")) return;
                var url = deactivateTemplate.replace("/0/", "/" + rootId + "/");
                postJson(url, {}).then(function () {
                    window.location.reload();
                }).catch(function (error) {
                    showStatus(error.message, "error");
                });
            });
        });
        updateSelection();
    });
})();
