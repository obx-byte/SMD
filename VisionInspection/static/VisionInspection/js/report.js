/**
 * VisionInspection — Inspection Report page behaviour
 *
 * Server-side rendering handles filtering, pagination and exports; this file
 * only adds the page-local interactions:
 *   • page-size selection (submits the filter form, keeps every filter)
 *   • Print (browser print of the report, see report.css @media print)
 *   • in-page search over the records shown on the current page
 */
(function () {
    "use strict";

    const form = document.getElementById("reportFilters");
    const table = document.getElementById("reportTable");
    if (!form || !table) return;

    // Page size → reload the report with the same filters.
    const pageSize = document.getElementById("reportPageSize");
    if (pageSize) {
        pageSize.addEventListener("change", function () {
            const param = document.createElement("input");
            param.type = "hidden";
            param.name = "page_size";
            param.value = this.value;
            form.appendChild(param);
            form.submit();
        });
    }

    // Print the report (the print stylesheet hides the app chrome).
    const printBtn = document.getElementById("reportPrintBtn");
    if (printBtn) {
        printBtn.addEventListener("click", function () {
            window.print();
        });
    }

    // Search the records of the current page (no server round-trip).
    const search = document.getElementById("reportSearch");
    const searchEmpty = document.getElementById("reportSearchEmpty");
    if (search) {
        search.addEventListener("input", function () {
            const query = this.value.trim().toLowerCase();
            let visible = 0;
            table.querySelectorAll("tbody tr[data-report-row]").forEach(function (row) {
                const match = !query || row.textContent.toLowerCase().indexOf(query) !== -1;
                row.style.display = match ? "" : "none";
                if (match) {
                    visible += 1;
                }
            });
            if (searchEmpty) {
                searchEmpty.style.display = visible ? "none" : "";
            }
        });
    }
})();
