(function () {
    "use strict";

    var root = document.documentElement;
    var compactToggle = document.getElementById("viSidebarCompactToggle");
    var tooltip = document.createElement("div");
    var tooltipTarget = null;
    tooltip.className = "vi-sidebar-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    document.body.appendChild(tooltip);

    function setCompact(compact) {
        var value = compact ? "true" : "false";
        root.setAttribute("data-sidebar-compact", value);
        if (compactToggle) {
            compactToggle.setAttribute("aria-expanded", compact ? "false" : "true");
            compactToggle.setAttribute("aria-label", compact ? "Expand navigation" : "Collapse navigation");
            compactToggle.setAttribute("data-sidebar-tooltip", compact ? "Expand navigation" : "Collapse navigation");
            compactToggle.removeAttribute("title");
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

        button.setAttribute("data-sidebar-tooltip", button.getAttribute("aria-label"));
        button.removeAttribute("title");
    });

    document.querySelectorAll(".vi-sidebar-link").forEach(function (link) {
        var label = link.querySelector(".vi-sidebar-label");
        var text = label ? label.textContent.trim() : link.textContent.trim();
        if (!text) return;
        link.setAttribute("aria-label", text);
        link.setAttribute("data-sidebar-tooltip", text);
        link.removeAttribute("title");
    });

    document.querySelectorAll(".vi-sidebar [title]").forEach(function (element) {
        element.removeAttribute("title");
    });

    function hideTooltip() {
        tooltip.classList.remove("is-visible");
        tooltip.hidden = true;
        tooltipTarget = null;
    }

    function showTooltip(event) {
        var target = event.target.closest("[data-sidebar-tooltip]");
        if (!target || root.getAttribute("data-sidebar-compact") !== "true") return;

        var label = target.getAttribute("data-sidebar-tooltip");
        if (!label) return;
        tooltipTarget = target;
        tooltip.textContent = label;
        tooltip.hidden = false;
        tooltip.classList.add("is-visible");

        var targetRect = target.getBoundingClientRect();
        var tooltipRect = tooltip.getBoundingClientRect();
        var left = Math.min(targetRect.right + 8, window.innerWidth - tooltipRect.width - 8);
        var top = Math.min(
            Math.max(8, targetRect.top + (targetRect.height - tooltipRect.height) / 2),
            window.innerHeight - tooltipRect.height - 8
        );
        tooltip.style.left = Math.max(8, left) + "px";
        tooltip.style.top = top + "px";
    }

    document.addEventListener("pointerover", showTooltip);
    document.addEventListener("focusin", showTooltip);
    document.addEventListener("pointerout", function (event) {
        if (tooltipTarget && tooltipTarget.contains(event.target) &&
                !tooltipTarget.contains(event.relatedTarget)) {
            hideTooltip();
        }
    });
    document.addEventListener("focusout", hideTooltip);
    document.addEventListener("scroll", hideTooltip, true);
    window.addEventListener("resize", hideTooltip);
})();
