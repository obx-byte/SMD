from django.db import migrations


ROLE_OPERATOR = "Operator"
ROLE_ADMIN = "Administrator"
BASE_PERMISSION = "perform_inspection"
REMOVED_DEFAULT = "view_inspection_history"


def restrict_operator_defaults(apps, schema_editor):
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Override = apps.get_model(
        "vision_inspection", "VisionInspectionPermissionOverride"
    )

    group = Group.objects.using(database).filter(name=ROLE_OPERATOR).first()
    content_type = ContentType.objects.using(database).filter(
        app_label="vision_inspection",
        model="visioninspectionsession",
    ).first()
    if group is None or content_type is None:
        return

    permissions = Permission.objects.using(database).filter(
        content_type_id=content_type.pk
    )
    base_permission = permissions.filter(codename=BASE_PERMISSION).first()
    if base_permission is None:
        return

    role_permission_ids = set(
        group.permissions.using(database)
        .filter(content_type_id=content_type.pk)
        .exclude(pk=base_permission.pk)
        .exclude(codename=REMOVED_DEFAULT)
        .values_list("pk", flat=True)
    )
    if role_permission_ids:
        for user in group.user_set.using(database).exclude(
            is_superuser=True
        ).exclude(groups__name=ROLE_ADMIN).distinct():
            for permission_id in role_permission_ids:
                Override.objects.using(database).get_or_create(
                    user_id=user.pk,
                    permission_id=permission_id,
                    defaults={"granted": True},
                )

    through = Group.permissions.through
    through.objects.using(database).filter(
        group_id=group.pk,
        permission__content_type_id=content_type.pk,
    ).exclude(permission_id=base_permission.pk).delete()
    through.objects.using(database).get_or_create(
        group_id=group.pk,
        permission_id=base_permission.pk,
    )


class Migration(migrations.Migration):
    dependencies = [("vision_inspection", "0024_configure_default_roles")]

    operations = [
        migrations.RunPython(restrict_operator_defaults, migrations.RunPython.noop),
    ]