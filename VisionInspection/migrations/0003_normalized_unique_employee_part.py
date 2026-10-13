# Generated for normalized-unique employee names and part numbers.

from django.db import migrations, models


def backfill_employee(apps, schema_editor):
    import re

    Employee = apps.get_model("vision_inspection", "VisionInspectionEmployee")
    for emp in Employee.objects.all().iterator():
        emp.name_normalized = re.sub(r"\s+", " ", emp.employee_name or "").strip().lower()
        emp.save(update_fields=["name_normalized"])


def backfill_part(apps, schema_editor):
    import re

    Part = apps.get_model("vision_inspection", "VisionInspectionPart")
    for part in Part.objects.all().iterator():
        part.part_number_normalized = re.sub(
            r"\s+", " ", part.part_number or ""
        ).strip().lower()
        part.save(update_fields=["part_number_normalized"])


def clear_employee(apps, schema_editor):
    pass  # non-destructive; field removal handled by reverse migration


def clear_part(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("vision_inspection", "0002_visioninspectionmasterroi"),
    ]

    operations = [
        migrations.AddField(
            model_name="visioninspectionemployee",
            name="name_normalized",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="visioninspectionpart",
            name="part_number_normalized",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.RunPython(backfill_employee, clear_employee),
        migrations.RunPython(backfill_part, clear_part),
        migrations.AlterField(
            model_name="visioninspectionemployee",
            name="name_normalized",
            field=models.CharField(
                editable=False,
                help_text="Case/space-normalized employee name (unique). Maintained on save().",
                max_length=100,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="visioninspectionpart",
            name="part_number_normalized",
            field=models.CharField(
                editable=False,
                help_text="Case-normalized part number (unique). Maintained on save().",
                max_length=40,
                unique=True,
            ),
        ),
    ]
