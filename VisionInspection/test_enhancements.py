from django.contrib.auth.models import Group, User
from django.test import TestCase, Client
from django.urls import reverse

from VisionInspection.models import (
    VisionInspectionLog,
    VisionInspectionSession,
    VisionInspectionUserProfile,
    StorageImageMetadata,
)
from VisionInspection.services.access_control import (
    ROLE_OPERATOR, ROLE_QUALITY, ROLE_SUPERVISOR, ROLE_ADMIN,
)


class AccountAndRoleAcceptanceTests(TestCase):
    def make_user(self, username, role, codes=None, password="CorrectHorseBattery77"):
        user = User.objects.create_user(username=username, password=password)
        user.groups.add(Group.objects.get(name=role))
        VisionInspectionUserProfile.objects.create(
            user=user, station_scope=", ".join(codes or [])
        )
        return user

    def test_employee_id_login_uses_django_password_hash_and_logout(self):
        user = self.make_user("E-101", ROLE_OPERATOR)
        self.assertNotEqual(user.password, "CorrectHorseBattery77")
        self.assertTrue(user.check_password("CorrectHorseBattery77"))
        response = self.client.post(reverse("VisionInspection:login"), {
            "username": " e-101 ", "password": "CorrectHorseBattery77",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("VisionInspection:inspection"))
        self.assertTrue(self.client.session.get("_auth_user_id"))
        response = self.client.post(reverse("VisionInspection:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_form_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("VisionInspection:login"), {
            "username": "E-103", "password": "unused",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get(reverse("VisionInspection:login")).status_code, 200)
        self.assertIn("csrftoken", client.cookies)

    def test_quality_cannot_post_user_management_actions(self):
        quality = self.make_user("Q-2", ROLE_QUALITY)
        self.client.force_login(quality)
        response = self.client.post(reverse("VisionInspection:user-management"), {
            "action": "create", "employee_id": "E-999", "password": "ForbiddenPasswordValid88",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="E-999").exists())

    def test_inactive_account_cannot_log_in(self):
        user = self.make_user("E-102", ROLE_OPERATOR)
        user.is_active = False
        user.save()
        response = self.client.post(reverse("VisionInspection:login"), {
            "username": user.username, "password": "CorrectHorseBattery77",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inactive")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_operator_reports_are_limited_to_own_employee_id(self):
        operator = self.make_user("E-103", ROLE_OPERATOR)
        VisionInspectionLog.objects.create(part_number="OWN-PART", employee_code="E-103", result="OK")
        VisionInspectionLog.objects.create(part_number="OTHER-PART", employee_code="E-999", result="OK")
        self.client.force_login(operator)
        response = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OWN-PART")
        self.assertNotContains(response, "OTHER-PART")

    def test_supervisor_reports_are_limited_to_assigned_employee_ids(self):
        supervisor = self.make_user("S-1", ROLE_SUPERVISOR, ["STATION-A"])
        team_session = VisionInspectionSession.objects.create(part_number="P-TEAM", employee_code="E-201", station_id="STATION-A")
        other_session = VisionInspectionSession.objects.create(part_number="P-OTHER", employee_code="E-202", station_id="STATION-B")
        VisionInspectionLog.objects.create(part_number="TEAM-PART", employee_code="E-201", session=team_session, result="NOT_OK")
        VisionInspectionLog.objects.create(part_number="OTHER-PART", employee_code="E-202", session=other_session, result="OK")
        self.client.force_login(supervisor)
        response = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TEAM-PART")
        self.assertNotContains(response, "OTHER-PART")

    def test_quality_can_view_reports_but_cannot_change_configuration_or_manage_users(self):
        quality = self.make_user("Q-1", ROLE_QUALITY)
        self.client.force_login(quality)
        self.assertEqual(self.client.get(reverse("VisionInspection:ui-reports")).status_code, 200)
        self.assertEqual(self.client.get(reverse("VisionInspection:user-management")).status_code, 403)
        self.assertEqual(self.client.get(reverse("VisionInspection:ui-settings")).status_code, 403)

    def test_operator_cannot_fetch_another_employees_image_by_direct_url(self):
        owner = self.make_user("E-250", ROLE_OPERATOR)
        other = self.make_user("E-251", ROLE_OPERATOR)
        session = VisionInspectionSession.objects.create(
            part_number="P-IMG", employee_code=owner.username, operator=owner,
        )
        metadata = StorageImageMetadata.objects.create(
            storage_key="a" * 32, inspection_session=session,
            image_field_name="front_image", relative_path="missing/evidence.jpg",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
        )
        self.client.force_login(other)
        response = self.client.get(reverse("VisionInspection:storage-image", args=[metadata.pk]))
        self.assertEqual(response.status_code, 403)

    def test_operator_cannot_open_another_employees_traceability_detail(self):
        operator = self.make_user("E-301", ROLE_OPERATOR)
        session = VisionInspectionSession.objects.create(
            part_number="P-1", employee_code="E-999", front_status="COMPLETED",
            front_verdict="OK", back_status="SKIPPED", back_verdict="NOT_REQUIRED",
        )
        log = VisionInspectionLog.objects.create(
            part_number="P-1", employee_code="E-999", session=session, result="OK",
            preset_name=f"SESSION#{session.session_id}",
        )
        self.client.force_login(operator)
        response = self.client.get(reverse("VisionInspection:traceability-detail", args=[log.pk]))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_employee_account_without_plaintext_password_storage(self):
        admin = User.objects.create_superuser(username="ADMIN-1", password="AdminPasswordValid99")
        self.client.force_login(admin)
        response = self.client.post(reverse("VisionInspection:user-management"), {
            "action": "create", "employee_id": " e-401 ",
            "password": "EmployeePasswordValid99", "role": ROLE_OPERATOR,
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="E-401")
        self.assertTrue(user.check_password("EmployeePasswordValid99"))
        self.assertNotEqual(user.password, "EmployeePasswordValid99")
        self.assertTrue(user.groups.filter(name=ROLE_OPERATOR).exists())


class SessionTraceabilityAcceptanceTests(TestCase):

    def test_workflow_save_preserves_incomplete_instead_of_relabeling_not_ok(self):
        from VisionInspection.services.inspection_workflow import InspectionWorkflow
        session = VisionInspectionSession.objects.create(
            part_number="AIR-2", employee_code="E-502", front_status="COMPLETED",
            front_verdict="INCOMPLETE", back_status="SKIPPED",
            back_verdict="NOT_REQUIRED", overall="INCOMPLETE",
        )
        result = InspectionWorkflow().save(str(session.session_id))
        self.assertTrue(result["ok"], result)
        log = VisionInspectionLog.objects.get(pk=result["inspection_id"])
        self.assertEqual(log.result, "INCOMPLETE")
        self.assertEqual(log.session_id, session.pk)

    def test_session_and_report_preserve_incomplete_and_side_evidence_fields(self):
        session = VisionInspectionSession.objects.create(
            part_number="AIR-1", barcode="SERIAL-77", employee_code="E-501",
            front_status="COMPLETED", front_verdict="OK",
            front_detections=[{"class": "perimeter", "confidence": 0.97}],
            front_results=[{"stitch": "perimeter", "actual_count": 1, "expected_count": 1, "status": "OK"}],
            back_status="ERROR", back_verdict="ERROR", overall="INCOMPLETE",
        )
        log = VisionInspectionLog.objects.create(
            part_number="AIR-1", barcode="SERIAL-77", employee_code="E-501",
            session=session, preset_name=f"SESSION#{session.session_id}", result="INCOMPLETE",
        )
        self.assertEqual(log.session, session)
        self.assertEqual(log.result, "INCOMPLETE")
        self.assertEqual(session.front_detections[0]["class"], "perimeter")
        user = User.objects.create_user("E-501", password="StrongPasswordForTest11")
        user.groups.add(Group.objects.get(name=ROLE_OPERATOR))
        VisionInspectionUserProfile.objects.create(user=user)
        self.client.force_login(user)
        response = self.client.get(reverse("VisionInspection:traceability-detail", args=[log.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INCOMPLETE")
        self.assertContains(response, "Image not captured")
        self.assertContains(response, "Annotation not generated")
        self.assertContains(response, "SERIAL-77")
        report = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "INCOMPLETE")
