from django.db import migrations, models


def grant_admin_model_access(apps, schema_editor):
    """Allow the Administrator role to use Django admin without delete rights."""
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    group = Group.objects.using(database).filter(name="Administrator").first()
    if not group:
        return
    app_types = ContentType.objects.using(database).filter(app_label="vision_inspection")
    app_permissions = Permission.objects.using(database).filter(
        content_type__in=app_types,
        codename__regex=r"^(add|change|view)_",
    )
    auth_types = ContentType.objects.using(database).filter(app_label="auth", model__in=("user", "group"))
    auth_permissions = Permission.objects.using(database).filter(
        content_type__in=auth_types,
        codename__regex=r"^(add|change|view)_",
    )
    group.permissions.add(*app_permissions, *auth_permissions)


class Migration(migrations.Migration):
    dependencies = [("vision_inspection", "0021_alter_visioninspectionsession_options")]
    operations = [
        migrations.AddField(
            model_name="visioninspectionsession",
            name="employee_code",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.RunPython(grant_admin_model_access, migrations.RunPython.noop),
    ]
