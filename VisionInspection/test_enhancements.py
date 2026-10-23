from io import BytesIO
from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase, Client
from django.urls import reverse
from unittest.mock import patch

from VisionInspection.models import (
    AccessGroupProfile,
    VisionInspectionLog,
    VisionInspectionPermissionOverride,
    VisionInspectionPart,
    VisionInspectionSession,
    VisionInspectionUserProfile,
    StorageImageMetadata,
)
from VisionInspection.services.access_control import (
    ROLE_OPERATOR, ROLE_QUALITY, ROLE_SUPERVISOR, ROLE_ADMIN, has_permission,
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

    def test_supervisor_login_lands_on_dashboard(self):
        supervisor = self.make_user("S-LOGIN", ROLE_SUPERVISOR)

        response = self.client.post(reverse("VisionInspection:login"), {
            "username": supervisor.username,
            "password": "CorrectHorseBattery77",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("VisionInspection:dashboard"))
        self.assertEqual(self.client.get(response.url).status_code, 200)

    def test_user_switch_starts_with_a_clean_session(self):
        first = self.make_user("E-SWITCH-1", ROLE_OPERATOR)
        second = self.make_user("E-SWITCH-2", ROLE_SUPERVISOR)
        self.client.post(
            reverse("VisionInspection:login"),
            {"username": first.username, "password": "CorrectHorseBattery77"},
        )
        session = self.client.session
        session["viInspectionWorkflow"] = {"session_id": "old-user-session"}
        session["station_scope"] = "OLD-STATION"
        session.save()

        self.client.post(reverse("VisionInspection:logout"))
        self.client.post(
            reverse("VisionInspection:login"),
            {"username": second.username, "password": "CorrectHorseBattery77"},
        )

        self.assertEqual(self.client.session.get("_auth_user_id"), str(second.pk))
        self.assertNotIn("viInspectionWorkflow", self.client.session)
        self.assertNotIn("station_scope", self.client.session)

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

    def test_operator_cannot_view_reports_by_default(self):
        operator = self.make_user("E-103", ROLE_OPERATOR)
        VisionInspectionLog.objects.create(part_number="OWN-PART", employee_code="E-103", result="OK")
        VisionInspectionLog.objects.create(part_number="OTHER-PART", employee_code="E-999", result="OK")
        self.client.force_login(operator)
        response = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(response.status_code, 403)

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

    def test_operator_and_supervisor_role_defaults_match_access_policy(self):
        operator = self.make_user("E-DEFAULT", ROLE_OPERATOR)
        supervisor = self.make_user("S-DEFAULT", ROLE_SUPERVISOR)

        self.assertTrue(has_permission(operator, "perform_inspection"))
        self.assertFalse(has_permission(operator, "view_inspection_history"))
        self.assertFalse(has_permission(operator, "view_inspection_dashboard"))
        self.assertFalse(has_permission(operator, "view_inspection_reports"))

        self.assertTrue(has_permission(supervisor, "view_inspection_dashboard"))
        self.assertTrue(has_permission(supervisor, "view_inspection_reports"))
        self.assertTrue(has_permission(supervisor, "view_storage_settings"))
        self.assertFalse(has_permission(supervisor, "change_storage_settings"))
        self.assertFalse(has_permission(supervisor, "view_camera_setup"))
        self.assertFalse(has_permission(supervisor, "view_yolo_models"))

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

    def test_operator_cannot_fetch_inspection_evidence_without_an_exception(self):
        operator = self.make_user("E-OWN-IMAGE", ROLE_OPERATOR)
        session = VisionInspectionSession.objects.create(
            part_number="P-OWN-IMG",
            employee_code=operator.username,
            operator=operator,
        )
        metadata = StorageImageMetadata.objects.create(
            storage_key="b" * 32,
            inspection_session=session,
            image_field_name="front_image",
            relative_path="evidence/front.jpg",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
        )
        self.client.force_login(operator)

        with patch(
            "VisionInspection.storage_views.storage_service.open_image",
            return_value=BytesIO(b"evidence"),
        ):
            response = self.client.get(
                reverse("VisionInspection:storage-image", args=[metadata.pk])
            )

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

    def test_admin_can_open_permission_matrix_but_supervisor_cannot(self):
        admin = User.objects.create_superuser(
            username="ADMIN-PERM", password="AdminPasswordValid99"
        )
        self.client.force_login(admin)
        response = self.client.get(
            reverse("VisionInspection:user-management-permissions")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role Permissions")
        self.assertContains(response, 'id="roleSelector"')
        self.assertContains(response, "Save Changes")
        self.assertNotContains(response, "Default role access")
        self.assertNotContains(response, "Account access overrides")

        supervisor = self.make_user("S-PERM", ROLE_SUPERVISOR)
        self.client.force_login(supervisor)
        response = self.client.get(
            reverse("VisionInspection:user-management-permissions")
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_set_supervisor_override(self):
        admin = User.objects.create_superuser(
            username="ADMIN-OVERRIDE", password="AdminPasswordValid99"
        )
        supervisor = self.make_user("S-OVERRIDE", ROLE_SUPERVISOR)
        permission = Permission.objects.get(
            content_type__app_label="vision_inspection",
            codename="view_yolo_models",
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse("VisionInspection:user-management-permissions"),
            {
                "action": "save_user_overrides",
                "user_id": supervisor.pk,
                "override_view_yolo_models": "grant",
            },
        )

        self.assertEqual(response.status_code, 302)
        override = VisionInspectionPermissionOverride.objects.get(
            user=supervisor, permission=permission
        )
        self.assertTrue(override.granted)
        self.assertTrue(has_permission(supervisor, "view_yolo_models"))

    def test_supervisor_override_cannot_grant_user_or_permission_management(self):
        admin = User.objects.create_superuser(
            username="ADMIN-RESTRICT", password="AdminPasswordValid99"
        )
        supervisor = self.make_user("S-RESTRICT", ROLE_SUPERVISOR)
        self.client.force_login(admin)

        response = self.client.post(
            reverse("VisionInspection:user-management-permissions"),
            {
                "action": "save_user_overrides",
                "user_id": supervisor.pk,
                "override_manage_inspection_users": "grant",
                "override_manage_inspection_permissions": "grant",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            VisionInspectionPermissionOverride.objects.filter(user=supervisor).exists()
        )


class AccessGroupAcceptanceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="ACCESS-ADMIN",
            email="access-admin@example.invalid",
            password="AdminPasswordValid47",
        )
        self.client.force_login(self.admin)

    def create_group(self, name="Quality Review"):
        response = self.client.post(
            reverse("VisionInspection:access-group-create"),
            {"name": name, "description": "Review access", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        return AccessGroupProfile.objects.get(group__name=name)

    def test_group_creation_and_duplicate_names_are_checked_case_insensitively(self):
        profile = self.create_group()
        self.assertEqual(profile.description, "Review access")

        list_response = self.client.get(reverse("VisionInspection:access-groups"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Quality Review")
        self.assertContains(list_response, "S.No")
        self.assertContains(list_response, "Group Name")
        self.assertContains(list_response, "View")
        self.assertContains(list_response, "Edit")
        self.assertNotContains(list_response, "Assigned users")
        self.assertNotContains(list_response, "Description")
        matrix_response = self.client.get(
            reverse("VisionInspection:access-group-permissions", args=[profile.pk])
        )
        self.assertEqual(matrix_response.status_code, 200)
        self.assertContains(matrix_response, "Sub Screen")
        self.assertContains(matrix_response, "Select all")
        self.assertContains(matrix_response, "Quality Review")

        response = self.client.post(
            reverse("VisionInspection:access-group-create"),
            {"name": "quality review", "description": "", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AccessGroupProfile.objects.count(), 1)

    def test_group_form_persists_permissions_on_create_and_edit(self):
        response = self.client.post(
            reverse("VisionInspection:access-group-create"),
            {
                "name": "Report Review",
                "description": "",
                "is_active": "on",
                "permission": ["view_inspection_reports"],
            },
        )
        self.assertEqual(response.status_code, 302)
        profile = AccessGroupProfile.objects.get(group__name="Report Review")
        self.assertTrue(
            profile.group.permissions.filter(
                content_type__app_label="vision_inspection",
                codename="view_inspection_reports",
            ).exists()
        )

        edit_response = self.client.post(
            reverse("VisionInspection:access-group-edit", args=[profile.pk]),
            {
                "name": "Report Review Updated",
                "description": "Updated",
                "is_active": "on",
                "permission": ["view_master_data"],
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.group.name, "Report Review Updated")
        self.assertTrue(
            profile.group.permissions.filter(
                content_type__app_label="vision_inspection",
                codename="view_master_data",
            ).exists()
        )
        self.assertFalse(
            profile.group.permissions.filter(
                content_type__app_label="vision_inspection",
                codename="view_inspection_reports",
            ).exists()
        )
        edit_page = self.client.get(
            reverse("VisionInspection:access-group-edit", args=[profile.pk])
        )
        self.assertEqual(edit_page.status_code, 200)
        self.assertTrue(
            any(
                item["codename"] == "view_master_data" and item["checked"]
                for module in edit_page.context["permission_groups"]
                for item in module["items"]
            )
        )

    def test_access_group_search_reset_and_user_assignment(self):
        profile = self.create_group("Quality Review")
        self.create_group("Production Operators")
        search_response = self.client.get(
            reverse("VisionInspection:access-groups"), {"q": "Quality"}
        )
        self.assertContains(search_response, "Quality Review")
        self.assertNotContains(search_response, "Production Operators")
        self.assertContains(
            search_response,
            reverse("VisionInspection:access-groups"),
        )

        create_response = self.client.post(
            reverse("VisionInspection:user-management"),
            {
                "action": "create",
                "employee_id": "GROUP-ASSIGNED",
                "password": "A-Long-Unique-Password-853!",
                "role": ROLE_OPERATOR,
                "access_group": str(profile.pk),
                "display_name": "Group Assigned",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        assigned_user = User.objects.get(username="GROUP-ASSIGNED")
        self.assertTrue(assigned_user.groups.filter(pk=profile.group_id).exists())

    def test_user_search_access_group_filter_and_user_detail(self):
        profile = self.create_group()
        operator = User.objects.create_user(
            username="SEARCH-OP",
            password="OperatorPasswordValid47",
            first_name="Sam Operator",
        )
        operator.groups.add(Group.objects.get(name=ROLE_OPERATOR), profile.group)
        VisionInspectionUserProfile.objects.create(user=operator)

        response = self.client.get(
            reverse("VisionInspection:user-management"),
            {"q": "Sam", "access_group": str(profile.pk)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SEARCH-OP")
        self.assertContains(response, "Quality Review")
        detail = self.client.get(
            reverse("VisionInspection:user-management-detail", args=[operator.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Employee ID / Username")

    def test_group_screen_permissions_persist_and_inactive_group_stops_granting(self):
        profile = self.create_group()
        operator = User.objects.create_user(
            username="ACCESS-OPERATOR", password="OperatorPasswordValid47"
        )
        operator.groups.add(Group.objects.get(name=ROLE_OPERATOR), profile.group)
        report_permission = Permission.objects.get(
            content_type__app_label="vision_inspection",
            codename="view_inspection_reports",
        )

        response = self.client.post(
            reverse("VisionInspection:access-group-edit", args=[profile.pk]),
            {
                "name": profile.group.name,
                "description": profile.description,
                "is_active": "on",
                "permission": ["view_inspection_reports"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(profile.group.permissions.filter(pk=report_permission.pk).exists())
        operator = User.objects.get(pk=operator.pk)
        self.assertTrue(has_permission(operator, "view_inspection_reports"))

        self.client.post(
            reverse("VisionInspection:access-group-edit", args=[profile.pk]),
            {
                "name": profile.group.name,
                "description": "Paused",
                "is_active": "",
                "permission": ["view_inspection_reports"],
            },
        )
        operator = User.objects.get(pk=operator.pk)
        self.assertFalse(has_permission(operator, "view_inspection_reports"))

    def test_assigned_groups_cannot_be_deleted_but_unassigned_groups_can(self):
        profile = self.create_group()
        operator = User.objects.create_user(
            username="GROUP-MEMBER", password="OperatorPasswordValid47"
        )
        operator.groups.add(profile.group)

        response = self.client.post(
            reverse("VisionInspection:access-group-delete", args=[profile.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(AccessGroupProfile.objects.filter(pk=profile.pk).exists())
        operator.groups.remove(profile.group)
        self.client.post(
            reverse("VisionInspection:access-group-delete", args=[profile.pk])
        )
        self.assertFalse(AccessGroupProfile.objects.filter(pk=profile.pk).exists())

    def test_system_role_groups_cannot_be_deleted_or_given_by_screen_matrix(self):
        role_group = Group.objects.get(name=ROLE_ADMIN)
        profile = AccessGroupProfile.objects.create(group=role_group)
        permission_ids_before = set(
            role_group.permissions.values_list("pk", flat=True)
        )

        delete_response = self.client.post(
            reverse("VisionInspection:access-group-delete", args=[profile.pk])
        )
        permission_response = self.client.post(
            reverse("VisionInspection:access-group-permissions", args=[profile.pk]),
            {"permission": ["manage_inspection_users"]},
        )
        edit_response = self.client.post(
            reverse("VisionInspection:access-group-edit", args=[profile.pk]),
            {
                "name": ROLE_ADMIN,
                "is_active": "on",
                "permission": ["view_inspection_reports"],
            },
        )

        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(permission_response.status_code, 405)
        self.assertEqual(edit_response.status_code, 302)
        self.assertTrue(Group.objects.filter(pk=role_group.pk).exists())
        self.assertEqual(
            set(role_group.permissions.values_list("pk", flat=True)),
            permission_ids_before,
        )

    def test_non_administrator_cannot_open_group_management(self):
        supervisor = User.objects.create_user(
            username="GROUP-SUPERVISOR", password="SupervisorPasswordValid47"
        )
        supervisor.groups.add(Group.objects.get(name=ROLE_SUPERVISOR))
        self.client.force_login(supervisor)

        response = self.client.get(reverse("VisionInspection:access-groups"))

        self.assertEqual(response.status_code, 403)

    def test_sidebar_group_links_are_admin_only(self):
        admin_response = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "Access Groups")
        self.assertContains(admin_response, "Role Permissions")

        supervisor = User.objects.create_user(
            username="SIDEBAR-SUPERVISOR", password="SupervisorPasswordValid47"
        )
        supervisor.groups.add(Group.objects.get(name=ROLE_SUPERVISOR))
        self.client.force_login(supervisor)
        supervisor_response = self.client.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(supervisor_response.status_code, 200)
        self.assertNotContains(supervisor_response, "Access Groups")
        self.assertNotContains(supervisor_response, "Role Permissions")

    def test_role_change_preserves_custom_group_and_last_admin_cannot_be_deactivated(self):
        profile = self.create_group()
        operator = User.objects.create_user(
            username="ROLE-CHANGE", password="OperatorPasswordValid47"
        )
        operator.groups.add(Group.objects.get(name=ROLE_OPERATOR), profile.group)
        self.client.post(
            reverse("VisionInspection:user-management"),
            {
                "action": "change_role",
                "user_id": operator.pk,
                "role": ROLE_SUPERVISOR,
            },
        )
        operator.refresh_from_db()
        self.assertTrue(operator.groups.filter(name=ROLE_SUPERVISOR).exists())
        self.assertTrue(operator.groups.filter(pk=profile.group_id).exists())

        response = self.client.post(
            reverse("VisionInspection:user-management"),
            {"action": "deactivate", "user_id": self.admin.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_user_management_hides_station_scope_and_deactivate_controls(self):
        operator = User.objects.create_user(
            username="NO-SCOPE-CONTROLS",
            password="local-test-password",
            first_name="No Scope",
        )
        operator.groups.add(Group.objects.get(name=ROLE_OPERATOR))
        profile = VisionInspectionUserProfile.objects.create(
            user=operator, station_scope="LINE-A"
        )

        response = self.client.get(reverse("VisionInspection:user-management"))
        detail = self.client.get(
            reverse("VisionInspection:user-management-detail", args=[operator.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Station Scope")
        self.assertNotContains(response, "Deactivate")
        self.assertContains(response, "No Access Group")
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "Station scope")

        self.client.post(
            reverse("VisionInspection:user-management"),
            {
                "action": "change_role",
                "user_id": operator.pk,
                "role": ROLE_SUPERVISOR,
                "scope": "LINE-B",
            },
        )
        profile.refresh_from_db()
        self.assertEqual(profile.station_scope, "LINE-A")


class DashboardRoleAcceptanceTests(TestCase):
    def make_user(self, username, role, scope=""):
        user = User.objects.create_user(
            username=username, password="CorrectHorseBattery77"
        )
        user.groups.add(Group.objects.get(name=role))
        VisionInspectionUserProfile.objects.create(
            user=user, station_scope=scope
        )
        return user

    def test_dashboard_requires_permission_and_operator_data_stays_scoped(self):
        operator = self.make_user("DASH-OP", ROLE_OPERATOR)
        denied = Client()
        denied.force_login(operator)
        response = denied.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(response.status_code, 403)

        dashboard_permission = Permission.objects.get(
            content_type__app_label="vision_inspection",
            codename="view_inspection_dashboard",
        )
        dashboard_only = Group.objects.create(name="Dashboard-only access")
        dashboard_only.permissions.add(dashboard_permission)
        operator.groups.add(dashboard_only)

        VisionInspectionPart.objects.create(
            part_number="OWN-PART", part_name="Own Airbag"
        )
        VisionInspectionPart.objects.create(
            part_number="OTHER-PART", part_name="Other Airbag"
        )
        VisionInspectionLog.objects.create(
            part_number="OWN-PART",
            employee_code=operator.username,
            result="OK",
        )
        VisionInspectionLog.objects.create(
            part_number="OTHER-PART",
            employee_code="OTHER-OP",
            result="NOT_OK",
        )

        response = denied.get(reverse("VisionInspection:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 1)
        self.assertContains(response, "Start Inspection")
        self.assertNotContains(response, "Recent Inspections")
        self.assertNotContains(response, "View Inspection Report")
        self.assertContains(response, "OWN-PART")
        self.assertNotContains(response, "OTHER-PART")
        self.assertEqual(
            denied.get(reverse("VisionInspection:api-dashboard-summary")).status_code,
            403,
        )

    def test_supervisor_dashboard_recent_inspections_follow_station_scope(self):
        supervisor = self.make_user("DASH-SUP", ROLE_SUPERVISOR, "LINE-A")
        VisionInspectionPart.objects.create(
            part_number="LINE-A-PART", part_name="Assigned Line"
        )
        VisionInspectionPart.objects.create(
            part_number="LINE-B-PART", part_name="Other Line"
        )
        assigned = VisionInspectionSession.objects.create(
            part_number="LINE-A-PART",
            employee_code="OP-A",
            station_id="LINE-A",
        )
        VisionInspectionSession.objects.create(
            part_number="LINE-B-PART",
            employee_code="OP-B",
            station_id="LINE-B",
        )
        VisionInspectionLog.objects.create(
            part_number="LINE-A-PART",
            employee_code="OP-A",
            result="OK",
            session=assigned,
        )
        VisionInspectionLog.objects.create(
            part_number="LINE-B-PART",
            employee_code="OP-B",
            result="NOT_OK",
        )

        self.client.force_login(supervisor)
        response = self.client.get(reverse("VisionInspection:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 1)
        self.assertContains(response, "Recent Inspections")
        self.assertContains(response, "View Inspection Report")
        self.assertContains(response, "LINE-A-PART")
        self.assertEqual(len(response.context["recent_rows"]), 1)
        self.assertEqual(
            response.context["recent_rows"][0]["log"].part_number,
            "LINE-A-PART",
        )


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
        history_permission = Permission.objects.get(
            content_type__app_label="vision_inspection",
            codename="view_inspection_history",
        )
        VisionInspectionPermissionOverride.objects.create(
            user=user,
            permission=history_permission,
            granted=True,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("VisionInspection:traceability-detail", args=[log.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INCOMPLETE")
        self.assertContains(response, "Image unavailable. Not captured.")
        self.assertContains(response, "Annotation not generated")
        self.assertContains(response, "SERIAL-77")
        report = self.client.get(reverse("VisionInspection:ui-reports"))
        self.assertEqual(report.status_code, 403)
