import json
import tempfile
from pathlib import Path

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse

from VisionInspection.models import (
    StorageImageMetadata,
    StorageManagerRoot,
    VisionInspectionLog,
    VisionInspectionSession,
)
from VisionInspection.services.access_control import can_view_log
from VisionInspection.services import storage_service


class StorageManagerTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_path = Path(self.temp.name)
        self.admin = User.objects.create_superuser(
            username="storage-admin", email="admin@example.test", password="secret"
        )

    def _root(self, path=None):
        path = Path(path or self.root_path)
        return StorageManagerRoot.objects.create(
            name="Test storage", path=str(path.resolve()), created_by=self.admin
        )

    def test_settings_page_renders_manager_and_not_runtime_configuration(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("VisionInspection:ui-settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Local Storage Manager")
        self.assertContains(response, "Browse Folder")
        self.assertNotContains(response, "Runtime Configuration")
        self.assertNotContains(response, "Django version")
        self.assertContains(response, "storage_manager.css")
        self.assertContains(response, "storage_manager.js")

    def test_browse_and_create_folder_are_confined_to_authorized_root(self):
        root = self._root()
        child = storage_service.storage_manager_create_folder(
            root.pk, "", "Images", self.admin
        )

        result = storage_service.storage_manager_browse(root.pk, "")

        self.assertEqual(result["root"]["id"], root.pk)
        self.assertEqual(result["entries"][0]["path"], "Images")
        self.assertEqual(child["path"], "Images")
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_create_folder(
                root.pk, "../", "outside", self.admin
            )
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_create_folder(
                root.pk, "", "..", self.admin
            )
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_browse(root.pk, "../")

    def test_symlink_escape_and_filesystem_root_are_rejected(self):
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_add_root(
                "Entire drive", str(Path(self.root_path.anchor)), self.admin
            )
        allowed = self.root_path / "allowed"
        allowed.mkdir()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        link = allowed / "linked-outside"
        try:
            link.symlink_to(Path(outside.name), target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        root = self._root(allowed)
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_browse(root.pk, "linked-outside")

    def test_merge_preserves_destination_collision_and_updates_traceability(self):
        source = self.root_path / "source"
        destination = self.root_path / "destination"
        source.mkdir()
        destination.mkdir()
        (source / "front.jpg").write_bytes(b"inspection evidence")
        (destination / "front.jpg").write_bytes(b"existing destination evidence")
        session = VisionInspectionSession.objects.create(
            part_number="P-1", front_image="source/front.jpg"
        )
        StorageImageMetadata.objects.create(
            storage_key="storage-manager-front-image",
            inspection_session=session,
            image_field_name="front_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            relative_path="source/front.jpg",
        )
        root = self._root()

        with override_settings(MEDIA_ROOT=str(self.root_path)):
            result = storage_service.storage_manager_merge(
                root.pk, "source", "destination", self.admin
            )

        self.assertFalse(result["source_removed"])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["metadata_updated"], 1)
        self.assertEqual((destination / "front.jpg").read_bytes(), b"existing destination evidence")
        merged_images = list(destination.glob("front_merged_*.jpg"))
        self.assertEqual(len(merged_images), 1)
        self.assertEqual(merged_images[0].read_bytes(), b"inspection evidence")
        self.assertFalse((source / "front.jpg").exists())
        self.assertTrue(source.is_dir())
        session.refresh_from_db()
        metadata = StorageImageMetadata.objects.get(storage_key="storage-manager-front-image")
        self.assertEqual(session.front_image.name, merged_images[0].relative_to(self.root_path).as_posix())
        self.assertEqual(metadata.relative_path, session.front_image.name)
        self.client.force_login(self.admin)
        with override_settings(MEDIA_ROOT=str(self.root_path)):
            image = self.client.get(
                reverse("VisionInspection:storage-image", args=[metadata.pk])
            )
        self.assertEqual(image.status_code, 200)
        self.assertEqual(b"".join(image.streaming_content), b"inspection evidence")

    def test_merge_rejects_same_nested_and_non_image_folders_without_changes(self):
        source = self.root_path / "source"
        nested = source / "nested"
        nested.mkdir(parents=True)
        (source / "readme.txt").write_text("not an inspection image", encoding="utf-8")
        root = self._root()
        for source_path, destination_path in (
            ("source", "source"),
            ("source", "source/nested"),
        ):
            with self.assertRaises(storage_service.StorageOperationError):
                storage_service.storage_manager_merge(
                    root.pk, source_path, destination_path, self.admin
                )
        with self.assertRaises(storage_service.StorageOperationError):
            storage_service.storage_manager_merge(
                root.pk, "source", "", self.admin
            )
        self.assertTrue((source / "readme.txt").exists())

    def test_storage_apis_enforce_permissions_and_administrator_root_management(self):
        user = User.objects.create_user(username="no-storage-access", password="secret")
        self.client.force_login(user)
        browse = self.client.get(
            reverse("VisionInspection:storage-manager-browse"),
            {"root_id": 1, "path": ""},
        )
        self.assertEqual(browse.status_code, 403)
        add_root = self.client.post(
            reverse("VisionInspection:storage-manager-add-root"),
            data=json.dumps({"name": "test", "path": str(self.root_path)}),
            content_type="application/json",
        )
        self.assertEqual(add_root.status_code, 403)

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("VisionInspection:storage-manager-add-root"),
            data=json.dumps({"name": "Authorized test folder", "path": str(self.root_path)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(StorageManagerRoot.objects.filter(is_active=True).count(), 1)

    def test_explicitly_granted_storage_users_can_browse_but_not_manage_roots(self):
        user = User.objects.create_user(username="granted-storage-user", password="secret")
        permissions = Permission.objects.filter(
            content_type__app_label="vision_inspection",
            codename__in=("view_storage_settings", "change_storage_settings"),
        )
        user.user_permissions.add(*permissions)
        root = self._root()
        self.client.force_login(user)

        settings_page = self.client.get(reverse("VisionInspection:ui-settings"))
        self.assertEqual(settings_page.status_code, 200)
        self.assertContains(settings_page, "Local Storage Manager")
        create = self.client.post(
            reverse("VisionInspection:storage-manager-create-folder"),
            data=json.dumps({"root_id": root.pk, "parent_path": "", "name": "New folder"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        add_root = self.client.post(
            reverse("VisionInspection:storage-manager-add-root"),
            data=json.dumps({"name": "another", "path": str(self.root_path)}),
            content_type="application/json",
        )
        self.assertEqual(add_root.status_code, 403)

    def test_sessionless_log_evidence_requires_admin_or_operator_ownership(self):
        log = VisionInspectionLog.objects.create(
            employee_code="UNRELATED-EMPLOYEE",
            part_number="P-1",
            result="OK",
        )
        metadata = StorageImageMetadata.objects.create(
            storage_key="legacy-sessionless-log-evidence",
            inspection_log=log,
            image_field_name="inspection_image",
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            relative_path="legacy/missing.jpg",
        )
        supervisor = User.objects.create_user(
            username="scoped-supervisor", password="secret"
        )
        supervisor.groups.add(Group.objects.get(name="Supervisor"))
        supervisor_client = self.client
        supervisor_client.force_login(supervisor)

        self.assertFalse(can_view_log(supervisor, log))
        response = supervisor_client.get(
            reverse("VisionInspection:storage-image", args=[metadata.pk])
        )
        self.assertEqual(response.status_code, 403)

        operator = User.objects.create_user(username="record-owner", password="secret")
        operator.groups.add(Group.objects.get(name="Operator"))
        log.operator = operator
        log.save(update_fields=["operator"])
        self.assertTrue(can_view_log(operator, log))
        self.assertTrue(can_view_log(self.admin, log))
