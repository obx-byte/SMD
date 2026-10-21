"""Staff-only storage settings APIs and per-image delivery."""

import json
import logging
import mimetypes

from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from VisionInspection.models import StorageDestination, StorageImageMetadata, StorageMigrationJob
from VisionInspection.services import storage_service
from VisionInspection.services.access_control import can_view_log, can_view_session, is_admin

logger = logging.getLogger(__name__)


def _payload(request):
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json(data, status=200):
    return JsonResponse(data, status=status)


@require_GET
def state(request):
    return _json({"ok": True, **storage_service.storage_state()})


@require_POST
def automatic_settings(request):
    data = _payload(request)
    try:
        config = storage_service.save_automatic_settings(
            data.get("enabled"), data.get("threshold_percent")
        )
        return _json({
            "ok": True,
            "enabled": config.automatic_migration_enabled,
            "threshold_percent": config.migration_threshold_pct,
            "destination_id": config.active_destination_id,
        })
    except storage_service.StorageOperationError as exc:
        return _json({"ok": False, "message": str(exc)}, 400)


@require_POST
def test_destination(request):
    data = _payload(request)
    try:
        result = storage_service.test_destination(data)
        return _json({"ok": True, **result})
    except storage_service.StorageOperationError as exc:
        message = str(exc)
        password = str(data.get("password") or "")
        if password:
            message = message.replace(password, "[redacted]")
        return _json({"ok": False, "status": "CONNECTION_FAILED", "message": message}, 400)
    except Exception as exc:
        password = str(data.get("password") or "")
        message = str(exc)
        if password:
            message = message.replace(password, "[redacted]")
        logger.warning("Storage connection test failed: %s", message)
        return _json({"ok": False, "status": "CONNECTION_FAILED", "message": message or "Connection test failed."}, 400)


@require_POST
def activate_destination(request):
    try:
        destination, tested = storage_service.activate_destination(_payload(request))
        return _json({
            "ok": True,
            "status": "CONNECTED",
            "message": "Destination tested and activated.",
            "destination": storage_service.serialize_destination(destination),
            "test": tested,
        }, 201)
    except storage_service.StorageOperationError as exc:
        return _json({"ok": False, "status": "CONNECTION_FAILED", "message": str(exc)}, 400)
    except Exception as exc:
        logger.exception("Could not activate storage destination")
        return _json({"ok": False, "status": "CONNECTION_FAILED", "message": "Destination activation failed. The previous destination remains active."}, 400)


@require_POST
def migration_start(request):
    data = _payload(request)
    try:
        job = storage_service.start_migration(
            data.get("source_destination_id"),
            StorageDestination.objects.filter(is_active=True).first(),
            delete_originals=bool(data.get("delete_originals")),
            confirmation=str(data.get("delete_confirmation") or ""),
        )
        return _json({"ok": True, "job": storage_service.migration_status(job)}, 201)
    except storage_service.StorageOperationError as exc:
        return _json({"ok": False, "message": str(exc)}, 400)


@require_POST
def migration_batch(request, job_id):
    try:
        result = storage_service.process_migration_batch(job_id, batch_size=10)
        return _json({"ok": True, "job": result})
    except StorageMigrationJob.DoesNotExist:
        return _json({"ok": False, "message": "Migration job not found."}, 404)
    except Exception as exc:
        logger.exception("Migration batch failed for job %s", job_id)
        return _json({"ok": False, "message": "This migration batch failed. The source files remain unchanged."}, 400)


@require_GET
def migration_status(request, job_id):
    job = get_object_or_404(StorageMigrationJob, pk=job_id)
    return _json({"ok": True, "job": storage_service.migration_status(job)})


@require_GET
def image(request, metadata_id):
    metadata = get_object_or_404(
        StorageImageMetadata.objects.select_related(
            "destination", "inspection_log", "inspection_session"
        ),
        pk=metadata_id,
    )
    if metadata.inspection_session_id:
        if not can_view_session(request.user, metadata.inspection_session):
            return _json({"ok": False, "message": "You cannot access this inspection evidence."}, 403)
    elif metadata.inspection_log_id:
        if not can_view_log(request.user, metadata.inspection_log):
            return _json({"ok": False, "message": "You cannot access this inspection evidence."}, 403)
    elif not is_admin(request.user):
        return _json({"ok": False, "message": "This image is not linked to an authorized inspection."}, 403)
    try:
        stream = storage_service.open_image(metadata)
    except storage_service.StorageOperationError as exc:
        return _json({"ok": False, "available": False, "message": str(exc)}, 503)

    content_type = mimetypes.guess_type(metadata.relative_path)[0] or "application/octet-stream"
    if metadata.storage_backend == StorageImageMetadata.STORAGE_LOCAL:
        response = FileResponse(stream, content_type=content_type)
    else:
        def chunks():
            try:
                while True:
                    block = stream.read(256 * 1024)
                    if not block:
                        break
                    yield block
            finally:
                stream.close()

        response = StreamingHttpResponse(chunks(), content_type=content_type)
        if metadata.file_size:
            response["Content-Length"] = str(metadata.file_size)
    response["Cache-Control"] = "private, max-age=60"
    response["X-Content-Type-Options"] = "nosniff"
    if request.GET.get("download") == "1":
        response["Content-Disposition"] = f'attachment; filename="inspection-image-{metadata.pk}{mimetypes.guess_extension(content_type) or ".bin"}"'
    return response
