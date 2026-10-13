/**
 * VisionInspection — Master data controller (Employee & Part Master)
 *
 * Data-table front-end driven entirely by the real backend JSON APIs:
 * server-side search + pagination + create/update with duplicate validation.
 * A single config object (window.__VI_MASTERS__) makes the same script power
 * both the Employee Master and the Part Master without duplication.
 */
(function () {
    "use strict";

    const cfg = window.__VI_MASTERS__;
    const root = document.getElementById("viMasterApp");
    if (!cfg || !root) return;

    const state = { q: "", page: 1, pageSize: cfg.pageSize || 15, editingId: null };

    function csrf() {
        if (cfg.csrfToken) return cfg.csrfToken;
        const m = (document.cookie || "").match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    function buildShell() {
        root.innerHTML =
            '<div class="vi-toolbar">' +
                '<input type="text" id="viMasterQ" placeholder="' + esc(cfg.searchPlaceholder || "Search…") + '" value="' + esc(state.q) + '">' +
                (cfg.readOnly ? "" : '<button type="button" class="vi-btn vi-btn-primary" id="viMasterAdd">+ Add New ' + esc(cfg.primaryLabel) + '</button>') +
            '</div>' +
            '<div class="vi-card" style="padding:0;overflow-x:auto"><table class="vi-table">' +
                '<thead><tr>' +
                    cfg.columns.map((c) => "<th>" + esc(c.label) + "</th>").join("") +
                    '<th>Status</th><th>Created</th><th></th>' +
                "</tr></thead>" +
                '<tbody id="viMasterBody"><tr><td colspan="' + (cfg.columns.length + 3) + '">Loading…</td></tr></tbody>' +
            '</table></div>' +
            '<div class="vi-pagination" id="viMasterPag"></div>' +
            /* modal */
            '<div class="vi-master-modal" id="viMasterModal">' +
                '<div class="vi-master-modal-card">' +
                    '<div class="vi-master-modal-head"><h3 id="viMasterModalTitle">Add New</h3>' +
                    '<button type="button" class="vi-master-x" id="viMasterClose" aria-label="Close">&times;</button></div>' +
                    '<div class="vi-master-form-error" id="viMasterFormError" style="display:none"></div>' +
                    '<form id="viMasterForm" novalidate>' +
                        cfg.fields.map(fieldInputHtml).join("") +
                        '<div class="vi-master-modal-actions">' +
                            '<button type="submit" class="vi-btn vi-btn-primary">Save</button>' +
                            '<button type="button" class="vi-btn" id="viMasterCancel">Cancel</button>' +
                        "</div>" +
                    "</form>" +
                "</div>" +
            "</div>" +
            '<div class="vi-master-toast" id="viMasterToast" style="display:none"></div>';
    }

    function fieldInputHtml(f) {
        const id = "fld_" + f.name;
        const req = f.required ? "required" : "";
        const input = f.type === "textarea"
            ? "<textarea id=\"" + id + "\" data-field=\"" + f.name + "\" " + req + " rows=\"2\"></textarea>"
            : "<input type=\"text\" id=\"" + id + "\" data-field=\"" + f.name + "\" " + req + " autocomplete=\"off\">";
        return '<div class="vi-master-field"><label for="' + id + '">' + esc(f.label) +
            (f.required ? ' <span class="req">*</span>' : "") + "</label>" + input +
            '<div class="vi-master-field-error" data-err="' + f.name + '"></div></div>';
    }

    // ------------------------------------------------------------------ #
    //  List
    // ------------------------------------------------------------------ #
    async function load() {
        const body = document.getElementById("viMasterBody");
        body.innerHTML = '<tr><td colspan="' + (cfg.columns.length + 3) + '">Loading…</td></tr>';
        try {
            const params = new URLSearchParams();
            if (state.q) params.set("q", state.q);
            params.set("page", state.page);
            params.set("page_size", state.pageSize);
            const res = await fetch(cfg.urls.list + "?" + params.toString(), {
                headers: { Accept: "application/json" },
            });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            renderRows(data);
            renderPagination(data);
        } catch (e) {
            body.innerHTML =
                '<tr><td colspan="' + (cfg.columns.length + 3) + '"><div class="vi-empty"><div class="vi-empty-title">Could not load data</div><div class="vi-empty-text">' + esc(e.message) + "</div></div></td></tr>";
        }
    }

    function renderRows(data) {
        const body = document.getElementById("viMasterBody");
        if (!data.items || !data.items.length) {
            body.innerHTML =
                '<tr><td colspan="' + (cfg.columns.length + 3) + '"><div class="vi-empty">' +
                '<div class="vi-empty-icon">&#9702;</div><div class="vi-empty-title">No records found</div>' +
                '<div class="vi-empty-text">Add your first record using “Add New ' + esc(cfg.primaryLabel) + '”.</div>' +
                "</div></td></tr>";
            return;
        }
        body.innerHTML = data.items.map((it) => {
            const cols = cfg.columns.map((c) => "<td>" + esc(it[c.key]) + "</td>").join("");
            const status = it.is_active
                ? '<span class="vi-badge vi-badge-active">Active</span>'
                : '<span class="vi-badge vi-badge-inactive">Inactive</span>';
            const created = it.created_at ? new Date(it.created_at).toLocaleString() : "—";
            const setAction = cfg.urls.visionConfiguration
                ? '<a class="vi-btn vi-btn-sm" href="' + esc(cfg.urls.visionConfiguration.replace("__ID__", it.id)) + '">SET</a>'
                : "";
            const editAction = cfg.readOnly ? ""
                : '<button type="button" class="vi-btn vi-btn-sm vi-align-edit" data-edit="' + it.id + '">Edit</button>';
            return "<tr data-id=\"" + it.id + "\">" + cols + "<td>" + status + "</td><td>" + created + "</td>" +
                "<td>" + setAction + editAction + "</td></tr>";
        }).join("");
        body.querySelectorAll("[data-edit]").forEach((btn) => {
            btn.addEventListener("click", () => openModal(btn.getAttribute("data-edit")));
        });
    }

    function renderPagination(data) {
        const pag = document.getElementById("viMasterPag");
        if (!data.pages || data.pages <= 1) {
            pag.innerHTML = data.total != null ? ("Total: " + data.total) : "";
            return;
        }
        const p = data.page, pages = data.pages;
        let html = '<button class="vi-btn vi-btn-sm" ' + (p <= 1 ? "disabled" : "") + ' data-page="' + (p - 1) + '">&laquo;</button>';
        html += "<span>Page " + p + " of " + pages + " · " + data.total + " records</span>";
        html += '<button class="vi-btn vi-btn-sm" ' + (p >= pages ? "disabled" : "") + ' data-page="' + (p + 1) + '">&raquo;</button>';
        pag.innerHTML = html;
        pag.querySelectorAll("[data-page]").forEach((b) =>
            b.addEventListener("click", () => {
                const page = parseInt(b.getAttribute("data-page"), 10);
                if (page >= 1) { state.page = page; load(); }
            })
        );
    }

    // ------------------------------------------------------------------ #
    //  Modal + create/update
    // ------------------------------------------------------------------ #
    function openModal(id) {
        state.editingId = id || null;
        clearErrors();
        document.getElementById("viMasterModalTitle").textContent =
            (id ? "Edit " : "Add New ") + cfg.primaryLabel;
        cfg.fields.forEach((f) => { document.getElementById("fld_" + f.name).value = ""; });
        if (id) fillForm(id);
        document.getElementById("viMasterModal").classList.add("open");
    }

    function closeModal() {
        document.getElementById("viMasterModal").classList.remove("open");
        state.editingId = null;
    }

    async function fillForm(id) {
        try {
            const res = await fetch(cfg.urls.list + "?q=&page_size=1000", { headers: { Accept: "application/json" } });
            const data = await res.json();
            const item = (data.items || []).find((x) => String(x.id) === String(id));
            if (!item) return;
            cfg.fields.forEach((f) => {
                const el = document.getElementById("fld_" + f.name);
                el.value = item[f.name] != null ? item[f.name] : "";
            });
        } catch (e) { /* ignore */ }
    }

    function collectPayload() {
        const payload = {};
        cfg.fields.forEach((f) => {
            const el = document.getElementById("fld_" + f.name);
            payload[f.name] = el.value.trim();
        });
        return payload;
    }

    function clearErrors() {
        document.getElementById("viMasterFormError").style.display = "none";
        root.querySelectorAll("[data-err]").forEach((el) => (el.textContent = ""));
    }

    function showErrors(fields, fallback) {
        const box = document.getElementById("viMasterFormError");
        if (fields && Object.keys(fields).length) {
            Object.entries(fields).forEach(([name, msg]) => {
                const err = root.querySelector('[data-err="' + name + '"]');
                if (err) err.textContent = msg;
                else { box.textContent = msg; box.style.display = "block"; }
            });
        } else if (fallback) {
            box.textContent = fallback;
            box.style.display = "block";
        }
    }

    function toast(message, ok) {
        const t = document.getElementById("viMasterToast");
        t.textContent = message;
        t.className = "vi-master-toast " + (ok ? "ok" : "err");
        t.style.display = "block";
        clearTimeout(toast._t);
        toast._t = setTimeout(() => (t.style.display = "none"), 3500);
    }

    async function submit(evt) {
        evt.preventDefault();
        clearErrors();
        const payload = collectPayload();
        const url = state.editingId
            ? cfg.urls.update.replace("__ID__", state.editingId)
            : cfg.urls.create;
        try {
            const res = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" },
                body: JSON.stringify(payload),
            });
            let data = {};
            try { data = await res.json(); } catch (e) { data = {}; }
            if (res.status === 201 || res.status === 200) {
                toast(cfg.primaryLabel + " saved successfully.", true);
                closeModal();
                load();
            } else if (res.status === 400 || res.status === 409) {
                showErrors(data.fields, data.error || "Validation failed.");
            } else {
                showErrors(null, data.error || ("HTTP " + res.status));
            }
        } catch (e) {
            showErrors(null, "Network error: " + e.message);
        }
    }

    // ------------------------------------------------------------------ #
    //  Init
    // ------------------------------------------------------------------ #
    function init() {
        buildShell();
        let debounce;
        document.getElementById("viMasterQ").addEventListener("input", (e) => {
            clearTimeout(debounce);
            debounce = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; load(); }, 300);
        });
        const addButton = document.getElementById("viMasterAdd");
        if (addButton) addButton.addEventListener("click", () => openModal(null));
        document.getElementById("viMasterClose").addEventListener("click", closeModal);
        document.getElementById("viMasterCancel").addEventListener("click", closeModal);
        const form = document.getElementById("viMasterForm");
        if (form) form.addEventListener("submit", submit);
        document.getElementById("viMasterModal").addEventListener("click", (e) => {
            if (e.target.id === "viMasterModal") closeModal();
        });
        load();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
