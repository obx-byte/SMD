"""Baseline probe: measure the CURRENT hot path of the inspection pipeline.

Run before any optimisation to capture "before" numbers.  The script talks to
the real service code (no re-implementation) so the numbers reflect the
shipped behaviour.

    python -m tools.baseline_probe --images media/vision_inspection/sessions/front \
        --model data/models/<file>.onnx --repeat 5
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "SMD.settings")

import django  # noqa: E402

django.setup()


def _rss_mb():
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except Exception:
            pass
        return float("nan")


def _cpu_time():
    t = time.process_time()
    return t


def collect_images(folder, limit):
    folder = Path(folder)
    paths = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    if limit:
        paths = paths[:limit]
    return paths


def measure(paths, model_path, roi, repeat, conf):
    from VisionInspection.services.model_registry import ModelRegistry

    registry = ModelRegistry()
    x, y, w, h = roi

    load_start = time.perf_counter()
    info = registry.inspect(model_path, task="segment")
    load_ms = (time.perf_counter() - load_start) * 1000.0
    if not info.get("ok"):
        print("MODEL LOAD FAILED:", info.get("error"))
        return None

    # Warm-up so the first-call overhead is not counted as steady state.
    warm = cv2.imread(str(paths[0]))
    registry.predict(model_path, warm, conf=conf, classes=None, task="segment")

    stage = {"crop": [], "infer": [], "post": [], "annotate": [], "encode": [], "total": []}
    detections_per_image = {}
    peak_rss = _rss_mb()
    cpu_start = _cpu_time()
    wall_start = time.perf_counter()

    for path in paths:
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        fh, fw = frame.shape[:2]
        px, py = int(x * fw), int(y * fh)
        pw, ph = int(w * fw), int(h * fh)
        for _ in range(repeat):
            t_all = time.perf_counter()

            t0 = time.perf_counter()
            crop = frame[py:py + ph, px:px + pw]
            stage["crop"].append((time.perf_counter() - t0) * 1000.0)

            t0 = time.perf_counter()
            dets, err = registry.predict(
                model_path, crop, conf=conf, classes=None, task="segment"
            )
            stage["infer"].append((time.perf_counter() - t0) * 1000.0)
            if isinstance(err, str):
                print("INFERENCE ERROR:", err)
                return None

            t0 = time.perf_counter()
            mapped = []
            for d in dets:
                item = dict(d)
                for key, off, lim in (
                    ("x1", px, fw - 1), ("x2", px, fw - 1),
                    ("y1", py, fh - 1), ("y2", py, fh - 1),
                ):
                    if key in item:
                        item[key] = max(0, min(lim, int(item[key]) + off))
                mapped.append(item)
            stage["post"].append((time.perf_counter() - t0) * 1000.0)

            t0 = time.perf_counter()
            out = frame.copy()
            mask_layer = out.copy()
            for d in mapped:
                poly = d.get("polygon")
                if poly and len(poly) >= 3:
                    pts = np.asarray(poly, dtype=np.int32).reshape((-1, 1, 2))
                    cv2.fillPoly(mask_layer, [pts], (0, 190, 255))
            out = cv2.addWeighted(mask_layer, 0.32, out, 0.68, 0)
            stage["annotate"].append((time.perf_counter() - t0) * 1000.0)

            t0 = time.perf_counter()
            cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            stage["encode"].append((time.perf_counter() - t0) * 1000.0)

            stage["total"].append((time.perf_counter() - t_all) * 1000.0)

        key = path.name
        detections_per_image[key] = len(dets)
        peak_rss = max(peak_rss, _rss_mb())

    wall = time.perf_counter() - wall_start
    cpu = _cpu_time() - cpu_start
    return {
        "model": str(model_path),
        "model_load_ms": round(load_ms, 1),
        "num_classes": info.get("num_classes"),
        "images": len(paths),
        "repeat": repeat,
        "samples": len(stage["total"]),
        "peak_rss_mb": round(peak_rss, 1),
        "wall_s": round(wall, 2),
        "cpu_s": round(cpu, 2),
        "cpu_util_pct": round(100.0 * cpu / wall, 1) if wall else 0.0,
        "inspections_per_hour": round(3600.0 / (wall / max(1, len(paths))), 1),
        "stages_ms": {
            name: {
                "mean": round(statistics.mean(vals), 2),
                "p50": round(statistics.median(vals), 2),
                "p95": round(sorted(vals)[int(len(vals) * 0.95) - 1], 2),
                "max": round(max(vals), 2),
            }
            for name, vals in stage.items() if vals
        },
        "detections_per_image": detections_per_image,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--conf", type=float, default=0.30)
    ap.add_argument("--roi", default="0.0125,0.006944,0.977344,0.993056")
    ap.add_argument("--json", default="")
    ap.add_argument("--label", default="baseline")
    args = ap.parse_args()

    roi = tuple(float(v) for v in args.roi.split(","))
    paths = collect_images(args.images, args.limit)
    if not paths:
        print("no images found")
        return 1

    result = measure(paths, args.model, roi, args.repeat, args.conf)
    if result is None:
        return 2
    result["label"] = args.label
    result["roi"] = list(roi)
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
