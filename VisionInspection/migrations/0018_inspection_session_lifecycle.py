from django.db import migrations, models
from django.db.models import Q


def close_stale_active_sessions(apps, schema_editor):
    Session = apps.get_model("vision_inspection", "VisionInspectionSession")
    database = schema_editor.connection.alias

    Session.objects.using(database).filter(
        is_active=True,
    ).filter(
        Q(is_saved=True)
        | Q(stage__in=("saved", "cancelled"))
        | Q(front_status="ERROR")
        | Q(back_status="ERROR")
    ).update(is_active=False)

    active_sessions = (
        Session.objects.using(database)
        .filter(is_active=True)
        .order_by("part_number", "-started_at", "-pk")
        .values_list("pk", "part_number")
    )
    seen_parts = set()
    stale_ids = []
    for session_pk, part_number in active_sessions.iterator():
        if part_number in seen_parts:
            stale_ids.append(session_pk)
        else:
            seen_parts.add(part_number)

    if stale_ids:
        Session.objects.using(database).filter(pk__in=stale_ids).update(
            is_active=False,
            stage="cancelled",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vision_inspection", "0017_storageconfiguration_last_transfer_at_and_more"),
    ]

    operations = [
        migrations.RunPython(close_stale_active_sessions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="visioninspectionsession",
            constraint=models.UniqueConstraint(
                fields=("part_number",),
                condition=Q(is_active=True),
                name="uniq_active_vi_session_part",
            ),
        ),
    ]
