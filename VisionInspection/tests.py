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


def _role_client(role, username=None):
    from django.contrib.auth.models import Group, User
    from django.test import Client

    username = username or "TEST-" + role.upper().replace(" ", "-")
    user, _created = User.objects.get_or_create(username=username)
    user.set_password("test-password")
    user.save(update_fields=["password"])
    user.groups.add(Group.objects.get(name=role))
    client = Client()
    client.force_login(user)
    return client, user


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
    """Employee master is administration â€” a staff (Django) account is required.

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
    """Part master is administration â€” a staff (Django) account is required."""

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
    def setUp(self):
        self.client, self.user = _role_client("Administrator", "TRACE-ADMIN")

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

    def test_reuse_existing_camera_does_not_restart_capture(self):
        service = CameraService()
        service._status = "ON"
        service._source = "opencv"
        service._camera_index = 2

        with mock.patch.object(service, "_retire_capture_thread") as retire:
            started = CameraService.start_camera(
                camera_index=2,
                source="opencv",
                reuse_existing=True,
            )

        self.assertFalse(started)
        retire.assert_not_called()
        self.assertEqual(CameraService.get_status(), "ON")

    def test_connect_opencv_keeps_initial_probe_frame_for_first_inspection(self):
        service = CameraService()
        probe = np.zeros((12, 16, 3), dtype=np.uint8)

        with mock.patch("VisionInspection.services.camera_service._claim_device", return_value=True), \
                mock.patch("VisionInspection.services.camera_service.cv2.VideoCapture") as capture_cls, \
                mock.patch("VisionInspection.services.camera_service._release_device"):
            cap = capture_cls.return_value
            cap.isOpened.return_value = True
            cap.read.return_value = (True, probe)

            service._source = "opencv"
            service._camera_index = 0
            service._target_fps = 30
            service._frame_time = 1 / 30
            service._status = "OFF"
            service._state = "STOPPED"
            service._error_message = None

            ok = service._connect_opencv()

        self.assertTrue(ok)
        self.assertIsNotNone(service._latest_frame)
        self.assertTrue(np.array_equal(service._latest_frame, probe))
        got_ok, frame, meta = service.get_frame_with_meta()
        self.assertTrue(got_ok)
        self.assertTrue(np.array_equal(frame, probe))
        self.assertEqual(meta["seq"], 1)


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
        status = eng.start(part_number="PART-A")   # camera is OFF â†’ error path
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
    StorageConfiguration,
    StorageDestination,
    StorageImageMetadata,
    StorageMigrationJob,
    StorageTransferHistory,
    VisionInspectionSession,
    VisionInspectionLog,
    VisionInspectionCameraConfig,
    VisionInspectionMasterROI,
    VisionYOLOModel,
)
from VisionInspection.services.inspection_workflow import InspectionWorkflow  # noqa: E402
from VisionInspection.services import storage_service  # noqa: E402
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
        from django.contrib.auth.models import Group, User

        self.part = VisionInspectionPart.objects.create(part_number="AB-100", part_name="Airbag")
        self.operator = User.objects.create_user(username="WF-OPERATOR", password="test-password")
        self.operator.groups.add(Group.objects.get(name="Operator"))
        self.wf = InspectionWorkflow()
        self.wf._yolo = _FakeYolo()

    def _roi(self, view):
        return VisionInspectionMasterROI.objects.create(
            part_number="AB-100", view=view,
            x=0.1, y=0.1, width=0.4, height=0.4, ref_width=1280, ref_height=720,
        )

    def _session(self, **kw):
        base = dict(part_number="AB-100", stage="result", operator=self.operator)
        base.update(kw)
        return VisionInspectionSession.objects.create(**base)

    def _evaluate_configured_rules(self, side, rules, detections):
        from types import SimpleNamespace

        classes = [
            {"id": rule["class_id"], "name": rule["class_name"]}
            for rule in rules
        ]
        model_config = {
            "model_id": 41,
            "name": "Configured segmentation",
            "path": "configured.onnx",
            "task_type": "segment",
            "classes": classes,
            "rules": rules,
        }
        session = SimpleNamespace(
            session_id="configured-rule-test",
            config_snapshot={"models": [model_config]},
        )
        with mock.patch.object(
            self.wf._model_registry,
            "inspect",
            return_value={"ok": True, "task": "segment", "classes": classes},
        ), mock.patch.object(
            self.wf._model_registry,
            "predict",
            return_value=(detections, 1.0),
        ) as predict:
            evaluated, results, error = self.wf._configured_side_results(
                session, side, _fake_frame()
            )
        return evaluated, results, error, predict.call_args.kwargs

    def test_configured_verdict_requires_every_side_rule_at_confidence_floor(self):
        def rule(class_id, name, side="FRONT", required=True, expected=1, threshold=0.3):
            return {
                "class_id": class_id,
                "class_name": name,
                "side": side,
                "expected_front": expected if side in ("FRONT", "BOTH") else 0,
                "expected_back": expected if side in ("BACK", "BOTH") else 0,
                "front_threshold": threshold,
                "back_threshold": threshold,
                "required": required,
                "acceptance_rule": "count_confidence",
                "priority": class_id + 1,
            }

        def detection(class_id, name, confidence):
            return {
                "class_id": class_id,
                "class": name,
                "confidence": confidence,
                "x1": 1,
                "y1": 2,
                "x2": 10,
                "y2": 12,
                "polygon": [[1, 2], [10, 2], [10, 12]],
            }

        required_rules = [
            rule(0, "front-a"),
            rule(1, "front-b"),
            rule(2, "front-optional", required=False, expected=0),
        ]
        detections = [detection(0, "front-a", 0.30), detection(1, "front-b", 0.91)]
        eligible, results, error, predict_kwargs = self._evaluate_configured_rules(
            "front", required_rules, detections
        )
        self.assertIsNone(error)
        self.assertEqual(predict_kwargs["conf"], 0.30)
        self.assertEqual([row["status"] for row in results], ["OK", "OK", "OPTIONAL_NOT_OK"])
        self.assertEqual(self.wf._side_verdict(results), "OK")
        self.assertEqual(len(eligible), 2)

        missing, results, error, _ = self._evaluate_configured_rules(
            "front", required_rules[:2], [detection(0, "front-a", 0.8)]
        )
        self.assertIsNone(error)
        self.assertEqual(len(missing), 1)
        self.assertEqual(self.wf._side_verdict(results), "NOT_OK")
        self.assertEqual(results[1]["status"], "NOT_OK")

        below_floor, results, error, predict_kwargs = self._evaluate_configured_rules(
            "front", [required_rules[0]], [detection(0, "front-a", 0.299)]
        )
        self.assertIsNone(error)
        self.assertEqual(predict_kwargs["conf"], 0.30)
        self.assertEqual(below_floor, [])
        self.assertEqual(results[0]["status"], "NOT_OK")
        self.assertEqual(self.wf._side_verdict(results), "NOT_OK")

        near_floor, results, error, _ = self._evaluate_configured_rules(
            "front", [required_rules[0]], [detection(0, "front-a", 0.29996)]
        )
        self.assertIsNone(error)
        self.assertEqual(near_floor, [])
        self.assertEqual(results[0]["status"], "NOT_OK")

        malformed_detection = detection(0, "front-a", 0.9)
        malformed_detection.pop("polygon")
        _detections, results, error, _ = self._evaluate_configured_rules(
            "front", [required_rules[0]], [malformed_detection]
        )
        self.assertIsNotNone(error)
        self.assertIsNone(results)

        higher_rule_threshold, results, error, _ = self._evaluate_configured_rules(
            "front",
            [rule(0, "front-a", threshold=0.65)],
            [detection(0, "front-a", 0.64)],
        )
        self.assertIsNone(error)
        self.assertEqual(len(higher_rule_threshold), 1)
        self.assertEqual(results[0]["status"], "NOT_OK")
        self.assertEqual(results[0]["threshold"], 0.65)

        count_rule = rule(0, "front-a", expected=2)
        _, results, error, _ = self._evaluate_configured_rules(
            "front", [count_rule], [detection(0, "front-a", 0.9)]
        )
        self.assertIsNone(error)
        self.assertEqual(results[0]["actual_count"], 1)
        self.assertEqual(self.wf._side_verdict(results), "NOT_OK")

        side_rules = [rule(0, "front-a", side="FRONT"), rule(1, "back-a", side="BACK")]
        front, front_results, front_error, front_kwargs = self._evaluate_configured_rules(
            "front",
            side_rules,
            [detection(0, "front-a", 0.8)],
        )
        back, back_results, back_error, back_kwargs = self._evaluate_configured_rules(
            "back",
            side_rules,
            [detection(1, "back-a", 0.8)],
        )
        self.assertIsNone(front_error)
        self.assertIsNone(back_error)
        self.assertEqual(front_kwargs["classes"], {0})
        self.assertEqual(back_kwargs["classes"], {1})
        self.assertEqual([row["side"] for row in front_results], ["FRONT"])
        self.assertEqual([row["side"] for row in back_results], ["BACK"])
        self.assertEqual([item["class_id"] for item in front], [0])
        self.assertEqual([item["class_id"] for item in back], [1])

    def test_configured_inference_error_marks_session_error(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.model_registry import ModelLoadError

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
            status=VisionPartConfiguration.STATUS_READY,
            is_active=True,
        )
        model = self._model_obj()
        config_snapshot = {
            "inspection_mode": VisionPartConfiguration.MODE_FRONT_ONLY,
            "camera": {"front": {"source": "OPENCV", "index": 0}},
            "models": [{
                "model_id": model.pk,
                "name": "Configured segmentation",
                "path": "configured.onnx",
                "task_type": "segment",
                "classes": [{"id": 0, "name": "front-a"}],
                "rules": [{
                    "class_id": 0,
                    "class_name": "front-a",
                    "side": "FRONT",
                    "expected_front": 1,
                    "front_threshold": 0.3,
                    "required": True,
                    "acceptance_rule": "count_confidence",
                    "priority": 1,
                }],
            }],
        }
        for failure in ("model_load", "inference"):
            with self.subTest(failure=failure):
                session = self._session(
                    part_number=f"AB-100-{failure}",
                    configuration=configuration,
                    stage="front",
                    front_status="PENDING",
                    back_status="SKIPPED",
                    front_roi_norm={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
                    config_snapshot=config_snapshot,
                )
                inspect_patch = mock.patch.object(
                    self.wf._model_registry,
                    "inspect",
                    side_effect=ModelLoadError("Model loading failed.")
                    if failure == "model_load"
                    else None,
                    return_value={
                        "ok": True,
                        "task": "segment",
                        "classes": [{"id": 0, "name": "front-a"}],
                    },
                )
                predict_patch = mock.patch.object(
                    self.wf._model_registry,
                    "predict",
                    return_value=([], "Inference failed"),
                )
                with mock.patch.object(self.wf._camera, "get_status", return_value="ON"), \
                        mock.patch(
                            "VisionInspection.services.inspection_workflow.CameraService.get_source",
                            return_value="opencv",
                        ), mock.patch(
                            "VisionInspection.services.inspection_workflow.CameraService.stop_camera",
                        ), inspect_patch, predict_patch:
                    result = self.wf.run_side(
                        str(session.session_id), "front", frame=_fake_frame()
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(result["state"], "front_error")
                session.refresh_from_db()
                self.assertEqual(session.front_status, "ERROR")
                self.assertEqual(session.front_verdict, "ERROR")
                self.assertTrue(session.is_active)

    # ---- Step 1: barcode ---- #
    def test_scan_invalid_barcode(self):
        r = self.wf.scan_part("NOPE")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "part_not_found")
        r2 = self.wf.scan_part("")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["state"], "waiting")
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_scan_valid_barcode_case_insensitive(self):
        r = self.wf.scan_part("ab-100")
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], "part_identified")
        self.assertEqual(r["part"]["part_number"], "AB-100")
        self.assertIsNone(r["active_session"])
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_scan_of_different_part_exposes_only_own_active_session_for_reset(self):
        VisionInspectionPart.objects.create(part_number="AB-200", part_name="Second Airbag")
        session = self._session(stage="front", front_status="IN_PROGRESS")

        result = self.wf.scan_part("AB-200", operator=self.operator)

        self.assertTrue(result["ok"])
        self.assertEqual(result["part"]["part_number"], "AB-200")
        self.assertEqual(result["active_session"]["session_id"], str(session.session_id))
        self.assertEqual(result["active_session"]["part_number"], "AB-100")
        self.assertEqual(VisionInspectionSession.objects.count(), 1)

    def test_scan_does_not_disclose_another_operators_active_session(self):
        from django.contrib.auth.models import User

        VisionInspectionPart.objects.create(part_number="AB-200", part_name="Second Airbag")
        session = self._session(stage="front", front_status="IN_PROGRESS")
        other_operator = User.objects.create_user(username="WF-OTHER", password="test-password")

        result = self.wf.scan_part("AB-200", operator=other_operator)

        self.assertTrue(result["ok"])
        self.assertIsNone(result["active_session"])
        self.assertNotIn(str(session.session_id), str(result))

    def test_duplicate_submit_does_not_disclose_another_operators_session_id(self):
        from django.contrib.auth.models import User

        other_operator = User.objects.create_user(username="WF-OTHER", password="test-password")
        session = self._session(
            stage="front",
            front_status="IN_PROGRESS",
            operator=other_operator,
        )
        with mock.patch(
            "VisionInspection.services.inspection_workflow.storage_service.ensure_evidence_capacity"
        ):
            result = self.wf.submit("AB-100", operator=self.operator)

        self.assertEqual(result["state"], "duplicate_session")
        self.assertNotIn("session_id", result)
        self.assertNotIn(str(session.session_id), str(result))

    def test_different_part_can_be_validated_after_prior_session_is_cancelled(self):
        VisionInspectionPart.objects.create(part_number="AB-200", part_name="Second Airbag")
        previous = self._session(stage="front", front_status="IN_PROGRESS")

        first_scan = self.wf.scan_part("AB-200", operator=self.operator)
        self.assertEqual(first_scan["active_session"]["session_id"], str(previous.session_id))
        self.assertEqual(self.wf.cancel(str(previous.session_id))["state"], "cancelled")
        second_scan = self.wf.scan_part("AB-200", operator=self.operator)

        self.assertTrue(second_scan["ok"])
        self.assertEqual(second_scan["part"]["part_number"], "AB-200")
        self.assertIsNone(second_scan["active_session"])
        previous.refresh_from_db()
        self.assertEqual(previous.stage, "cancelled")
        self.assertEqual(VisionInspectionSession.objects.count(), 1)

    def test_repeated_barcode_validation_does_not_create_sessions(self):
        first = self.wf.scan_part("AB-100")
        second = self.wf.scan_part("AB-100")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_failed_camera_start_does_not_create_an_inspection_session(self):
        model = self._model_obj()
        self._roi("front")
        validation = {
            "ok": True,
            "model_id": model.pk,
            "model_version": "1",
            "classes": ["perimeter"],
            "num_classes": 1,
        }
        with mock.patch.object(self.wf, "validate_model", return_value=validation) as validate_model, \
                mock.patch.object(self.wf._camera, "get_status", return_value="OFF"), \
                mock.patch("VisionInspection.services.inspection_workflow.CameraService.get_source", return_value="opencv"), \
                mock.patch("VisionInspection.services.inspection_workflow.CameraService.stop_camera"), \
                mock.patch("VisionInspection.services.inspection_workflow.CameraService.start_camera", return_value=False):
            result = self.wf.submit("AB-100")

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "camera_error")
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_cancel_is_idempotent_and_preserves_session_evidence(self):
        session = self._session(
            stage="front",
            front_status="IN_PROGRESS",
            front_image="vision_inspection/sessions/front/retained.jpg",
        )

        first = self.wf.cancel(str(session.session_id))
        second = self.wf.cancel(str(session.session_id))

        self.assertTrue(first["ok"])
        self.assertEqual(first["state"], "cancelled")
        self.assertTrue(second["ok"])
        self.assertEqual(second["state"], "already_cancelled")
        session.refresh_from_db()
        self.assertFalse(session.is_active)
        self.assertEqual(session.stage, "cancelled")
        self.assertEqual(session.front_image.name, "vision_inspection/sessions/front/retained.jpg")

    def test_cancel_does_not_discard_completed_inspection(self):
        session = self._session(
            stage="result",
            front_status="COMPLETED",
            back_status="COMPLETED",
            front_image="vision_inspection/sessions/front/completed.jpg",
        )

        result = self.wf.cancel(str(session.session_id))

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "completed")
        session.refresh_from_db()
        self.assertTrue(session.is_active)
        self.assertEqual(session.stage, "result")
        self.assertEqual(session.front_image.name, "vision_inspection/sessions/front/completed.jpg")

    def test_cancelled_inference_worker_cannot_write_late_results(self):
        session = self._session(
            stage="front",
            front_status="PENDING",
            model=self._model_obj(),
            front_roi=self._roi("front"),
        )

        def cancel_during_inference(*args, **kwargs):
            self.assertTrue(self.wf.cancel(str(session.session_id))["ok"])
            return [{"class": "perimeter", "confidence": 0.91, "x1": 10, "y1": 10, "x2": 60, "y2": 60}]

        with mock.patch.object(self.wf._yolo, "detect", side_effect=cancel_during_inference):
            result = self.wf.run_side(str(session.session_id), "front", frame=_fake_frame())

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "session_inactive")
        session.refresh_from_db()
        self.assertEqual(session.stage, "cancelled")
        self.assertEqual(session.front_results, [])
        self.assertFalse(session.front_image)

    # ---- Step 2: model validation (honest failures) ---- #
    def test_validate_model_missing(self):
        r = self.wf.validate_model()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "model_missing")

    def test_validate_model_invalid_path(self):
        VisionYOLOModel.objects.create(
            name="m", model_path="/nonexistent/best.pt", is_active=True, num_classes=2
        )
        self.wf._yolo = YOLOModelService()  # real loader â†’ path check fails
        r = self.wf.validate_model()
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "model_invalid")
        self.wf._yolo = _FakeYolo()

    def test_validate_model_retries_inactive_configuration_failure(self):
        from unittest.mock import patch

        from VisionInspection.models import VisionPartConfiguration

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            is_active=False,
            status=VisionPartConfiguration.STATUS_VALIDATION_FAILED,
            validation_errors=["The model file does not exist."],
        )
        retry_result = {
            "ok": False,
            "state": "model_runtime_unavailable",
            "message": "Ultralytics is unavailable.",
        }
        with patch.object(
            self.wf,
            "_validate_part_configuration",
            return_value=retry_result,
        ) as validate:
            result = self.wf.validate_model(part_number=self.part.part_number)

        validate.assert_called_once_with(configuration)
        self.assertEqual(result, retry_result)

    def test_validate_model_reactivates_failed_configuration_only_after_success(self):
        from unittest.mock import patch

        from VisionInspection.models import VisionPartConfiguration

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            is_active=False,
            status=VisionPartConfiguration.STATUS_VALIDATION_FAILED,
            validation_errors=["The model file does not exist."],
        )
        with patch.object(
            self.wf,
            "_validate_part_configuration",
            return_value={"ok": True, "configuration": configuration},
        ):
            result = self.wf.validate_model(part_number=self.part.part_number)

        configuration.refresh_from_db()
        self.assertTrue(result["ok"])
        self.assertTrue(configuration.is_active)

    def test_submit_blocked_by_model(self):
        r = self.wf.submit("AB-100")
        self.assertFalse(r["ok"])  # no model â†’ blocked, no session created
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_back_camera_activation_requires_completed_front(self):
        session = self._session(front_status="PENDING", back_status="PENDING", stage="front")
        result = self.wf.activate_side(str(session.session_id), "back")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "incomplete")
        direct_result = self.wf.run_side(str(session.session_id), "back", frame=_fake_frame())
        self.assertFalse(direct_result["ok"])
        self.assertEqual(direct_result["state"], "incomplete")

    def test_camera_activation_accepts_matching_connecting_camera(self):
        session = self._session(front_status="IN_PROGRESS", stage="front")
        with mock.patch.object(self.wf._camera, "get_status", return_value="CONNECTING"):
            result = self.wf.activate_side(str(session.session_id), "front")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "camera_connecting")

    def test_submit_blocked_by_missing_roi(self):
        model = VisionYOLOModel.objects.create(
            name="m", model_path=__file__, is_active=True, num_classes=2
        )  # existing file â†’ load will still fail (not a .pt), but validate_model path exists
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
        self.assertTrue(s.is_active)
        self.assertEqual(self.wf.get_progress(str(s.session_id))["inspection_status"], "ERROR")
        s.front_roi = self._roi("front")
        s.save(update_fields=["front_roi"])
        retry = self.wf.run_side(str(s.session_id), "front", frame=_fake_frame())
        self.assertTrue(retry["ok"], retry)
        s.refresh_from_db()
        self.assertEqual(s.front_status, "COMPLETED")

    def test_run_side_success_stores_evidence_and_indeterminate(self):
        model = self._model_obj()
        roi = self._roi("front")
        s = self._session(model=model, front_roi=roi, front_status="PENDING", stage="front")
        detect = self.wf._yolo.detect
        statuses_during_inference = []

        def detect_after_status_update(frame, conf=0.5, iou=0.45):
            statuses_during_inference.append(
                VisionInspectionSession.objects.get(pk=s.pk).front_status
            )
            return detect(frame, conf=conf, iou=iou)

        with mock.patch.object(
            self.wf._yolo, "detect", side_effect=detect_after_status_update
        ):
            r = self.wf.run_side(str(s.session_id), "front", frame=_fake_frame())

        self.assertEqual(statuses_during_inference, ["IN_PROGRESS"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"], "front_completed")
        s.refresh_from_db()
        self.assertEqual(s.front_status, "COMPLETED")
        self.assertEqual(s.front_verdict, "INDETERMINATE")  # no acceptance rule â†’ not OK
        self.assertEqual(s.stage, "back")
        self.assertTrue(bool(s.front_image))
        self.assertTrue(bool(s.front_annotated))
        self.assertTrue(bool(s.front_roi_image))
        self.assertEqual(len(s.front_detections), 1)
        self.assertEqual(s.front_detections[0]["class"], "perimeter")
        self.assertEqual(s.front_results[0]["stitch"], "perimeter")
        self.assertEqual(s.front_results[0]["status"], "NO_RULE")

    def test_camera_first_available_frame_runs_normal_inspection_inference(self):
        model = self._model_obj()
        session = self._session(
            model=model,
            front_roi=self._roi("front"),
            front_status="PENDING",
            stage="front",
        )
        frame = _fake_frame()
        with mock.patch.object(self.wf._camera, "get_status", return_value="ON"), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.get_frame_with_meta",
                    return_value=(True, frame, {"seq": 1, "age_ms": 1.0, "dropped": 0}),
                ), mock.patch.object(self.wf._yolo, "detect", wraps=self.wf._yolo.detect) as detect:
            result = self.wf.run_side(str(session.session_id), "front")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "front_completed")
        detect.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.front_status, "COMPLETED")
        self.assertEqual(session.front_detections[0]["class"], "perimeter")
        self.assertTrue(session.front_annotated)

    def test_front_and_back_evidence_are_saved_to_independent_fields(self):
        model = self._model_obj()
        roi = self._roi("front")
        session = self._session(
            model=model, front_roi=roi, back_roi=roi, stage="front",
            front_status="PENDING", back_status="PENDING",
        )
        front = self.wf.run_side(str(session.session_id), "front", frame=_fake_frame())
        self.assertTrue(front["ok"], front)
        back = self.wf.run_side(str(session.session_id), "back", frame=_fake_frame())
        self.assertTrue(back["ok"], back)
        session.refresh_from_db()
        self.assertTrue(session.front_image)
        self.assertTrue(session.back_image)
        self.assertNotEqual(session.front_image.name, session.back_image.name)
        self.assertTrue(session.front_annotated)
        self.assertTrue(session.back_annotated)
        self.assertNotEqual(session.front_annotated.name, session.back_annotated.name)
        self.assertTrue(session.front_roi_image)
        self.assertTrue(session.back_roi_image)
        self.assertNotEqual(session.front_roi_image.name, session.back_roi_image.name)
        self.assertEqual(session.front_detections[0]["class"], "perimeter")
        self.assertEqual(session.back_detections[0]["class"], "perimeter")

    def test_completed_side_retry_returns_stored_state_without_rerunning_inference(self):
        session = self._session(
            stage="back",
            front_status="COMPLETED",
            front_verdict="INDETERMINATE",
            front_results=[{"stitch": "front stitch", "actual_count": 0}],
            back_status="IN_PROGRESS",
        )
        with mock.patch.object(self.wf._model_registry, "predict") as predict:
            result = self.wf.run_side(str(session.session_id), "front", frame=_fake_frame())

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_completed"])
        self.assertEqual(result["results"], session.front_results)
        predict.assert_not_called()

        self.client.force_login(self.operator)
        with mock.patch("VisionInspection.views._workflow", return_value=self.wf):
            response = self.client.post(
                reverse(
                    "VisionInspection:workflow-inspect-side",
                    kwargs={"side": "front"},
                ),
                data=json.dumps({"session_id": str(session.session_id)}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["already_completed"])

    def test_side_already_in_progress_does_not_start_duplicate_inference(self):
        session = self._session(stage="front", front_status="IN_PROGRESS")

        with mock.patch.object(self.wf._yolo, "detect") as detect:
            result = self.wf.run_side(str(session.session_id), "front", frame=_fake_frame())

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "side_in_progress")
        detect.assert_not_called()

    def test_configured_segmentation_uses_masks_and_returns_frame_overlay(self):
        from VisionInspection.models import (
            VisionClassConfiguration,
            VisionPartConfiguration,
            VisionPartModel,
        )

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
            status=VisionPartConfiguration.STATUS_READY,
            is_active=True,
        )
        model = self._model_obj()
        association = VisionPartModel.objects.create(
            configuration=configuration,
            model=model,
            task_type=VisionPartModel.TASK_SEGMENT,
            classes=[{"id": 0, "name": "perimeter"}],
            num_classes=1,
            is_approved=True,
        )
        VisionClassConfiguration.objects.create(
            part_model=association,
            class_id=0,
            class_name="perimeter",
            side=VisionPartConfiguration.SIDE_FRONT,
            expected_front=1,
            expected_back=0,
            front_threshold=0.5,
            is_required=True,
            acceptance_rule="count",
        )
        detection = {
            "class": "perimeter",
            "class_id": 0,
            "confidence": 0.91,
            "x1": 10,
            "y1": 20,
            "x2": 40,
            "y2": 50,
            "polygon": [[10, 20], [40, 20], [40, 50], [10, 50]],
        }
        session = self._session(
            configuration=configuration,
            stage="front",
            front_status="PENDING",
            back_status="SKIPPED",
            front_roi_norm={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
            config_snapshot={
                "inspection_mode": VisionPartConfiguration.MODE_FRONT_ONLY,
                "camera": {"front": {"source": "OPENCV", "index": 0}},
                "models": [{
                    "model_id": model.id,
                    "name": "Configured segmentation",
                    "path": "segmentation.onnx",
                    "task_type": "segment",
                    "classes": [{"id": 0, "name": "perimeter"}],
                    "rules": [{
                        "class_id": 0,
                        "class_name": "perimeter",
                        "side": "FRONT",
                        "front_threshold": 0.5,
                        "back_threshold": 0.5,
                        "expected_front": 1,
                        "required": True,
                        "acceptance_rule": "count",
                        "priority": 1,
                    }],
                }],
            },
        )
        with mock.patch.object(self.wf._camera, "get_status", return_value="ON"), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.get_source",
                    return_value="opencv",
                ), \
                mock.patch.object(
                    self.wf._model_registry,
                    "inspect",
                    return_value={
                        "ok": True,
                        "task": "segment",
                        "classes": [{"id": 0, "name": "perimeter"}],
                    },
                ) as inspect, \
                mock.patch.object(
                    self.wf._model_registry,
                    "predict",
                    return_value=([detection], 4.2),
                ) as predict, \
                mock.patch(
                    "VisionInspection.services.inspection_workflow._encode_jpeg",
                    return_value=b"jpeg",
                ):
            result = self.wf.run_side(
                str(session.session_id), "front", frame=_fake_frame()
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mask_count"], 1)
        self.assertTrue(result["annotated_frame"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(result["detections"][0]["x1"], 138)
        self.assertEqual(result["detections"][0]["y1"], 92)
        self.assertEqual(result["detections"][0]["polygon"][0], [138.0, 92.0])
        self.assertEqual(result["progress"]["front_verdict"], "OK")
        self.assertEqual(result["progress"]["inspection_status"], "OKAY PART")
        self.assertEqual(inspect.call_args.args[0], "segmentation.onnx")
        self.assertEqual(inspect.call_args.kwargs["task"], "segment")
        self.assertEqual(predict.call_args.args[0], "segmentation.onnx")
        self.assertEqual(predict.call_args.kwargs["task"], "segment")
        predicted_crop = predict.call_args.args[1]
        self.assertEqual(predicted_crop.shape[:2], (360, 640))
        session.refresh_from_db()
        self.assertEqual(session.front_results[0]["stitch"], "perimeter")
        self.assertEqual(session.front_results[0]["status"], "OK")

    def test_configured_back_inference_uses_selected_onnx_segmentation_model(self):
        from types import SimpleNamespace

        model_config = {
            "model_id": 57,
            "name": "Back ONNX model",
            "path": "back-segmentation.onnx",
            "task_type": "segment",
            "classes": [{"id": 3, "name": "back-stitch"}],
            "rules": [{
                "class_id": 3,
                "class_name": "back-stitch",
                "side": "BACK",
                "front_threshold": 0.5,
                "back_threshold": 0.7,
                "expected_back": 1,
                "required": True,
                "acceptance_rule": "presence",
                "priority": 1,
            }],
        }
        session = SimpleNamespace(
            session_id="onnx-back-test",
            config_snapshot={"models": [model_config]},
        )
        detection = {
            "class": "back-stitch",
            "class_id": 3,
            "confidence": 0.82,
            "x1": 1,
            "y1": 2,
            "x2": 10,
            "y2": 12,
            "polygon": [[1, 2], [10, 2], [10, 12]],
        }
        with mock.patch.object(
            self.wf._model_registry,
            "inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": [{"id": 3, "name": "back-stitch"}],
            },
        ) as inspect, mock.patch.object(
            self.wf._model_registry,
            "predict",
            return_value=([detection], 3.5),
        ) as predict:
            detections, results, error = self.wf._configured_side_results(
                session,
                "back",
                _fake_frame(),
            )

        self.assertIsNone(error)
        self.assertEqual(detections[0]["model_id"], 57)
        self.assertEqual(results[0]["status"], "OK")
        self.assertEqual(inspect.call_args.args[0], "back-segmentation.onnx")
        self.assertEqual(inspect.call_args.kwargs["task"], "segment")
        self.assertEqual(predict.call_args.args[0], "back-segmentation.onnx")
        self.assertEqual(predict.call_args.kwargs["task"], "segment")
        self.assertEqual(predict.call_args.kwargs["classes"], {3})
        self.assertEqual(predict.call_args.kwargs["conf"], 0.30)

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
                          front_verdict="NOT_OK", back_verdict="PENDING")
        self.assertEqual(self.wf.compute_result(str(s.session_id))["overall"], "INCOMPLETE")
        s.back_status = "COMPLETED"
        s.back_verdict = "OK"
        s.save(update_fields=["back_status", "back_verdict"])
        self.assertEqual(self.wf.compute_result(str(s.session_id))["overall"], "NOT_OK")

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
        r2 = self.wf.save(str(s.session_id))  # repeated save â†’ no duplicate
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
        self.assertEqual(p["inspection_status"], "REQUEST PENDING")

    def test_progress_status_waits_for_all_required_sides(self):
        session = self._session(
            stage="front",
            front_status="IN_PROGRESS",
            back_status="PENDING",
        )
        self.assertEqual(
            self.wf.get_progress(str(session.session_id))["inspection_status"],
            "INSPECTING",
        )

        session.front_status = "COMPLETED"
        session.front_verdict = "OK"
        session.save(update_fields=["front_status", "front_verdict"])
        self.assertEqual(
            self.wf.get_progress(str(session.session_id))["inspection_status"],
            "REQUEST PENDING",
        )

        session.back_status = "COMPLETED"
        session.back_verdict = "NOT_OK"
        session.save(update_fields=["back_status", "back_verdict"])
        self.assertEqual(
            self.wf.get_progress(str(session.session_id))["inspection_status"],
            "NOT OKAY PART",
        )

        session.back_verdict = "OK"
        session.save(update_fields=["back_verdict"])
        self.assertEqual(
            self.wf.get_progress(str(session.session_id))["inspection_status"],
            "OKAY PART",
        )

    def test_progress_endpoint_returns_persisted_overall_verdict(self):
        session = self._session(
            stage="result",
            front_status="COMPLETED",
            back_status="COMPLETED",
            front_verdict="OK",
            back_verdict="NOT_OK",
            front_results=[{"class_id": 0, "confidence": 0.3, "status": "OK"}],
            back_results=[{"class_id": 1, "confidence": None, "status": "NOT_OK"}],
        )

        self.client.force_login(self.operator)
        response = self.client.get(
            reverse(
                "VisionInspection:workflow-progress",
                kwargs={"session_id": session.session_id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], str(session.session_id))
        self.assertEqual(response.json()["inspection_status"], "NOT OKAY PART")
        self.assertEqual(response.json()["back_results"], session.back_results)

    def _model_obj(self):
        obj, _ = VisionYOLOModel.objects.get_or_create(
            name="mtest", model_path=__file__, defaults={"is_active": True, "num_classes": 2}
        )
        return obj


class CameraConfigWorkflowTests(TestCase):
    """One-time camera ROI config: saved once, reused across parts (no redraw)."""

    def setUp(self):
        self.client, self.user = _role_client("Administrator", "CAMERA-ADMIN")
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

    def test_side_roi_lookup_rejects_invalid_saved_camera_roi(self):
        from VisionInspection.models import VisionPartConfiguration

        self.wf.save_camera_config(
            "back", 1200, 600, 200, 120, 1280, 720, index=0
        )

        roi, source = self.wf._resolve_side_roi(
            "AB-100",
            "back",
            {"source": VisionPartConfiguration.SOURCE_OPENCV, "index": 0},
        )

        self.assertIsNone(roi)
        self.assertIsNone(source)

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

    def test_saved_cycles_create_fresh_sessions_for_the_same_part(self):
        model = self._model_obj()
        for view in ("front", "back"):
            VisionInspectionMasterROI.objects.create(
                part_number="AB-100",
                view=view,
                x=0.1,
                y=0.1,
                width=0.4,
                height=0.4,
                ref_width=1280,
                ref_height=720,
            )
        validation = {
            "ok": True,
            "model_id": model.pk,
            "model_version": "1",
            "classes": ["perimeter"],
            "num_classes": 1,
        }
        with mock.patch.object(
            self.wf, "validate_model", return_value=validation
        ) as validate_model, \
                mock.patch.object(self.wf._camera, "get_status", return_value="ON"), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.get_source",
                    return_value="opencv",
                ), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.start_camera",
                    return_value=True,
                ), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.stop_camera"
                ):
            first = self.wf.submit("AB-100")
            self.assertTrue(first["ok"], first)
            first_session = VisionInspectionSession.objects.get(
                session_id=first["session_id"]
            )
            first_session.front_status = "COMPLETED"
            first_session.back_status = "COMPLETED"
            first_session.front_verdict = "OK"
            first_session.back_verdict = "OK"
            first_session.overall = "OK"
            first_session.stage = "result"
            first_session.save()
            saved = self.wf.save(str(first_session.session_id))
            self.assertTrue(saved["ok"], saved)

            second = self.wf.submit("AB-100")
            self.assertTrue(second["ok"], second)
            second_session = VisionInspectionSession.objects.get(
                session_id=second["session_id"]
            )
            second_session.front_status = "COMPLETED"
            second_session.back_status = "COMPLETED"
            second_session.front_verdict = "OK"
            second_session.back_verdict = "OK"
            second_session.overall = "OK"
            second_session.stage = "result"
            second_session.save()
            second_saved = self.wf.save(str(second_session.session_id))
            self.assertTrue(second_saved["ok"], second_saved)

            third = self.wf.submit("AB-100")

        self.assertEqual(validate_model.call_count, 3)
        self.assertTrue(third["ok"], third)
        self.assertEqual(
            len({first["session_id"], second["session_id"], third["session_id"]}),
            3,
        )
        self.assertEqual(
            VisionInspectionSession.objects.filter(part_number="AB-100").count(),
            3,
        )
        self.assertTrue(VisionInspectionSession.objects.get(
            session_id=first["session_id"]
        ).is_saved)
        self.assertTrue(VisionInspectionSession.objects.get(
            session_id=second["session_id"]
        ).is_saved)
        self.assertTrue(
            VisionInspectionSession.objects.get(
                session_id=third["session_id"]
            ).is_active
        )

    def test_submit_blocked_without_any_roi(self):
        model = self._model_obj()
        with mock.patch.object(self.wf, "validate_model", return_value={
            "ok": True, "model_id": model.id, "model_version": "1",
            "classes": ["x"], "num_classes": 1}):
            r = self.wf.submit("AB-100")   # no config, no per-part ROI
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "roi_missing")
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_submit_reuses_previous_common_roi_for_matching_camera_roles(self):
        from VisionInspection.models import (
            VisionPartConfiguration,
            VisionPartROI,
        )

        model = self._model_obj()
        previous = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_index=0,
            back_camera_index=0,
        )
        roi = VisionPartROI.objects.create(
            configuration=previous,
            version=1,
            x=302,
            y=52,
            width=644,
            height=424,
            ref_width=1280,
            ref_height=720,
        )
        current = VisionPartConfiguration.objects.create(
            part=self.part,
            version=2,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_index=0,
            back_camera_index=0,
            is_active=True,
        )
        validation = {
            "ok": True,
            "configuration": current,
            "inspection_mode": current.inspection_mode,
            "models": [{"model_id": model.pk}],
            "classes": ["x"],
            "model_version": "1",
        }

        with mock.patch.object(self.wf, "validate_model", return_value=validation), \
                mock.patch.object(self.wf._camera, "get_status", return_value="OFF"), \
                mock.patch(
                    "VisionInspection.services.inspection_workflow.storage_service.ensure_evidence_capacity"
                ), mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.start_camera",
                    return_value=True,
                ), mock.patch(
                    "VisionInspection.services.inspection_workflow.CameraService.stop_camera"
                ):
            result = self.wf.submit(self.part.part_number)

        self.assertTrue(result["ok"], result)
        session = VisionInspectionSession.objects.get(session_id=result["session_id"])
        self.assertEqual(session.front_status, VisionInspectionSession.PENDING)
        self.assertEqual(session.back_status, VisionInspectionSession.PENDING)
        expected_roi = {
            "x": roi.x / roi.ref_width,
            "y": roi.y / roi.ref_height,
            "w": roi.width / roi.ref_width,
            "h": roi.height / roi.ref_height,
            "ref_width": roi.ref_width,
            "ref_height": roi.ref_height,
            "source": "part_configuration",
            "version": roi.version,
            "configuration_id": previous.pk,
            "inherited": True,
        }
        self.assertEqual(session.front_roi_norm, expected_roi)
        self.assertEqual(session.back_roi_norm, expected_roi)
        self.assertEqual(session.roi_version, roi.version)

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
#  Station deployment â€” no login
#
#  The inspection station serves its operator routes anonymously.  No route in
#  the inspection workflow may redirect to a login page, and privileged admin /
#  configuration routes must stay protected behind a Django staff account.
# ---------------------------------------------------------------------------


def _staff_client():
    from django.contrib.auth.models import User

    from django.contrib.auth.models import Group
    user = User.objects.create_user(
        username="engineer", password="x", is_staff=True
    )
    user.groups.add(Group.objects.get(name="Administrator"))
    from django.test import Client

    client = Client()
    assert client.login(username="engineer", password="x")
    return client, user


class StationAccessTests(TestCase):
    """Acceptance: station pages are scoped to authenticated roles."""

    def setUp(self):
        self.client, self.admin = _role_client("Administrator", "STATION-ADMIN")
        self.operator_client, self.operator = _role_client("Operator", "STATION-OPERATOR")
        self.supervisor_client, self.supervisor = _role_client("Supervisor", "STATION-SUPERVISOR")

    def test_admin_audit_log_renders_account_target(self):
        from VisionInspection.models import VisionInspectionAuditLog

        client, user = _staff_client()
        VisionInspectionAuditLog.objects.create(
            actor=user,
            action="account.updated",
            target_type="auth.User",
            target_id=str(user.pk),
            details={"message": "Account permissions updated"},
        )

        response = client.get(reverse("VisionInspection:audit-log"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Administrative audit log")
        self.assertContains(response, f"auth.User #{user.pk}")
        self.assertContains(response, "Account permissions updated")

    def test_root_url_opens_dashboard(self):
        r = self.client.get("/vision-inspection/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Inspection Overview")
        self.assertContains(r, "Inspection Result Distribution")
        self.assertContains(r, "Part Wise Inspection")
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

    def test_dashboard_requires_an_authenticated_role(self):
        from django.test import Client

        response = Client().get("/vision-inspection/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/vision-inspection/login/", response.url)
        self.assertEqual(self.client.get("/vision-inspection/").status_code, 200)

    def test_live_inspection_page_requires_inspection_permission(self):
        r = self.operator_client.get(reverse("VisionInspection:inspection"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "wfBarcode")  # barcode scan step of the workflow

    def test_operator_page_starts_with_scan_and_no_active_camera_stream(self):
        r = self.client.get(reverse("VisionInspection:inspection"))
        self.assertContains(r, 'id="wfStageScan"')
        self.assertContains(r, 'id="wfStageFront" hidden')
        self.assertContains(r, 'id="wfStageBack" hidden')
        self.assertContains(r, 'name="csrfmiddlewaretoken"')
        self.assertNotContains(r, 'id="wfProgressRows"')
        self.assertNotContains(r, 'id="wfDetFront"')
        self.assertNotContains(r, 'id="mxSaveRoiBtn"')

    def test_storage_settings_is_available_to_station_users(self):
        response = self.client.get(reverse("VisionInspection:storage-settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Destination Configuration")
        self.assertContains(response, "TEST CONNECTION")
        self.assertContains(response, "Automatic Storage Transfer")
        self.assertContains(response, "Historical Image Migration")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, reverse("VisionInspection:storage-settings"))
        state = self.client.get(reverse("VisionInspection:storage-state"))
        self.assertEqual(state.status_code, 200)

    def test_part_setup_and_configuration_require_master_data_access(self):
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

    def test_inspection_master_page_requires_camera_setup_access(self):
        self.assertEqual(
            self.operator_client.get(reverse("VisionInspection:inspection-master")).status_code,
            403,
        )
        r = self.client.get(reverse("VisionInspection:inspection-master"))
        self.assertEqual(r.status_code, 200)

    def test_history_and_traceability_require_report_access(self):
        for name in ("VisionInspection:history", "VisionInspection:ui-traceability"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)

    def test_station_shows_configured_station_id(self):
        """The configured station identifier is shown on the station pages."""
        from django.test import override_settings

        with override_settings(VISION_INSPECTION_STATION_ID="ST-42"):
            r = self.client.get(reverse("VisionInspection:inspection-master"))
            self.assertContains(r, "ST-42")

    def test_station_hides_admin_entry_points_for_supervisors(self):
        """Admin-only links are not advertised to read-only Supervisors."""
        r = self.supervisor_client.get("/vision-inspection/")
        self.assertNotContains(r, "Admin Panel")
        self.assertNotContains(r, "Employee Master")


class RoleBoundaryTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group, User

        self.operator = User.objects.create_user(
            username="ROLE-OPERATOR", password="test-password"
        )
        self.operator.groups.add(Group.objects.get(name="Operator"))
        self.supervisor = User.objects.create_user(
            username="ROLE-SUPERVISOR", password="test-password"
        )
        self.supervisor.groups.add(Group.objects.get(name="Supervisor"))
        self.admin = User.objects.create_user(
            username="ROLE-ADMIN", password="test-password"
        )
        self.admin.groups.add(Group.objects.get(name="Administrator"))

    def test_operator_defaults_to_inspection_only_and_is_denied_direct_pages(self):
        from VisionInspection.services.access_control import has_permission

        self.assertTrue(has_permission(self.operator, "perform_inspection"))
        self.assertFalse(has_permission(self.operator, "view_inspection_dashboard"))
        self.assertFalse(has_permission(self.operator, "view_inspection_history"))
        self.assertFalse(has_permission(self.operator, "view_inspection_reports"))

        self.client.force_login(self.operator)
        self.assertEqual(
            self.client.get(reverse("VisionInspection:inspection")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("VisionInspection:dashboard")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("VisionInspection:ui-reports")).status_code,
            403,
        )

    def test_supervisor_has_read_only_default_access(self):
        self.client.force_login(self.supervisor)
        for route_name in (
            "VisionInspection:dashboard",
            "VisionInspection:ui-reports",
            "VisionInspection:ui-parts",
            "VisionInspection:storage-settings",
        ):
            with self.subTest(route_name=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("VisionInspection:inspection-master")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("VisionInspection:user-management")).status_code,
            403,
        )

    def test_admin_can_grant_an_operator_a_per_user_exception(self):
        from VisionInspection.services.access_control import has_permission

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("VisionInspection:user-management-permissions"),
            {
                "action": "save_user_overrides",
                "user_id": self.operator.pk,
                "override_view_inspection_reports": "grant",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(has_permission(self.operator, "view_inspection_reports"))

    def test_admin_only_permission_page_lists_operator_override_targets(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("VisionInspection:user-management-permissions")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operator or Supervisor account")
        self.assertContains(response, "ROLE-OPERATOR")


class StationApiTests(TestCase):
    """Acceptance: station APIs work anonymously and keep server validation."""

    def setUp(self):
        self.client, self.admin = _role_client("Administrator", "API-ADMIN")
        from django.test import Client
        self.anonymous_client = Client()
        self.part = VisionInspectionPart.objects.create(
            part_number="AB-100", part_name="Airbag"
        )

    def test_validate_model_uses_structured_validation_statuses(self):
        from unittest.mock import patch

        url = reverse("VisionInspection:workflow-validate-model")
        invalid_result = {
            "ok": False,
            "state": "configuration_invalid",
            "message": "No required Front class rule is configured.",
            "validation_errors": ["No enabled class rule is assigned to Front."],
        }
        with patch("VisionInspection.views._workflow") as workflow:
            workflow.return_value.validate_model.return_value = invalid_result
            response = _post(self.client, url, {"part_number": self.part.part_number})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "configuration_invalid")
        self.assertEqual(response.json()["validation_errors"], invalid_result["validation_errors"])

        with patch("VisionInspection.views._workflow") as workflow:
            workflow.return_value.validate_model.return_value = {
                "ok": False,
                "state": "model_runtime_unavailable",
                "message": "ONNX Runtime is unavailable.",
            }
            unavailable = _post(
                self.client,
                url,
                {"part_number": self.part.part_number},
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.json(),
            {
                "ok": False,
                "state": "model_runtime_unavailable",
                "message": "ONNX Runtime is unavailable.",
                "error_code": "model_runtime_unavailable",
            },
        )

        missing_target = _post(self.client, url, {})
        self.assertEqual(missing_target.status_code, 400)
        self.assertEqual(missing_target.json()["error_code"], "validation_target_required")

    def test_validate_model_rejects_malformed_identifiers(self):
        url = reverse("VisionInspection:workflow-validate-model")
        for payload, error_code in (
            ({"part_number": ["AB-100"]}, "invalid_part_number"),
            ({"model_id": 1.5}, "invalid_model_id"),
            ({"model_id": True}, "invalid_model_id"),
        ):
            with self.subTest(payload=payload):
                response = _post(self.client, url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], error_code)

    def test_validate_model_returns_runtime_diagnostic_for_registered_onnx(self):
        from unittest.mock import patch

        from VisionInspection.models import (
            VisionPartConfiguration,
            VisionPartModel,
        )
        from VisionInspection.services.model_registry import ModelRegistry

        class FakeYOLO:
            def __init__(self, path, task=None):
                self.task = task
                self.names = {0: "stitch"}

        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "stitch.onnx")
            with open(model_path, "wb") as model_file:
                model_file.write(b"onnx model")
            model = VisionYOLOModel.objects.create(
                name="ONNX segmentation",
                model_path=model_path,
                num_classes=1,
            )
            configuration = VisionPartConfiguration.objects.create(
                part=self.part,
                version=1,
                inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
                is_active=True,
                status=VisionPartConfiguration.STATUS_READY,
            )
            VisionPartModel.objects.create(
                configuration=configuration,
                model=model,
                task_type=VisionPartModel.TASK_SEGMENT,
                classes=[{"id": 0, "name": "stitch"}],
                num_classes=1,
                is_enabled=True,
            )

            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(ModelRegistry, "_import_onnx", return_value=None):
                response = _post(
                    self.client,
                    reverse("VisionInspection:workflow-validate-model"),
                    {"part_number": self.part.part_number},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "model_runtime_unavailable")
        self.assertIn("ONNX Runtime is unavailable", response.json()["message"])

    def test_inspection_inference_errors_return_server_error(self):
        from unittest.mock import patch
        from VisionInspection.models import VisionInspectionSession

        session = VisionInspectionSession.objects.create(
            part_number=self.part.part_number,
            operator=self.admin,
        )

        with patch("VisionInspection.views._workflow") as workflow:
            workflow.return_value.run_side.return_value = {
                "ok": False,
                "state": "front_error",
                "message": "Segmentation inference failed: invalid ONNX output.",
            }
            response = _post(
                self.client,
                reverse(
                    "VisionInspection:workflow-inspect-side",
                    kwargs={"side": "front"},
                ),
                {"session_id": str(session.session_id)},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["state"], "front_error")
        self.assertIn("invalid ONNX output", response.json()["message"])

    def test_duplicate_side_inference_returns_conflict(self):
        from unittest.mock import patch
        from VisionInspection.models import VisionInspectionSession

        session = VisionInspectionSession.objects.create(
            part_number=self.part.part_number,
            operator=self.admin,
        )

        with patch("VisionInspection.views._workflow") as workflow:
            workflow.return_value.run_side.return_value = {
                "ok": False,
                "state": "side_in_progress",
                "message": "Front inspection is already in progress.",
            }
            response = _post(
                self.client,
                reverse(
                    "VisionInspection:workflow-inspect-side",
                    kwargs={"side": "front"},
                ),
                {"session_id": str(session.session_id)},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["state"], "side_in_progress")

    def test_presence_rules_with_zero_counts_save_reload_and_validate(self):
        from unittest.mock import patch

        from VisionInspection.models import (
            VisionClassConfiguration,
            VisionPartConfiguration,
            VisionPartModel,
            VisionYOLOModel,
        )

        model = VisionYOLOModel.objects.create(
            name="Presence model",
            model_path=__file__,
            num_classes=2,
        )
        payload = {
            "confirm_changes": True,
            "inspection_mode": "FRONT_BACK",
            "acceptance_rule": "presence",
            "front_camera_source": "OPENCV",
            "front_camera_index": 0,
            "back_camera_source": "OPENCV",
            "back_camera_index": 1,
            "models": [{
                "model_id": model.pk,
                "enabled": True,
                "classes": [
                    {
                        "class_id": 0,
                        "class_name": "front-stitch",
                        "enabled": True,
                        "required": True,
                        "side": "FRONT",
                        "expected_front": 0,
                        "expected_back": 0,
                        "priority": 1,
                        "acceptance_rule": "presence",
                    },
                    {
                        "class_id": 1,
                        "class_name": "back-stitch",
                        "enabled": True,
                        "required": True,
                        "side": "BACK",
                        "expected_front": 0,
                        "expected_back": 0,
                        "priority": 2,
                        "acceptance_rule": "presence",
                    },
                ],
            }],
        }
        discovered = [
            {"id": 0, "name": "front-stitch"},
            {"id": 1, "name": "back-stitch"},
        ]
        save_url = reverse(
            "VisionInspection:api-part-vision-configuration-save",
            args=[self.part.pk],
        )
        with patch(
            "VisionInspection.views._discover_configurable_classes",
            return_value=discovered,
        ):
            saved = _post(self.client, save_url, payload)

        self.assertEqual(saved.status_code, 201, saved.content)
        self.assertEqual(saved.json()["status"], VisionPartConfiguration.STATUS_READY)
        self.assertTrue(saved.json()["is_active"])
        configuration_id = saved.json()["configuration_id"]

        association = VisionPartModel.objects.get(
            configuration_id=configuration_id,
            model=model,
        )
        persisted = list(
            VisionClassConfiguration.objects.filter(part_model=association)
            .order_by("class_id")
            .values(
                "class_name",
                "is_enabled",
                "is_required",
                "side",
                "expected_front",
                "expected_back",
                "acceptance_rule",
            )
        )
        self.assertEqual(
            persisted,
            [
                {
                    "class_name": "front-stitch",
                    "is_enabled": True,
                    "is_required": True,
                    "side": "FRONT",
                    "expected_front": 0,
                    "expected_back": 0,
                    "acceptance_rule": "presence",
                },
                {
                    "class_name": "back-stitch",
                    "is_enabled": True,
                    "is_required": True,
                    "side": "BACK",
                    "expected_front": 0,
                    "expected_back": 0,
                    "acceptance_rule": "presence",
                },
            ],
        )

        reloaded = self.client.get(
            reverse(
                "VisionInspection:api-part-vision-configuration",
                args=[self.part.pk],
            )
        )
        self.assertEqual(reloaded.status_code, 200)
        api_config = reloaded.json()["configuration"]
        self.assertEqual(api_config["id"], configuration_id)
        api_rules = {
            rule["class_name"]: rule
            for configured_model in api_config["models"]
            for rule in configured_model["classes"]
        }
        self.assertTrue(api_rules["front-stitch"]["required"])
        self.assertEqual(api_rules["front-stitch"]["side"], "FRONT")
        self.assertTrue(api_rules["back-stitch"]["required"])
        self.assertEqual(api_rules["back-stitch"]["side"], "BACK")

        with patch(
            "VisionInspection.services.inspection_workflow.ModelRegistry.inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": discovered,
            },
        ):
            validation = _post(
                self.client,
                reverse("VisionInspection:workflow-validate-model"),
                {"part_number": self.part.part_number},
            )

        self.assertEqual(validation.status_code, 200, validation.content)
        self.assertTrue(validation.json()["ok"])
        self.assertEqual(validation.json()["configuration_id"], configuration_id)

    def test_validate_model_returns_200_only_after_success(self):
        from unittest.mock import patch

        with patch("VisionInspection.views._workflow") as workflow:
            workflow.return_value.validate_model.return_value = {
                "ok": True,
                "state": "model_loaded",
                "model_id": 42,
                "message": "Model and part configuration are valid.",
            }
            response = _post(
                self.client,
                reverse("VisionInspection:workflow-validate-model"),
                {"part_number": self.part.part_number},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["model_id"], 42)

    def test_camera_status_api_rejects_anonymous_access(self):
        r = self.anonymous_client.get(reverse("VisionInspection:camera-status"))
        self.assertEqual(r.status_code, 302)

    def test_camera_stop_api_rejects_anonymous_access(self):
        r = self.anonymous_client.get(reverse("VisionInspection:camera-stop"))
        self.assertEqual(r.status_code, 302)

    def test_roi_camera_start_reuses_matching_active_camera(self):
        with mock.patch(
            "VisionInspection.views.CameraService.start_camera", return_value=False
        ) as start, mock.patch(
            "VisionInspection.views.CameraService.get_status", return_value="ON"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_source", return_value="opencv"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_camera_index", return_value=2
        ), mock.patch(
            "VisionInspection.views.CameraService.get_state", return_value="RUNNING"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_error", return_value=None
        ):
            response = self.client.get(
                reverse("VisionInspection:camera-start"),
                {"source": "opencv", "camera_index": 2, "reuse_if_active": "1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reused"])
        self.assertFalse(response.json()["started_by_request"])
        start.assert_called_once_with(
            camera_index=2,
            source="opencv",
            reuse_existing=True,
        )

    def test_roi_camera_start_refuses_to_replace_a_different_active_camera(self):
        with mock.patch(
            "VisionInspection.views.CameraService.start_camera", return_value=False
        ), mock.patch(
            "VisionInspection.views.CameraService.get_status", return_value="ON"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_source", return_value="opencv"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_camera_index", return_value=2
        ):
            response = self.client.get(
                reverse("VisionInspection:camera-start"),
                {"source": "opencv", "camera_index": 0, "reuse_if_active": "1"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already active", response.json()["error"])

    def test_roi_camera_start_ignores_index_for_shared_picamera_source(self):
        with mock.patch(
            "VisionInspection.views.CameraService.start_camera", return_value=False
        ), mock.patch(
            "VisionInspection.views.CameraService.get_status", return_value="ON"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_source", return_value="picamera"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_camera_index", return_value=0
        ), mock.patch(
            "VisionInspection.views.CameraService.get_state", return_value="RUNNING"
        ), mock.patch(
            "VisionInspection.views.CameraService.get_error", return_value=None
        ):
            response = self.client.get(
                reverse("VisionInspection:camera-start"),
                {"source": "picamera", "camera_index": 1, "reuse_if_active": "1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reused"])

    def test_part_validation_requires_inspection_permission(self):
        anonymous = self.anonymous_client.post(
            reverse("VisionInspection:api-part-validate"),
            data=json.dumps({"part_number": "ab-100"}),
            content_type="application/json",
        )
        self.assertEqual(anonymous.status_code, 302)
        operator_client, _operator = _role_client("Operator", "API-OPERATOR")
        r = operator_client.post(
            reverse("VisionInspection:api-part-validate"),
            data=json.dumps({"part_number": "ab-100"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["valid"])

    def test_part_list_requires_master_data_permission_and_includes_inactive_parts(self):
        VisionInspectionPart.objects.create(part_number="OLD-01", part_name="Inactive", is_active=False)
        response = self.client.get(reverse("VisionInspection:api-part-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["part_number"] for row in response.json()["items"]],
            ["AB-100", "OLD-01"],
        )

    def test_part_configuration_api_is_part_scoped(self):
        response = self.client.get(
            reverse("VisionInspection:api-part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["part"]["id"], self.part.pk)
        self.assertIsNone(response.json()["configuration"])
        self.assertEqual(response.json()["available_models"], [])

    def test_part_configuration_page_is_protected(self):
        response = self.client.get(
            reverse("VisionInspection:part-vision-configuration", args=[self.part.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("VisionInspection:part-model-upload"))
        self.assertContains(response, 'class="vi-btn pc-roi-configure is-disabled"')

    def test_configured_part_page_links_to_roi_editor_for_latest_version(self):
        from VisionInspection.models import VisionPartConfiguration

        old = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
            is_active=True,
        )
        latest = VisionPartConfiguration.objects.create(
            part=self.part,
            version=2,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
        )
        self.assertNotEqual(old.pk, latest.pk)

        page = self.client.get(
            reverse("VisionInspection:part-vision-configuration", args=[self.part.pk])
        )
        self.assertContains(page, 'id="pcConfigureRoi"')
        self.assertContains(page, reverse("VisionInspection:part-roi-setup", args=[self.part.pk]))
        roi_page = self.client.get(
            reverse("VisionInspection:part-roi-setup", args=[self.part.pk])
        )
        self.assertEqual(roi_page.status_code, 200)
        self.assertContains(roi_page, "configurationId: " + str(latest.pk))
        self.assertContains(roi_page, "?configuration_id=" + str(latest.pk))
        self.assertContains(roi_page, 'id="rsCancel"')
        self.assertContains(roi_page, 'name="csrfmiddlewaretoken"')

    def test_roi_save_versions_existing_roi_and_rejects_out_of_bounds(self):
        from VisionInspection.models import VisionPartConfiguration, VisionPartROI

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
        )
        url = reverse("VisionInspection:api-part-vision-roi-save", args=[self.part.pk])
        payload = {
            "configuration_id": configuration.pk,
            "x": 10,
            "y": 20,
            "width": 100,
            "height": 80,
            "ref_width": 640,
            "ref_height": 480,
        }
        first = _post(self.client, url, payload)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertTrue(first.json()["saved"])
        self.assertFalse(configuration.__class__.objects.get(pk=configuration.pk).is_active)

        second_payload = dict(payload, x=30, y=40)
        second = _post(self.client, url, second_payload)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["roi"]["version"], 2)
        self.assertEqual(
            list(VisionPartROI.objects.filter(configuration=configuration, is_active=True).values_list("version", flat=True)),
            [2],
        )

        invalid = _post(self.client, url, dict(payload, x=630))
        self.assertEqual(invalid.status_code, 400)
        for invalid_coordinates in (
            dict(payload, x=float("nan")),
            dict(payload, width=float("inf")),
            dict(payload, ref_width=float("inf")),
        ):
            with self.subTest(payload=invalid_coordinates):
                response = _post(self.client, url, invalid_coordinates)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(
            VisionPartROI.objects.filter(configuration=configuration, is_active=True).get().version,
            2,
        )

    def test_roi_save_activates_only_a_valid_configuration(self):
        from VisionInspection.models import (
            VisionClassConfiguration,
            VisionPartConfiguration,
            VisionPartModel,
        )

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
            acceptance_rule="count",
        )
        model = VisionYOLOModel.objects.create(
            name="ROI validation model", model_path=__file__, num_classes=1
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
            side=VisionPartConfiguration.SIDE_FRONT,
            expected_front=1,
            priority=1,
            acceptance_rule="count",
        )

        response = _post(
            self.client,
            reverse("VisionInspection:api-part-vision-roi-save", args=[self.part.pk]),
            {
                "configuration_id": configuration.pk,
                "x": 10,
                "y": 10,
                "width": 100,
                "height": 80,
                "ref_width": 640,
                "ref_height": 480,
            },
        )
        configuration.refresh_from_db()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(configuration.status, VisionPartConfiguration.STATUS_READY)
        self.assertTrue(configuration.is_active)
        status = self.client.get(
            reverse("VisionInspection:api-part-vision-configuration", args=[self.part.pk])
        )
        self.assertTrue(status.json()["configuration"]["roi_configured"])

    def test_roi_save_refuses_different_front_and_back_cameras_without_overwriting(self):
        from VisionInspection.models import VisionPartConfiguration, VisionPartROI

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_index=0,
            back_camera_index=1,
        )
        old_roi = VisionPartROI.objects.create(
            configuration=configuration,
            version=1,
            x=10,
            y=10,
            width=100,
            height=80,
            ref_width=640,
            ref_height=480,
        )
        response = _post(
            self.client,
            reverse("VisionInspection:api-part-vision-roi-save", args=[self.part.pk]),
            {
                "configuration_id": configuration.pk,
                "x": 20,
                "y": 20,
                "width": 100,
                "height": 80,
                "ref_width": 640,
                "ref_height": 480,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("same camera source", response.json()["error"])
        old_roi.refresh_from_db()
        self.assertTrue(old_roi.is_active)
        self.assertEqual(VisionPartROI.objects.filter(configuration=configuration).count(), 1)

    def test_roi_save_requires_csrf(self):
        from django.test import Client
        from VisionInspection.models import VisionPartConfiguration

        configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
        )
        client = Client(enforce_csrf_checks=True)
        response = _post(
            client,
            reverse("VisionInspection:api-part-vision-roi-save", args=[self.part.pk]),
            {
                "configuration_id": configuration.pk,
                "x": 10,
                "y": 10,
                "width": 100,
                "height": 80,
                "ref_width": 640,
                "ref_height": 480,
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_roi_save_reports_missing_configuration_before_frame_validation(self):
        response = _post(
            self.client,
            reverse("VisionInspection:api-part-vision-roi-save", args=[self.part.pk]),
            {},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Save the model configuration", response.json()["error"])

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
            "VisionInspection.views.registry.inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": [{"id": 0, "name": "actual-class"}],
            },
        ):
            response = self.client.get(
                reverse("VisionInspection:api-yolo-model-classes", args=[model.pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["classes"], [{"id": 0, "name": "actual-class"}])

    def test_frontend_model_upload_accepts_pt_and_onnx_and_discovers_classes(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        with tempfile.TemporaryDirectory() as model_dir, override_settings(
            VISION_MODEL_OUTPUT_DIR=model_dir
        ):
            for suffix in (".pt", ".onnx"):
                with self.subTest(suffix=suffix), patch(
                    "VisionInspection.views.registry.inspect",
                    return_value={
                        "ok": True,
                        "task": "segment",
                        "num_classes": 2,
                        "classes": [
                            {"id": 0, "name": "class-a"},
                            {"id": 1, "name": "class-b"},
                        ],
                    },
                ) as inspect:
                    response = self.client.post(
                        reverse("VisionInspection:part-model-upload"),
                        {
                            "version": "v2",
                            "model_file": SimpleUploadedFile(
                                f"safe{suffix}", b"model-bytes"
                            ),
                        },
                    )
                    self.assertEqual(response.status_code, 201, response.content)
                    model = VisionYOLOModel.objects.get(
                        pk=response.json()["model"]["id"]
                    )
                    self.assertFalse(model.is_active)
                    self.assertEqual(model.num_classes, 2)
                    self.assertTrue(model.model_path.startswith(model_dir))
                    self.assertEqual(
                        os.path.splitext(model.model_path)[1].lower(),
                        suffix,
                    )
                    self.assertEqual(
                        response.json()["classes"],
                        [
                            {"id": 0, "name": "class-a"},
                            {"id": 1, "name": "class-b"},
                        ],
                    )
                    inspect.assert_called_once()
                    self.assertEqual(
                        inspect.call_args.kwargs["task"],
                        "segment",
                    )

    def test_frontend_model_upload_rejects_unsupported_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("VisionInspection:part-model-upload"),
            {"model_file": SimpleUploadedFile("model.txt", b"not a model")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(VisionYOLOModel.objects.exists())

    def test_onnx_upload_reports_missing_runtime_and_invalid_model(self):
        from unittest.mock import patch

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from VisionInspection.services.model_registry import (
            ModelLoadError,
            ModelRuntimeUnavailableError,
        )

        with tempfile.TemporaryDirectory() as model_dir, override_settings(
            VISION_MODEL_OUTPUT_DIR=model_dir
        ):
            for exc, expected_status, message in (
                (
                    ModelRuntimeUnavailableError(
                        "ONNX Runtime is unavailable for this architecture."
                    ),
                    503,
                    "ONNX Runtime is unavailable",
                ),
                (
                    ModelLoadError(
                        "The ONNX model is invalid or incompatible."
                    ),
                    422,
                    "invalid or incompatible",
                ),
            ):
                with self.subTest(status=expected_status), patch(
                    "VisionInspection.views.registry.inspect",
                    side_effect=exc,
                ):
                    response = self.client.post(
                        reverse("VisionInspection:part-model-upload"),
                        {
                            "model_file": SimpleUploadedFile(
                                "uploaded.onnx",
                                b"test onnx",
                            ),
                        },
                    )
                    self.assertEqual(response.status_code, expected_status)
                    self.assertIn(message, response.json()["error"])
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
        saved_rule = configuration.part_models.get(model=model).class_configs.get(class_id=0)
        self.assertTrue(saved_rule.is_required)
        self.assertEqual(saved_rule.side, "FRONT")

    def test_configuration_save_and_reload_preserves_class_rule_values(self):
        from unittest.mock import patch

        from VisionInspection.models import (
            VisionClassConfiguration,
            VisionPartConfiguration,
            VisionPartModel,
        )

        model = VisionYOLOModel.objects.create(
            name="Registered model", model_path=__file__, is_active=False, num_classes=2
        )
        save_url = reverse(
            "VisionInspection:api-part-vision-configuration-save",
            args=[self.part.pk],
        )
        base_payload = {
            "confirm_changes": True,
            "inspection_mode": "FRONT_BACK",
            "acceptance_rule": "count_confidence",
            "front_camera_source": "OPENCV",
            "front_camera_index": 0,
            "back_camera_source": "OPENCV",
            "back_camera_index": 1,
            "models": [{
                "model_id": model.pk,
                "enabled": True,
                "classes": [
                    {
                        "class_id": 0, "class_name": "front-stitch",
                        "enabled": True, "side": "FRONT",
                        "expected_front": 2, "expected_back": 0,
                        "front_threshold": 0.72, "back_threshold": 0.51,
                        "priority": 1, "required": False,
                        "acceptance_rule": "count",
                    },
                    {
                        "class_id": 1, "class_name": "back-stitch",
                        "enabled": True, "side": "BACK",
                        "expected_front": 0, "expected_back": 3,
                        "front_threshold": 0.62, "back_threshold": 0.81,
                        "priority": 2, "required": False,
                        "acceptance_rule": "presence",
                    },
                ],
            }],
        }
        classes = [
            {"id": 0, "name": "front-stitch"},
            {"id": 1, "name": "back-stitch"},
        ]
        with patch(
            "VisionInspection.views._discover_configurable_classes",
            return_value=classes,
        ):
            first = _post(self.client, save_url, base_payload)
            self.assertEqual(first.status_code, 201, first.content)
            configuration_id = first.json()["configuration_id"]
            self.assertFalse(first.json()["is_active"])
            association = VisionPartModel.objects.get(
                configuration_id=configuration_id,
                model=model,
            )
            self.assertFalse(
                VisionClassConfiguration.objects.get(
                    part_model=association,
                    class_id=0,
                ).is_required
            )
            initially_saved = self.client.get(
                reverse(
                    "VisionInspection:api-part-vision-configuration",
                    args=[self.part.pk],
                )
            ).json()["configuration"]
            initially_saved_rules = {
                rule["class_id"]: rule
                for saved_model in initially_saved["models"]
                for rule in saved_model["classes"]
            }
            self.assertFalse(initially_saved_rules[0]["required"])
            self.assertTrue(any(
                "all optional" in error
                for error in first.json()["validation_errors"]
            ))
            from VisionInspection.services.inspection_workflow import InspectionWorkflow

            workflow = InspectionWorkflow()
            with patch.object(
                workflow._model_registry,
                "inspect",
                return_value={
                    "ok": True,
                    "task": "segment",
                    "classes": [
                        {"id": 0, "name": "front-stitch"},
                        {"id": 1, "name": "back-stitch"},
                    ],
                },
            ):
                optional_validation = _post(
                    self.client,
                    reverse("VisionInspection:workflow-validate-model"),
                    {"part_number": self.part.part_number},
                )
            self.assertEqual(optional_validation.status_code, 422)
            self.assertTrue(any(
                "all optional" in error
                for error in optional_validation.json()["validation_errors"]
            ))

            corrected_payload = {
                **base_payload,
                "configuration_id": configuration_id,
                "models": [{
                    **base_payload["models"][0],
                    "classes": [
                        {
                            **base_payload["models"][0]["classes"][0],
                            "required": True,
                        },
                        {
                            **base_payload["models"][0]["classes"][1],
                            "side": "BACK",
                            "expected_back": 3,
                            "required": True,
                            "acceptance_rule": "presence",
                        },
                    ],
                }],
            }
            second = _post(self.client, save_url, corrected_payload)

        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["configuration_id"], configuration_id)
        self.assertTrue(second.json()["is_active"])
        self.assertEqual(
            VisionPartConfiguration.objects.filter(part=self.part).count(),
            1,
        )
        association = VisionPartModel.objects.get(
            configuration_id=configuration_id,
            model=model,
        )
        self.assertEqual(association.class_configs.count(), 2)
        self.assertEqual(
            VisionClassConfiguration.objects.filter(
                part_model=association,
                class_id=0,
            ).values(
                "class_name",
                "is_enabled",
                "side",
                "expected_front",
                "expected_back",
                "is_required",
                "acceptance_rule",
            ).get(),
            {
                "class_name": "front-stitch",
                "is_enabled": True,
                "side": "FRONT",
                "expected_front": 2,
                "expected_back": 0,
                "is_required": True,
                "acceptance_rule": "count",
            },
        )
        self.assertEqual(
            VisionClassConfiguration.objects.filter(
                part_model=association,
                class_id=1,
            ).values(
                "class_name",
                "is_enabled",
                "side",
                "expected_front",
                "expected_back",
                "front_threshold",
                "back_threshold",
                "priority",
                "is_required",
                "acceptance_rule",
            ).get(),
            {
                "class_name": "back-stitch",
                "is_enabled": True,
                "side": "BACK",
                "expected_front": 0,
                "expected_back": 3,
                "front_threshold": 0.62,
                "back_threshold": 0.81,
                "priority": 2,
                "is_required": True,
                "acceptance_rule": "presence",
            },
        )

        saved = self.client.get(
            reverse(
                "VisionInspection:api-part-vision-configuration",
                args=[self.part.pk],
            )
        ).json()["configuration"]
        saved_rules = {
            rule["class_id"]: rule
            for saved_model in saved["models"]
            for rule in saved_model["classes"]
        }
        self.assertEqual(saved["id"], configuration_id)
        self.assertEqual(saved["status"], VisionPartConfiguration.STATUS_READY)
        self.assertEqual(saved_rules[0]["side"], "FRONT")
        self.assertTrue(saved_rules[0]["required"])
        self.assertEqual(saved_rules[0]["expected_front"], 2)
        self.assertEqual(saved_rules[0]["front_threshold"], 0.72)
        self.assertEqual(saved_rules[0]["acceptance_rule"], "count")
        self.assertEqual(saved_rules[1]["side"], "BACK")
        self.assertTrue(saved_rules[1]["required"])
        self.assertEqual(saved_rules[1]["expected_back"], 3)
        self.assertEqual(saved_rules[1]["back_threshold"], 0.81)
        self.assertEqual(saved_rules[1]["acceptance_rule"], "presence")

        workflow = InspectionWorkflow()
        with patch.object(
            workflow._model_registry,
            "inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": [
                    {"id": 0, "name": "front-stitch"},
                    {"id": 1, "name": "back-stitch"},
                ],
            },
        ):
            service_result = workflow.validate_model(
                part_number=self.part.part_number
            )
            self.assertTrue(service_result["ok"], service_result)
            self.assertEqual(
                service_result["configuration_id"],
                configuration_id,
            )
            validation = _post(
                self.client,
                reverse("VisionInspection:workflow-validate-model"),
                {"part_number": self.part.part_number},
            )
        self.assertEqual(validation.status_code, 200, validation.content)
        self.assertTrue(validation.json()["ok"])
        self.assertEqual(
            validation.json()["configuration_id"],
            configuration_id,
        )

    def test_configuration_save_requires_csrf(self):
        from django.test import Client

        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.admin)
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

    def test_inspection_state_api_rejects_anonymous_access(self):
        r = self.anonymous_client.get(reverse("VisionInspection:api-state"))
        self.assertEqual(r.status_code, 302)

    def test_workflow_scan_unknown_barcode_is_rejected(self):
        r = self.client.post(
            reverse("VisionInspection:workflow-scan-part"),
            data=json.dumps({"barcode": "NOPE"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {
            "ok": False,
            "state": "part_not_found",
            "message": "Part not found for this barcode.",
        })

    def test_workflow_scan_empty_barcode_is_rejected_with_specific_message(self):
        r = self.client.post(
            reverse("VisionInspection:workflow-scan-part"),
            data=json.dumps({"barcode": ""}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {
            "ok": False,
            "state": "waiting",
            "message": "Scan a part barcode.",
        })

    def test_workflow_cancel_requires_csrf(self):
        from django.test import Client

        strict_client = Client(enforce_csrf_checks=True)
        response = strict_client.post(
            reverse("VisionInspection:workflow-cancel"),
            data=json.dumps({"session_id": "00000000-0000-0000-0000-000000000000"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_workflow_submit_accepts_post_only(self):
        self.assertEqual(
            self.client.get(reverse("VisionInspection:workflow-submit")).status_code,
            405,
        )

    def test_workflow_submit_without_session_is_idempotent_safe(self):
        """Duplicate submissions are refused on the server."""
        r = self.client.post(
            reverse("VisionInspection:workflow-submit"),
            data=json.dumps({"part_number": "AB-100"}),
            content_type="application/json",
        )
        # No approved model configured â†’ the server refuses (never fabricated OK).
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])


class ModelPathResolutionTests(TestCase):
    def test_relative_windows_path_uses_configured_model_root(self):
        from pathlib import Path

        from VisionInspection.services.model_paths import resolve_model_path

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "data", "models")
            with override_settings(VISION_MODEL_OUTPUT_DIR=root):
                resolved = resolve_model_path(r"nested\stitch.onnx")

        self.assertEqual(resolved, Path(root, "nested", "stitch.onnx").resolve())

    def test_existing_model_in_root_recovers_stale_absolute_storage_path(self):
        from pathlib import Path
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        class FakeYOLO:
            def __init__(self, path, task=None):
                self.task = task
                self.names = {0: "stitch"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "data", "models")
            root.mkdir(parents=True)
            model_path = root / "stitch.onnx"
            model_path.write_bytes(b"valid test model")
            stale_path = r"Z:\old-computer\project\data\models\stitch.onnx"

            with override_settings(VISION_MODEL_OUTPUT_DIR=root):
                with patch.object(
                    ModelRegistry, "_import_ultralytics", return_value=FakeYOLO
                ), patch.object(
                    ModelRegistry, "_import_onnx", return_value=object()
                ):
                    info = ModelRegistry().inspect(stale_path, task="segment")

        self.assertTrue(info["ok"])
        self.assertEqual(info["path"], str(model_path.resolve()))
        self.assertEqual(info["classes"], [{"id": 0, "name": "stitch"}])

    def test_missing_model_error_identifies_configured_and_checked_paths(self):
        from pathlib import Path

        from VisionInspection.services.model_registry import ModelRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "data", "models")
            configured_path = r"Z:\old-computer\project\data\models\missing.onnx"
            with override_settings(VISION_MODEL_OUTPUT_DIR=root):
                info = ModelRegistry().inspect(configured_path, task="segment")

        self.assertFalse(info["ok"])
        self.assertIn(configured_path, info["error"])
        self.assertIn(str(Path(configured_path).resolve()), info["error"])


class ModelRegistryConcurrencyTests(TestCase):
    def test_concurrent_predictions_for_same_model_are_serialized(self):
        import threading
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def infer(*args, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_entered.set()
                release_first.wait(2)
            else:
                second_entered.set()
            return []

        with tempfile.NamedTemporaryFile(suffix=".pt") as model_file:
            registry = ModelRegistry()
            entry = registry._entry(model_file.name, task="segment")
            entry.model = object()
            entry.task = "segment"
            results = []
            with patch.object(registry, "_refresh_if_stale"), patch.object(
                registry, "_predict_ultralytics", side_effect=infer
            ):
                first = threading.Thread(
                    target=lambda: results.append(
                        registry.predict(model_file.name, object(), task="segment")
                    )
                )
                second = threading.Thread(
                    target=lambda: results.append(
                        registry.predict(model_file.name, object(), task="segment")
                    )
                )
                first.start()
                self.assertTrue(first_entered.wait(2))
                second.start()
                try:
                    self.assertFalse(second_entered.wait(0.05))
                finally:
                    release_first.set()
                first.join(2)
                second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(calls, 2)
        self.assertEqual(len(results), 2)


class ModelRuntimeImportTests(TestCase):
    def test_ultralytics_import_failure_logs_original_exception(self):
        import builtins
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        original_import = builtins.__import__

        def import_without_ultralytics(name, *args, **kwargs):
            if name == "ultralytics":
                raise ModuleNotFoundError("No module named 'ultralytics'")
            return original_import(name, *args, **kwargs)

        with self.assertLogs(
            "VisionInspection.services.model_registry", level="ERROR"
        ) as captured:
            with patch("builtins.__import__", side_effect=import_without_ultralytics):
                self.assertIsNone(ModelRegistry._import_ultralytics())

        self.assertIn("Failed to import the Ultralytics YOLO runtime", captured.output[0])
        self.assertIn("No module named 'ultralytics'", captured.output[0])

    def test_onnxruntime_import_failure_logs_original_exception(self):
        import importlib
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        original_import = importlib.import_module

        def import_without_onnxruntime(name, *args, **kwargs):
            if name == "onnxruntime":
                raise ModuleNotFoundError("No module named 'onnxruntime'")
            return original_import(name, *args, **kwargs)

        with self.assertLogs(
            "VisionInspection.services.model_registry", level="ERROR"
        ) as captured:
            with patch(
                "VisionInspection.services.model_registry.importlib.import_module",
                side_effect=import_without_onnxruntime,
            ):
                self.assertIsNone(ModelRegistry._import_onnx())

        self.assertIn("Failed to import the ONNX Runtime package", captured.output[0])
        self.assertIn("No module named 'onnxruntime'", captured.output[0])


class SegmentationPreviewRegistryTests(TestCase):
    def test_empty_segmentation_results_are_safe(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        class Result:
            names = {0: "stitch"}
            boxes = []
            masks = None

            def __init__(self, source):
                self.source = source

            def plot(self, **kwargs):
                self.plot_options = kwargs
                return self.source

        class FakeYOLO:
            task = "segment"

            def __init__(self, path, task=None):
                pass

            def predict(self, **kwargs):
                return [Result(kwargs["source"])]

        frame = np.zeros((24, 24, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "empty.onnx")
            with open(path, "wb") as model_file:
                model_file.write(b"mock onnx")
            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(
                ModelRegistry,
                "_import_onnx",
                return_value=object(),
            ):
                detections, rendered, _elapsed = ModelRegistry().predict_segmentation(
                    path,
                    frame,
                )

        self.assertEqual(detections, [])
        self.assertEqual(rendered.shape, frame.shape)

    def test_segmentation_detection_without_masks_is_rejected(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import (
            ModelLoadError,
            ModelRegistry,
        )

        class Box:
            cls = [0]
            conf = [0.91]
            xyxy = [[1, 1, 10, 10]]

        class Result:
            names = {0: "stitch"}
            boxes = [Box()]
            masks = None

        class FakeYOLO:
            task = "segment"

            def __init__(self, path, task=None):
                pass

            def predict(self, **kwargs):
                return [Result()]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing-masks.onnx")
            with open(path, "wb") as model_file:
                model_file.write(b"mock onnx")
            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(
                ModelRegistry,
                "_import_onnx",
                return_value=object(),
            ):
                with self.assertRaisesRegex(ModelLoadError, "without masks"):
                    ModelRegistry().predict_segmentation(
                        path,
                        np.zeros((24, 24, 3), dtype=np.uint8),
                    )

    def test_non_segmentation_model_is_rejected_by_preview(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import (
            ModelLoadError,
            ModelRegistry,
        )

        class FakeYOLO:
            def __init__(self, path, task=None):
                self.task = "detect"
                self.names = {0: "object"}

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "detect.onnx")
            with open(path, "wb") as model_file:
                model_file.write(b"mock onnx")
            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(
                ModelRegistry,
                "_import_onnx",
                return_value=object(),
            ):
                with self.assertRaisesRegex(
                    ModelLoadError,
                    "does not expose Ultralytics segmentation results",
                ):
                    ModelRegistry().predict_segmentation(
                        path,
                        np.zeros((32, 32, 3), dtype=np.uint8),
                    )

    def test_onnx_is_loaded_and_inferred_through_cached_ultralytics_segmentation(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry

        class Box:
            cls = [0]
            conf = [0.91]
            xyxy = [[2, 3, 20, 25]]

        class Mask:
            xy = [[[2, 3], [20, 3], [20, 25]]]

        class Result:
            names = {0: "stitch"}
            boxes = [Box()]
            masks = Mask()

        class FakeYOLO:
            instances = []

            def __init__(self, path, task=None):
                self.task = task
                self.names = {0: "stitch"}
                self.calls = []
                self.__class__.instances.append(self)

            def predict(self, **kwargs):
                self.calls.append(kwargs)
                return [Result()]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stitch.onnx")
            with open(path, "wb") as model_file:
                model_file.write(b"mock onnx")
            FakeYOLO.instances.clear()

            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(
                ModelRegistry,
                "_import_onnx",
                return_value=object(),
            ):
                registry = ModelRegistry()
                info = registry.inspect(path, task="segment")
                frame = np.zeros((32, 32, 3), dtype=np.uint8)
                detections, elapsed = registry.predict(
                    path,
                    frame,
                    conf=0.65,
                    classes={0},
                    task="segment",
                )

            self.assertTrue(info["ok"])
            self.assertEqual(info["task"], "segment")
            self.assertEqual(info["classes"], [{"id": 0, "name": "stitch"}])
            self.assertEqual(len(FakeYOLO.instances), 1)
            self.assertEqual(len(FakeYOLO.instances[0].calls), 1)
            prediction = FakeYOLO.instances[0].calls[0]
            self.assertIs(prediction["source"], frame)
            self.assertEqual(prediction["conf"], 0.65)
            self.assertEqual(prediction["iou"], 0.45)
            self.assertEqual(prediction["imgsz"], 640)
            self.assertEqual(prediction["verbose"], False)
            self.assertEqual(prediction["task"], "segment")
            self.assertEqual(detections[0]["class"], "stitch")
            self.assertEqual(detections[0]["confidence"], 0.91)
            self.assertEqual(detections[0]["polygon"], [[2.0, 3.0], [20.0, 3.0], [20.0, 25.0]])
            self.assertIsInstance(elapsed, float)

    def test_onnx_reports_missing_runtime_and_invalid_file_separately(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import (
            ModelLoadError,
            ModelRegistry,
            ModelRuntimeUnavailableError,
        )

        class FakeYOLO:
            def __init__(self, path, task=None):
                raise RuntimeError("invalid ONNX protobuf")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bad.onnx")
            with open(path, "wb") as model_file:
                model_file.write(b"not an ONNX model")

            registry = ModelRegistry()
            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(ModelRegistry, "_import_onnx", return_value=None):
                with self.assertRaisesRegex(
                    ModelRuntimeUnavailableError,
                    "ONNX Runtime is unavailable",
                ):
                    registry.inspect(path, task="segment")

            with patch.object(
                ModelRegistry,
                "_import_ultralytics",
                return_value=FakeYOLO,
            ), patch.object(
                ModelRegistry,
                "_import_onnx",
                return_value=object(),
            ):
                with self.assertRaisesRegex(
                    ModelLoadError,
                    "invalid or incompatible.*invalid ONNX protobuf",
                ):
                    registry.inspect(path, task="segment")

    def test_pt_and_onnx_models_are_cached_and_use_ultralytics_predict(self):
        from unittest.mock import patch

        from VisionInspection.services.model_registry import ModelRegistry, registry

        class Frame:
            def copy(self):
                return self

        class Box:
            cls = [2]
            conf = [0.876]
            xyxy = [[1, 2, 30, 40]]

        class Result:
            names = {2: "actual-class"}
            boxes = [Box()]
            masks = object()

            def __init__(self, source, plot_calls):
                self.source = source
                self.plot_calls = plot_calls

            def plot(self, **kwargs):
                self.plot_calls.append(kwargs)
                return self.source

        class FakeYOLO:
            loads = []

            def __init__(self, path, task=None):
                self.task = task or "segment"
                self.predict_calls = []
                self.plot_calls = []
                self.__class__.loads.append((path, task, self))

            def predict(self, **kwargs):
                self.predict_calls.append(kwargs)
                return [Result(kwargs["source"], self.plot_calls)]

        frame = Frame()
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".pt", ".onnx"):
                path = os.path.join(directory, f"preview{suffix}")
                with open(path, "wb") as model_file:
                    model_file.write(b"test model placeholder")
                FakeYOLO.loads.clear()

                with patch.object(
                    ModelRegistry,
                    "_import_ultralytics",
                    return_value=FakeYOLO,
                ), patch.object(
                    ModelRegistry,
                    "_import_onnx",
                    return_value=object(),
                ):
                    first = registry.predict_segmentation(
                        path,
                        frame,
                        conf=0.70,
                        show_masks=False,
                        show_boxes=True,
                    )
                    second = registry.predict_segmentation(
                        path,
                        frame,
                        conf=0.50,
                        show_masks=True,
                        show_boxes=False,
                    )

                self.assertEqual(len(FakeYOLO.loads), 1)
                loaded_path, loaded_task, model = FakeYOLO.loads[0]
                self.assertEqual(loaded_path, path)
                if suffix == ".onnx":
                    self.assertEqual(loaded_task, "segment")
                else:
                    self.assertEqual(loaded_task, "segment")
                self.assertEqual(
                    model.predict_calls[0],
                    {
                        "source": frame,
                        "conf": 0.70,
                        "task": "segment",
                        "verbose": False,
                        "imgsz": 640,
                        "show_boxes": True,
                    },
                )
                self.assertEqual(model.predict_calls[1]["conf"], 0.50)
                self.assertEqual(model.predict_calls[1]["imgsz"], 640)
                self.assertFalse(model.predict_calls[1]["show_boxes"])
                self.assertEqual(
                    model.plot_calls,
                    [
                        {
                            "masks": False,
                            "boxes": True,
                            "labels": True,
                            "conf": True,
                        },
                        {
                            "masks": True,
                            "boxes": False,
                            "labels": False,
                            "conf": False,
                        },
                    ],
                )
                self.assertEqual(first[0][0]["class"], "actual-class")
                self.assertEqual(first[0][0]["class_id"], 2)
                self.assertEqual(first[0][0]["confidence"], 0.876)
                self.assertEqual(first[0][0]["x1"], 1)
                self.assertIs(first[1], frame)
                self.assertGreaterEqual(first[2], 0)
                self.assertEqual(second[0], first[0])


class SegmentationPreviewApiTests(TestCase):
    def setUp(self):
        self.client, self.admin = _role_client("Administrator", "PREVIEW-ADMIN")
        from VisionInspection.models import (
            VisionPartConfiguration,
            VisionPartModel,
            VisionYOLOModel,
        )

        self.part = VisionInspectionPart.objects.create(
            part_number="PREVIEW-001",
            part_name="Preview test part",
        )
        self.configuration = VisionPartConfiguration.objects.create(
            part=self.part,
            version=1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_ONLY,
        )
        self.model = VisionYOLOModel.objects.create(
            name="Preview segmentation model",
            model_path=__file__,
            num_classes=1,
        )
        self.association = VisionPartModel.objects.create(
            configuration=self.configuration,
            model=self.model,
            task_type=VisionPartModel.TASK_SEGMENT,
            is_enabled=True,
        )

    def test_preview_uses_roi_and_returns_coordinates_for_same_frame(self):
        import numpy as np
        from unittest.mock import patch

        from VisionInspection.models import VisionPartROI, VisionInspectionSession

        VisionPartROI.objects.create(
            configuration=self.configuration,
            version=1,
            x=20,
            y=10,
            width=40,
            height=30,
            ref_width=120,
            ref_height=100,
        )
        frame = np.zeros((100, 120, 3), dtype=np.uint8)
        detections = [{
            "class_id": 0,
            "class": "real-class",
            "confidence": 0.876,
            "x1": 1,
            "y1": 2,
            "x2": 11,
            "y2": 12,
        }]
        with patch("VisionInspection.views.CameraService.get_state", return_value="RUNNING"), \
                patch("VisionInspection.views.CameraService.get_source", return_value="opencv"), \
                patch("VisionInspection.views.CameraService.get_camera_index", return_value=0), \
                patch(
                    "VisionInspection.views.CameraService.get_frame_with_meta",
                    return_value=(True, frame, {"seq": 12, "age_ms": 10, "dropped": 0}),
                ), \
                patch(
                    "VisionInspection.views.registry.predict_segmentation",
                    return_value=(detections, frame[10:40, 20:60].copy(), 42.5),
                ) as predict, \
                patch(
                    "VisionInspection.views._jpeg_data_url",
                    return_value="data:image/jpeg;base64,frame",
                ):
            response = self.client.get(
                reverse(
                    "VisionInspection:api-part-vision-preview",
                    args=[self.part.pk],
                ),
                {
                    "side": "FRONT",
                    "model_id": self.model.pk,
                    "confidence": "0.65",
                    "show_masks": "0",
                    "show_boxes": "1",
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["frame_seq"], 12)
        self.assertTrue(payload["roi_applied"])
        self.assertEqual(payload["image"], "data:image/jpeg;base64,frame")
        self.assertEqual(payload["inference_ms"], 42.5)
        self.assertEqual(payload["detection_count"], 1)
        self.assertEqual(payload["detections"][0]["class"], "real-class")
        self.assertEqual(payload["detections"][0]["confidence_percent"], 87.6)
        self.assertEqual(
            [payload["detections"][0][key] for key in ("x1", "y1", "x2", "y2")],
            [21, 12, 31, 22],
        )
        self.assertEqual(VisionInspectionSession.objects.count(), 0)
        args, kwargs = predict.call_args
        self.assertEqual(args[0], self.model.model_path)
        self.assertEqual(args[1].shape, (30, 40, 3))
        self.assertEqual(kwargs, {
            "conf": 0.65,
            "show_masks": False,
            "show_boxes": True,
        })

    def test_preview_rejects_a_stopped_camera_without_creating_records(self):
        from unittest.mock import patch

        from VisionInspection.models import VisionInspectionSession

        with patch(
            "VisionInspection.views.CameraService.get_state",
            return_value="STOPPED",
        ):
            response = self.client.get(
                reverse(
                    "VisionInspection:api-part-vision-preview",
                    args=[self.part.pk],
                ),
                {"side": "FRONT", "model_id": self.model.pk},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("camera is stopped", response.json()["error"])
        self.assertEqual(VisionInspectionSession.objects.count(), 0)

    def test_preview_rejects_missing_or_invalid_roi_without_running_inference(self):
        import numpy as np
        from unittest.mock import patch

        from VisionInspection.models import VisionPartROI

        frame = np.zeros((100, 120, 3), dtype=np.uint8)
        with patch("VisionInspection.views.CameraService.get_state", return_value="RUNNING"), \
                patch("VisionInspection.views.CameraService.get_source", return_value="opencv"), \
                patch("VisionInspection.views.CameraService.get_camera_index", return_value=0), \
                patch(
                    "VisionInspection.views.CameraService.get_frame_with_meta",
                    return_value=(True, frame, {"seq": 14, "age_ms": 5, "dropped": 0}),
                ), patch("VisionInspection.views.registry.predict_segmentation") as predict:
            missing = self.client.get(
                reverse(
                    "VisionInspection:api-part-vision-preview",
                    args=[self.part.pk],
                ),
                {"side": "FRONT", "model_id": self.model.pk},
            )
            self.assertEqual(missing.status_code, 409)
            self.assertIn("No compatible ROI", missing.json()["error"])

            VisionPartROI.objects.create(
                configuration=self.configuration,
                version=1,
                x=120,
                y=10,
                width=20,
                height=30,
                ref_width=120,
                ref_height=100,
            )
            invalid = self.client.get(
                reverse(
                    "VisionInspection:api-part-vision-preview",
                    args=[self.part.pk],
                ),
                {"side": "FRONT", "model_id": self.model.pk},
            )
            self.assertEqual(invalid.status_code, 409)
            self.assertIn("ROI", invalid.json()["error"])

        predict.assert_not_called()

    def test_inspection_route_does_not_disclose_unknown_session(self):
        import uuid

        response = self.client.post(
            reverse(
                "VisionInspection:workflow-inspect-side",
                kwargs={"side": "front"},
            ),
            data=json.dumps({"session_id": str(uuid.uuid4())}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("permission", response.json()["message"].lower())


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
                self.assertIn("/vision-inspection/login/", r.url, name)

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
    """Regression guards for the staged Airbag Stitch operator page."""

    URL = "/vision-inspection/inspection/"

    def setUp(self):
        self.client, self.operator = _role_client("Operator", "WORKFLOW-OPERATOR")

    # Required operator controls and workflow targets.
    TOOLBAR_IDS = ["wsApp", "wsError"]
    WORKFLOW_IDS = [
        "wfScanForm", "wfBarcode", "wfScanBtn", "wfStartInspectionBtn",
        "wfReturnToInspectionBtn",
        "wfScanState", "wfScanDot", "wfScanResult", "wfPartConfigLink",
        "wfModelState", "wfModelDot", "wfModelDetail", "wfPartInfo", "wfStageScan", "wfStageFront",
        "wfStageBack", "wfStageSaving", "wfStageFinal", "wfFrontCameraStatus",
        "feedFront", "ovFront", "wfFrontResult", "wfFrontRetryCamera", "wfFrontSubmitBtn",
        "wfBackCameraStatus", "feedBack", "ovBack", "wfBackResult",
        "wfBackRetryCamera", "wfBackSubmitBtn", "wfInactivityCountdown", "wfScreenSaver",
        "wfSavingTitle", "wfSavingMessage", "wfRetrySaveBtn", "wfFinalTitle", "wfFinalPart",
        "wfFinalId", "wfFinalTime", "wfFinalFront", "wfFinalBack", "wfFinalSaved",
        "wfFinalMessage", "wfNewInspectionBtn", "wfChangePartBtn",
    ]

    def _page(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _all_ids(self):
        return list(self.TOOLBAR_IDS) + list(self.WORKFLOW_IDS)

    def test_page_renders_for_an_operator(self):
        self.assertEqual(self.client.get(self.URL).status_code, 200)

    def test_inspection_header_navigation_remains_available_for_camera_stage(self):
        from django.conf import settings
        from django.urls import reverse

        html = self._page()
        css_path = (
            __import__("pathlib").Path(settings.BASE_DIR)
            / "VisionInspection"
            / "static"
            / "VisionInspection"
            / "css"
            / "inspection_workspace.css"
        )
        css = css_path.read_text(encoding="utf-8")

        self.assertIn('class="vi-ins-header-home"', html)
        self.assertEqual(html.count('aria-label="Home dashboard"'), 1)
        self.assertIn(
            f'href="{reverse("VisionInspection:dashboard")}" class="vi-ins-header-home"',
            html,
        )
        self.assertNotIn('class="vi-header-home"', html)
        self.assertIn('class="vi-header-inspect"', html)
        self.assertIn("Airbag Stitch Inspection", html)
        self.assertIn("vi-camera-fullscreen .vi-sidebar", css)
        self.assertIn("body.vi-camera-fullscreen .vi-top-header", css)
        self.assertNotIn("body.vi-camera-fullscreen .vi-sidebar,\nbody.vi-camera-fullscreen .vi-top-header", css)
        self.assertIn("inspection_workspace.css?v=20261022-1", html)

    def test_camera_result_status_uses_backend_side_status_and_verdict(self):
        from django.conf import settings

        root = __import__("pathlib").Path(settings.BASE_DIR) / "VisionInspection"
        script = (root / "static" / "VisionInspection" / "js" / "inspection_workflow_ui.js").read_text(encoding="utf-8")
        css = (root / "static" / "VisionInspection" / "css" / "inspection_workspace.css").read_text(encoding="utf-8")

        self.assertIn('setResult("Front", progress.front_status, progress.front_verdict, progress.inspection_status)', script)
        self.assertIn('normalizedOverall === "INSPECTING"', script)
        self.assertIn('normalizedOverall === "OKAY PART"', script)
        self.assertIn('normalizedOverall === "NOT OKAY PART"', script)
        self.assertIn('normalizedStatus === "ERROR" || normalizedVerdict === "ERROR"', script)
        self.assertIn("function startSessionProgressPolling(side)", script)
        self.assertIn("var progress = await loadProgress(sessionId)", script)
        self.assertIn("startSessionProgressPolling(side)", script)
        self.assertIn("Inspection result received and rendered", script)
        self.assertIn('text: "REQUEST PENDING"', script)
        self.assertIn('setResult(isBack ? "Back" : "Front", "IN_PROGRESS", "PENDING")', script)
        self.assertIn("INSPECTION_REQUEST_TIMEOUT_MS", script)
        for state in ("inspecting", "ok", "not_ok", "error"):
            self.assertIn(".wf-camera-result .wf-side-result.is-" + state, css)

    def test_favicon_is_served_at_site_root(self):
        response = self.client.get(reverse("favicon"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", response.content)

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

    def test_next_cycle_revalidates_and_preserves_same_part(self):
        from django.conf import settings

        html = self._page()
        script = (
            __import__("pathlib").Path(settings.BASE_DIR)
            / "VisionInspection"
            / "static"
            / "VisionInspection"
            / "js"
            / "inspection_workflow_ui.js"
        ).read_text(encoding="utf-8")
        self.assertIn("NEXT INSPECTION CYCLE", html)
        self.assertIn('id="wfChangePartBtn"', html)
        self.assertIn('part_number: state.partNumber', script)
        self.assertIn('await startInspection(button);', script)
        self.assertIn('changePart(byId("wfChangePartBtn"))', script)
        self.assertIn('submitButton.dataset.completed = "false"', script)

    def test_scan_validation_enables_explicit_inspection_start(self):
        from django.conf import settings

        html = self._page()
        script = (
            __import__("pathlib").Path(settings.BASE_DIR)
            / "VisionInspection"
            / "static"
            / "VisionInspection"
            / "js"
            / "inspection_workflow_ui.js"
        ).read_text(encoding="utf-8")
        self.assertIn('id="wfStartInspectionBtn" class="wf-button wf-primary" type="button" disabled', html)
        self.assertIn('addEventListener("submit", scan)', script)
        self.assertIn('addEventListener("click", startInspection)', script)
        self.assertIn("partData.ok !== true", script)
        self.assertIn("modelData.ok !== true", script)
        self.assertIn("state.validatedModelId = Number(modelData.model_id);", script)
        self.assertIn('byId("wfStartInspectionBtn").disabled = false;', script)
        self.assertIn('byId("wfStartInspectionBtn").addEventListener("click", startInspection)', script)
        self.assertIn("partVisionPreview", html)
        self.assertIn("confidence=0.30", script)
        self.assertNotIn("No detections | Total: 0", script)
        self.assertIn("async function cameraStatusRequest()", script)
        self.assertIn("var data = await cameraStatusRequest();", script)
        self.assertIn("function isSessionId(value)", script)
        self.assertIn("!isSessionId(sessionData.session_id)", script)
        self.assertIn("session_id: state.sessionId", script)
        self.assertNotIn('jsonRequest(URL.cameraStatus, "GET")', script)
        self.assertIn("inspection_workflow_ui.js?v=20261017-2", html)
        self.assertIn("state.requestVersion", script)

    def test_layout_uses_grid_and_flex_not_absolute_positioning(self):
        html = self._page()
        self.assertNotIn("position:absolute", html.lower())
        self.assertIn('id="wfStageFront" hidden', html)
        self.assertIn('id="wfStageBack" hidden', html)
        self.assertEqual(html.count('class="wf-camera-frame"'), 2)

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
        """The active camera takes the remaining stage space."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        self.assertIn(".wf-camera-frame", css)
        self.assertIn("flex: 1 1 auto", css)

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

    def test_shared_pages_scroll_except_the_start_inspection_screen(self):
        """Shared chrome scrolls normally; only Start Inspection is fixed."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "admin.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()

        body_rules = re.findall(r"(?m)^body \{([^}]*)\}", css)
        inspection = re.search(r"(?m)^body\.vi-inspection-chrome \{([^}]*)\}", css)
        self.assertTrue(body_rules)
        self.assertIsNotNone(inspection)
        self.assertTrue(any("overflow-y: auto" in rule for rule in body_rules))
        self.assertIn("overflow: hidden", inspection.group(1))

        inspection_css_path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection.css",
        )
        with open(inspection_css_path, encoding="utf-8") as handle:
            inspection_css = handle.read()
        self.assertIn("overflow-y: auto", inspection_css)
        container = re.search(r"\.vi-container \{([^}]*)\}", inspection_css)
        self.assertIsNotNone(container)
        self.assertIn("min-height: 100vh", container.group(1))
        self.assertNotRegex(container.group(1), r"(?m)^\s*height\s*:")

    def test_camera_frame_is_bounded(self):
        """Camera feeds stay contained by the active stage frame."""
        from django.conf import settings

        path = os.path.join(
            settings.BASE_DIR, "VisionInspection", "static",
            "VisionInspection", "css", "inspection_workspace.css",
        )
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        cam = re.search(r"\.wf-camera-frame \{(.*?)\}", css, re.S)
        self.assertIsNotNone(cam)
        self.assertIn("overflow: hidden", cam.group(1))

        feed = re.search(r"\.wf-camera-frame img \{(.*?)\}", css, re.S)
        self.assertIsNotNone(feed)
        self.assertIn("object-fit: contain", feed.group(1))

    def test_light_controls_are_disabled_by_default(self):
        """Unrelated illumination controls are removed from the operator page."""
        html = self._page()
        for element_id in ("wsLightUp", "wsLightDn"):
            self.assertNotIn('id="%s"' % element_id, html)

    def test_submit_is_disabled_until_validation(self):
        """Side submissions remain disabled until their camera is ready."""
        html = self._page()
        self.assertRegex(html, r'id="wfFrontSubmitBtn"[^>]*disabled')
        self.assertRegex(html, r'id="wfBackSubmitBtn"[^>]*disabled')

    def test_application_flow_steps_are_present(self):
        html = self._page()
        for stage in ("Scan", "Front", "Back", "Saving", "Final"):
            self.assertIn('id="wfStage%s"' % stage, html)

    def test_legend_shows_no_unconfigured_classes(self):
        """Model legends and detection tables do not take space from the feed."""
        html = self._page()
        self.assertNotIn("Segmentation Legend", html)
        self.assertNotIn('id="wfDetFront"', html)
        self.assertNotIn('id="wfDetBack"', html)

    def test_reference_layout_regions_are_present(self):
        """The operator sees only controls for the requested workflow stages."""
        html = self._page()
        for text in (
            "Scan Part", "Front Camera", "Back Camera", "SUBMIT FRONT",
            "SUBMIT BACK", "REQUEST PENDING", "NEXT INSPECTION CYCLE",
        ):
            self.assertIn(text, html, text)

    def test_no_template_comment_leaks_into_html(self):
        """A Django comment must never reach the browser as visible text.

        Multi-line ``{# ... #}`` comments are not stripped by this Django
        version, so they render as literal text inside the page.  Any comment
        therefore has to be single-line â€” this guards the whole app.
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
    def setUp(self):
        self.client, self.admin = _role_client("Administrator", "CONFIG-ADMIN")

    def _configuration(self, mode="FRONT_BACK", side="BOTH"):
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
            front_camera_index=0,
            back_camera_index=0,
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
            is_required=True,
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

    def test_common_roi_is_reused_by_new_version_with_matching_cameras(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.part_configuration import active_roi

        previous = self._configuration()
        previous_roi = previous.rois.get(is_active=True)
        previous.is_active = False
        previous.save(update_fields=["is_active"])
        current = VisionPartConfiguration.objects.create(
            part=previous.part,
            version=previous.version + 1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_source=previous.front_camera_source,
            front_camera_index=previous.front_camera_index,
            back_camera_source=previous.front_camera_source,
            back_camera_index=previous.front_camera_index,
            is_active=True,
        )

        resolved_roi = active_roi(current)

        self.assertEqual(resolved_roi.pk, previous_roi.pk)
        workflow_roi = InspectionWorkflow()._configuration_roi(current)
        self.assertTrue(workflow_roi["inherited"])
        self.assertEqual(workflow_roi["configuration_id"], previous.pk)
        self.assertEqual(workflow_roi["x"], previous_roi.x / previous_roi.ref_width)

        response = self.client.get(
            reverse(
                "VisionInspection:api-part-vision-roi",
                args=[previous.part.pk],
            ),
            {"configuration_id": current.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])
        self.assertEqual(response.json()["roi"]["id"], previous_roi.pk)

    def test_common_roi_is_not_reused_when_camera_role_changes(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.part_configuration import active_roi

        previous = self._configuration()
        previous_roi = previous.rois.get(is_active=True)
        current = VisionPartConfiguration.objects.create(
            part=previous.part,
            version=previous.version + 1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_index=1,
            back_camera_index=1,
            is_active=True,
        )

        self.assertIsNone(active_roi(current))
        self.assertNotEqual(previous_roi.configuration_id, current.pk)

    def test_invalid_previous_roi_is_not_reused(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.part_configuration import active_roi

        previous = self._configuration()
        previous_roi = previous.rois.get(is_active=True)
        previous_roi.x = previous_roi.ref_width - 10
        previous_roi.save(update_fields=["x"])
        current = VisionPartConfiguration.objects.create(
            part=previous.part,
            version=previous.version + 1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            front_camera_index=0,
            back_camera_index=0,
            is_active=True,
        )

        self.assertIsNone(active_roi(current))

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

    def test_rejects_common_roi_across_different_camera_roles(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration()
        configuration.back_camera_index = 1
        configuration.save(update_fields=["back_camera_index"])
        errors = validate_configuration(configuration, {1: ["stitch"]})
        self.assertTrue(any("same camera source" in error for error in errors))

    def test_common_rpi_roi_does_not_depend_on_opencv_index(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration()
        configuration.front_camera_source = VisionPartConfiguration.SOURCE_RPI
        configuration.back_camera_source = VisionPartConfiguration.SOURCE_RPI
        configuration.back_camera_index = configuration.front_camera_index + 1
        configuration.save(
            update_fields=["front_camera_source", "back_camera_source", "back_camera_index"]
        )
        errors = validate_configuration(configuration, {1: ["stitch"]})
        self.assertFalse(any("common ROI requires" in error for error in errors))

    def test_rejects_required_side_without_a_required_class_rule(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration()
        rule = configuration.part_models.first().class_configs.first()
        rule.is_required = False
        rule.save(update_fields=["is_required"])

        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertTrue(any("required stitch class" in error for error in errors))

    def test_required_presence_rule_allows_zero_expected_count_for_both_sides(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_BACK", side="BOTH")
        configuration.acceptance_rule = "presence"
        configuration.save(update_fields=["acceptance_rule"])
        rule = configuration.part_models.first().class_configs.first()
        rule.acceptance_rule = "presence"
        rule.expected_front = 0
        rule.expected_back = 0
        rule.save(update_fields=["acceptance_rule", "expected_front", "expected_back"])

        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertEqual(errors, [])

    def test_required_count_rule_still_rejects_zero_expected_count(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_BACK", side="BOTH")
        rule = configuration.part_models.first().class_configs.first()
        rule.acceptance_rule = "count"
        rule.expected_front = 0
        rule.expected_back = 0
        rule.save(update_fields=["acceptance_rule", "expected_front", "expected_back"])

        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertFalse(any("all optional" in error for error in errors))
        self.assertTrue(any("no expected count for the front side" in error for error in errors))
        self.assertTrue(any("no expected count for the back side" in error for error in errors))

    def test_front_only_requires_front_but_not_back(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_ONLY", side="FRONT")
        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertFalse(any("Back inspection" in error for error in errors))

    def test_back_only_requires_back_but_not_front(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="BACK_ONLY", side="BACK")
        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertFalse(any("Front inspection" in error for error in errors))

    def test_front_back_requires_both_sides(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_BACK", side="BOTH")
        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertEqual(errors, [])

    def test_front_back_error_names_missing_back_assignment(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_BACK", side="FRONT")
        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertTrue(any(
            "No enabled class rule is assigned to the Back inspection" in error
            and "stitch" in error
            for error in errors
        ))

    def test_optional_front_rule_error_names_class_and_action(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_ONLY", side="FRONT")
        rule = configuration.part_models.first().class_configs.first()
        rule.is_required = False
        rule.save(update_fields=["is_required"])

        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertTrue(any(
            "'stitch'" in error and "mark at least one Front or Both class as Required" in error
            for error in errors
        ))

    def test_disabled_model_does_not_satisfy_required_side(self):
        from VisionInspection.services.part_configuration import validate_configuration

        configuration = self._configuration(mode="FRONT_ONLY", side="FRONT")
        part_model = configuration.part_models.first()
        part_model.is_enabled = False
        part_model.save(update_fields=["is_enabled"])

        errors = validate_configuration(configuration, {1: ["stitch"]})

        self.assertTrue(any("At least one enabled model" in error for error in errors))
        self.assertTrue(any("No enabled class rule is assigned to the Front" in error for error in errors))

    def test_workflow_returns_persisted_mode_aware_configuration_errors(self):
        from VisionInspection.services.inspection_workflow import InspectionWorkflow

        configuration = self._configuration(mode="FRONT_BACK", side="FRONT")
        workflow = InspectionWorkflow()
        with mock.patch.object(
            workflow._model_registry,
            "inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": [{"id": 0, "name": "stitch"}],
            },
        ):
            result = workflow.validate_model(part_number=configuration.part.part_number)
            repeated = workflow.validate_model(
                part_number=configuration.part.part_number
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "configuration_invalid")
        self.assertEqual(result["configuration_id"], configuration.pk)
        self.assertEqual(result["configuration_version"], configuration.version)
        self.assertTrue(any("Back inspection" in error for error in result["validation_errors"]))
        configuration.refresh_from_db()
        self.assertEqual(
            configuration.status,
            configuration.STATUS_VALIDATION_FAILED,
        )
        self.assertFalse(configuration.is_active)
        self.assertEqual(configuration.validation_errors, result["validation_errors"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["state"], "configuration_invalid")
        self.assertEqual(
            repeated["configuration_version"],
            configuration.version,
        )
        self.assertEqual(
            repeated["validation_errors"],
            result["validation_errors"],
        )
        response = self.client.get(
            reverse(
                "VisionInspection:api-part-vision-configuration",
                args=[configuration.part_id],
            )
        )
        self.assertEqual(
            response.json()["configuration"]["validation_errors"],
            result["validation_errors"],
        )

    def test_workflow_validates_active_configuration_not_a_newer_draft(self):
        from VisionInspection.models import VisionPartConfiguration
        from VisionInspection.services.inspection_workflow import InspectionWorkflow

        active = self._configuration(mode="FRONT_BACK", side="BOTH")
        VisionPartConfiguration.objects.create(
            part=active.part,
            version=active.version + 1,
            inspection_mode=VisionPartConfiguration.MODE_FRONT_BACK,
            status=VisionPartConfiguration.STATUS_VALIDATION_FAILED,
            is_active=False,
        )
        workflow = InspectionWorkflow()
        with mock.patch.object(
            workflow._model_registry,
            "inspect",
            return_value={
                "ok": True,
                "task": "segment",
                "classes": [{"id": 0, "name": "stitch"}],
            },
        ):
            result = workflow.validate_model(part_number=active.part.part_number)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["configuration_id"], active.pk)
        self.assertEqual(result["configuration_version"], active.version)


class StationDuplicateSubmissionTests(TestCase):
    """Acceptance: a duplicate inspection submission is refused on the server."""

    def tearDown(self):
        CameraService.stop_camera()
        super().tearDown()

    def setUp(self):
        self.client, self.operator = _role_client("Operator", "SUBMIT-OPERATOR")

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

                # A second submit for the same part is refused â€” no duplicate
                # inspection record is created.
                second = self._submit(self.client)
                self.assertEqual(second.status_code, 400)
                self.assertFalse(second.json()["ok"])
                self.assertEqual(second.json()["state"], "duplicate_session")

        self.assertEqual(VisionInspectionSession.objects.count(), 1)

    def test_next_part_can_be_submitted_by_the_operator(self):
        """After a session is cancelled, the authenticated station can inspect the next part."""
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


# =========================================================================== #
#  Dashboard + Inspection Report — content served from real database records
# =========================================================================== #

from datetime import datetime, timedelta, timezone as dt_timezone  # noqa: E402
from django.test import Client  # noqa: E402

from VisionInspection.models import VisionInspectionSession  # noqa: E402


def _record(part_number="P-1", result="OK", employee_code="E001", **kw):
    """One stored inspection record (the only data source of both pages)."""
    defaults = dict(
        part_number=part_number,
        employee_code=employee_code,
        preset_name="Preset A",
        preset_version=1,
        result=result,
        roi_results=[],
        detected_objects=[],
    )
    defaults.update(kw)
    return VisionInspectionLog.objects.create(**defaults)


def _records(part_numbers, results, employee_code="E001", base=None, **kw):
    """Records with predictable timestamps — the newest one is created last."""
    created = []
    if base is None:
        base = datetime(2026, 6, 24, 9, 0, tzinfo=dt_timezone.utc)
    for index, (part_number, result) in enumerate(zip(part_numbers, results)):
        record = _record(
            part_number=part_number,
            result=result,
            employee_code=employee_code,
            **kw,
        )
        VisionInspectionLog.objects.filter(pk=record.pk).update(
            timestamp=base + timedelta(minutes=index)
        )
        record.refresh_from_db()
        created.append(record)
    return created


def _card_values(html, body_marker):
    """``{card label: value}`` of the summary cards of one page body."""
    start = html.index(body_marker)
    block = html[start:]
    values = {}
    for match in re.finditer(
        r'vi-kpi-value">([^<]+)</div>\s*<div class="vi-kpi-label">([^<]+)</div>',
        block,
    ):
        values[match.group(2).strip()] = match.group(1).strip()
    return values


def _dashboard_card_values(html):
    """Summary values rendered by the professional dashboard KPI cards."""
    return {
        match.group(1).strip(): match.group(2).strip()
        for match in re.finditer(
            r'class="vi-dash-kpi-label">([^<]+)</p>\s*'
            r'<p class="vi-dash-kpi-value">([^<]+)</p>',
            html,
        )
    }


class DashboardPageTests(TestCase):
    """Acceptance — the Dashboard shows the real records, sidebar unchanged."""

    def setUp(self):
        self.client, _user = _staff_client()
        VisionInspectionPart.objects.create(part_number="P-1", part_name="Airbag Front")
        VisionInspectionPart.objects.create(part_number="P-2", part_name="Airbag Rear")
        self.records = _records(
            ["P-1", "P-2", "P-1", "P-2"],
            ["OK", "OK", "NOT_OK", "ERROR"],
        )

    def test_heading_and_sidebar_stay_unchanged(self):
        r = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Inspection Overview")
        # The shared chrome is rendered by ui/base.html, not by this page:
        # sidebar (with Dashboard highlighted), top navigation and workspace link.
        self.assertContains(r, 'id="viSidebar"')
        self.assertContains(r, 'class="vi-sidebar-toggle"')
        self.assertContains(r, 'class="vi-header-title"')
        self.assertContains(
            r, 'href="/vision-inspection/" class="vi-sidebar-link active"'
        )
        self.assertContains(r, 'class="vi-sidebar-collapse" id="section-home"')
        self.assertContains(r, 'href="/vision-inspection/inspection/"')

    def test_summary_cards_use_database_counts_and_percentages(self):
        r = self.client.get(reverse("VisionInspection:dashboard"))
        html = r.content.decode()
        values = _dashboard_card_values(html)
        self.assertEqual(values["Total Inspections"], "4")
        self.assertEqual(values["Total OK"], "2")
        self.assertEqual(values["Total NOT OK"], "1")
        self.assertEqual(values["Incomplete / Error"], "1")
        self.assertEqual(len(values), 4)
        self.assertContains(r, "Quality rate: 50.0%")
        self.assertContains(r, "25.0% of inspections")
        self.assertEqual(
            len(re.findall(r'<article class="vi-card vi-dash-kpi"', html)),
            4,
        )
        self.assertContains(r, "Incomplete / Error")
        self.assertContains(r, "25.0%")
        self.assertContains(r, "Recent Inspections")
        self.assertContains(r, "data-report-row")
        self.assertContains(r, "View Inspection Report")
        self.assertEqual(html.count('<th scope="col">'), 7)
        self.assertContains(r, "<table class=\"vi-dash-recent-table\">")
        self.assertContains(r, "<th scope=\"col\">Part Number</th>")
        self.assertContains(r, "<th scope=\"col\">Part Name</th>")
        self.assertEqual(
            [row["log"].pk for row in r.context["recent_rows"]],
            [record.pk for record in reversed(self.records)],
        )

    def test_part_filter_restricts_cards_and_both_charts(self):
        r = self.client.get(reverse("VisionInspection:dashboard"), {"part": "P-1"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        values = _dashboard_card_values(html)
        self.assertEqual(values["Total Inspections"], "2")
        self.assertEqual(values["Total OK"], "1")
        self.assertEqual(values["Total NOT OK"], "1")
        self.assertContains(r, "Showing results for P-1")
        self.assertEqual(html.count('class="vi-dash-bar-row"'), 1)
        self.assertContains(r, 'data-part-number="P-1"')
        self.assertContains(r, 'aria-label="1 OK"')
        self.assertContains(r, 'aria-label="1 NOT OK"')
        self.assertContains(r, "--ok-sweep: 180.0deg")
        self.assertContains(r, "--classified-sweep: 360.0deg")

        reset = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(
            _dashboard_card_values(reset.content.decode())["Total Inspections"],
            "4",
        )

    def test_part_chart_aggregates_real_results_without_showing_records(self):
        _records(
            ["P-1", "P-1"], ["OK", "NOT_OK"],
            base=datetime(2026, 6, 24, 10, 0, tzinfo=dt_timezone.utc),
        )
        r = self.client.get(reverse("VisionInspection:dashboard"))
        html = r.content.decode()
        rows = re.findall(
            r'<div class="vi-dash-bar-row"[^>]*data-part-number="([^"]+)">',
            html,
        )
        self.assertEqual(rows, ["P-1", "P-2"])
        self.assertIn('data-part-number="P-1"', html)
        self.assertIn('data-part-number="P-2"', html)
        self.assertEqual(html.count('aria-label="2 OK"'), 1)
        self.assertEqual(html.count('aria-label="2 NOT OK"'), 1)
        self.assertEqual(html.count("data-report-row"), 6)

    def test_part_dropdown_lists_database_parts(self):
        r = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertContains(r, ">All Parts</option>")
        self.assertContains(r, "P-1 — Airbag Front")
        self.assertContains(r, "P-2 — Airbag Rear")

    def test_removed_analytics_are_gone(self):
        r = self.client.get(reverse("VisionInspection:dashboard"))
        for removed in (
            "viDashRoot", "viDashTrend", "viDashDonut", "viDashAlerts",
            "Real-Time Inspection Flow", "AI Inspection Results", "Quick Actions",
            "System Status", "Welcome back",
        ):
            self.assertNotContains(r, removed)

    def test_empty_state_without_records(self):
        VisionInspectionLog.objects.all().delete()
        r = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            _dashboard_card_values(r.content.decode()),
            {
                "Total Inspections": "0",
                "Total OK": "0",
                "Total NOT OK": "0",
                "Incomplete / Error": "0",
            },
        )
        self.assertContains(r, "Quality rate: 0.0%")
        self.assertContains(r, "No inspection results yet")
        self.assertContains(r, "No inspection data available")
        self.assertContains(r, "No inspections match the current filter.")
        self.assertContains(r, "--ok-sweep: 0deg")


class InspectionReportTests(TestCase):
    """Acceptance — the Inspection Report filters, paginates and exports."""

    def setUp(self):
        self.client, _user = _staff_client()
        self.part_one = VisionInspectionPart.objects.create(
            part_number="P-1", part_name="Airbag Front"
        )
        self.part_two = VisionInspectionPart.objects.create(
            part_number="P-2", part_name="Cover Panel"
        )
        # 12 records (8 OK / 4 NOT OK) + 1 session record below = 13 records.
        self.records = _records(
            ["P-1"] * 6 + ["P-2"] * 6,
            ["OK", "OK", "NOT_OK"] * 4,
        )
        # Session-derived record: per-side verdicts and a scanned unique number.
        self.session = VisionInspectionSession.objects.create(
            part_number="P-1", barcode="U-000123"
        )
        self.session.front_verdict = "OK"
        self.session.back_verdict = "NOT_OK"
        self.session.front_status = "COMPLETED"
        self.session.back_status = "COMPLETED"
        self.session.save()
        self.session_record = _record(
            part_number="P-1", preset_name=f"SESSION#{self.session.session_id}"
        )
        VisionInspectionLog.objects.filter(pk=self.session_record.pk).update(
            timestamp=datetime(2026, 6, 24, 9, 12, tzinfo=dt_timezone.utc)
        )
        self.session_record.refresh_from_db()

    # -- page ----------------------------------------------------------- #

    def test_anonymous_report_redirects_to_employee_login(self):
        r = Client().get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/vision-inspection/login/", r.url)

    def test_anonymous_filtered_report_redirects_to_employee_login(self):
        r = Client().get(
            reverse("VisionInspection:ui-reports"), {"part_number": "NO-SUCH-PART"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/vision-inspection/login/", r.url)

    def test_jazzmin_search_model_uses_the_registered_app_label(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.test import Client

        self.assertEqual(
            settings.JAZZMIN_SETTINGS["search_model"],
            "vision_inspection.visioninspectionpreset",
        )
        model_admin_url = reverse(
            "admin:vision_inspection_visioninspectionpreset_changelist"
        )
        self.assertEqual(
            model_admin_url, "/admin/vision_inspection/visioninspectionpreset/"
        )
        admin = get_user_model().objects.create_superuser(
            username="superadmin", email="admin@example.com", password="password"
        )
        admin_client = Client()
        self.assertTrue(
            admin_client.login(username=admin.username, password="password")
        )
        self.assertEqual(admin_client.get(model_admin_url).status_code, 200)

    def test_report_shows_header_filters_and_cards(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Inspection Report")
        self.assertContains(r, "View and export Airbag Stitch Inspection records.")
        self.assertContains(r, reverse("VisionInspection:ui-report-export-excel"))
        self.assertContains(r, reverse("VisionInspection:ui-report-export-pdf"))
        for field in (
            "date_from", "date_to", "part", "employee", "result",
            "part_number", "part_name", "unique_number",
        ):
            self.assertContains(r, 'name="%s"' % field)
        values = _card_values(r.content.decode(), "vi-report")
        self.assertEqual(values["Total Inspections"], "13")
        self.assertEqual(values["Total OK"], "9")
        self.assertEqual(values["Total NOT OK"], "4")
        self.assertEqual(values["Incomplete / Error"], "0")

    def test_first_page_is_paginated_with_serial_numbers(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertContains(r, "Showing 1 to 10 of 13 entries")
        rows = re.findall(r'data-report-row[\s\S]*?<td>(\d+)</td>',
                          r.content.decode())
        self.assertEqual(rows, [str(n) for n in range(1, 11)])

    def test_print_table_includes_all_filtered_rows_on_every_page(self):
        response = self.client.get(
            reverse("VisionInspection:ui-reports"), {"page": 2}
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertEqual(len(re.findall(r'data-print-row', body)), 13)
        self.assertEqual(len(re.findall(r'data-report-row', body)), 3)
        self.assertContains(response, "Front Result")
        self.assertContains(response, "Back Result")

        filtered = self.client.get(
            reverse("VisionInspection:ui-reports"),
            {"page": 2, "result": "NOT_OK"},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(
            len(re.findall(r'data-print-row', filtered.content.decode())), 4
        )

    def test_identical_timestamps_use_descending_primary_key(self):
        second = _record(
            part_number="P-1", preset_name="same-timestamp-report-test"
        )
        VisionInspectionLog.objects.filter(pk=second.pk).update(
            timestamp=self.session_record.timestamp
        )
        response = self.client.get(reverse("VisionInspection:ui-reports"))
        page_logs = list(response.context["page_obj"].object_list)
        tied_ids = [
            log.pk for log in page_logs
            if log.timestamp == self.session_record.timestamp
        ]
        self.assertGreaterEqual(len(tied_ids), 2)
        self.assertEqual(tied_ids, sorted(tied_ids, reverse=True))

    def test_page_two_continues_the_serial_numbers(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"), {"page": 2})
        self.assertContains(r, "Showing 11 to 13 of 13 entries")
        rows = re.findall(r'data-report-row[\s\S]*?<td>(\d+)</td>',
                          r.content.decode())
        self.assertEqual(rows, ["11", "12", "13"])

    def test_newest_record_comes_first(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"))
        first_row = re.search(
            r'<tr data-report-row>[\s\S]*?</tr>', r.content.decode()
        ).group(0)
        self.assertIn(str(self.session_record.pk), first_row)
        self.assertContains(r, "U-000123")                       # unique number
        self.assertIn("vi-badge-ok", first_row)                  # front verdict OK
        self.assertContains(r, "NOT OK")                          # back verdict

    def test_eye_icon_opens_inspection_details(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertContains(
            r, reverse("VisionInspection:traceability-detail", args=[self.session_record.pk])
        )

    def test_filters_are_applied_on_the_backend(self):
        cases = [
            ("full listing", {}, "13"),
            ("part dropdown", {"part": "P-2"}, "6"),
            ("part number search", {"part_number": "P-1"}, "7"),
            ("part name search", {"part_name": "Cover"}, "6"),
            ("employee dropdown", {"employee": "E001"}, "13"),
            ("result dropdown", {"result": "NOT_OK"}, "4"),
            ("unique number", {"unique_number": "U-000123"}, "1"),
            ("date from", {"date_from": "2026-06-24"}, "13"),
            ("date to", {"date_to": "2026-06-23"}, "0"),
            ("no match", {"part_number": "NO-SUCH-PART"}, "0"),
        ]
        for label, params, expected in cases:
            with self.subTest(label=label):
                r = self.client.get(reverse("VisionInspection:ui-reports"), params)
                self.assertEqual(r.status_code, 200)
                values = _card_values(r.content.decode(), "vi-report")
                self.assertEqual(values["Total Inspections"], expected)

    def test_result_filter_counts_match_the_table(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"), {"result": "NOT_OK"})
        values = _card_values(r.content.decode(), "vi-report")
        self.assertEqual(values["Total NOT OK"], "4")
        self.assertEqual(values["Total OK"], "0")
        self.assertEqual(
            len(re.findall(r'data-report-row[\s\S]*?</tr>', r.content.decode())), 4
        )

    def test_page_size_selection(self):
        r = self.client.get(reverse("VisionInspection:ui-reports"), {"page_size": 25})
        self.assertContains(r, "Showing 1 to 13 of 13 entries")
        self.assertEqual(
            len(re.findall(r'data-report-row', r.content.decode())), 13
        )

    def test_reset_link_restores_the_full_listing(self):
        filtered = self.client.get(
            reverse("VisionInspection:ui-reports"), {"part": "P-1"}
        )
        self.assertContains(filtered, "Filters applied")
        self.assertContains(filtered, 'href="/vision-inspection/reports/"')

    def test_empty_state_when_nothing_matches(self):
        r = self.client.get(
            reverse("VisionInspection:ui-reports"), {"part_number": "NO-SUCH-PART"}
        )
        self.assertContains(r, "No inspection records found")
        self.assertEqual(
            _card_values(r.content.decode(), "vi-report")["Total Inspections"], "0"
        )


class InspectionReportExportTests(TestCase):
    """Acceptance — exports contain exactly the filtered records."""

    def setUp(self):
        self.client, _user = _staff_client()
        VisionInspectionPart.objects.create(part_number="P-1", part_name="Airbag")
        VisionInspectionPart.objects.create(part_number="P-2", part_name="Cover")
        _records(["P-1"] * 2 + ["P-2"] * 2, ["OK", "NOT_OK"] * 2)
        self.export_url = reverse("VisionInspection:ui-report-export-excel")
        self.pdf_url = reverse("VisionInspection:ui-report-export-pdf")

    def test_exports_need_staff_login(self):
        for url in (self.export_url, self.pdf_url):
            with self.subTest(url=url):
                r = Client().get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/vision-inspection/login/", r.url)

    def test_excel_export_contains_the_filtered_records(self):
        r = self.client.get(self.export_url, {"part": "P-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/vnd.ms-excel")
        self.assertIn("Inspection_Report_", r["Content-Disposition"])
        body = r.content.decode()
        self.assertIn("Inspection Report", body)
        self.assertIn("P-1", body)
        self.assertNotIn("P-2", body)
        self.assertIn("Date &amp; Time", body)

    def test_pdf_export_is_a_valid_pdf_of_the_filtered_records(self):
        r = self.client.get(self.pdf_url, {"part": "P-2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        pdf = r.content
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"P-2", pdf)                     # WinAnsi encoded cell text
        self.assertNotIn(b"P-1", pdf)                  # no other part leaked
        self.assertIn(b"/Type /Page", pdf)

        # The xref table must point at the object headers it declares.
        tail = pdf.rstrip()
        start_xref = int(tail[tail.rfind(b"startxref") + 10:].split()[0])
        self.assertIn(b"xref", pdf[start_xref:start_xref + 4])
        for match in re.finditer(rb"(\d{10}) (\d{5}) n", pdf[start_xref:]):
            offset = int(match.group(1))
            self.assertIn(b" obj", pdf[offset:offset + 40])

    def test_pdf_export_is_paginated_for_many_records(self):
        _records(["P-1"] * 40, ["OK"] * 40)
        r = self.client.get(self.pdf_url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content.count(b"/Type /Page "), 2)   # 44 rows, 26/page
        self.assertIn(b"/Type /Pages /Count 2", r.content)
        self.assertIn(b"Page 1 of 2", r.content)


    def test_exports_ignore_an_invalid_result_value(self):
        r = self.client.get(self.export_url, {"result": "DROP"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("P-1", r.content.decode())
        self.assertIn("P-2", r.content.decode())


class StorageServiceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_root = os.path.join(self.temp.name, "media")
        self.local_root = os.path.join(self.temp.name, "managed")

    def _local_destination(self, name, directory):
        return StorageDestination.objects.create(
            name=name,
            destination_type=StorageDestination.DEST_TYPE_LOCAL,
            destination_directory=directory,
            connection_status="CONNECTED",
        )

    def test_local_connection_test_writes_reads_and_removes_only_its_probe(self):
        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            result = storage_service.test_destination({
                "destination_type": "LOCAL",
                "destination_directory": "station/evidence",
            })
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_ok"])
        self.assertTrue(result["write_ok"])
        self.assertEqual(os.listdir(os.path.join(self.local_root, "station", "evidence")), [])

    def test_smb_connection_test_uses_submitted_password_and_removes_probe(self):
        from contextlib import contextmanager
        from io import BytesIO
        from types import SimpleNamespace

        class MemorySmbClient:
            def __init__(self):
                self.files = {}
                self.registered = None
                self.removed = []
                self.path = SimpleNamespace(exists=lambda path: path in self.files)

            def register_session(self, server, **options):
                self.registered = (server, options)

            def makedirs(self, path, exist_ok=True):
                return None

            @contextmanager
            def open_file(self, path, mode):
                if mode == "xb":
                    stream = BytesIO()
                    yield stream
                    self.files[path] = stream.getvalue()
                else:
                    yield BytesIO(self.files[path])

            def remove(self, path):
                self.removed.append(path)
                self.files.pop(path, None)

        client = MemorySmbClient()
        with mock.patch.object(storage_service, "_smbclient", return_value=client):
            result = storage_service.test_destination({
                "destination_type": "SMB",
                "ip_address": "nas.example.test",
                "share_name": "inspection",
                "destination_directory": "line-a/run-1",
                "username": "operator",
                "password": "not-persisted-by-test",
            })
        self.assertTrue(result["ok"])
        self.assertEqual(client.registered[0], "nas.example.test")
        self.assertEqual(client.registered[1]["password"], "not-persisted-by-test")
        self.assertEqual(len(client.removed), 1)
        self.assertEqual(client.files, {})

    def test_local_activation_preserves_previous_destination(self):
        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            first, _ = storage_service.activate_destination({
                "destination_type": "LOCAL", "destination_directory": "first",
            })
            second, _ = storage_service.activate_destination({
                "destination_type": "LOCAL", "destination_directory": "second",
            })
        first.refresh_from_db()
        config = StorageConfiguration.objects.get(pk=1)
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(config.active_destination_id, second.pk)
        self.assertEqual(StorageDestination.objects.count(), 2)

    def test_new_session_image_gets_stable_reference_and_active_local_destination(self):
        session = VisionInspectionSession.objects.create(part_number="STORE-1")
        destination = self._local_destination("Local", "captured")
        StorageConfiguration.objects.create(pk=1, active_destination=destination)
        image = b"verified inspection evidence"
        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            metadata = storage_service.register_image(
                session, "front_image", image, "front.jpg"
            )
            session.save()
            resolved = storage_service.metadata_for_field(session, "front_image")
            with storage_service.open_image(resolved) as stored:
                self.assertEqual(stored.read(), image)
        self.assertEqual(metadata.destination_id, destination.pk)
        self.assertEqual(metadata.storage_backend, StorageImageMetadata.STORAGE_LOCAL)
        self.assertEqual(len(metadata.storage_key), 32)

    def test_migration_verifies_copy_and_updates_reference_after_copy(self):
        source = self._local_destination("Source", "source")
        destination = self._local_destination("Target", "target")
        contents = b"historical frame" * 100
        key = "a" * 32
        digest = __import__("hashlib").sha256(contents).hexdigest()
        metadata = StorageImageMetadata.objects.create(
            storage_key=key,
            image_field_name="inspection_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination=source,
            relative_path="old.jpg",
            file_size=len(contents),
            sha256_checksum=digest,
        )
        job = StorageMigrationJob.objects.create(
            source_destination=source,
            destination=destination,
            total_count=1,
        )
        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            source_path = storage_service._local_root(source) / "old.jpg"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(contents)
            first_batch = storage_service.process_migration_batch(job.pk)
            second_batch = storage_service.process_migration_batch(job.pk)
            metadata.refresh_from_db()
            target_path = storage_service._local_root(destination) / metadata.relative_path
            self.assertEqual(target_path.read_bytes(), contents)
        self.assertEqual(first_batch["success"], 1)
        self.assertTrue(second_batch["done"])
        self.assertEqual(metadata.destination_id, destination.pk)
        self.assertEqual(metadata.migration_status, StorageImageMetadata.MIGRATION_SUCCESS)
        self.assertEqual(StorageTransferHistory.objects.filter(status="SUCCESS").count(), 1)

    def test_checksum_failure_keeps_original_reference_and_file(self):
        source = self._local_destination("Source", "source")
        destination = self._local_destination("Target", "target")
        metadata = StorageImageMetadata.objects.create(
            storage_key="b" * 32,
            image_field_name="inspection_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination=source,
            relative_path="original.jpg",
            file_size=5,
            sha256_checksum="0" * 64,
        )
        job = StorageMigrationJob.objects.create(source_destination=source, destination=destination, total_count=1)
        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            original = storage_service._local_root(source) / "original.jpg"
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b"valid")
            storage_service.process_migration_batch(job.pk)
            metadata.refresh_from_db()
            self.assertEqual(original.read_bytes(), b"valid")
            target_path = storage_service._local_root(destination) / (metadata.storage_key + ".jpg")
            self.assertFalse(target_path.exists())
        self.assertEqual(metadata.destination_id, source.pk)
        self.assertEqual(metadata.migration_status, StorageImageMetadata.MIGRATION_FAILED)
        self.assertEqual(StorageTransferHistory.objects.filter(status="FAILED").count(), 1)

    def test_unavailable_original_destination_does_not_fall_back_to_local_copy(self):
        destination = StorageDestination.objects.create(
            name="Offline SMB", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="unreachable", share_name="share",
        )
        metadata = StorageImageMetadata.objects.create(
            storage_key="c" * 32,
            image_field_name="inspection_image",
            storage_backend=StorageImageMetadata.STORAGE_NETWORK,
            destination=destination,
            relative_path="frame.jpg",
        )
        with mock.patch.object(storage_service, "_remote_path", side_effect=storage_service.StorageOperationError("offline")):
            with self.assertRaises(storage_service.StorageOperationError):
                storage_service.open_image(metadata)

    def test_automatic_threshold_accepts_10_through_95_and_persists(self):
        config = storage_service.save_automatic_settings(True, 50)
        self.assertTrue(config.automatic_migration_enabled)
        self.assertEqual(config.migration_threshold_pct, 50)
        self.assertEqual(storage_service.save_automatic_settings(False, 60).migration_threshold_pct, 60)
        self.assertEqual(storage_service.save_automatic_settings(True, 80).migration_threshold_pct, 80)
        self.assertEqual(storage_service.save_automatic_settings(True, 95).migration_threshold_pct, 95)
        for invalid in (9, 96, "not-a-number"):
            with self.assertRaises(storage_service.StorageOperationError):
                storage_service.save_automatic_settings(True, invalid)

    def test_automatic_transfer_waits_below_threshold_and_when_disabled(self):
        destination = StorageDestination.objects.create(
            name="Target", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="nas", share_name="images", is_active=True,
        )
        StorageConfiguration.objects.create(pk=1, active_destination=destination, migration_threshold_pct=50)
        with mock.patch.object(storage_service, "_local_volume_usage", return_value={"usage_percent": 49.9, "total_bytes": 100, "free_bytes": 50, "used_bytes": 50}):
            below = storage_service.automatic_transfer_batch()
        self.assertEqual(below["reason"], "below_threshold")
        config = StorageConfiguration.objects.get(pk=1)
        config.automatic_migration_enabled = False
        config.save(update_fields=["automatic_migration_enabled"])
        with mock.patch.object(storage_service, "_local_volume_usage", return_value={"usage_percent": 80, "total_bytes": 100, "free_bytes": 20, "used_bytes": 80}):
            disabled = storage_service.automatic_transfer_batch()
        self.assertEqual(disabled["reason"], "disabled")

    def test_automatic_transfer_starts_at_threshold_and_uses_oldest_local_image(self):
        destination = StorageDestination.objects.create(
            name="Target", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="nas", share_name="images", is_active=True,
        )
        StorageConfiguration.objects.create(pk=1, active_destination=destination, migration_threshold_pct=50)
        oldest = StorageImageMetadata.objects.create(
            storage_key="d" * 32, image_field_name="front_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL, relative_path="old.jpg",
            file_size=3, sha256_checksum="0" * 64,
        )
        StorageImageMetadata.objects.create(
            storage_key="e" * 32, image_field_name="back_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination=self._local_destination("Prior Local", "old"), relative_path="other.jpg",
            file_size=4, sha256_checksum="1" * 64,
        )

        def complete_transfer(metadata, target, delete_originals=False):
            metadata.storage_backend = StorageImageMetadata.STORAGE_NETWORK
            metadata.destination = target
            metadata.migration_status = StorageImageMetadata.MIGRATION_SUCCESS
            metadata.save(update_fields=["storage_backend", "destination", "migration_status", "updated_at"])

        usage = {"usage_percent": 50, "total_bytes": 100, "free_bytes": 50, "used_bytes": 50}
        with mock.patch.object(storage_service, "_local_volume_usage", side_effect=[usage, usage]), \
             mock.patch.object(storage_service, "migrate_image", side_effect=complete_transfer) as transfer:
            result = storage_service.automatic_transfer_batch()
        self.assertTrue(result["started"])
        self.assertEqual(result["success"], 1)
        transfer.assert_called_once()
        self.assertEqual(transfer.call_args.args[0].pk, oldest.pk)

    def test_automatic_transfer_failure_keeps_local_reference_and_records_error(self):
        destination = StorageDestination.objects.create(
            name="Target", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="nas", share_name="images", is_active=True,
        )
        config = StorageConfiguration.objects.create(pk=1, active_destination=destination, migration_threshold_pct=10)
        metadata = StorageImageMetadata.objects.create(
            storage_key="f" * 32, image_field_name="front_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL, relative_path="local.jpg",
            file_size=4, sha256_checksum="2" * 64,
        )
        usage = {"usage_percent": 50, "total_bytes": 100, "free_bytes": 50, "used_bytes": 50}
        with mock.patch.object(storage_service, "_local_volume_usage", side_effect=[usage, usage]), \
             mock.patch.object(storage_service, "migrate_image", side_effect=storage_service.StorageOperationError("share unavailable")):
            result = storage_service.automatic_transfer_batch()
        metadata.refresh_from_db()
        config.refresh_from_db()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(metadata.storage_backend, StorageImageMetadata.STORAGE_LOCAL)
        self.assertEqual(metadata.migration_status, StorageImageMetadata.MIGRATION_FAILED)
        self.assertEqual(metadata.migration_destination_id, destination.pk)
        self.assertIn("share unavailable", config.last_transfer_error)

    def test_failed_automatic_transfer_respects_bounded_retry_backoff(self):
        destination = StorageDestination.objects.create(
            name="Target", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="nas", share_name="images", is_active=True,
        )
        StorageConfiguration.objects.create(pk=1, active_destination=destination, migration_threshold_pct=10)
        StorageImageMetadata.objects.create(
            storage_key="g" * 32, image_field_name="front_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL, relative_path="local.jpg",
            migration_status=StorageImageMetadata.MIGRATION_FAILED,
            migration_destination=destination, migration_attempt=1,
        )
        usage = {"usage_percent": 90, "total_bytes": 100, "free_bytes": 10, "used_bytes": 90}
        with mock.patch.object(storage_service, "_local_volume_usage", side_effect=[usage, usage]), \
             mock.patch.object(storage_service, "migrate_image") as transfer:
            result = storage_service.automatic_transfer_batch()
        self.assertFalse(result["started"])
        transfer.assert_not_called()

    def test_old_local_destination_is_not_automatically_redirected(self):
        old_local = self._local_destination("Previous local", "archive")
        active = StorageDestination.objects.create(
            name="New SMB", destination_type=StorageDestination.DEST_TYPE_SMB,
            ip_address="nas", share_name="new-share", is_active=True,
        )
        StorageConfiguration.objects.create(pk=1, active_destination=active, migration_threshold_pct=10)
        StorageImageMetadata.objects.create(
            storage_key="h" * 32, image_field_name="front_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination=old_local, relative_path="old.jpg",
        )
        usage = {"usage_percent": 90, "total_bytes": 100, "free_bytes": 10, "used_bytes": 90}
        with mock.patch.object(storage_service, "_local_volume_usage", side_effect=[usage, usage]), \
             mock.patch.object(storage_service, "migrate_image") as transfer:
            result = storage_service.automatic_transfer_batch()
        self.assertFalse(result["started"])
        transfer.assert_not_called()

    def test_database_update_failure_after_copy_keeps_source_file(self):
        source = self._local_destination("Source", "source")
        destination = self._local_destination("Target", "target")
        contents = b"preserve until reference commits"
        metadata = StorageImageMetadata.objects.create(
            storage_key="i" * 32,
            image_field_name="inspection_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination=source,
            relative_path="source.jpg",
            file_size=len(contents),
            sha256_checksum=__import__("hashlib").sha256(contents).hexdigest(),
        )
        real_save = StorageImageMetadata.save

        def fail_destination_update(instance, *args, **kwargs):
            if instance.pk == metadata.pk and instance.destination_id == destination.pk:
                raise RuntimeError("simulated database update failure")
            return real_save(instance, *args, **kwargs)

        with override_settings(MEDIA_ROOT=self.media_root, VISION_STORAGE_LOCAL_ROOT=self.local_root):
            source_path = storage_service._local_root(source) / "source.jpg"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(contents)
            with mock.patch.object(StorageImageMetadata, "save", new=fail_destination_update):
                with self.assertRaisesRegex(RuntimeError, "database update failure"):
                    storage_service.migrate_image(metadata, destination, delete_originals=True)
            metadata.refresh_from_db()
            self.assertEqual(source_path.read_bytes(), contents)
        self.assertEqual(metadata.destination_id, source.pk)

    def test_worker_lock_rejects_a_second_worker(self):
        from django.core.management.base import CommandError
        from VisionInspection.management.commands.run_storage_worker import BaseWorkerLock

        with mock.patch("VisionInspection.management.commands.run_storage_worker.tempfile.gettempdir", return_value=self.temp.name):
            first = BaseWorkerLock()
            second = BaseWorkerLock()
            with mock.patch.object(BaseWorkerLock, "_pid_is_running", return_value=True):
                first.acquire()
                try:
                    with self.assertRaises(CommandError):
                        second.acquire()
                finally:
                    first.release()

    def test_smb_credential_wrapper_never_returns_plaintext_storage_value(self):
        import base64

        class FakeFernet:
            def encrypt(self, value):
                return base64.urlsafe_b64encode(value)

            def decrypt(self, value):
                return base64.urlsafe_b64decode(value)

        with mock.patch.object(storage_service, "_credential_fernet", return_value=FakeFernet()):
            encrypted = storage_service.encrypt_credential("sample-password")
            self.assertTrue(encrypted.startswith(storage_service.CREDENTIAL_PREFIX))
            self.assertNotIn("sample-password", encrypted)
            self.assertEqual(storage_service.decrypt_credential(encrypted), "sample-password")
