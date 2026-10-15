# Storage Settings Deployment

Install the optional storage integrations into the same virtual environment as Django:

```bash
python -m pip install -r requirements-storage.txt
```

Create one Fernet key for this station and keep it in a root-readable environment file. Do not commit or rotate it without first re-encrypting the destination credentials:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated value as `VISION_STORAGE_ENCRYPTION_KEY` for the Django service. The key must remain stable across restarts and backups. If the database already has saved SMB passwords, set the key before running migrations; migration `0016` encrypts those values and refuses to continue if the key is missing.

Back up the SQLite database, install the dependencies, configure the key, and apply the forward migrations:

```bash
python manage.py migrate
python manage.py check
```

Run the automatic transfer loop as a separate supervised process:

```bash
python manage.py run_storage_worker
```

For Raspberry Pi deployment, configure this command as a systemd service with `Restart=always`, the same virtualenv and Django environment as the web service, and `VISION_STORAGE_ENCRYPTION_KEY` loaded from a root-readable environment file. The command uses a process lock to prevent a second worker and resumes eligible SQLite metadata rows after restart. It sleeps for the configured monitoring interval when idle and checks again quickly after a transfer batch.

The SMB backend connects directly to SMB on the configured host/share and uses bounded connection setup timeouts; it does not mount/unmount the share for each image. The Ubuntu station must be permitted to reach the SMB server on TCP 445. Local evidence and local destination files are stored below `MEDIA_ROOT/vision_storage` unless `VISION_STORAGE_LOCAL_ROOT` is set.

Storage Settings is available to all station users from Configuration, as requested. The APIs remain CSRF-protected; SMB passwords are never returned to the browser and are encrypted in SQLite. Test Connection writes, reads, verifies, and removes a uniquely named probe. Activation repeats that test before changing the active destination. Historical migration copies and verifies one bounded batch per request; originals are retained unless deletion is separately confirmed with `DELETE`.
