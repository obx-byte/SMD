import os

from django.db import migrations


PREFIX = "fernet:v1:"


def encrypt_existing_credentials(apps, schema_editor):
    Destination = apps.get_model("vision_inspection", "StorageDestination")
    rows = Destination.objects.exclude(password="").only("id", "password")
    if not rows.exists():
        return

    key = os.environ.get("VISION_STORAGE_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Set VISION_STORAGE_ENCRYPTION_KEY before migrating stored SMB credentials."
        )
    try:
        from cryptography.fernet import Fernet

        cipher = Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("VISION_STORAGE_ENCRYPTION_KEY is invalid or cryptography is unavailable.") from exc

    for destination in rows.iterator(chunk_size=100):
        if destination.password.startswith(PREFIX):
            continue
        destination.password = PREFIX + cipher.encrypt(destination.password.encode("utf-8")).decode("ascii")
        destination.save(update_fields=["password"])


class Migration(migrations.Migration):
    dependencies = [("vision_inspection", "0015_alter_storagedestination_destination_type_and_more")]

    operations = [
        migrations.RunPython(encrypt_existing_credentials, migrations.RunPython.noop),
    ]
