from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vision_inspection", "0025_operator_role_defaults")]

    operations = [
        migrations.AddField(
            model_name="storageimagemetadata",
            name="captured_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="storageimagemetadata",
            name="is_permanent_local",
            field=models.BooleanField(default=False),
        ),
    ]
