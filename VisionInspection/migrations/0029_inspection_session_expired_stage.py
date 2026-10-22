from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vision_inspection", "0028_accessgroupprofile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="visioninspectionsession",
            name="stage",
            field=models.CharField(
                choices=[
                    ("scan", "Scan Part"),
                    ("model", "Model Loaded"),
                    ("front", "Front Inspection"),
                    ("back", "Back Inspection"),
                    ("result", "Final Result"),
                    ("saved", "Saved"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                ],
                db_index=True,
                default="scan",
                max_length=20,
            ),
        ),
    ]
