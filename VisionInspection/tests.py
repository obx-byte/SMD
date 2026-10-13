"""Tests for Employee Master and Part Master duplicate prevention.

These exercise the real model constraints (normalized-unique columns) and the
CRUD API validation (create/update with self-exclusion on edit).
"""

from django.test import TestCase
from django.db import IntegrityError, transaction
from django.urls import reverse

import json
import os
import re
import tempfile

from VisionInspection.models import (
    VisionInspectionEmployee,
    VisionInspectionPart,
    normalize_token,
)


def _post(client, url, payload):
    import json
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _emp_create_url():
    return reverse("VisionInspection:employee-create")


def _emp_update_url(pk):
    return reverse("VisionInspection:employee-update", args=[pk])


def _emp_list_url(query=""):
    return reverse("VisionInspection:api-employee-list") + query


def _part_create_url():
    return reverse("VisionInspection:part-create")


def _part_update_url(pk):
    return reverse("VisionInspection:part-update", args=[pk])


class NormalizeTokenTests(TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize_token("Ravi Kumar"), normalize_token("ravi  kumar"))
        self.assertEqual(normalize_token(" Ravi Kumar "), normalize_token("RAVI   KUMAR"))
        self.assertEqual(normalize_token(""), "")


class EmployeeModelTests(TestCase):
    def test_save_populates_normalized(self):
        emp = VisionInspectionEmployee.objects.create(
            employee_code="E1", employee_name="  Ravi   Kumar "
        )
        self.assertEqual(emp.name_normalized, "ravi kumar")

    def test_db_rejects_normalized_duplicate(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VisionInspectionEmployee.objects.create(
                    employee_code="E2", employee_name="ravi  kumar"
                )


class EmployeeApiTests(TestCase):
    """Employee master is administration — a staff (Django) account is required.

    The inspection station itself stays anonymous: these routes are not part of
    the operator workflow (see ``StationAccessTests``).
    """

    def setUp(self):
        self.client_obj, self.admin = _staff_client()

    def test_create_valid(self):
        r = _post(self.client_obj, _emp_create_url(), {
            "employee_code": "E100", "employee_name": "Ravi Kumar",
            "department": "QA", "designation": "Inspector",
        })
        self.assertEqual(r.status_code, 201)
        self.assertTrue(VisionInspectionEmployee.objects.filter(employee_code="E100").exists())

    def test_reject_exact_duplicate_name(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        r = _post(self.client_obj, _emp_create_url(), {
            "employee_code": "E2", "employee_name": "Ravi Kumar",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("employee_name", r.json()["fields"])

    def test_reject_case_insensitive_duplicate_name(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        r = _post(self.client_obj, _emp_create_url(), {
            "employee_code": "E2", "employee_name": "RAVI KUMAR",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("employee_name", r.json()["fields"])

    def test_reject_whitespace_duplicate_name(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        r = _post(self.client_obj, _emp_create_url(), {
            "employee_code": "E2", "employee_name": "  Ravi   Kumar  ",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("employee_name", r.json()["fields"])

    def test_reject_duplicate_employee_id(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        r = _post(self.client_obj, _emp_create_url(), {
            "employee_code": "e1", "employee_name": "Other Person",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("employee_code", r.json()["fields"])

    def test_edit_self_is_not_false_duplicate(self):
        emp = VisionInspectionEmployee.objects.create(
            employee_code="E1", employee_name="Ravi Kumar"
        )
        r = _post(self.client_obj, _emp_update_url(emp.id), {
            "employee_code": "E1", "employee_name": "ravi kumar",  # same normalized name
        })
        self.assertEqual(r.status_code, 200)
        emp.refresh_from_db()
        self.assertEqual(emp.name_normalized, "ravi kumar")

    def test_edit_rejects_other_employees_name(self):
        VisionInspectionEmployee.objects.create(employee_code="E1", employee_name="Ravi Kumar")
        emp2 = VisionInspectionEmployee.objects.create(employee_code="E2", employee_name="Sunil")
        r = _post(self.client_obj, _emp_update_url(emp2.id), {
            "employee_code": "E2", "employee_name": "Ravi Kumar",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("employee_name", r.json()["fields"])

    def test_list_pagination(self):
        for i in range(20):
            VisionInspectionEmployee.objects.create(
                employee_code=f"E{i:03d}", employee_name=f"Person {i:03d}"
            )
        r = self.client_obj.get(_emp_list_url("?page=1&page_size=15"))
        data = r.json()
        self.assertEqual(len(data["items"]), 15)
        self.assertEqual(data["total"], 20)
        self.assertEqual(data["pages"], 2)


class PartModelTests(TestCase):
    def test_save_populates_normalized(self):
        part = VisionInspectionPart.objects.create(
            part_number="PART-001", part_name="Bracket"
        )
        self.assertEqual(part.part_number_normalized, "part-001")

    def test_db_rejects_normalized_duplicate(self):
        VisionInspectionPart.objects.create(part_number="PART-001", part_name="Bracket")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VisionInspectionPart.objects.create(
                    part_number="part-001", part_name="Other"
                )


class PartApiTests(TestCase):
    """Part master is administration — a staff (Django) account is required."""

    def setUp(self):
        self.client_obj, self.admin = _staff_client()

    def test_create_valid(self):
        r = _post(self.client_obj, _part_create_url(), {
            "part_number": "PART-001", "part_name": "Bracket", "description": "Metal",
        })
        self.assertEqual(r.status_code, 201)
        self.assertTrue(VisionInspectionPart.objects.filter(part_number="PART-001").exists())

    def test_reject_case_insensitive_duplicate(self):
        VisionInspectionPart.objects.create(part_number="PART-001", part_name="Bracket")
        r = _post(self.client_obj, _part_create_url(), {
            "part_number": "part-001", "part_name": "Other",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("part_number", r.json()["fields"])

    def test_edit_self_ok(self):
        part = VisionInspectionPart.objects.create(part_number="PART-001", part_name="Bracket")
        r = _post(self.client_obj, _part_update_url(part.id), {
            "part_number": "part-001", "part_name": "Bracket v2",
        })
        self.assertEqual(r.status_code, 200)
        part.refresh_from_db()
        self.assertEqual(part.part_name, "Bracket v2")

    def test_edit_rejects_conflict(self):
        VisionInspectionPart.objects.create(part_number="PART-001", part_name="Bracket")
        p2 = VisionInspectionPart.objects.create(part_number="PART-002", part_name="Clip")
        r = _post(self.client_obj, _part_update_url(p2.id), {
            "part_number": "PART-001", "part_name": "Clip",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("part_number", r.json()["fields"])


from VisionInspection.models import VisionInspectionLog  # noqa: E402


class TraceabilityTests(TestCase):
    def _make_log(self, **kw):
        defaults = dict(
            part_number="PART-001",
            employee_code="E1",
            preset_name="Preset A",
            preset_version=1,
            result="OK",
            roi_results=[{"roi_name": "ROI-01", "result": "MATCH", "details": "bolt x4"}],
            detected_objects=[],
        )
        defaults.update(kw)
        return VisionInspectionLog.objects.create(**defaults)

    def test_list_empty(self):
        r = self.client.get(reverse("VisionInspection:ui-traceability"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No inspection records")

    def test_list_shows_record(self):
        self._make_log()
        r = self.client.get(reverse("VisionInspection:ui-traceability"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PART-001")

    def test_detail_marks_missing_evidence_and_model(self):
        log = self._make_log()  # no image, no model
        r = self.client.get(reverse("VisionInspection:traceability-detail", args=[log.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No image stored")
        self.assertContains(r, "Not captured")        # cropped ROI honest absence
        self.assertContains(r, "Not generated")       # annotated honest absence
        self.assertContains(r, "MODEL NOT CONFIGURED")

    def test_detail_404(self):
        r = self.client.get(reverse("VisionInspection:traceability-detail", args=[999999]))
        self.assertEqual(r.status_code, 404)

    def test_detail_with_model_shows_detections(self):
        log = self._make_log(
            yolo_model_name="yolo_v1", yolo_model_version="1",
            detected_objects=[{"class": "bolt", "confidence": 0.9, "bbox": [1, 2, 3, 4]}],
        )
        r = self.client.get(reverse("VisionInspection:traceability-detail", args=[log.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "yolo_v1")
        self.assertContains(r, "Detections")


import numpy as np  # noqa: E402
from VisionInspection.services.camera_service import CameraService  # noqa: E402
from VisionInspection.services.continuous_inspection import (  # noqa: E402
    ContinuousInspectionEngine,
)
from VisionInspection.services.roi_inference import (  # noqa: E402
    PlaceholderROIInferenceModel,
)


class CameraBufferTests(TestCase):
    """Latest-frame buffer is bounded, single-slot, and tracks drops/age."""

    def setUp(self):
        CameraService._instance = None  # fresh singleton per test

    def tearDown(self):
        CameraService._instance = None

    def test_latest_only_and_drop_counting(self):
        svc = CameraService()
        for i in range(3):
            svc.push_frame(np.full((4, 4, 3), i, dtype=np.uint8))
        ok, frame, meta = svc.get_frame_with_meta()
        self.assertTrue(ok)
        self.assertTrue(np.array_equal(frame, np.full((4, 4, 3), 2, dtype=np.uint8)))
        self.assertEqual(meta["seq"], 3)
        self.assertEqual(meta["dropped"], 2)  # first two frames discarded
        self.assertIsNotNone(meta["age_ms"])

    def test_buffer_is_bounded(self):
        svc = CameraService()
        for i in range(12):
            svc.push_frame(np.full((4, 4, 3), i, dtype=np.uint8))
        self.assertLessEqual(len(svc._frame_buffer), svc._frame_buffer.maxlen)
        self.assertEqual(len(svc.get_frame_buffer()), svc._frame_buffer.maxlen)

    def test_get_frame_returns_copy(self):
        svc = CameraService()
        src = np.full((4, 4, 3), 7, dtype=np.uint8)
        svc.push_frame(src)
        ok, frame = svc.get_frame()
        self.assertTrue(ok)
        frame[:] = 255  # mutating the returned copy must not affect the stored frame
        ok2, again, _meta = svc.get_frame_with_meta()
        self.assertTrue(np.array_equal(again, src))


class ContinuousROIDSATests(TestCase):
    """O(1) ROI cache keyed per part, view-based cropping, single-loop guard."""

    def setUp(self):
        ContinuousInspectionEngine._instance = None
        CameraService._instance = None

    def tearDown(self):
        ContinuousInspectionEngine.stop()
        ContinuousInspectionEngine._instance = None
        CameraService._instance = None

    def _engine(self):
        return ContinuousInspectionEngine()

    def test_roi_cache_is_keyed_and_isolated(self):
        eng = self._engine()
        eng.configure("PART-A", 100, 100, 200, 200, 1280, 720)
        eng.configure("PART-B", 50, 40, 150, 160, 1280, 720)

        eng._active_part = "PART-A"
        rect_a = eng._current_roi_pixels((720, 1280, 3))
        self.assertEqual(rect_a, (100, 100, 200, 200))  # O(1), part A's config

        eng._active_part = "PART-B"
        rect_b = eng._current_roi_pixels((720, 1280, 3))
        self.assertEqual(rect_b, (50, 40, 150, 160))    # no cross-part leakage

    def test_crop_is_a_roi_view_not_full_frame(self):
        eng = self._engine()
        eng.configure("PART-A", 100, 100, 200, 150, 1280, 720)
        eng._active_part = "PART-A"
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        px, py, pw, ph = eng._current_roi_pixels(frame.shape)
        crop = frame[py:py + ph, px:px + pw]  # same operation as the worker
        self.assertEqual(crop.shape, (150, 200, 3))
        self.assertLess(crop.size, frame.size)  # only ROI pixels are processed

    def test_model_only_receives_roi_and_reports_not_configured(self):
        model = PlaceholderROIInferenceModel()
        roi = np.zeros((150, 200, 3), dtype=np.uint8)
        res = model.predict(roi)
        self.assertEqual(res["status"], "MODEL_NOT_CONFIGURED")
        self.assertIn("MODEL NOT CONFIGURED", res["label"])
        self.assertFalse(model.is_configured())

    def test_start_without_camera_does_not_spawn_worker(self):
        eng = self._engine()
        eng.configure("PART-A", 10, 10, 100, 100, 1280, 720)
        status = eng.start(part_number="PART-A")   # camera is OFF → error path
        self.assertEqual(status["state"], "ERROR")
        self.assertTrue(eng._worker is None or not eng._worker.is_alive())

    def test_stop_is_idempotent(self):
        eng = self._engine()
        self.assertTrue(eng.stop())
        self.assertTrue(eng.stop())  # safe to call again


class PartPaginationTests(TestCase):
    def test_part_list_pagination_page2(self):
        """Paginated part list stays stable for large tables (staff-only data)."""
        client, _admin = _staff_client()
        for i in range(1, 21):
            VisionInspectionPart.objects.create(
                part_number=f"PART-{i:03d}", part_name=f"Part {i}"
            )
        r = client.get(reverse("VisionInspection:api-part-list") + "?page=2&page_size=15")
        data = r.json()
        self.assertEqual(len(data["items"]), 5)
        self.assertEqual(data["total"], 20)
        self.assertEqual(data["pages"], 2)
        self.assertEqual(data["page"], 2)


import numpy as np  # noqa: E402
import tempfile  # noqa: E402
import json  # noqa: E402
from django.test import override_settings  # noqa: E402
from unittest import mock  # noqa: E402
from VisionInspection.models import (  # noqa: E402
    VisionInspectionSession,
    VisionInspectionLog,
    VisionInspectionCameraConfig,
    VisionInspectionMasterROI,
    VisionYOLOModel,
)
from VisionInspection.services.inspection_workflow import InspectionWorkflow  # noqa: E402
from VisionInspection.services.yolo_service import YOLOModelService  # noqa: E402


def _fake_frame():
    return np.zeros((720, 1280, 3), dtype=np.uint8)


class _FakeYolo:
    def is_loaded(self):
        return True

    def get_model_path(self):
        return "__fake__"

    def get_class_names(self):
        return ["perimeter", "circular"]

    def detect(self, frame, conf=0.5, iou=0.45):
        return [{"class": "perimeter", "confidence": 0.91, "x1": 10, "y1": 10, "x2": 60, "y2": 60}]


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class InspectionWorkflowTests(TestCase):
    def setUp(self):
        self.part = VisionInspectionPart.objects.create(part_number="AB-100", part_name="Airbag")
        self.wf = InspectionWorkflow()
        self.wf._yolo = _FakeYolo()

    def _roi(self, view):
        return VisionInspectionMasterROI.objects.create(
            part_number="AB-100", view=view,
            x=0.1, y=0.1, width=0.4, height=0.4, ref_width=1280, ref_height=720,
        )

    def _session(self, **kw):
        base = dict(part_number="AB-100", stage="result")
        base.update(kw)
        return VisionInspectionSession.objects.create(**base)

    # ---- Step 1: barcode ---- #
    def test_scan_invalid_barcode(self):
        r = self.wf.scan_part("NOPE")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "part_not_found")
        r2 = self.wf.scan_part("")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["state"], "waiting")

    def test_scan_valid_barcode_case_insensitive(self):
        r = self.wf.scan_part("ab-100")
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], "part_identified")
        self.assertEqual(r["part"]["part_number"], "AB-100")

    # ---- Step 2: model validation (honest failures) ---- #
    def test_validate_model_missing(self):
        r = self.wf.validate_model()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "model_missing")

    def test_validate_model_invalid_path(self):
        VisionYOLOModel.objects.create(
            name="m", model_path="/nonexistent/best.pt", is_active=True, num_classes=2
        )
        self.wf._yolo = YOLOModelService()  # real loader → path check fails
        r = self.wf.validate_model()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "model_invalid")
        self.wf._yolo = _FakeYolo()

    def test_submit_blocked_by_model(self):
        r = self.wf.submit("AB-100")
        self.assertFalse(r["ok"])  # no model → blocked, no session created
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_submit_blocked_by_missing_roi(self):
        model = VisionYOLOModel.objects.create(
            name="m", model_path=__file__, is_active=True, num_classes=2
        )  # existing file → load will still fail (not a .pt), but validate_model path exists
        with mock.patch.object(InspectionWorkflow, "validate_model",
                               return_value={"ok": True, "model_id": model.id,
                                             "model_version": "1", "classes": ["x"], "num_classes": 1}):
            r = self.wf.submit("AB-100")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "roi_missing")

    # ---- Step 4/5: run side (honest errors + real detection) ---- #
    def test_run_side_no_roi_errors(self):
        s = self._session(front_roi=None, model=self._model_obj())
        r = self.wf.run_side(str(s.session_id), "front", frame=_fake_frame())
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "front_error")
        s.refresh_from_db()
        self.assertEqual(s.front_status, "ERROR")

    def test_run_side_success_stores_evidence_and_indeterminate(self):
        model = self._model_obj()
        roi = self._roi("front")
        s = self._session(model=model, front_roi=roi, front_status="IN_PROGRESS", stage="front")
        r = self.wf.run_side(str(s.session_id), "front", frame=_fake_frame())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "front_completed")
        s.refresh_from_db()
        self.assertEqual(s.front_status, "COMPLETED")
        self.assertEqual(s.front_verdict, "INDETERMINATE")  # no acceptance rule → not OK
        self.assertEqual(s.stage, "back")
        self.assertTrue(bool(s.front_image))
        self.assertTrue(bool(s.front_annotated))
        self.assertEqual(s.front_results[0]["stitch"], "perimeter")
        self.assertEqual(s.front_results[0]["status"], "NO_RULE")

    # ---- Step 8: overall result matrix ---- #
    def test_result_ok_only_when_both_ok(self):
        s = self._session(front_status="COMPLETED", back_status="COMPLETED",
                          front_verdict="OK", back_verdict="OK")
        r = self.wf.compute_result(str(s.session_id))
        self.assertEqual(r["overall"], "OK")

    def test_result_not_ok_when_any_side_not_ok(self):
        s = self._session(front_status="COMPLETED", back_status="COMPLETED",
                          front_verdict="OK", back_verdict="NOT_OK")
        self.assertEqual(self.wf.compute_result(str(s.session_id))["overall"], "NOT_OK")

    def test_result_incomplete_never_ok(self):
        s = self._session(front_status="COMPLETED", back_status="PENDING",
                          front_verdict="INDETERMINATE", back_verdict="PENDING")
        self.assertEqual(self.wf.compute_result(str(s.session_id))["overall"], "INCOMPLETE")

    def test_result_error_is_incomplete(self):
        s = self._session(front_status="ERROR", back_status="PENDING",
                          front_verdict="ERROR", back_verdict="PENDING")
        self.assertEqual(self.wf.compute_result(str(s.session_id))["overall"], "INCOMPLETE")

    # ---- Step 9: idempotent save ---- #
    def test_save_gate_and_idempotency(self):
        s = self._session(front_status="COMPLETED", back_status="COMPLETED",
                          front_verdict="OK", back_verdict="OK", overall="OK")
        r1 = self.wf.save(str(s.session_id))
        self.assertTrue(r1["ok"], r1)
        self.assertIn("inspection_id", r1)
        self.assertEqual(VisionInspectionLog.objects.count(), 1)
        r2 = self.wf.save(str(s.session_id))  # repeated save → no duplicate
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["state"], "already_saved")
        self.assertEqual(VisionInspectionLog.objects.count(), 1)

    def test_save_blocked_when_incomplete(self):
        s = self._session(front_status="PENDING", back_status="PENDING", overall="PENDING")
        r = self.wf.save(str(s.session_id))
        self.assertFalse(r["ok"])
        self.assertEqual(VisionInspectionLog.objects.count(), 0)

    # ---- progress ---- #
    def test_progress_reflects_state(self):
        s = self._session(front_status="COMPLETED")
        p = self.wf.get_progress(str(s.session_id))
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["stages"]), 6)
        self.assertEqual(p["stages"][2]["status"], "Completed")  # front completed

    def _model_obj(self):
        obj, _ = VisionYOLOModel.objects.get_or_create(
            name="mtest", model_path=__file__, defaults={"is_active": True, "num_classes": 2}
        )
        return obj


class CameraConfigWorkflowTests(TestCase):
    """One-time camera ROI config: saved once, reused across parts (no redraw)."""

    def setUp(self):
        self.part = VisionInspectionPart.objects.create(part_number="AB-100", part_name="Airbag")
        self.wf = InspectionWorkflow()
        self.wf._yolo = _FakeYolo()

    def _model_obj(self):
        obj, _ = VisionYOLOModel.objects.get_or_create(
            name="cfgmodel", model_path=__file__, defaults={"is_active": True, "num_classes": 2}
        )
        return obj

    def test_save_and_version(self):
        c1 = self.wf.save_camera_config("front", 16, 5, 1251, 715, 1280, 720)
        self.assertEqual(c1.version, 1)
        self.assertEqual(c1.frame_width, 1280)
        got = self.wf.get_camera_config("front")
        self.assertEqual(round(got.roi_w * got.frame_width), 1251)
        self.assertEqual(round(got.roi_h * got.frame_height), 715)
        self.assertEqual(round(got.roi_x * got.frame_width), 16)
        c2 = self.wf.save_camera_config("front", 10, 10, 800, 600, 1280, 720)
        self.assertEqual(c2.version, 2)
        self.assertEqual(VisionInspectionCameraConfig.objects.filter(view="front", is_active=True).count(), 1)

    def test_resolve_prefers_config_over_part(self):
        VisionInspectionMasterROI.objects.create(
            part_number="AB-100", view="front", x=0.2, y=0.2,
            width=0.2, height=0.2, ref_width=1280, ref_height=720,
        )
        self.wf.save_camera_config("front", 16, 5, 1251, 715, 1280, 720)
        roi, src = self.wf._resolve_side_roi("AB-100", "front")
        self.assertEqual(src, "config")
        self.assertEqual(round(roi["w"] * roi["ref_width"]), 1251)

    def test_submit_reuses_config_without_part_roi(self):
        self.wf.save_camera_config("front", 16, 5, 1251, 715, 1280, 720)
        model = self._model_obj()
        with mock.patch.object(self.wf, "validate_model", return_value={
            "ok": True, "model_id": model.id, "model_version": "1",
            "classes": ["perimeter"], "num_classes": 1}):
            r = self.wf.submit("AB-100")
        self.assertTrue(r["ok"], r)  # succeeds despite NO per-part ROI
        sid = r["session_id"]
        s = VisionInspectionSession.objects.get(session_id=sid)
        self.assertTrue(s.front_roi_norm.get("w"))          # ROI frozen from config
        out = self.wf.run_side(sid, "front", frame=_fake_frame())
        self.assertTrue(out["ok"], out)
        s.refresh_from_db()
        self.assertEqual(s.front_status, "COMPLETED")
        # Crop matches the configured ROI height.
        self.assertEqual(out["detections"][0]["class"], "perimeter")

    def test_submit_blocked_without_any_roi(self):
        model = self._model_obj()
        with mock.patch.object(self.wf, "validate_model", return_value={
            "ok": True, "model_id": model.id, "model_version": "1",
            "classes": ["x"], "num_classes": 1}):
            r = self.wf.submit("AB-100")   # no config, no per-part ROI
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "roi_missing")
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_master_roi_get_falls_back_to_config(self):
        self.wf.save_camera_config("front", 16, 5, 1251, 715, 1280, 720)
        r = self.client.get(
            reverse("VisionInspection:master-roi-get") + "?part_number=NEWPART&camera_view=front"
        )
        data = r.json()
        self.assertTrue(data["saved"])
        self.assertEqual(data["source"], "config")
        self.assertEqual(data["width"], 1251)

    def test_camera_config_endpoints(self):
        save = self.client.post(
            reverse("VisionInspection:api-camera-config-save"),
            data=json.dumps({"view": "front", "x": 16, "y": 5, "width": 1251,
                             "height": 715, "ref_width": 1280, "ref_height": 720,
                             "camera_source": "opencv", "camera_index": 0}),
            content_type="application/json",
        )
        self.assertEqual(save.status_code, 201)
        get = self.client.get(reverse("VisionInspection:api-camera-config"))
        data = get.json()
        self.assertIsNotNone(data["front"])
        self.assertEqual(data["front"]["width"], 1251)


# ---------------------------------------------------------------------------
#  Station deployment — no login
#
#  The inspection station serves its operator routes anonymously.  No route in
#  the inspection workflow may redirect to a login page, and privileged admin /
#  configuration routes must stay protected behind a Django staff account.
# ---------------------------------------------------------------------------


def _staff_client():
    from django.contrib.auth.models import User

    user = User.objects.create_user(
        username="engineer", password="x", is_staff=True
    )
    from django.test import Client

    client = Client()
    assert client.login(username="engineer", password="x")
    return client, user


class StationAccessTests(TestCase):
    """Acceptance: the application URL opens the Dashboard, with no login."""

    def test_root_url_opens_dashboard(self):
        r = self.client.get("/vision-inspection/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Airbag Stitch Inspection Overview")
        self.assertContains(r, "Recent Inspection Results")
        self.assertNotContains(r, "Sign in")
        self.assertNotContains(r, "/login/")

    def test_root_url_is_not_a_redirect(self):
        r = self.client.get("/vision-inspection/")
        self.assertNotEqual(r.status_code, 302)
        self.assertLess(r.status_code, 400)

    def test_refresh_does_not_force_login(self):
        for _ in range(3):
            r = self.client.get("/vision-inspection/")
            self.assertEqual(r.status_code, 200)

    def test_station_stays_anonymous(self):
        """No session is created — no user identity is invented."""
        self.client.get("/vision-inspection/")
        self.assertEqual(self.client.session.get("_auth_user_id"), None)

    def test_live_inspection_page_needs_no_login(self):
        r = self.client.get(reverse("VisionInspection:inspection"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "wfBarcode")  # barcode scan step of the workflow

    def test_part_setup_and_part_configuration_need_no_login(self):
        part = VisionInspectionPart.objects.create(
            part_number="SET-01", part_name="Operator accessible part"
        )
        parts_page = self.client.get(reverse("VisionInspection:ui-parts"))
        self.assertEqual(parts_page.status_code, 200)
        self.assertContains(parts_page, "Part Vision Setup")
        self.assertContains(parts_page, "visionConfiguration")

        config_page = self.client.get(
            reverse("VisionInspection:part-vision-configuration", args=[part.pk])
        )
        self.assertEqual(config_page.status_code, 200)
        self.assertContains(config_page, "SET-01")
        self.assertContains(config_page, "Save Model Configuration")
        self.assertContains(config_page, reverse("VisionInspection:part-model-upload"))

    def test_inspection_master_page_needs_no_login(self):
        r = self.client.get(reverse("VisionInspection:inspection-master"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/login/")

    def test_history_and_traceability_need_no_login(self):
        for name in ("VisionInspection:history", "VisionInspection:ui-traceability"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)

    def test_station_shows_configured_station_id(self):
        from django.test import override_settings

        with override_settings(VISION_INSPECTION_STATION_ID="ST-42"):
            r = self.client.get("/vision-inspection/")
            self.assertContains(r, "ST-42")

    def test_station_hides_admin_entry_points_for_anonymous(self):
        """Privileged links are not advertised to station operators."""
        r = self.client.get("/vision-inspection/")
        self.assertNotContains(r, "Admin Panel")
        self.assertNotContains(r, "Employee Master")


class StationApiTests(TestCase):
    """Acceptance: station APIs work anonymously and keep server validation."""

    def setUp(self):
        self.part = VisionInspectionPart.objects.create(
            part_number="AB-100", part_name="Airbag"
        )

    def test_camera_status_api_needs_no_login(self):
        r = self.client.get(reverse("VisionInspection:camera-status"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("status", r.json())

    def test_camera_stop_api_needs_no_login(self):
        r = self.client.get(reverse("VisionInspection:camera-stop"))
        self.assertEqual(r.status_code, 200)

    def test_part_validation_needs_no_login(self):
        r = self.client.post(
            reverse("VisionInspection:api-part-validate"),
            data=json.dumps({"part_number": "ab-100"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["valid"])

    def test_part_list_is_public_and_hides_inactive_parts(self):
        VisionInspectionPart.objects.create(part_number="OLD-01", part_name="Inactive", is_active=False)
        response = self.client.get(reverse("VisionInspection:api-part-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["part_number"] for row in response.json()["items"]], ["AB-100"])

    def test_part_configuration_api_is_part_scoped(self):
        response = self.client.get(
            reverse("VisionInspection:api-part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["part"]["id"], self.part.pk)
        self.assertIsNone(response.json()["configuration"])
        self.assertEqual(response.json()["available_models"], [])

    def test_part_configuration_page_exposes_anonymous_upload_endpoint(self):
        response = self.client.get(
            reverse("VisionInspection:part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("VisionInspection:part-model-upload"))

    def test_model_picker_includes_registered_inactive_models(self):
        model = VisionYOLOModel.objects.create(
            name="Draft model", model_path=__file__, is_active=False
        )
        response = self.client.get(
            reverse("VisionInspection:api-part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["available_models"][0]["id"], model.pk)

    def test_registered_inactive_model_classes_are_available(self):
        from unittest.mock import patch

        model = VisionYOLOModel.objects.create(
            name="Draft model", model_path=__file__, is_active=False
        )
        with patch(
            "VisionInspection.views._discover_configurable_classes",
            return_value=[{"id": 0, "name": "actual-class"}],
        ):
            response = self.client.get(
                reverse("VisionInspection:api-yolo-model-classes", args=[model.pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["classes"], [{"id": 0, "name": "actual-class"}])

    def test_frontend_model_upload_discovers_classes_and_stores_inactive_model(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        with tempfile.TemporaryDirectory() as model_dir, override_settings(
            VISION_MODEL_OUTPUT_DIR=model_dir
        ):
            with patch("VisionInspection.views.YOLOModelService.load_model", return_value=True), patch(
                "VisionInspection.views.YOLOModelService.get_class_names",
                return_value=["class-a", "class-b"],
            ):
                response = self.client.post(
                    reverse("VisionInspection:part-model-upload"),
                    {"version": "v2", "model_file": SimpleUploadedFile("safe.pt", b"model-bytes")},
                )
            self.assertEqual(response.status_code, 201, response.content)
            model = VisionYOLOModel.objects.get(pk=response.json()["model"]["id"])
            self.assertFalse(model.is_active)
            self.assertEqual(model.num_classes, 2)
            self.assertTrue(model.model_path.startswith(model_dir))
            self.assertEqual(response.json()["classes"], [
                {"id": 0, "name": "class-a"}, {"id": 1, "name": "class-b"},
            ])

    def test_frontend_model_upload_rejects_unsupported_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("VisionInspection:part-model-upload"),
            {"model_file": SimpleUploadedFile("model.txt", b"not a model")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(VisionYOLOModel.objects.exists())

    def test_configuration_save_needs_explicit_confirmation(self):
        response = _post(
            self.client,
            reverse("VisionInspection:api-part-vision-configuration-save", args=[self.part.pk]),
            {},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_changes", response.json()["fields"])

    def test_configuration_save_activates_valid_configuration(self):
        from unittest.mock import patch

        from VisionInspection.models import VisionPartConfiguration

        model = VisionYOLOModel.objects.create(
            name="Registered model", model_path=__file__, is_active=False, num_classes=1
        )
        payload = {
            "confirm_changes": True,
            "inspection_mode": "FRONT_ONLY",
            "acceptance_rule": "count",
            "front_camera_source": "OPENCV",
            "front_camera_index": 0,
            "back_camera_source": "OPENCV",
            "back_camera_index": 1,
            "models": [{
                "model_id": model.pk,
                "classes": [{
                    "class_id": 0, "class_name": "stitch", "enabled": True,
                    "side": "FRONT", "expected_front": 1, "expected_back": 0,
                    "front_threshold": 0.7, "back_threshold": 0.5,
                    "priority": 1, "required": True, "acceptance_rule": "count",
                }],
            }],
        }
        with patch(
            "VisionInspection.views._discover_configurable_classes",
            return_value=[{"id": 0, "name": "stitch"}],
        ):
            response = _post(
                self.client,
                reverse("VisionInspection:api-part-vision-configuration-save", args=[self.part.pk]),
                payload,
            )
        self.assertEqual(response.status_code, 201, response.content)
        configuration = VisionPartConfiguration.objects.get(pk=response.json()["configuration_id"])
        self.assertTrue(configuration.is_active)
        self.assertEqual(configuration.status, VisionPartConfiguration.STATUS_READY)
        self.assertTrue(response.json()["is_active"])

    def test_configuration_save_requires_csrf(self):
        from django.test import Client

        strict_client = Client(enforce_csrf_checks=True)
        page = strict_client.get(
            reverse("VisionInspection:part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(page.status_code, 200)
        response = _post(
            strict_client,
            reverse("VisionInspection:api-part-vision-configuration-save", args=[self.part.pk]),
            {"confirm_changes": True},
        )
        self.assertEqual(response.status_code, 403)

    def test_inspection_state_api_needs_no_login(self):
        r = self.client.get(reverse("VisionInspection:api-state"))
        self.assertEqual(r.status_code, 200)

    def test_workflow_scan_unknown_barcode_is_rejected(self):
        r = self.client.post(
            reverse("VisionInspection:workflow-scan-part"),
            data=json.dumps({"barcode": "NOPE"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_workflow_submit_without_session_is_idempotent_safe(self):
        """Duplicate submissions are refused on the server."""
        r = self.client.post(
            reverse("VisionInspection:workflow-submit"),
            data=json.dumps({"part_number": "AB-100"}),
            content_type="application/json",
        )
        # No approved model configured → the server refuses (never fabricated OK).
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])


class AdminProtectionTests(TestCase):
    """Acceptance: administrative functions remain protected."""

    PROTECTED = [
        "VisionInspection:configuration",
        "VisionInspection:master-image",
        "VisionInspection:preset-create",
        "VisionInspection:api-employee-list",
        "VisionInspection:employee-create",
        "VisionInspection:part-create",
        "VisionInspection:api-parts-list",
        "VisionInspection:yolo-models",
        "VisionInspection:yolo-model-upload",
        "VisionInspection:training-start",
        "VisionInspection:training-dataset-paths",
        "VisionInspection:ui-settings",
        "VisionInspection:ui-employees",
        "VisionInspection:ui-reports",
        "VisionInspection:ui-logs",
        "VisionInspection:api-dashboard-summary",
        "VisionInspection:preset-detail",
        "VisionInspection:roi-create",
    ]

    def test_anonymous_access_redirects_to_admin_login(self):
        for name in self.PROTECTED:
            with self.subTest(route=name):
                url = (
                    reverse(name, args=[1])
                    if name == "VisionInspection:preset-detail"
                    else reverse(name)
                )
                r = self.client.get(url)
                self.assertEqual(r.status_code, 302, name)
                self.assertIn("/admin/login/", r.url, name)

    def test_staff_can_access_configuration(self):
        client, _user = _staff_client()
        r = client.get(reverse("VisionInspection:configuration"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Vision Inspection Configuration")

    def test_staff_can_list_parts(self):
        client, _user = _staff_client()
        r = client.get(reverse("VisionInspection:api-part-list"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.json())

    def test_model_upload_is_not_public(self):
        r = self.client.post(
            reverse("VisionInspection:yolo-model-upload"),
            data=json.dumps({"name": "m", "model_path": "/tmp/m.pt"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(VisionYOLOModel.objects.exists())
    def test_security_middleware_is_untouched(self):
        """Login protection must not be bypassed by disabling middleware."""
        from django.conf import settings

        self.assertIn(
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            settings.MIDDLEWARE,
        )
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)

    def test_station_flow_view_still_uses_csrf(self):
        """State-changing station endpoints must still require a CSRF token.

        The station is anonymous, but CSRF protection stays enabled: a POST
        made without a valid token is rejected with 403.
        """
        VisionInspectionPart.objects.create(
            part_number="AB-100", part_name="Airbag"
        )
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        r = strict.post(
            reverse("VisionInspection:workflow-scan-part"),
            data=json.dumps({"barcode": "AB-100"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)


class StationTraceabilityTests(TestCase):
    """Saved inspections carry the station id, not a fabricated operator."""

    def tearDown(self):
        # A submit starts the capture thread; never leave it running across
        # tests (it would hold the physical camera device).
        CameraService.stop_camera()
        super().tearDown()

    def test_saved_inspection_records_station_identifier(self):
        from unittest import mock

        from django.conf import settings
        from django.test import override_settings

        from VisionInspection.models import (
            VisionInspectionMasterROI,
            VisionYOLOModel,
        )
        from VisionInspection.services.inspection_workflow import InspectionWorkflow

        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            model = VisionYOLOModel.objects.create(
                name="m", model_path=__file__, is_active=True, num_classes=2
            )
            VisionInspectionMasterROI.objects.create(
                part_number="AB-100", view="front",
                x=0.1, y=0.1, width=0.4, height=0.4,
                ref_width=1280, ref_height=720,
            )
            wf = InspectionWorkflow()
            wf._yolo = _FakeYolo()
            with mock.patch.object(wf, "validate_model", return_value={
                "ok": True, "model_id": model.id, "model_version": "1",
                "classes": ["perimeter"], "num_classes": 1}):
                submitted = wf.submit("AB-100")
            self.assertTrue(submitted["ok"], submitted)
            sid = submitted["session_id"]
            session = VisionInspectionSession.objects.get(session_id=sid)
            session.front_status = "COMPLETED"
            session.back_status = "COMPLETED"
            session.front_verdict = "OK"
            session.back_verdict = "OK"
            session.overall = "OK"
            session.save()
            saved = wf.save(sid)
            self.assertTrue(saved["ok"], saved)

            log = VisionInspectionLog.objects.get(pk=saved["inspection_id"])
            self.assertEqual(
                log.employee_code,
                getattr(settings, "VISION_INSPECTION_STATION_ID", ""),
            )


class InspectionWorkspaceUiTests(TestCase):
    """Regression guards for the redesigned Airbag Stitch Inspection page.

    The JavaScript is bound to element ids; renaming one silently breaks the
    operator workflow.  These tests pin the contract that matters: the page
    renders (no login), every id the JS looks up is present, the layout is
    built from grid/flex rules rather than absolute positioning, and the
    camera containers keep the image inside their box.
    """

    URL = "/vision-inspection/inspection/"

    # Ids read by inspection_workspace.js / inspection_workflow_ui.js.
    TOOLBAR_IDS = [
        "wsApp", "wsPart", "wsCamSrc", "wsCamIdx", "wsCamIdxField", "wsCapture",
        "wsStartCam", "wsStopCam", "wsLightUp", "wsLightDn", "wsFullscreen",
        "wsClock", "wsWorkspace", "wsHint", "wsError",
    ]
    WORKFLOW_IDS = [
        "wfBarcode", "wfScanBtn", "wfValidateBtn", "wfSubmitBtn", "wfScanState",
        "wfModelState", "wfPartInfo", "wfProgress",
        "wfResult", "wfResultIcon", "wfResultState", "wfResultNote", "wfDetFront",
        "wfDetBack", "wfBackBtn", "wfSaveBtn", "wfCancelBtn",
    ]
    VIEW_IDS = ["cam", "feed", "draw", "ov", "off", "badge", "mask"]
    ROI_SUFFIXES = ["X", "Y", "W", "H", "Ref", "Save", "Edit", "Reset"]
    SIDEBAR_IDS = [
        "wsCamState", "wsModelState", "wsRoiFront", "wsRoiBack",
        "wsFps", "wsLatency", "wsE2E", "wsFrameAge",
    ]
    PANEL_IDS = [
        "wsModelCard", "wsModelCardTitle", "wsModelName", "wsModelClasses",
        "wsInfoPart", "wsInfoModel", "wsInfoRevision",
        "wsInfoType", "wfProgressRows", "wsFrontSide", "wsBackSide",
        "wsFrontSideIcon", "wsFrontSideValue", "wsBackSideIcon", "wsBackSideValue",
    ]

    def _page(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _all_ids(self):
        ids = list(self.TOOLBAR_IDS) + list(self.WORKFLOW_IDS) + list(self.SIDEBAR_IDS)
        ids += list(self.PANEL_IDS)
        for view in ("front", "back"):
            for stem in self.VIEW_IDS:
                ids.append(stem + view.capitalize())
            for suffix in self.ROI_SUFFIXES:
                ids.append("ro" + view.capitalize() + suffix)
        return ids

    def test_page_renders_without_login(self):
        self.assertEqual(self.client.get(self.URL).status_code, 200)

    def test_all_javascript_bound_ids_are_present(self):
        html = self._page()
        missing = [i for i in self._all_ids() if ('id="%s"' % i) not in html]
        self.assertEqual(missing, [], "ids the JS depends on were removed")

    def test_no_duplicate_ids(self):
        """A duplicated id makes getElementById return the wrong node."""
        html = self._page()
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(
            sorted(i for i in ids if ids.count(i) > 1), [],
            "duplicate ids break per-view camera state",
        )

    def test_layout_uses_grid_and_flex_not_absolute_positioning(self):
        html = self._page()
        # No inline absolute positioning: layout must come from the stylesheet.
        self.assertNotIn("position:absolute", html.lower())
        # Exactly one camera card per view (a second one would duplicate every id).
        self.assertEqual(html.count('<section class="ws-view" data-view="front"'), 1)
        self.assertEqual(html.count('<section class="ws-view" data-view="back"'), 1)

    def test_application_flow_is_removed_completely(self):
        """The Application Flow section is not part of the Inspection screen."""
        html = self._page()
        self.assertNotIn("Application Flow", html)
        self.assertNotIn("ws-flow", html)
        self.assertNotIn("Step by Step", html)
        self.assertNotIn('id="wfFlow"', html)
        self.assertNotIn("wfFlowStrip", html)

    def test_application_flow_css_is_removed(self):
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        for selector in (".ws-flow", ".ws-flow-card", ".ws-flow-section",
                         ".ws-flow-bar", ".ws-flow-arrow"):
            self.assertNotIn(selector, css, "%s should be removed" % selector)
        # The stylesheet must stay well-formed after the removal.
        self.assertEqual(css.count("{"), css.count("}"))

    def test_readable_typography_scales(self):
        """The station UI must not be scaled down to tiny text."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        self.assertIn("font-size: 25px", css)          # application title
        self.assertIn("font-size: 16px", css)          # section headings
        self.assertIn("font-size: 14px", css)          # card body / buttons
        self.assertIn("font-size: 13.5px", css)       # table rows / labels
        self.assertIn("height: 64px", css)             # header height
        self.assertIn("height: 38px", css)             # control height
        # No page-level shrinking transform or zoom anywhere in the stylesheet.
        self.assertNotIn("zoom:", css)
        self.assertNotIn("transform: scale", css)

    def test_camera_panels_are_prominent(self):
        """Camera frames must dominate, not shrink to a thumbnail."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        self.assertIn("--ws-cam-max-h: 52vh", css)
        # Both camera panels share one equal-width grid track.
        self.assertIn("repeat(2, minmax(0, 1fr))", css)

    def test_stylesheets_declare_layout_foundations(self):
        """The rules that fix overflow/scrolling must actually exist."""
        from django.conf import settings

        css = []
        for relative in (
            "VisionInspection/css/inspection_workspace.css",
            "VisionInspection/css/inspection_master.css",
        ):
            path = os.path.join(settings.BASE_DIR, "VisionInspection", "static", relative)
            with open(path, encoding="utf-8") as handle:
                css.append(handle.read())
        css = "\n".join(css)

        for declaration in (
            "box-sizing: border-box",
            "grid-template-columns",
            "min-width: 0",
            "object-fit: contain",
            "aspect-ratio",
            "overflow: hidden",
            "overflow-y: auto",
            "@media",
            ".ws:fullscreen",
        ):
            self.assertIn(declaration, css, "%s is missing" % declaration)

    def test_camera_frame_is_bounded(self):
        """Camera frames must clip their content and never stretch."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        cam = re.search(r"\.ws \.vi-master-cam \{(.*?)\}", css, re.S)
        self.assertIsNotNone(cam)
        self.assertIn("overflow: hidden", cam.group(1))
        self.assertIn("aspect-ratio", cam.group(1))

        feed = re.search(r"\.ws \.vi-master-cam img\.vi-master-feed \{(.*?)\}", css, re.S)
        self.assertIsNotNone(feed)
        self.assertIn("object-fit: contain", feed.group(1))

    def test_light_controls_are_disabled_by_default(self):
        """Illumination hardware is absent, so those actions must read as off."""
        html = self._page()
        for element_id in ("wsLightUp", "wsLightDn"):
            self.assertRegex(html, r'id="%s"[^>]*disabled' % element_id)

    def test_submit_is_disabled_until_validation(self):
        """Submit must not be clickable before a part is scanned and validated."""
        html = self._page()
        self.assertRegex(html, r'id="wfSubmitBtn"[^>]*disabled')
        self.assertNotIn('id="wsStartCam"[^>]*disabled', html)

    def test_application_flow_steps_are_present(self):
        html = self._page()
        for step in ("scan", "model", "front", "back", "result", "saved"):
            self.assertIn('data-step="%s"' % step, html)

    def test_legend_shows_no_unconfigured_classes(self):
        """Without a model the legend must not invent class colours."""
        html = self._page()
        self.assertIn("No inspection model configured", html)

    def test_reference_layout_regions_are_present(self):
        """The reference station's regions and headings must exist."""
        html = self._page()
        for text in (
            "Part Information", "Inspection Result", "Overall Result",
            "Inspection Progress", "Stitch Detection (Front)",
            "Stitch Detection (Back)",
            "Front Camera View (Live Inspection)",
            "Back Camera View (After Front OK)",
        ):
            self.assertIn(text, html, text)

    def test_no_template_comment_leaks_into_html(self):
        """A Django comment must never reach the browser as visible text.

        Multi-line ``{# ... #}`` comments are not stripped by this Django
        version, so they render as literal text inside the page.  Any comment
        therefore has to be single-line — this guards the whole app.
        """
        import glob
        import os

        from django.conf import settings

        roots = list(getattr(settings, "TEMPLATES", [])[0]["DIRS"]) + [
            os.path.join(settings.BASE_DIR, "VisionInspection", "templates"),
        ]
        templates = []
        for root in roots:
            templates += glob.glob(os.path.join(root, "**", "*.html"), recursive=True)
        offenders = []
        for path in templates:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for match in re.finditer(r"\{#.*?#\}", text, re.DOTALL):
                if "\n" in match.group(0):
                    offenders.append(os.path.basename(path))
                    break
        self.assertEqual(sorted(set(offenders)), [])

        # And the rendered page must not contain any comment markup at all.
        html = self._page()
        self.assertNotIn("{#", html)
        self.assertNotIn("#}", html)


class PartConfigurationValidationTests(TestCase):
    def _configuration(self, mode="FRONT_BACK", side="FRONT"):
        from VisionInspection.models import (
            VisionClassConfiguration,
            VisionPartConfiguration,
            VisionPartModel,
            VisionPartROI,
            VisionYOLOModel,
        )

        part = VisionInspectionPart.objects.create(
            part_number="CFG-01", part_name="Configured part"
        )
        configuration = VisionPartConfiguration.objects.create(
            part=part,
            version=1,
            inspection_mode=mode,
            status=VisionPartConfiguration.STATUS_READY,
            is_active=True,
        )
        model = VisionYOLOModel.objects.create(
            name="Configured model", model_path=__file__, num_classes=1
        )
        association = VisionPartModel.objects.create(
            configuration=configuration,
            model=model,
            classes=[{"id": 0, "name": "stitch"}],
            num_classes=1,
            is_approved=True,
            validation_status="VALID",
        )
        VisionClassConfiguration.objects.create(
            part_model=association,
            class_id=0,
            class_name="stitch",
            side=side,
            expected_front=1,
            expected_back=1,
            priority=1,
            acceptance_rule="count",
        )
        VisionPartROI.objects.create(
            configuration=configuration,
            version=1,
            x=10,
            y=10,
            width=100,
            height=100,
            ref_width=640,
            ref_height=480,
        )
        return configuration

    def test_valid_ready_configuration_matches_discovered_classes(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration()
        self.assertEqual(
            validate_configuration(configuration, {1: ["stitch"]}), []
        )

    def test_rejects_class_metadata_that_no_longer_matches_model(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration()
        errors = validate_configuration(configuration, {1: ["different-class"]})
        self.assertTrue(any("no longer matches" in error for error in errors))

    def test_rejects_back_rule_for_front_only_mode(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_ONLY", side="BACK")
        errors = validate_configuration(configuration, {1: ["stitch"]})
        self.assertTrue(any("requires Back" in error for error in errors))


class StationDuplicateSubmissionTests(TestCase):
    """Acceptance: a duplicate inspection submission is refused on the server."""

    def tearDown(self):
        CameraService.stop_camera()
        super().tearDown()

    def _submit(self, client):
        return client.post(
            reverse("VisionInspection:workflow-submit"),
            data=json.dumps({"part_number": "AB-100"}),
            content_type="application/json",
        )

    def test_second_submit_is_rejected(self):
        VisionInspectionPart.objects.create(part_number="AB-100", part_name="Airbag")
        model, _ = VisionYOLOModel.objects.get_or_create(
            name="mtest", model_path=__file__,
            defaults={"is_active": True, "num_classes": 2},
        )
        VisionInspectionMasterROI.objects.create(
            part_number="AB-100", view="front",
            x=0.1, y=0.1, width=0.4, height=0.4,
            ref_width=1280, ref_height=720,
        )

        wf = InspectionWorkflow()
        wf._yolo = _FakeYolo()
        with mock.patch.object(wf, "validate_model", return_value={
            "ok": True, "model_id": model.id, "model_version": "1",
            "classes": ["perimeter"], "num_classes": 1}):
            with mock.patch(
                "VisionInspection.views.InspectionWorkflow", return_value=wf
            ):
                first = self._submit(self.client)
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.json()["ok"])

                # A second submit for the same part is refused — no duplicate
                # inspection record is created.
                second = self._submit(self.client)
                self.assertEqual(second.status_code, 400)
                self.assertFalse(second.json()["ok"])
                self.assertEqual(second.json()["state"], "duplicate_session")

        self.assertEqual(VisionInspectionSession.objects.count(), 1)

    def test_next_part_can_be_submitted_without_login(self):
        """After a session is cancelled, the station is ready for the next part."""
        VisionInspectionPart.objects.create(part_number="AB-100", part_name="Airbag")
        model, _ = VisionYOLOModel.objects.get_or_create(
            name="mtest", model_path=__file__,
            defaults={"is_active": True, "num_classes": 2},
        )
        VisionInspectionMasterROI.objects.create(
            part_number="AB-100", view="front",
            x=0.1, y=0.1, width=0.4, height=0.4,
            ref_width=1280, ref_height=720,
        )

        wf = InspectionWorkflow()
        wf._yolo = _FakeYolo()
        with mock.patch.object(wf, "validate_model", return_value={
            "ok": True, "model_id": model.id, "model_version": "1",
            "classes": ["perimeter"], "num_classes": 1}):
            with mock.patch(
                "VisionInspection.views.InspectionWorkflow", return_value=wf
            ):
                first = self._submit(self.client)
                sid = first.json()["session_id"]
                cancelled = self.client.post(
                    reverse("VisionInspection:workflow-cancel"),
                    data=json.dumps({"session_id": sid}),
                    content_type="application/json",
                )
                self.assertTrue(cancelled.json()["ok"])
                again = self._submit(self.client)
                self.assertTrue(again.json()["ok"])
        self.assertEqual(VisionInspectionSession.objects.filter(is_active=True).count(), 1)
        self.assertTrue(model)



