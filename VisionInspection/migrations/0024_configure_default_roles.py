from django.db import migrations


ROLE_OPERATOR = "Operator"
ROLE_SUPERVISOR = "Supervisor"
ROLE_ADMIN = "Administrator"
ROLE_QUALITY = "Quality User"

PERMISSIONS = {
    "perform_inspection": "Can perform an inspection",
    "view_inspection_dashboard": "Can view the inspection dashboard",
    "view_inspection_history": "Can view inspection history",
    "view_inspection_reports": "Can view inspection reports",
    "export_inspection_reports": "Can export inspection reports",
    "view_master_data": "Can view master data",
    "create_master_data": "Can create master data",
    "change_master_data": "Can change master data",
    "delete_master_data": "Can delete master data",
    "view_part_vision_configuration": "Can view Part Vision configuration",
    "change_part_vision_configuration": "Can change Part Vision configuration",
    "view_storage_settings": "Can view storage settings",
    "change_storage_settings": "Can change storage settings",
    "view_camera_setup": "Can view camera setup",
    "change_camera_roi_settings": "Can change camera and ROI settings",
    "view_model_configuration": "Can view model configuration",
    "change_model_configuration": "Can change model configuration",
    "view_approved_models": "Can view approved models",
    "manage_approved_models": "Can manage approved models",
    "view_yolo_models": "Can view YOLO models",
    "manage_yolo_models": "Can manage YOLO models",
    "view_training_jobs": "Can view training jobs",
    "manage_training_jobs": "Can manage training jobs",
    "manage_inspection_users": "Can manage inspection users",
    "manage_inspection_permissions": "Can manage inspection permissions",
    "view_inspection_audit_log": "Can view inspection audit logs",
    "view_inspection_state": "Can view inspection state",
    "view_inspection_logs": "Can view inspection logs",
    "view_roi_configuration": "Can view ROI configuration",
    "change_roi_configuration": "Can change ROI configuration",
    "view_trigger_zone_configuration": "Can view trigger zone configuration",
    "change_trigger_zone_configuration": "Can change trigger zone configuration",
    "view_inspection_presets": "Can view inspection presets",
    "change_inspection_presets": "Can change inspection presets",
    "view_master_images": "Can view master images",
    "change_master_images": "Can change master images",
    "view_system_settings": "Can view system settings",
    "change_system_settings": "Can change system settings",
}

DEFAULTS = {
    ROLE_OPERATOR: {"perform_inspection", "view_inspection_history"},
    ROLE_SUPERVISOR: {
        "view_inspection_dashboard",
        "view_inspection_reports",
        "export_inspection_reports",
        "view_master_data",
        "view_storage_settings",
    },
}


def configure_roles(apps, schema_editor):
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Override = apps.get_model("vision_inspection", "VisionInspectionPermissionOverride")

    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="vision_inspection",
        model="visioninspectionsession",
    )
    permission_objects = {}
    for codename, name in PERMISSIONS.items():
        permission_objects[codename], _ = Permission.objects.using(database).get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )

    operator, _ = Group.objects.using(database).get_or_create(name=ROLE_OPERATOR)
    supervisor, _ = Group.objects.using(database).get_or_create(name=ROLE_SUPERVISOR)
    administrator, _ = Group.objects.using(database).get_or_create(name=ROLE_ADMIN)
    quality = Group.objects.using(database).filter(name=ROLE_QUALITY).first()

    supervisor_defaults = set(DEFAULTS[ROLE_SUPERVISOR])
    legacy_user_permissions = {}
    user_groups = Group.user_set.through.objects.using(database)
    for legacy_group in (supervisor, quality):
        if legacy_group is None:
            continue
        granted = list(legacy_group.permissions.using(database).all())
        for user in legacy_group.user_set.using(database).all():
            legacy_user_permissions.setdefault(user.pk, []).extend(granted)
            if legacy_group.pk == supervisor.pk:
                continue
            user_groups.get_or_create(user_id=user.pk, group_id=supervisor.pk)
            user_groups.filter(user_id=user.pk, group_id=legacy_group.pk).delete()

    group_permissions = Group.permissions.through.objects.using(database)
    for group, codes in (
        (operator, DEFAULTS[ROLE_OPERATOR]),
        (supervisor, DEFAULTS[ROLE_SUPERVISOR]),
    ):
        group_permissions.filter(group_id=group.pk).delete()
        group_permissions.bulk_create(
            [
                Group.permissions.through(
                    group_id=group.pk,
                    permission_id=permission_objects[codename].pk,
                )
                for codename in sorted(codes)
            ],
            ignore_conflicts=True,
        )

    for user_id, old_permissions in legacy_user_permissions.items():
        for permission in old_permissions:
            if (
                permission.content_type_id == content_type.pk
                and permission.codename in supervisor_defaults
            ):
                continue
            Override.objects.using(database).update_or_create(
                user_id=user_id,
                permission_id=permission.pk,
                defaults={"granted": True},
            )

    admin_permission_ids = {permission.pk for permission in permission_objects.values()}
    app_permissions = Permission.objects.using(database).filter(
        content_type__app_label="vision_inspection",
        codename__regex=r"^(add|change|delete|view)_",
    )
    auth_permissions = Permission.objects.using(database).filter(
        content_type__app_label="auth",
        content_type__model__in=("user", "group"),
    )
    admin_permission_ids.update(app_permissions.values_list("pk", flat=True))
    admin_permission_ids.update(auth_permissions.values_list("pk", flat=True))
    group_permissions.bulk_create(
        [
            Group.permissions.through(
                group_id=administrator.pk,
                permission_id=permission_id,
            )
            for permission_id in admin_permission_ids
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vision_inspection", "0023_permission_overrides"),
    ]

    operations = [
        migrations.RunPython(configure_roles, migrations.RunPython.noop),
    ]
