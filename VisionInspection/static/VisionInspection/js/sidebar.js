(function () {
    "use strict";

    var root = document.documentElement;
    var compactToggle = document.getElementById("viSidebarCompactToggle");

    function setCompact(compact) {
        var value = compact ? "true" : "false";
        root.setAttribute("data-sidebar-compact", value);
        if (compactToggle) {
            compactToggle.setAttribute("aria-expanded", compact ? "false" : "true");
            compactToggle.setAttribute("aria-label", compact ? "Expand navigation" : "Collapse navigation");
            compactToggle.setAttribute("title", compact ? "Expand navigation" : "Collapse navigation");
            compactToggle.firstElementChild.textContent = compact ? "\u00bb" : "\u00ab";
        }
    }

    var compactPreference = root.getAttribute("data-sidebar-compact");
    if (compactPreference === null) {
        compactPreference = window.matchMedia("(max-width: 1023px)").matches ? "true" : "false";
    }
    setCompact(compactPreference === "true");

    if (compactToggle) {
        compactToggle.addEventListener("click", function () {
            var compact = root.getAttribute("data-sidebar-compact") !== "true";
            setCompact(compact);
            try {
                localStorage.setItem("vi-sidebar-compact", compact ? "true" : "false");
            } catch (error) {
                // The in-page preference still applies when browser storage is unavailable.
            }
        });
    }

    document.querySelectorAll("[data-sidebar-group]").forEach(function (button) {
        var panel = document.getElementById(button.getAttribute("aria-controls"));
        if (!panel) return;

        var active = Boolean(panel.querySelector(".vi-sidebar-link.active"));
        var savedState = null;
        try {
            savedState = localStorage.getItem("vi-sidebar-group-" + panel.id);
        } catch (error) {
            savedState = null;
        }
        var expanded = savedState === null ? active : savedState === "true";

        function setExpanded(open) {
            button.setAttribute("aria-expanded", open ? "true" : "false");
            button.classList.toggle("vi-expanded", open);
            button.classList.toggle("has-active", active);
            panel.classList.toggle("vi-collapsed", !open);
            panel.setAttribute("aria-hidden", open ? "false" : "true");
            panel.inert = !open;
        }

        setExpanded(expanded || active);
        button.addEventListener("click", function () {
            var open = button.getAttribute("aria-expanded") !== "true";
            setExpanded(open);
            try {
                localStorage.setItem("vi-sidebar-group-" + panel.id, open ? "true" : "false");
            } catch (error) {
                // The group can still be expanded for the current page.
            }
        });

        if (!button.hasAttribute("title")) {
            button.setAttribute("title", button.getAttribute("aria-label"));
        }
    });

    document.querySelectorAll(".vi-sidebar-link").forEach(function (link) {
        var label = link.querySelector(".vi-sidebar-label");
        var text = label ? label.textContent.trim() : link.textContent.trim();
        if (!text) return;
        if (!link.hasAttribute("title")) link.setAttribute("title", text);
        link.setAttribute("aria-label", text);
    });
})();
