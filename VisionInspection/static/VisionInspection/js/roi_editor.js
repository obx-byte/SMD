/**
 * VisionInspection — ROI Editor Canvas
 *
 * Handles drawing and editing four-corner polygon ROIs on a canvas overlay
 * for both the trigger zone and individual ROI areas.
 */

(function () {
    "use strict";

    let canvas = null;
    let ctx = null;
    let scaleX = 1;
    let scaleY = 1;
    let masterWidth = 0;
    let masterHeight = 0;
    let mode = "trigger"; // "trigger" or "roi"
    let polygons = [];
    let activePolygonIndex = -1;
    let draggingVertex = -1;
    let draggingPolygon = false;
    let isMouseDown = false;

    // ------------------------------------------------------------------ #
    //  Setup
    // ------------------------------------------------------------------ #

    function setMasterDimensions(w, h) {
        masterWidth = w;
        masterHeight = h;
    }

    function setMode(m) {
        mode = m;
        render();
    }

    function setPolygons(polys) {
        if (mode === "trigger") {
            polygons = polys.length > 0 ? [polys[0]] : [
                [0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]
            ];
        } else {
            polygons = polys.length > 0 ? polys : [];
        }
        render();
    }

    // ------------------------------------------------------------------ #
    //  Canvas initialization
    // ------------------------------------------------------------------ #

    function initCanvas(canvasId) {
        canvas = document.getElementById(canvasId);
        if (!canvas) {
            console.warn("Canvas not found:", canvasId);
            return;
        }

        ctx = canvas.getContext("2d");

        // Set up scaling: canvas displays at its CSS size, but we work in
        // normalized coordinates (0-1), mapping to the canvas pixel space.
        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width;
        canvas.height = rect.height;
        scaleX = rect.width / masterWidth;
        scaleY = rect.height / masterHeight;
        if (masterWidth === 0 || masterHeight === 0) {
            scaleX = 1;
            scaleY = 1;
        }

        canvas.addEventListener("mousedown", onMouseDown);
        canvas.addEventListener("mousemove", onMouseMove);
        canvas.addEventListener("mouseup", onMouseUp);
        canvas.addEventListener("mouseleave", onMouseUp);
        canvas.addEventListener("wheel", onWheel);

        render();
    }

    // ------------------------------------------------------------------ #
    //  Coordinate conversion
    // ------------------------------------------------------------------ #

    function normalizePoint(px, py) {
        if (masterWidth === 0 || masterHeight === 0) return [px, py];
        return [px / scaleX / masterWidth, py / scaleY / masterHeight];
    }

    function denormalizePoint(nx, ny) {
        if (masterWidth === 0 || masterHeight === 0) return [nx, ny];
        return [nx * masterWidth * scaleX, ny * masterHeight * scaleY];
    }

    // ------------------------------------------------------------------ #
    //  Rendering
    // ------------------------------------------------------------------ #

    function render() {
        if (!ctx || !canvas) return;

        ctx.clearRect(0, 0, canvas.width, canvas.height);

        polygons.forEach((poly, idx) => {
            if (poly.length < 4) return;

            const active = idx === activePolygonIndex;
            ctx.strokeStyle = active ? "#58a6ff" : "#2ea043";
            ctx.lineWidth = active ? 3 : 2;
            ctx.setLineDash(active ? [] : [5, 3]);

            // Fill
            ctx.fillStyle = active
                ? "rgba(88, 166, 255, 0.15)"
                : "rgba(46, 160, 67, 0.1)";
            ctx.beginPath();
            poly.forEach((pt, i) => {
                const [px, py] = denormalizePoint(pt[0], pt[1]);
                if (i === 0) ctx.moveTo(px, py);
                else ctx.lineTo(px, py);
            });
            ctx.closePath();
            ctx.fill();
            ctx.stroke();
            ctx.setLineDash([]);

            // Draw vertices
            poly.forEach((pt, vi) => {
                const [px, py] = denormalizePoint(pt[0], pt[1]);
                ctx.fillStyle = "#58a6ff";
                ctx.beginPath();
                ctx.arc(px, py, 6, 0, Math.PI * 2);
                ctx.fill();
                ctx.strokeStyle = "#fff";
                ctx.lineWidth = 1.5;
                ctx.stroke();
            });

            // Label
            if (mode === "roi" && idx >= 0) {
                const [cx, cy] = denormalizePoint(
                    poly.reduce((a, p) => a + p[0], 0) / poly.length,
                    poly.reduce((a, p) => a + p[1], 0) / poly.length
                );
                ctx.fillStyle = "#fff";
                ctx.font = "12px monospace";
                ctx.textAlign = "center";
                ctx.fillText("ROI-" + (idx + 1).toString().padStart(2, "0"), cx, cy);
            }
        });
    }

    // ------------------------------------------------------------------ #
    //  Mouse events
    // ------------------------------------------------------------------ #

    function getMousePos(evt) {
        const rect = canvas.getBoundingClientRect();
        const x = evt.clientX - rect.left;
        const y = evt.clientY - rect.top;
        return { x, y };
    }

    function hitTestVertex(pos) {
        if (activePolygonIndex < 0 || activePolygonIndex >= polygons.length)
            return -1;

        const poly = polygons[activePolygonIndex];
        for (let i = 0; i < poly.length; i++) {
            const [px, py] = denormalizePoint(poly[i][0], poly[i][1]);
            const dist = Math.sqrt(Math.pow(pos.x - px, 2) + Math.pow(pos.y - py, 2));
            if (dist < 12) return i;
        }
        return -1;
    }

    function hitTestPolygon(pos) {
        for (let i = polygons.length - 1; i >= 0; i--) {
            const poly = polygons[i];
            if (poly.length < 3) continue;

            let inside = false;
            for (let j = 0, k = poly.length - 1; j < poly.length; k = j++) {
                const [xj, yj] = denormalizePoint(poly[j][0], poly[j][1]);
                const [xk, yk] = denormalizePoint(poly[k][0], poly[k][1]);
                if (
                    yj > pos.y !== yk > pos.y &&
                    pos.x < ((xk - xj) * (pos.y - yj)) / (yk - yj) + xj
                ) {
                    inside = !inside;
                }
            }
            if (inside) return i;
        }
        return -1;
    }

    function onMouseDown(evt) {
        const pos = getMousePos(evt);
        const vertexIdx = hitTestVertex(pos);

        if (vertexIdx >= 0) {
            draggingVertex = vertexIdx;
            isMouseDown = true;
            return;
        }

        const polyIdx = hitTestPolygon(pos);
        if (polyIdx >= 0) {
            activePolygonIndex = polyIdx;
            draggingPolygon = true;
            isMouseDown = true;
            return;
        }

        // Click on empty space
        if (mode === "roi") {
            // Add new ROI
            const norm = normalizePoint(pos.x, pos.y);
            const size = 0.05;
            polygons.push([
                [norm[0] - size, norm[1] - size],
                [norm[0] + size, norm[1] - size],
                [norm[0] + size, norm[1] + size],
                [norm[0] - size, norm[1] + size],
            ]);
            activePolygonIndex = polygons.length - 1;
            render();
        }
    }

    function onMouseMove(evt) {
        if (!isMouseDown) return;

        const pos = getMousePos(evt);
        const norm = normalizePoint(pos.x, pos.y);

        if (draggingVertex >= 0 && activePolygonIndex >= 0) {
            polygons[activePolygonIndex][draggingVertex] = [
                Math.max(0, Math.min(1, norm[0])),
                Math.max(0, Math.min(1, norm[1])),
            ];
            render();
        } else if (draggingPolygon && activePolygonIndex >= 0) {
            // Move entire polygon
            const poly = polygons[activePolygonIndex];
            const prevPos = getMousePos(evt.movementX ? evt : evt);
            const dx = normalizePoint(evt.movementX || 0, 0)[0];
            const dy = normalizePoint(0, evt.movementY || 0)[1];

            const newPoly = poly.map(p => [
                Math.max(0, Math.min(1, p[0] + dx)),
                Math.max(0, Math.min(1, p[1] + dy)),
            ]);
            polygons[activePolygonIndex] = newPoly;
            render();
        }
    }

    function onMouseUp() {
        isMouseDown = false;
        draggingVertex = -1;
        draggingPolygon = false;
    }

    function onWheel(evt) {
        evt.preventDefault();
        const delta = evt.deltaY < 0 ? 1.1 : 0.9;
        // Adjust scale
        scaleX *= delta;
        scaleY *= delta;
        render();
    }

    // ------------------------------------------------------------------ #
    //  Public API
    // ------------------------------------------------------------------ #

    window.roiEditor = {
        init: initCanvas,
        setMasterDimensions: setMasterDimensions,
        setMode: setMode,
        setPolygons: setPolygons,
        getPolygons: function () {
            // Return normalized polygons
            return polygons.map(poly => {
                return poly.map(p => [
                    parseFloat(p[0].toFixed(6)),
                    parseFloat(p[1].toFixed(6)),
                ]);
            });
        },
        getActivePolygon: function () {
            if (activePolygonIndex >= 0 && activePolygonIndex < polygons.length) {
                return polygons[activePolygonIndex];
            }
            return null;
        },
        addRoi: function () {
            if (mode === "roi") {
                polygons.push([
                    [0.3, 0.3], [0.5, 0.3], [0.5, 0.5], [0.3, 0.5]
                ]);
                activePolygonIndex = polygons.length - 1;
                render();
            }
        },
        deleteActive: function () {
            if (activePolygonIndex >= 0 && activePolygonIndex < polygons.length) {
                polygons.splice(activePolygonIndex, 1);
                activePolygonIndex = Math.max(0, polygons.length - 1);
                render();
            }
        },
        duplicateActive: function () {
            if (activePolygonIndex >= 0 && activePolygonIndex < polygons.length) {
                const poly = JSON.parse(JSON.stringify(polygons[activePolygonIndex]));
                polygons.push(poly);
                activePolygonIndex = polygons.length - 1;
                render();
            }
        },
        reset: function () {
            if (mode === "trigger") {
                polygons = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]];
            } else {
                polygons = [];
            }
            render();
        },
        save: function () {
            return this.getPolygons();
        },
    };

    // Expose window functions for configuration.js to call
    window.initRoiEditor = initCanvas;
    window.setMasterDims = setMasterDimensions;

})();
