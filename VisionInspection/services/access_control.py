from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.utils.cache import patch_cache_control

from VisionInspection.models import (
    VisionInspectionPermissionOverride,
    VisionInspectionSession,
    VisionInspectionUserProfile,
)

ROLE_OPERATOR = "Operator"
ROLE_SUPERVISOR = "Supervisor"
ROLE_ADMIN = "Administrator"
ROLE_QUALITY = "Quality User"

PERMISSION_CATALOG = (
    ("view_inspection_dashboard", "Dashboard", "View"),
    ("perform_inspection", "Start Inspection", "Perform"),
    ("view_inspection_reports", "Inspection Report", "View"),
    ("export_inspection_reports", "Inspection Report", "Export"),
    ("view_master_data", "Master Data", "View"),
    ("create_master_data", "Master Data", "Create"),
    ("change_master_data", "Master Data", "Edit"),
    ("delete_master_data", "Master Data", "Delete"),
    ("view_part_vision_configuration", "Part Vision Setup", "View"),
    ("change_part_vision_configuration", "Part Vision Setup", "Configure"),
    ("view_storage_settings", "Storage Settings", "View"),
    ("change_storage_settings", "Storage Settings", "Configure"),
    ("view_camera_setup", "Camera Setup", "View"),
    ("change_camera_roi_settings", "Camera Setup", "Configure"),
    ("view_model_configuration", "Model Configuration", "View"),
    ("change_model_configuration", "Model Configuration", "Configure"),
    ("view_approved_models", "Approved Models", "View"),
    ("manage_approved_models", "Approved Models", "Manage"),
    ("view_yolo_models", "YOLO Models", "View"),
    ("manage_yolo_models", "YOLO Models", "Manage"),
    ("view_training_jobs", "Training Jobs", "View"),
    ("manage_training_jobs", "Training Jobs", "Manage"),
    ("manage_inspection_users", "User Management", "Manage users"),
    ("manage_inspection_permissions", "User Management", "Manage permissions"),
    ("view_inspection_audit_log", "Audit Log", "View"),
    ("view_inspection_state", "Inspection State", "View"),
    ("view_roi_configuration", "ROI Configuration", "View"),
    ("change_roi_configuration", "ROI Configuration", "Configure"),
    ("view_trigger_zone_configuration", "Trigger Zone Configuration", "View"),
    ("change_trigger_zone_configuration", "Trigger Zone Configuration", "Configure"),
    ("view_inspection_presets", "Inspection Presets", "View"),
    ("change_inspection_presets", "Inspection Presets", "Configure"),
    ("view_master_images", "Master Images", "View"),
    ("change_master_images", "Master Images", "Configure"),
    ("view_system_settings", "System Settings", "View"),
    ("change_system_settings", "System Settings", "Configure"),
)

DEFAULT_ROLE_PERMISSIONS = {
    ROLE_OPERATOR: {"perform_inspection"},
    ROLE_SUPERVISOR: {
        "view_inspection_dashboard",
        "view_inspection_reports",
        "export_inspection_reports",
        "view_master_data",
        "view_storage_settings",
    },
}


def roles(user):
    if not user.is_authenticated or not user.is_active:
        return set()
    if not hasattr(user, "_vi_role_names"):
        user._vi_role_names = set(user.groups.values_list("name", flat=True))
    return user._vi_role_names


def is_admin(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and (user.is_superuser or ROLE_ADMIN in roles(user))
    )


def has_permission(user, codename):
    """Resolve one permission from an Admin-managed user override or Django RBAC."""
    if not user.is_authenticated or not user.is_active:
        return False
    if is_admin(user):
        return True

    cache = getattr(user, "_vi_permission_cache", None)
    if cache is None:
        cache = {}
        user._vi_permission_cache = cache
    if codename in cache:
        return cache[codename]

    override = (
        VisionInspectionPermissionOverride.objects.filter(
            user_id=user.pk,
            permission__content_type__app_label="vision_inspection",
            permission__codename=codename,
        )
        .values_list("granted", flat=True)
        .first()
    )
    if override is not None:
        cache[codename] = override
    else:
        cache[codename] = user.has_perm(f"vision_inspection.{codename}")
    return cache[codename]


def _allowed(user, codename):
    return is_admin(user) or has_permission(user, codename)


def can_view_session(user, session):
    if not user.is_authenticated or not user.is_active:
        return False
    if is_admin(user):
        return True
    if ROLE_OPERATOR in roles(user):
        return (
            session.operator_id == user.pk
            or str(session.employee_code or "").casefold()
            == user.get_username().casefold()
        )
    if has_permission(user, "view_inspection_history") or has_permission(
        user, "view_inspection_reports"
    ):
        try:
            scopes = {
                item.strip().upper()
                for item in user.vision_inspection_profile.station_scope.split(",")
                if item.strip()
            }
        except VisionInspectionUserProfile.DoesNotExist:
            scopes = set()
        return bool(session.station_id and session.station_id.upper() in scopes)
    return False


def can_view_log(user, log):
    if not user.is_authenticated or not user.is_active:
        return False
    if is_admin(user):
        return True
    if log.session_id:
        return can_view_session(user, log.session)
    if ROLE_OPERATOR in roles(user):
        return log.operator_id == user.pk
    return has_permission(user, "view_inspection_reports") or has_permission(
        user, "view_inspection_history"
    )


def _forbidden(request):
    if not request.user.is_authenticated or not request.user.is_active:
        return redirect_to_login(request.get_full_path(), "/vision-inspection/login/")
    if request.path.startswith("/vision-inspection/api/") or request.method in (
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ):
        return JsonResponse(
            {"ok": False, "message": "You do not have permission to access this resource."},
            status=403,
        )
    raise PermissionDenied


def require_permission(codename=None, predicate=None):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            allowed = predicate(request.user) if predicate else _allowed(
                request.user, codename
            )
            if not allowed:
                return _forbidden(request)
            response = view(request, *args, **kwargs)
            patch_cache_control(
                response,
                private=True,
                no_cache=True,
                no_store=True,
                must_revalidate=True,
            )
            return response

        return wrapped

    return decorator


require_authenticated = require_permission(
    predicate=lambda user: user.is_authenticated and user.is_active
)
require_inspection_access = require_permission("perform_inspection")
require_dashboard_access = require_permission("view_inspection_dashboard")
require_report_access = require_permission("view_inspection_reports")
require_report_export = require_permission("export_inspection_reports")
require_history_access = require_permission("view_inspection_history")
require_master_data_view = require_permission("view_master_data")
require_master_data_create = require_permission("create_master_data")
require_master_data_change = require_permission("change_master_data")
require_master_data_delete = require_permission("delete_master_data")
require_configuration_access = require_permission("change_part_vision_configuration")
require_configuration_view = require_permission("view_part_vision_configuration")
require_camera_setup_view = require_permission("view_camera_setup")
require_camera_roi_access = require_permission("change_camera_roi_settings")
require_storage_view = require_permission("view_storage_settings")
require_storage_change = require_permission("change_storage_settings")
require_model_configuration_view = require_permission("view_model_configuration")
require_model_configuration_change = require_permission("change_model_configuration")
require_approved_models_view = require_permission("view_approved_models")
require_approved_models_change = require_permission("manage_approved_models")
require_yolo_models_view = require_permission("view_yolo_models")
require_yolo_models_change = require_permission("manage_yolo_models")
require_training_jobs_view = require_permission("view_training_jobs")
require_training_jobs_change = require_permission("manage_training_jobs")
require_administrator = require_permission(predicate=is_admin)
require_user_management = require_permission(
    predicate=lambda user: is_admin(user)
    and has_permission(user, "manage_inspection_users")
)
require_permission_management = require_permission(predicate=is_admin)
require_audit_access = require_permission("view_inspection_audit_log")
require_inspection_state_view = require_permission("view_inspection_state")
require_inspection_logs_view = require_permission("view_inspection_logs")
require_roi_configuration_view = require_permission("view_roi_configuration")
require_roi_configuration_change = require_permission("change_roi_configuration")
require_trigger_zone_view = require_permission("view_trigger_zone_configuration")
require_trigger_zone_change = require_permission("change_trigger_zone_configuration")
require_presets_view = require_permission("view_inspection_presets")
require_presets_change = require_permission("change_inspection_presets")
require_master_images_view = require_permission("view_master_images")
require_master_images_change = require_permission("change_master_images")
require_system_settings_view = require_permission("view_system_settings")
require_system_settings_change = require_permission("change_system_settings")


def require_master_data_page(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        codename = (
            "create_master_data"
            if request.GET.get("action") == "add"
            else "view_master_data"
        )
        return require_permission(codename)(view)(request, *args, **kwargs)

    return wrapped


def require_yolo_models_page(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        codename = (
            "view_approved_models"
            if request.GET.get("approved") == "1"
            else "view_yolo_models"
        )
        return require_permission(codename)(view)(request, *args, **kwargs)

    return wrapped


def _session_allowed(request, mutation=False):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    if not is_admin(user):
        if mutation and not has_permission(user, "perform_inspection"):
            return False
        if not mutation and not (
            has_permission(user, "perform_inspection")
            or has_permission(user, "view_inspection_history")
            or has_permission(user, "view_inspection_reports")
        ):
            return False

    raw = request.resolver_match.kwargs.get("session_id") if request.resolver_match else None
    if not raw and request.method in ("POST", "PUT", "PATCH"):
        try:
            import json

            raw = json.loads(request.body.decode("utf-8")).get("session_id")
        except (UnicodeDecodeError, ValueError, AttributeError):
            raw = None
    if not raw:
        return False
    session = VisionInspectionSession.objects.filter(session_id=raw).first()
    if session is None:
        return False
    if mutation:
        return bool(is_admin(user) or session.operator_id == user.pk)
    return can_view_session(user, session)


def require_session_read_access(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _session_allowed(request, mutation=False):
            return _forbidden(request)
        response = view(request, *args, **kwargs)
        patch_cache_control(
            response,
            private=True,
            no_cache=True,
            no_store=True,
            must_revalidate=True,
        )
        return response

    return wrapped


def require_session_mutation_access(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _session_allowed(request, mutation=True):
            return _forbidden(request)
        response = view(request, *args, **kwargs)
        patch_cache_control(
            response,
            private=True,
            no_cache=True,
            no_store=True,
            must_revalidate=True,
        )
        return response

    return wrapped


def scope_logs(user, queryset):
    """Apply per-account record scope to a log queryset."""
    if is_admin(user):
        return queryset
    if ROLE_SUPERVISOR in roles(user):
        try:
            codes = [
                item.strip().upper()
                for item in user.vision_inspection_profile.station_scope.split(",")
                if item.strip()
            ]
        except VisionInspectionUserProfile.DoesNotExist:
            codes = []
        return queryset.filter(
            Q(session__station_id__in=codes) | Q(employee_code__in=codes)
        ).distinct() if codes else queryset.none()
    if ROLE_OPERATOR in roles(user):
        return queryset.filter(
            Q(operator_id=user.pk)
            | Q(session__operator_id=user.pk)
            | Q(employee_code__iexact=user.get_username())
        ).distinct()
    return queryset.none()


def scope_sessions(user, queryset):
    if is_admin(user):
        return queryset
    if ROLE_SUPERVISOR in roles(user):
        try:
            codes = [
                item.strip().upper()
                for item in user.vision_inspection_profile.station_scope.split(",")
                if item.strip()
            ]
        except VisionInspectionUserProfile.DoesNotExist:
            codes = []
        return queryset.filter(station_id__in=codes) if codes else queryset.none()
    if ROLE_OPERATOR in roles(user):
        return queryset.filter(
            Q(operator_id=user.pk) | Q(employee_code__iexact=user.get_username())
        )
    return queryset.none()


def cancel_user_workflows(user):
    """Cancel only this user's unsaved sessions; never stop an unrelated camera."""
    if not user.is_authenticated or not user.pk:
        return
    sessions = VisionInspectionSession.objects.filter(
        operator_id=user.pk,
        is_active=True,
        is_saved=False,
    ).exclude(stage__in=("saved", "cancelled"))
    if not sessions.exists():
        return

    from VisionInspection.services.inspection_workflow import InspectionWorkflow

    workflow = InspectionWorkflow()
    for session_id in sessions.values_list("session_id", flat=True).iterator():
        workflow.cancel(str(session_id))
