"""Storage destinations, per-image references, and verified migration helpers."""

import hashlib
import logging
import ntpath
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from VisionInspection.models import (
    StorageConfiguration,
    StorageDestination,
    StorageImageMetadata,
    StorageManagerRoot,
    StorageMigrationJob,
    StorageTransferHistory,
    VisionInspectionAuditLog,
    VisionInspectionLog,
    VisionInspectionSession,
)

logger = logging.getLogger(__name__)
CHUNK_SIZE = 256 * 1024
CREDENTIAL_PREFIX = "fernet:v1:"
STORAGE_MANAGER_MAX_FILES = 1000


class StorageOperationError(Exception):
    """Expected storage/configuration failure safe to show to an operator."""


def _credential_fernet():
    key = str(
        getattr(settings, "VISION_STORAGE_ENCRYPTION_KEY", "")
        or os.environ.get("VISION_STORAGE_ENCRYPTION_KEY", "")
    ).strip()
    if not key:
        raise StorageOperationError(
            "Set VISION_STORAGE_ENCRYPTION_KEY before activating SMB storage."
        )
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise StorageOperationError(
            "The configured storage encryption key is invalid or cryptography is unavailable."
        ) from exc


def encrypt_credential(value):
    value = str(value or "")
    if not value:
        return ""
    token = _credential_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return CREDENTIAL_PREFIX + token


def decrypt_credential(value):
    value = str(value or "")
    if not value:
        return ""
    if not value.startswith(CREDENTIAL_PREFIX):
        raise StorageOperationError(
            "A legacy SMB credential requires secure migration before use."
        )
    try:
        return _credential_fernet().decrypt(value[len(CREDENTIAL_PREFIX):].encode("ascii")).decode("utf-8")
    except StorageOperationError:
        raise
    except Exception as exc:
        raise StorageOperationError("The stored SMB credential could not be decrypted.") from exc


def safe_relative_path(value):
    value = str(value or "").strip().replace("\\", "/")
    if not value:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts):
        raise StorageOperationError("Destination directory must be a relative path without traversal.")
    if "\x00" in value:
        raise StorageOperationError("Destination directory contains an invalid character.")
    return "/".join(path.parts)


def _storage_manager_root(root_id):
    try:
        record = StorageManagerRoot.objects.get(pk=int(root_id), is_active=True)
    except (StorageManagerRoot.DoesNotExist, TypeError, ValueError) as exc:
        raise StorageOperationError("Choose an authorized storage root.") from exc
    path = Path(record.path).expanduser()
    if not path.is_absolute():
        raise StorageOperationError("The authorized storage root is invalid.")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise StorageOperationError(
            "An authorized storage root is unavailable on this Django host."
        ) from exc
    if not root.is_dir():
        raise StorageOperationError("The configured storage root is not a directory.")
    if root != Path(os.path.abspath(path)):
        raise StorageOperationError("The authorized storage root cannot be reached through a symbolic link.")
    return record, root


def storage_manager_roots():
    roots = []
    for record in StorageManagerRoot.objects.filter(is_active=True):
        try:
            _, root = _storage_manager_root(record.pk)
        except StorageOperationError:
            roots.append({
                "id": record.pk, "name": record.name, "path": record.path,
                "available": False,
            })
            continue
        roots.append({
            "id": record.pk, "name": record.name, "path": str(root),
            "available": True,
        })
    return roots


def storage_manager_add_root(name, path, actor):
    label = str(name or "").strip()
    candidate = Path(str(path or "").strip()).expanduser()
    if not label or len(label) > 100 or any(ord(char) < 32 for char in label):
        raise StorageOperationError("Enter a valid root name.")
    if not candidate.is_absolute():
        raise StorageOperationError("Choose an absolute folder on the Django host.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise StorageOperationError(
            "The selected folder is unavailable on this Django host."
        ) from exc
    if not resolved.is_dir():
        raise StorageOperationError("The selected root must be a folder.")
    if resolved == Path(resolved.anchor):
        raise StorageOperationError("A filesystem or drive root cannot be authorized directly.")
    with transaction.atomic():
        root, created = StorageManagerRoot.objects.get_or_create(
            path=str(resolved),
            defaults={"name": label, "created_by": actor, "is_active": True},
        )
        if not created:
            root.name = label
            root.is_active = True
            root.created_by = actor
            root.save(update_fields=["name", "is_active", "created_by", "updated_at"])
        try:
            _audit_storage_manager(
                actor, "storage_root_added",
                {"root_id": root.pk, "root_name": label, "root": str(resolved)},
            )
        except Exception:
            if created:
                root.delete()
            raise
    return root


def storage_manager_deactivate_root(root_id, actor):
    try:
        record = StorageManagerRoot.objects.get(pk=int(root_id), is_active=True)
    except (StorageManagerRoot.DoesNotExist, TypeError, ValueError) as exc:
        raise StorageOperationError("Choose an active authorized root.") from exc
    with transaction.atomic():
        record.is_active = False
        record.save(update_fields=["is_active", "updated_at"])
        _audit_storage_manager(
            actor, "storage_root_deactivated",
            {"root_id": record.pk, "root_name": record.name},
        )
    return record


def _manager_path(root, relative_path="", require_exists=False):
    relative = safe_relative_path(relative_path)
    candidate = root.joinpath(*relative.split("/")) if relative else root
    current = root
    for part in relative.split("/") if relative else ():
        current = current / part
        if current.is_symlink():
            raise StorageOperationError("Symbolic links are not allowed in managed folders.")
    try:
        resolved = candidate.resolve(strict=require_exists)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise StorageOperationError("The requested path is unavailable or outside its authorized root.") from exc
    return resolved


def storage_manager_browse(root_id, relative_path=""):
    record, root = _storage_manager_root(root_id)
    directory = _manager_path(root, relative_path, require_exists=True)
    if not directory.is_dir():
        raise StorageOperationError("Choose a folder to browse.")
    entries = []
    try:
        children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        for child in children[:500]:
            if child.is_symlink():
                continue
            stat = child.stat()
            entries.append({
                "name": child.name,
                "path": child.relative_to(root).as_posix(),
                "type": "directory" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
            })
    except OSError as exc:
        raise StorageOperationError("The selected folder could not be read.") from exc
    return {
        "root": {"id": record.pk, "name": record.name, "path": str(root)},
        "path": directory.relative_to(root).as_posix(),
        "entries": entries,
        "truncated": len(children) > 500,
    }


def _audit_storage_manager(actor, action, details):
    VisionInspectionAuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type="StorageManager",
        target_id=str(details.get("root_id", ""))[:100],
        details=details,
    )


def storage_manager_create_folder(root_id, parent_path, folder_name, actor):
    record, root = _storage_manager_root(root_id)
    name = str(folder_name or "").strip()
    reserved = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)
    if (
        not name or name in (".", "..") or ".." in name or len(name) > 120
        or any(char in name for char in ("/", "\\", "\x00", "<", ">", ":", '"', "|", "?", "*"))
        or name.endswith((".", " ")) or reserved.match(name)
        or any(ord(char) < 32 for char in name)
    ):
        raise StorageOperationError("Enter a valid folder name without path or reserved characters.")
    parent = _manager_path(root, parent_path, require_exists=True)
    if not parent.is_dir():
        raise StorageOperationError("Choose a parent folder.")
    target = _manager_path(root, "/".join(filter(None, (parent_path, name))))
    try:
        with transaction.atomic():
            target.mkdir()
            _audit_storage_manager(actor, "storage_folder_created", {
                "root_id": record.pk,
                "root": str(root),
                "path": target.relative_to(root).as_posix(),
            })
    except FileExistsError as exc:
        raise StorageOperationError("A file or folder with that name already exists.") from exc
    except OSError as exc:
        raise StorageOperationError("The folder could not be created.") from exc
    except Exception:
        try:
            target.rmdir()
        except OSError:
            logger.exception("Could not roll back folder creation at %s", target)
        raise
    return {"path": target.relative_to(root).as_posix()}


def _sha256_path(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _metadata_local_path(metadata):
    if metadata.storage_backend != StorageImageMetadata.STORAGE_LOCAL:
        return None
    if metadata.destination:
        if metadata.destination.destination_type != StorageDestination.DEST_TYPE_LOCAL:
            return None
        base = _local_root(metadata.destination)
    else:
        base = Path(settings.MEDIA_ROOT)
    return (base / safe_relative_path(metadata.relative_path)).resolve()


def storage_manager_merge(root_id, source_path, destination_path, actor):
    record, root = _storage_manager_root(root_id)
    source = _manager_path(root, source_path, require_exists=True)
    destination = _manager_path(root, destination_path, require_exists=True)
    if not source.is_dir() or not destination.is_dir():
        raise StorageOperationError("Source and destination must both be folders.")
    if source == destination or source in destination.parents or destination in source.parents:
        raise StorageOperationError("Choose separate folders that are not nested within one another.")

    files = []
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
    try:
        def raise_walk_error(error):
            raise error

        for current, dir_names, file_names in os.walk(
            source, followlinks=False, onerror=raise_walk_error
        ):
            current_path = Path(current)
            for name in list(dir_names):
                entry = current_path / name
                if entry.is_symlink():
                    raise StorageOperationError("Symbolic links are not allowed in managed folders.")
            for name in file_names:
                entry = current_path / name
                if (
                    entry.is_symlink()
                    or not entry.is_file()
                    or entry.suffix.casefold() not in allowed_extensions
                ):
                    raise StorageOperationError(
                        "Merge stopped at unsupported file "
                        + entry.relative_to(root).as_posix()
                        + ". Only regular inspection image files can be merged."
                    )
                files.append(entry)
                if len(files) > STORAGE_MANAGER_MAX_FILES:
                    raise StorageOperationError(
                        f"A merge is limited to {STORAGE_MANAGER_MAX_FILES} files at a time."
                    )
    except OSError as exc:
        raise StorageOperationError("The source folder could not be read.") from exc

    plan = []
    for source_file in files:
        suffix = source_file.relative_to(source)
        target = destination / suffix
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise StorageOperationError("A merge target escapes the authorized root.") from exc
        if target.exists():
            if target.is_file() and _sha256_path(source_file) == _sha256_path(target):
                duplicate = True
            else:
                target = target.with_name(
                    f"{target.stem}_merged_{uuid.uuid4().hex[:10]}{target.suffix}"
                )
                while target.exists():
                    target = target.with_name(
                        f"{target.stem}_merged_{uuid.uuid4().hex[:10]}{target.suffix}"
                    )
                duplicate = False
        else:
            duplicate = False
        plan.append((source_file, target, duplicate))

    metadata_by_source = {}
    for metadata in StorageImageMetadata.objects.select_related(
        "destination", "inspection_log", "inspection_session"
    ).iterator(chunk_size=500):
        try:
            stored_path = _metadata_local_path(metadata)
        except (OSError, StorageOperationError):
            continue
        if stored_path is not None:
            metadata_by_source.setdefault(stored_path, []).append(metadata)

    media_root = Path(settings.MEDIA_ROOT).resolve()
    image_field_references = {}
    for record_id, field_value in VisionInspectionLog.objects.exclude(
        inspection_image=""
    ).values_list("pk", "inspection_image").iterator(chunk_size=1000):
        if field_value:
            image_field_references.setdefault(
                (media_root / field_value).resolve(), []
            ).append((VisionInspectionLog, record_id, "inspection_image"))
    session_image_fields = (
        "front_image", "back_image", "front_annotated", "back_annotated",
        "front_roi_image", "back_roi_image",
    )
    session_values = ("pk",) + session_image_fields
    for row in VisionInspectionSession.objects.values_list(*session_values).iterator(
        chunk_size=1000
    ):
        record_id = row[0]
        for field_name, field_value in zip(session_image_fields, row[1:]):
            if field_value:
                image_field_references.setdefault(
                    (media_root / field_value).resolve(), []
                ).append((VisionInspectionSession, record_id, field_name))

    copied = []
    created_directories = set()
    processing_file = None
    try:
        for position, (source_file, target, duplicate) in enumerate(plan):
            processing_file = source_file
            parent = target.parent
            missing = []
            while not parent.exists() and parent != root:
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.add(directory)
            if duplicate and (
                not target.is_file()
                or _sha256_path(source_file) != _sha256_path(target)
            ):
                target = target.with_name(
                    f"{target.stem}_merged_{uuid.uuid4().hex[:10]}{target.suffix}"
                )
                while target.exists():
                    target = target.with_name(
                        f"{target.stem}_merged_{uuid.uuid4().hex[:10]}{target.suffix}"
                    )
                duplicate = False
                plan[position] = (source_file, target, duplicate)
            if not duplicate:
                if source_file.is_symlink() or not source_file.is_file():
                    raise StorageOperationError(
                        f"Source image {source_file.relative_to(root).as_posix()} changed during the merge."
                    )
                while True:
                    try:
                        with source_file.open("rb") as source_stream, target.open("xb") as target_stream:
                            shutil.copyfileobj(source_stream, target_stream, CHUNK_SIZE)
                        copied.append((source_file, target))
                        shutil.copystat(source_file, target)
                        break
                    except FileExistsError:
                        target = target.with_name(
                            f"{target.stem}_merged_{uuid.uuid4().hex[:10]}{target.suffix}"
                        )
                        plan[position] = (source_file, target, False)
                    except Exception:
                        try:
                            target.unlink()
                        except OSError:
                            logger.exception("Could not remove partial merge file %s", target)
                        raise
                if _sha256_path(source_file) != _sha256_path(target):
                    raise StorageOperationError(
                        f"Copied image {source_file.relative_to(root).as_posix()} failed verification."
                    )

        with transaction.atomic():
            metadata_updates = []
            field_updates = []
            for source_file, target, _duplicate in plan:
                processing_file = source_file
                old_metadata = metadata_by_source.get(source_file.resolve(), ())
                for old in old_metadata:
                    locked = StorageImageMetadata.objects.select_for_update().get(pk=old.pk)
                    if locked.relative_path != old.relative_path:
                        raise StorageOperationError(
                            "An image reference changed during merge; the operation was rolled back."
                        )
                    base = (
                        _local_root(locked.destination)
                        if locked.destination_id
                        else Path(settings.MEDIA_ROOT).resolve()
                    )
                    try:
                        new_relative = target.resolve().relative_to(base.resolve()).as_posix()
                    except ValueError as exc:
                        raise StorageOperationError(
                            "A referenced image cannot be represented relative to its storage root."
                        ) from exc
                    locked.relative_path = new_relative
                    locked.save(update_fields=["relative_path", "updated_at"])
                    metadata_updates.append(locked.pk)
                field_references = image_field_references.get(source_file.resolve(), ())
                if field_references:
                    try:
                        relative_target = target.resolve().relative_to(media_root).as_posix()
                    except ValueError as exc:
                        raise StorageOperationError(
                            "An inspection image cannot be moved outside the media storage root."
                        ) from exc
                    for model, record_id, field_name in field_references:
                        model.objects.filter(pk=record_id).update(**{field_name: relative_target})
                        field_updates.append((model._meta.label, record_id, field_name))
            _audit_storage_manager(actor, "storage_folders_merged", {
                "root_id": record.pk,
                "root": str(root),
                "source": source.relative_to(root).as_posix(),
                "destination": destination.relative_to(root).as_posix(),
                "file_count": len(plan),
                "metadata_updated": len(metadata_updates),
                "image_fields_updated": len(field_updates),
            })
    except Exception as exc:
        rollback_failures = []
        for _source_path, new_path in reversed(copied):
            try:
                if new_path.exists():
                    new_path.unlink()
            except OSError:
                logger.exception("Could not roll back storage merge file %s", new_path)
                rollback_failures.append(new_path.relative_to(root).as_posix())
        for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_failures:
            raise StorageOperationError(
                "Merge failed and these destination files could not be rolled back: "
                + ", ".join(rollback_failures)
            ) from exc
        if isinstance(exc, StorageOperationError):
            raise
        if processing_file is not None:
            raise StorageOperationError(
                "Merge failed while processing "
                + processing_file.relative_to(root).as_posix()
                + ". Source files were retained."
            ) from exc
        raise

    source_files_retained = 0
    source_files_retained_paths = []
    for source_file, _target, duplicate in plan:
        if duplicate:
            source_files_retained += 1
            source_files_retained_paths.append(source_file.relative_to(root).as_posix())
            continue
        try:
            source_file.unlink()
        except OSError:
            source_files_retained += 1
            source_files_retained_paths.append(source_file.relative_to(root).as_posix())
            logger.warning("Verified source image remains after merge: %s", source_file)
    return {
        "file_count": len(plan),
        "metadata_updated": len(metadata_updates),
        "source_removed": False,
        "source_files_retained": source_files_retained,
        "source_files_retained_paths": source_files_retained_paths,
        "destination": str(destination),
    }


def validate_destination_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    destination_type = str(payload.get("destination_type") or "SMB").strip().upper()
    supported_types = (
        StorageDestination.DEST_TYPE_SMB,
        StorageDestination.DEST_TYPE_WINDOWS,
        StorageDestination.DEST_TYPE_LINUX,
        StorageDestination.DEST_TYPE_LOCAL,
    )
    if destination_type not in supported_types:
        raise StorageOperationError("Choose Windows SMB, Ubuntu Samba, another SMB share, or Local Storage.")

    host = str(payload.get("ip_address") or payload.get("host") or "").strip()
    share = str(payload.get("share_name") or "").strip().strip("\\/")
    directory = safe_relative_path(payload.get("destination_directory"))
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    name = str(payload.get("name") or "").strip()

    if destination_type != StorageDestination.DEST_TYPE_LOCAL:
        if not host or any(c in host for c in ("/", "\\", "@")):
            raise StorageOperationError("Enter an SMB hostname or IP address.")
        if not share or any(c in share for c in ("/", "\\")):
            raise StorageOperationError("Enter one SMB share name.")
        if not name:
            name = f"{host}/{share}"
    else:
        host = ""
        share = ""
        username = ""
        password = ""
        if not name:
            name = "Local Storage"

    return {
        "name": name[:100],
        "destination_type": destination_type,
        "ip_address": host,
        "share_name": share,
        "destination_directory": directory,
        "username": username,
        "password": password,
    }


def _smbclient():
    try:
        import smbclient

        return smbclient
    except ImportError as exc:
        raise StorageOperationError(
            "SMB support is not installed. Install the storage requirements on this station."
        ) from exc


def _register_smb(destination, password=None):
    client = _smbclient()
    if password is None:
        password = decrypt_credential(destination.password)
    options = {"connection_timeout": int(getattr(settings, "VISION_STORAGE_SMB_TIMEOUT", 5))}
    if destination.username:
        options["username"] = destination.username
    if password:
        options["password"] = password
    client.register_session(destination.ip_address, **options)
    return client


def _remote_path(destination, relative_path, password=None):
    client = _register_smb(destination, password=password)
    base = "\\\\{}\\{}".format(destination.ip_address, destination.share_name)
    parts = [safe_relative_path(destination.destination_directory), safe_relative_path(relative_path)]
    suffix = "\\".join(part.replace("/", "\\") for part in parts if part)
    return client, ntpath.join(base, suffix) if suffix else base


def _local_root(destination):
    configured = getattr(settings, "VISION_STORAGE_LOCAL_ROOT", None)
    root = Path(configured) if configured else Path(settings.MEDIA_ROOT) / "vision_storage"
    root = root.resolve()
    directory = safe_relative_path(destination.destination_directory if destination else "")
    target = (root / directory).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise StorageOperationError("Local destination directory escapes its storage root.") from exc
    return target


def _sha256_stream(stream, output=None):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if output is not None:
            output.write(chunk)
    return size, digest.hexdigest()


def _write_local_verified(root, relative_path, chunks):
    relative_path = safe_relative_path(relative_path)
    destination = (Path(root) / relative_path).resolve()
    root = Path(root).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise StorageOperationError("Storage path escaped its configured root.") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vi-storage-", suffix=".tmp", dir=str(destination.parent))
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            for chunk in chunks:
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify_size, verify_digest = _hash_local(Path(temporary))
        if size != verify_size or digest.hexdigest() != verify_digest:
            raise StorageOperationError("Local storage verification failed.")
        os.replace(temporary, destination)
        return size, verify_digest, destination
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _hash_local(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _write_smb_verified(destination, relative_path, chunks, password=None):
    client, final_path = _remote_path(destination, relative_path, password=password)
    parent = ntpath.dirname(final_path)
    temporary_path = final_path + ".tmp-" + uuid.uuid4().hex
    client.makedirs(parent, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with client.open_file(temporary_path, mode="wb") as output:
            for chunk in chunks:
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        verify_digest = hashlib.sha256()
        verify_size = 0
        with client.open_file(temporary_path, mode="rb") as verify:
            while True:
                chunk = verify.read(CHUNK_SIZE)
                if not chunk:
                    break
                verify_digest.update(chunk)
                verify_size += len(chunk)
        if verify_size != size or verify_digest.hexdigest() != digest.hexdigest():
            raise StorageOperationError("SMB copy verification failed.")
        if client.path.exists(final_path):
            client.remove(final_path)
        client.rename(temporary_path, final_path)
        return size, verify_digest.hexdigest(), final_path
    except Exception:
        try:
            if client.path.exists(temporary_path):
                client.remove(temporary_path)
        except Exception:
            pass
        raise


def _test_local(destination):
    root = _local_root(destination)
    root.mkdir(parents=True, exist_ok=True)
    test_name = ".vi-test-" + uuid.uuid4().hex
    data = uuid.uuid4().bytes
    path = root / test_name
    try:
        path.write_bytes(data)
        if path.read_bytes() != data:
            raise StorageOperationError("Local read-back verification failed.")
        return {"ok": True, "read_ok": True, "write_ok": True}
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def test_destination(payload):
    values = validate_destination_payload(payload)
    if values["destination_type"] == StorageDestination.DEST_TYPE_LOCAL:
        destination = StorageDestination(**{
            key: value for key, value in values.items() if key != "password"
        })
        result = _test_local(destination)
    else:
        # Use an unsaved destination object only to construct the SMB paths.
        destination = StorageDestination(**{key: value for key, value in values.items() if key != "password"})
        client, parent = _remote_path(destination, "", password=values["password"])
        client.makedirs(parent, exist_ok=True)
        test_path = ntpath.join(parent, ".vi-test-" + uuid.uuid4().hex)
        test_data = uuid.uuid4().bytes
        created = True
        try:
            with client.open_file(test_path, mode="xb") as output:
                output.write(test_data)
            with client.open_file(test_path, mode="rb") as source:
                actual = source.read(len(test_data) + 1)
            if actual != test_data:
                raise StorageOperationError("SMB read-back verification failed.")
            result = {"ok": True, "read_ok": True, "write_ok": True}
        finally:
            if created:
                try:
                    client.remove(test_path)
                except Exception:
                    logger.warning("Could not remove temporary SMB test file from %s", values["ip_address"])
    result.update({"status": "CONNECTED", "checked_at": timezone.now().isoformat()})
    return result


def active_destination():
    return StorageConfiguration.get_config().active_destination


def activate_destination(payload):
    values = validate_destination_payload(payload)
    tested = test_destination({**values, "password": values["password"]})
    encrypted = encrypt_credential(values["password"]) if values["password"] else ""
    with transaction.atomic():
        config, _created = StorageConfiguration.objects.get_or_create(pk=1)
        # SQLite ignores SELECT FOR UPDATE; this short write serializes activation.
        StorageConfiguration.objects.filter(pk=1).update(updated_at=timezone.now())
        config = StorageConfiguration.objects.get(pk=1)
        StorageDestination.objects.filter(is_active=True).update(is_active=False)
        destination = StorageDestination.objects.create(
            **{key: value for key, value in values.items() if key != "password"},
            password=encrypted,
            connection_status="CONNECTED",
            last_verified_at=timezone.now(),
            is_active=True,
        )
        config.active_destination = destination
        config.save(update_fields=["active_destination", "updated_at"])
    return destination, tested


def serialize_destination(destination):
    return {
        "id": destination.pk,
        "name": destination.name,
        "destination_type": destination.destination_type,
        "ip_address": destination.ip_address,
        "share_name": destination.share_name,
        "destination_directory": destination.destination_directory,
        "username": destination.username,
        "password_configured": bool(destination.password),
        "connection_status": destination.connection_status,
        "is_active": destination.is_active,
        "created_at": destination.created_at.isoformat(),
        "last_verified_at": destination.last_verified_at.isoformat() if destination.last_verified_at else None,
    }


def _local_volume_usage():
    root = Path(getattr(settings, "VISION_STORAGE_VOLUME_ROOT", None) or settings.MEDIA_ROOT)
    usage = shutil.disk_usage(root)
    percent = round((usage.total - usage.free) * 100.0 / usage.total, 1) if usage.total else 0
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "usage_percent": percent,
    }


def save_automatic_settings(enabled, threshold):
    try:
        threshold = int(threshold)
    except (TypeError, ValueError) as exc:
        raise StorageOperationError("Storage threshold must be a whole percentage from 10 to 95.") from exc
    if not 10 <= threshold <= 95:
        raise StorageOperationError("Storage threshold must be between 10% and 95%.")
    if isinstance(enabled, str):
        normalized = enabled.strip().lower()
        if normalized not in ("true", "false", "1", "0"):
            raise StorageOperationError("Automatic transfer must be enabled or disabled.")
        enabled = normalized in ("true", "1")
    if not isinstance(enabled, bool):
        raise StorageOperationError("Automatic transfer must be enabled or disabled.")
    config = StorageConfiguration.get_config()
    config.automatic_migration_enabled = enabled
    config.migration_threshold_pct = threshold
    config.save(update_fields=["automatic_migration_enabled", "migration_threshold_pct", "updated_at"])
    return config


def ensure_evidence_capacity():
    """Reject new inspections when durable local fallback evidence cannot be written."""
    usage = _local_volume_usage()
    minimum = int(getattr(settings, "VISION_STORAGE_MIN_FREE_BYTES", 20 * 1024 * 1024))
    if usage["free_bytes"] < minimum:
        raise StorageOperationError("Local evidence storage is critically low; free disk space before starting another inspection.")
    probe_dir = Path(settings.MEDIA_ROOT) / "vision_inspection" / "local_fallback"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".vi-capacity-", dir=str(probe_dir))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise StorageOperationError("Local evidence storage is not writable; inspection cannot safely start.") from exc
    return usage


def storage_state():
    config = StorageConfiguration.get_config()
    active = config.active_destination
    destinations = [serialize_destination(item) for item in StorageDestination.objects.all()]
    summary = StorageImageMetadata.objects.filter(destination=active).aggregate(
        image_count=Count("pk"),
        bytes=Sum("file_size"),
    ) if active else StorageImageMetadata.objects.filter(
        storage_backend=StorageImageMetadata.STORAGE_LOCAL, destination__isnull=True
    ).aggregate(
        image_count=Count("pk"),
        bytes=Sum("file_size"),
    )
    try:
        usage = _local_volume_usage()
        summary.update(usage)
    except OSError:
        summary.update({"total_bytes": None, "free_bytes": None, "used_bytes": None, "usage_percent": None})
    pending = StorageImageMetadata.objects.filter(
        storage_backend=StorageImageMetadata.STORAGE_LOCAL,
        destination__isnull=True,
        is_permanent_local=False,
    ).filter(Q(migration_status__in=(StorageImageMetadata.MIGRATION_PENDING, StorageImageMetadata.MIGRATION_FAILED)))
    if active:
        pending = pending.filter(Q(migration_destination__isnull=True) | Q(migration_destination=active))
    latest_failure = StorageTransferHistory.objects.filter(
        status=StorageTransferHistory.STATUS_FAILED
    ).first()
    return {
        "active": serialize_destination(active) if active else None,
        "destinations": destinations,
        "automatic": {
            "enabled": config.automatic_migration_enabled,
            "threshold_percent": config.migration_threshold_pct,
            "destination_id": config.active_destination_id,
            "destination_status": active.connection_status if active else "NOT_CONFIGURED",
            "pending_files": pending.count(),
            "transferred_files": StorageTransferHistory.objects.filter(status=StorageTransferHistory.STATUS_SUCCESS).count(),
            "last_transfer_at": config.last_transfer_at.isoformat() if config.last_transfer_at else None,
            "last_transfer_error": config.last_transfer_error or (latest_failure.error_message if latest_failure else ""),
        },
        "summary": {
            "image_count": summary.get("image_count") or 0,
            "bytes": summary.get("bytes") or 0,
            "total_bytes": summary.get("total_bytes"),
            "free_bytes": summary.get("free_bytes"),
            "used_bytes": summary.get("used_bytes"),
            "usage_percent": summary.get("usage_percent"),
        },
        "local_migration_count": StorageImageMetadata.objects.filter(
            storage_backend=StorageImageMetadata.STORAGE_LOCAL, destination__isnull=True
        ).count(),
    }


def _image_hash(file_object):
    return _sha256_stream(file_object)


def register_image(instance, field_name, payload, filename):
    """Persist a local safety copy, then mirror to the active destination."""
    payload = bytes(payload or b"")
    if not payload:
        raise StorageOperationError("Image evidence is empty and cannot be saved.")
    key = uuid.uuid4().hex
    suffix = Path(filename or "image.jpg").suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    relative_key = key + suffix
    fallback_name = f"vision_inspection/local_fallback/{relative_key}"
    saved_fallback = default_storage.save(fallback_name, ContentFile(payload))
    digest = hashlib.sha256(payload).hexdigest()
    active = active_destination()

    values = {
        "storage_key": key,
        "image_field_name": field_name,
        "relative_path": saved_fallback,
        "storage_backend": StorageImageMetadata.STORAGE_LOCAL,
        "destination": None,
        "migration_status": StorageImageMetadata.MIGRATION_PENDING,
        "file_size": len(payload),
        "sha256_checksum": digest,
    }
    if active and active.destination_type == StorageDestination.DEST_TYPE_LOCAL:
        try:
            _write_local_verified(_local_root(active), relative_key, (payload[i:i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)))
            values.update({"destination": active, "relative_path": relative_key})
        except Exception as exc:
            values.update({
                "migration_destination": active,
                "migration_status": StorageImageMetadata.MIGRATION_FAILED,
                "migration_error": str(exc)[:1000],
            })
    elif active:
        try:
            password = decrypt_credential(active.password)
            size, verified_hash, _path = _write_smb_verified(
                active, relative_key,
                (payload[i:i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)),
                password=password,
            )
            values.update({
                "storage_backend": StorageImageMetadata.STORAGE_NETWORK,
                "destination": active,
                "relative_path": relative_key,
                "file_size": size,
                "sha256_checksum": verified_hash,
                "checksum_verified_at": timezone.now(),
                "migration_status": StorageImageMetadata.MIGRATION_SUCCESS,
            })
            StorageDestination.objects.filter(pk=active.pk).update(
                connection_status="CONNECTED",
                last_verified_at=timezone.now(),
            )
        except Exception as exc:
            values.update({
                "migration_destination": active,
                "migration_status": StorageImageMetadata.MIGRATION_FAILED,
                "migration_error": str(exc)[:1000],
            })
            StorageDestination.objects.filter(pk=active.pk).update(
                connection_status="ERROR",
                last_verified_at=timezone.now(),
            )
            logger.warning("SMB image mirror failed for destination %s; retained local evidence", active.pk)

    field = getattr(instance, field_name)
    field.name = saved_fallback
    try:
        metadata = StorageImageMetadata.objects.create(
            inspection_log=instance if instance._meta.model_name == "visioninspectionlog" else None,
            inspection_session=instance if instance._meta.model_name == "visioninspectionsession" else None,
            **values,
        )
    except Exception:
        logger.exception("Could not register storage metadata for image %s", field_name)
        raise StorageOperationError("Image was retained locally but its storage reference could not be saved.")
    if metadata.relative_path != saved_fallback:
        try:
            default_storage.delete(saved_fallback)
        except Exception:
            logger.warning("Verified evidence %s has a redundant local safety copy", metadata.storage_key)
    return metadata


def register_inspection_image(instance, field_name, payload, captured_at):
    """Persist inspection evidence permanently under MEDIA_ROOT."""
    if instance._meta.model_name != "visioninspectionsession":
        raise StorageOperationError("Inspection evidence must be linked to a session.")
    payload = bytes(payload or b"")
    if not payload:
        raise StorageOperationError("Image evidence is empty and cannot be saved.")
    if timezone.is_naive(captured_at):
        captured_at = timezone.make_aware(captured_at, timezone.get_current_timezone())

    sequence = StorageImageMetadata.objects.filter(
        inspection_session=instance,
        image_field_name=field_name,
    ).count() + 1
    timestamp = timezone.localtime(captured_at).strftime("%Y%m%d_%H%M%S")
    while True:
        filename = f"{field_name}_{sequence}_{timestamp}.jpg"
        relative_path = f"vision_inspection/inspections/{filename}"
        if not default_storage.exists(relative_path):
            break
        sequence += 1

    saved_path = default_storage.save(relative_path, ContentFile(payload))
    try:
        metadata = StorageImageMetadata.objects.create(
            storage_key=uuid.uuid4().hex,
            inspection_session=instance,
            image_field_name=field_name,
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            relative_path=saved_path,
            migration_status=StorageImageMetadata.MIGRATION_PENDING,
            file_size=len(payload),
            sha256_checksum=hashlib.sha256(payload).hexdigest(),
            captured_at=captured_at,
            is_permanent_local=True,
        )
    except Exception as exc:
        try:
            default_storage.delete(saved_path)
        except Exception:
            logger.exception("Could not remove unregistered inspection image %s", saved_path)
        logger.exception("Could not register inspection image %s", saved_path)
        raise StorageOperationError(
            "Image was retained locally but its storage reference could not be saved."
        ) from exc

    getattr(instance, field_name).name = saved_path
    return metadata


def delete_uncommitted_inspection_image(metadata):
    """Remove an image file when its surrounding inspection transaction rolls back."""
    if (
        metadata.storage_backend != StorageImageMetadata.STORAGE_LOCAL
        or not metadata.is_permanent_local
        or not metadata.relative_path.startswith("vision_inspection/inspections/")
    ):
        raise StorageOperationError("Refusing to delete an image outside inspection evidence storage.")
    try:
        default_storage.delete(metadata.relative_path)
    except Exception as exc:
        logger.exception(
            "Could not remove inspection evidence after a database rollback: %s",
            metadata.relative_path,
        )
        raise StorageOperationError("Uncommitted inspection evidence could not be removed.") from exc


def metadata_for_field(instance, field_name, backfill=True):
    lookup = {"image_field_name": field_name}
    if instance._meta.model_name == "visioninspectionlog":
        lookup["inspection_log"] = instance
    elif instance._meta.model_name == "visioninspectionsession":
        lookup["inspection_session"] = instance
    else:
        return None
    metadata = StorageImageMetadata.objects.filter(**lookup).first()
    if metadata or not backfill:
        return metadata
    field = getattr(instance, field_name, None)
    name = getattr(field, "name", "") if field else ""
    if not name or not field.storage.exists(name):
        return None
    try:
        with field.storage.open(name, "rb") as source:
            size, digest = _image_hash(source)
        values = {
            "storage_key": uuid.uuid4().hex,
            "image_field_name": field_name,
            "storage_backend": StorageImageMetadata.STORAGE_LOCAL,
            "relative_path": name,
            "file_size": size,
            "sha256_checksum": digest,
        }
        if instance._meta.model_name == "visioninspectionlog":
            values["inspection_log"] = instance
        else:
            values["inspection_session"] = instance
        with transaction.atomic():
            metadata, _created = StorageImageMetadata.objects.get_or_create(
                **lookup, defaults=values
            )
        return metadata
    except IntegrityError:
        return StorageImageMetadata.objects.filter(**lookup).first()
    except Exception:
        logger.exception("Could not backfill local image reference for %s", field_name)
        return None


def link_image_reference(source_instance, source_field_name, target_instance, target_field_name):
    """Give a second model field its own metadata row for the same evidence."""
    source_metadata = metadata_for_field(source_instance, source_field_name)
    if source_metadata is None:
        raise StorageOperationError("The source evidence has no verified storage reference.")
    source_field = getattr(source_instance, source_field_name)
    target_field = getattr(target_instance, target_field_name)
    target_field.name = source_field.name
    target_instance.save(update_fields=[target_field_name])
    values = {
        "storage_key": uuid.uuid4().hex,
        "image_field_name": target_field_name,
        "storage_backend": source_metadata.storage_backend,
        "destination": source_metadata.destination,
        "relative_path": source_metadata.relative_path,
        "migration_status": source_metadata.migration_status,
        "migration_destination": source_metadata.migration_destination,
        "file_size": source_metadata.file_size,
        "sha256_checksum": source_metadata.sha256_checksum,
        "checksum_verified_at": source_metadata.checksum_verified_at,
        "captured_at": source_metadata.captured_at,
        "is_permanent_local": source_metadata.is_permanent_local,
    }
    if target_instance._meta.model_name == "visioninspectionlog":
        values["inspection_log"] = target_instance
    elif target_instance._meta.model_name == "visioninspectionsession":
        values["inspection_session"] = target_instance
    else:
        raise StorageOperationError("This image record type is not supported.")
    return StorageImageMetadata.objects.create(**values)


def image_url(instance, field_name):
    metadata = metadata_for_field(instance, field_name)
    if not metadata:
        return ""
    return reverse("VisionInspection:storage-image", args=[metadata.pk])


def _open_metadata(metadata):
    if metadata.storage_backend == StorageImageMetadata.STORAGE_NETWORK:
        if metadata.destination is None:
            raise StorageOperationError("The original network destination is no longer configured.")
        client, path = _remote_path(metadata.destination, metadata.relative_path)
        return client.open_file(path, mode="rb")
    if metadata.destination and metadata.destination.destination_type == StorageDestination.DEST_TYPE_LOCAL:
        path = _local_root(metadata.destination) / safe_relative_path(metadata.relative_path)
        return path.open("rb")
    return default_storage.open(metadata.relative_path, "rb")


def open_image(metadata):
    try:
        return _open_metadata(metadata)
    except Exception as exc:
        logger.warning("Image storage reference %s is unavailable: %s", metadata.storage_key, exc)
        raise StorageOperationError("This image is temporarily unavailable at its original destination.") from exc


def _source_stream(metadata):
    return open_image(metadata)


def _write_destination_stream(destination, relative_path, source, expected_size=0, expected_checksum=""):
    if destination.destination_type == StorageDestination.DEST_TYPE_LOCAL:
        root = _local_root(destination)
        root.mkdir(parents=True, exist_ok=True)
        relative_path = safe_relative_path(relative_path)
        final_path = (root / relative_path).resolve()
        final_path.relative_to(root.resolve())
        final_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".vi-migrate-", suffix=".tmp", dir=str(final_path.parent))
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                size, checksum = _sha256_stream(source, output)
                output.flush()
                os.fsync(output.fileno())
            if expected_size and size != expected_size:
                raise StorageOperationError("Source image size does not match its stored reference.")
            if expected_checksum and checksum != expected_checksum:
                raise StorageOperationError("Source image checksum does not match its stored reference.")
            verify_size, verify_hash = _hash_local(Path(temporary))
            if size != verify_size or checksum != verify_hash:
                raise StorageOperationError("Migration copy verification failed.")
            os.replace(temporary, final_path)
            return size, verify_hash
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    client, final_path = _remote_path(destination, relative_path)
    client.makedirs(ntpath.dirname(final_path), exist_ok=True)
    temp_path = final_path + ".tmp-" + uuid.uuid4().hex
    digest = hashlib.sha256()
    size = 0
    try:
        with client.open_file(temp_path, mode="wb") as output:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        verify_digest = hashlib.sha256()
        verify_size = 0
        with client.open_file(temp_path, mode="rb") as verify:
            while True:
                chunk = verify.read(CHUNK_SIZE)
                if not chunk:
                    break
                verify_digest.update(chunk)
                verify_size += len(chunk)
        if size != verify_size or digest.hexdigest() != verify_digest.hexdigest():
            raise StorageOperationError("Migration copy verification failed.")
        if expected_size and verify_size != expected_size:
            raise StorageOperationError("Source image size does not match its stored reference.")
        if expected_checksum and verify_digest.hexdigest() != expected_checksum:
            raise StorageOperationError("Source image checksum does not match its stored reference.")
        if client.path.exists(final_path):
            client.remove(final_path)
        client.rename(temp_path, final_path)
        return size, verify_digest.hexdigest()
    except Exception:
        try:
            if client.path.exists(temp_path):
                client.remove(temp_path)
        except Exception:
            pass
        raise


def _delete_original(metadata, old_backend, old_destination, old_path):
    aliases = StorageImageMetadata.objects.filter(
        storage_backend=old_backend,
        destination=old_destination,
        relative_path=old_path,
    ).exclude(pk=metadata.pk)
    if aliases.exists():
        return
    if old_backend == StorageImageMetadata.STORAGE_NETWORK and old_destination:
        client, path = _remote_path(old_destination, old_path)
        client.remove(path)
    elif old_destination and old_destination.destination_type == StorageDestination.DEST_TYPE_LOCAL:
        path = _local_root(old_destination) / safe_relative_path(old_path)
        path.unlink(missing_ok=True)
    else:
        default_storage.delete(old_path)


def migrate_image(metadata, destination, delete_originals=False):
    if metadata.is_permanent_local:
        raise StorageOperationError("Permanent local inspection evidence cannot be migrated.")
    old_backend = metadata.storage_backend
    old_destination = metadata.destination
    old_path = metadata.relative_path
    extension = Path(old_path).suffix or ".jpg"
    target_path = metadata.storage_key + extension
    source = _source_stream(metadata)
    try:
        size, checksum = _write_destination_stream(
            destination, target_path, source,
            expected_size=metadata.file_size,
            expected_checksum=metadata.sha256_checksum,
        )
    finally:
        source.close()
    if metadata.file_size and size != metadata.file_size:
        raise StorageOperationError("Migration file size did not match the source reference.")
    if metadata.sha256_checksum and checksum != metadata.sha256_checksum:
        raise StorageOperationError("Migration checksum did not match the source reference.")

    with transaction.atomic():
        locked = StorageImageMetadata.objects.select_for_update().get(pk=metadata.pk)
        if locked.relative_path != old_path or locked.destination_id != getattr(old_destination, "pk", None):
            raise StorageOperationError("The image reference changed during migration; retry this image.")
        locked.storage_backend = (
            StorageImageMetadata.STORAGE_NETWORK
            if destination.destination_type != StorageDestination.DEST_TYPE_LOCAL
            else StorageImageMetadata.STORAGE_LOCAL
        )
        locked.destination = destination
        locked.relative_path = target_path
        locked.file_size = size
        locked.sha256_checksum = checksum
        locked.checksum_verified_at = timezone.now()
        locked.migration_destination = None
        locked.migration_status = StorageImageMetadata.MIGRATION_SUCCESS
        locked.migration_error = ""
        locked.migration_completed_at = timezone.now()
        locked.save()
        StorageTransferHistory.objects.create(
            image_metadata=locked,
            source_storage_backend=old_backend,
            destination_storage_backend=locked.storage_backend,
            source_destination=old_destination,
            destination_destination=destination,
            file_size=size,
            source_checksum=metadata.sha256_checksum,
            destination_checksum=checksum,
            status=StorageTransferHistory.STATUS_SUCCESS,
            completed_at=timezone.now(),
        )
    if delete_originals:
        try:
            _delete_original(metadata, old_backend, old_destination, old_path)
        except Exception as exc:
            logger.warning("Verified migration %s kept its original: %s", metadata.pk, exc)
    return size, checksum


def start_migration(source_destination_id, destination, delete_originals=False, confirmation=""):
    if destination is None:
        raise StorageOperationError("Activate a destination before starting migration.")
    if delete_originals and confirmation != "DELETE":
        raise StorageOperationError("Confirm deletion by entering DELETE.")
    if str(source_destination_id or "") == "LOCAL":
        source = None
        source_filter = {
            "storage_backend": StorageImageMetadata.STORAGE_LOCAL,
            "destination__isnull": True,
            "is_permanent_local": False,
        }
    else:
        try:
            source = StorageDestination.objects.get(pk=int(source_destination_id))
        except (TypeError, ValueError, StorageDestination.DoesNotExist) as exc:
            raise StorageOperationError("Choose a valid source destination.") from exc
        if source.pk == destination.pk:
            raise StorageOperationError("Source and active destination must be different.")
        source_filter = {"destination": source}
    count = StorageImageMetadata.objects.filter(**source_filter).count()
    return StorageMigrationJob.objects.create(
        source_destination=source,
        destination=destination,
        delete_originals=bool(delete_originals),
        status=StorageMigrationJob.STATUS_PENDING,
        total_count=count,
    )


def _claim_metadata(metadata):
    now = timezone.now()
    eligible = (
        StorageImageMetadata.MIGRATION_PENDING,
        StorageImageMetadata.MIGRATION_FAILED,
        StorageImageMetadata.MIGRATION_RETRYING,
        StorageImageMetadata.MIGRATION_SUCCESS,
    )
    return StorageImageMetadata.objects.filter(
        pk=metadata.pk,
        migration_status__in=eligible,
    ).update(
        migration_status=StorageImageMetadata.MIGRATION_IN_PROGRESS,
        migration_started_at=now,
        updated_at=now,
    ) == 1


def process_migration_batch(job_id, batch_size=10):
    job = StorageMigrationJob.objects.get(pk=job_id)
    if job.status == StorageMigrationJob.STATUS_COMPLETED:
        return migration_status(job)
    batch_size = max(1, min(int(batch_size), 25))
    if job.source_destination_id is None:
        query = StorageImageMetadata.objects.filter(
            storage_backend=StorageImageMetadata.STORAGE_LOCAL,
            destination__isnull=True,
            is_permanent_local=False,
        )
    else:
        query = StorageImageMetadata.objects.filter(destination=job.source_destination)
    rows = list(query.filter(pk__gt=job.last_metadata_id).order_by("pk")[:batch_size])
    if not rows:
        job.status = StorageMigrationJob.STATUS_COMPLETED
        job.save(update_fields=["status", "updated_at"])
        return migration_status(job)
    job.status = StorageMigrationJob.STATUS_RUNNING
    job.save(update_fields=["status", "updated_at"])
    for metadata in rows:
        if not _claim_metadata(metadata):
            job.last_metadata_id = metadata.pk
            job.save(update_fields=["last_metadata_id", "updated_at"])
            continue
        try:
            migrate_image(metadata, job.destination, job.delete_originals)
            job.success_count += 1
        except Exception as exc:
            StorageImageMetadata.objects.filter(pk=metadata.pk).update(
                migration_status=StorageImageMetadata.MIGRATION_FAILED,
                migration_destination=job.destination,
                migration_attempt=metadata.migration_attempt + 1,
                migration_error=str(exc)[:1000],
                updated_at=timezone.now(),
            )
            StorageTransferHistory.objects.create(
                image_metadata=metadata,
                source_storage_backend=metadata.storage_backend,
                destination_storage_backend=(
                    StorageImageMetadata.STORAGE_NETWORK
                    if job.destination.destination_type != StorageDestination.DEST_TYPE_LOCAL
                    else StorageImageMetadata.STORAGE_LOCAL
                ),
                source_destination=metadata.destination,
                destination_destination=job.destination,
                file_size=metadata.file_size,
                source_checksum=metadata.sha256_checksum,
                status=StorageTransferHistory.STATUS_FAILED,
                retry_count=metadata.migration_attempt + 1,
                error_code="COPY_OR_VERIFY_FAILED",
                error_message=str(exc)[:1000],
            )
            job.failure_count += 1
            job.last_error = str(exc)[:1000]
        job.last_metadata_id = metadata.pk
        job.save(update_fields=[
            "last_metadata_id", "success_count", "failure_count", "last_error", "updated_at"
        ])
    return migration_status(job)


def automatic_transfer_batch(batch_size=None):
    """Transfer one oldest-first bounded batch after crossing the saved threshold."""
    config = StorageConfiguration.get_config()
    now = timezone.now()
    config.last_monitoring_run = now
    config.save(update_fields=["last_monitoring_run", "updated_at"])
    try:
        usage = _local_volume_usage()
    except OSError as exc:
        config.last_transfer_error = f"Local storage usage could not be read: {exc}"[:1000]
        config.save(update_fields=["last_transfer_error", "updated_at"])
        return {"started": False, "reason": "usage_unavailable"}

    destination = config.active_destination
    if not config.automatic_migration_enabled:
        return {"started": False, "reason": "disabled", **usage}
    if not config.monitoring_enabled:
        return {"started": False, "reason": "monitoring disabled", **usage}
    if destination is None or destination.destination_type == StorageDestination.DEST_TYPE_LOCAL:
        return {"started": False, "reason": "SMB destination not active", **usage}
    if usage["usage_percent"] < config.migration_threshold_pct:
        return {"started": False, "reason": "below_threshold", **usage}

    limit = max(1, min(int(batch_size or config.max_files_per_batch or 10), 25))
    candidates = StorageImageMetadata.objects.filter(
        storage_backend=StorageImageMetadata.STORAGE_LOCAL,
        destination__isnull=True,
        is_permanent_local=False,
    ).filter(
        Q(migration_destination__isnull=True) | Q(migration_destination=destination)
    ).filter(
        Q(migration_status=StorageImageMetadata.MIGRATION_PENDING)
        | Q(
            migration_status=StorageImageMetadata.MIGRATION_FAILED,
            migration_attempt__lt=config.retry_max_attempts,
        )
    ).order_by("created_at", "pk")

    success_count = 0
    failure_count = 0
    last_error = ""
    delay_base = max(1, int(config.retry_base_delay_seconds or 300))
    for metadata in candidates[:limit]:
        if metadata.migration_status == StorageImageMetadata.MIGRATION_FAILED:
            delay = min(delay_base * (2 ** max(0, metadata.migration_attempt - 1)), 3600)
            if metadata.updated_at and (now - metadata.updated_at).total_seconds() < delay:
                continue
        if not _claim_metadata(metadata):
            continue
        try:
            migrate_image(metadata, destination, delete_originals=True)
            success_count += 1
            StorageDestination.objects.filter(pk=destination.pk).update(
                connection_status="CONNECTED",
                last_verified_at=timezone.now(),
            )
            config.last_transfer_at = timezone.now()
            config.last_transfer_error = ""
        except Exception as exc:
            failure_count += 1
            last_error = str(exc)[:1000]
            StorageImageMetadata.objects.filter(pk=metadata.pk).update(
                migration_status=StorageImageMetadata.MIGRATION_FAILED,
                migration_destination=destination,
                migration_attempt=metadata.migration_attempt + 1,
                migration_error=last_error,
                updated_at=timezone.now(),
            )
            StorageDestination.objects.filter(pk=destination.pk).update(
                connection_status="ERROR",
                last_verified_at=timezone.now(),
            )
            StorageTransferHistory.objects.create(
                image_metadata=metadata,
                source_storage_backend=StorageImageMetadata.STORAGE_LOCAL,
                destination_storage_backend=StorageImageMetadata.STORAGE_NETWORK,
                source_destination=None,
                destination_destination=destination,
                file_size=metadata.file_size,
                source_checksum=metadata.sha256_checksum,
                status=StorageTransferHistory.STATUS_FAILED,
                retry_count=metadata.migration_attempt + 1,
                error_code="AUTO_TRANSFER_FAILED",
                error_message=last_error,
            )
            config.last_transfer_error = last_error
        config.last_transfer_at = timezone.now()
        config.save(update_fields=["last_transfer_at", "last_transfer_error", "updated_at"])
    try:
        usage = _local_volume_usage()
    except OSError:
        pass
    return {
        "started": bool(success_count or failure_count),
        "success": success_count,
        "failed": failure_count,
        "last_error": last_error,
        **usage,
    }


def migration_status(job):
    job.refresh_from_db()
    return {
        "id": job.pk,
        "status": job.status,
        "total": job.total_count,
        "success": job.success_count,
        "failed": job.failure_count,
        "last_error": job.last_error,
        "done": job.status == StorageMigrationJob.STATUS_COMPLETED,
    }


def open_storage_image(metadata_id):
    metadata = StorageImageMetadata.objects.select_related("destination").get(pk=metadata_id)
    return metadata, open_image(metadata)
