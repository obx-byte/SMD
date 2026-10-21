from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from VisionInspection.models import VisionInspectionSession

ROLE_OPERATOR = "Operator"
ROLE_QUALITY = "Quality User"
ROLE_SUPERVISOR = "Supervisor"
ROLE_ADMIN = "Administrator"

def roles(user):
    return {g.name for g in user.groups.all()} if user.is_authenticated else set()

def is_admin(user):
    return bool(user.is_authenticated and (user.is_superuser or ROLE_ADMIN in roles(user) and user.has_perm("vision_inspection.manage_inspection_users")))

def _allowed(user, codename):
    return bool(user.is_authenticated and (is_admin(user) or user.has_perm("vision_inspection." + codename)))

def can_view_session(user, session):
    if not user.is_authenticated:
        return False
    if is_admin(user) or ROLE_QUALITY in roles(user) or _allowed(user, "view_inspection_audit_log"):
        return True
    if ROLE_SUPERVISOR in roles(user):
        try: codes = [x.strip().upper() for x in user.vision_inspection_profile.station_scope.split(",") if x.strip()]
        except Exception: codes = []
        return bool(session.station_id and session.station_id.upper() in codes)
    if ROLE_OPERATOR in roles(user):
        return session.operator_id == user.pk or bool(session.employee_code and session.employee_code.casefold() == user.get_username().casefold())
    return False

def can_view_log(user, log):
    if not user.is_authenticated:
        return False
    if is_admin(user) or ROLE_QUALITY in roles(user) or _allowed(user, "view_inspection_audit_log"):
        return True
    if log.session_id:
        return can_view_session(user, log.session)
    if log.operator_id == user.pk:
        return True
    if ROLE_SUPERVISOR in roles(user):
        try: codes = [x.strip().upper() for x in user.vision_inspection_profile.station_scope.split(",") if x.strip()]
        except Exception: codes = []
        return bool(log.session_id and log.session.station_id and log.session.station_id.upper() in codes) or bool(log.employee_code and log.employee_code.upper() in codes)
    if ROLE_OPERATOR in roles(user):
        return bool(log.employee_code and log.employee_code.casefold() == user.get_username().casefold())
    return False

def _forbidden(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), "/vision-inspection/login/")
    return JsonResponse({"ok": False, "message": "You do not have permission to access this resource."}, status=403) if request.path.startswith("/vision-inspection/api/") or request.path.endswith("/") and request.method in ("POST", "PUT", "PATCH", "DELETE") else PermissionDenied

def require_permission(codename=None, predicate=None):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            allowed = predicate(request.user) if predicate else _allowed(request.user, codename)
            if not allowed:
                denied = _forbidden(request)
                if denied is PermissionDenied:
                    raise denied
                return denied
            return view(request, *args, **kwargs)
        return wrapped
    return decorator

require_authenticated = require_permission(predicate=lambda u: u.is_authenticated)
require_inspection_access = require_permission(predicate=lambda u: u.is_authenticated and (is_admin(u) or ROLE_OPERATOR in roles(u) or _allowed(u, "perform_inspection")))
require_report_access = require_permission(predicate=lambda u: u.is_authenticated and (is_admin(u) or ROLE_OPERATOR in roles(u) or _allowed(u, "view_inspection_reports")))
require_report_export = require_permission(predicate=lambda u: u.is_authenticated and (is_admin(u) or _allowed(u, "export_inspection_reports")))
require_configuration_access = require_permission("change_part_vision_configuration")
require_configuration_view = require_permission(predicate=lambda u: is_admin(u) or _allowed(u, "view_part_vision_configuration"))
require_camera_roi_access = require_permission("change_camera_roi_settings")
require_administrator = require_permission(predicate=is_admin)
require_user_management = require_permission("manage_inspection_users")
require_audit_access = require_permission("view_inspection_audit_log")
def _session_allowed(request, mutation=False):
    if not require_inspection_access_predicate(request.user):
        return False
    if is_admin(request.user) or not ROLE_OPERATOR in roles(request.user):
        return True
    raw = request.resolver_match.kwargs.get("session_id") if request.resolver_match else None
    if not raw and request.method in ("POST", "PUT", "PATCH"):
        try:
            import json
            raw = json.loads(request.body.decode("utf-8")).get("session_id")
        except Exception:
            raw = None
    if not raw:
        return True  # starting a new session; ownership is recorded at creation
    session = VisionInspectionSession.objects.filter(session_id=raw).first()
    return bool(session and can_view_session(request.user, session))

require_inspection_access_predicate = lambda u: bool(u.is_authenticated and (is_admin(u) or ROLE_OPERATOR in roles(u) or _allowed(u, "perform_inspection")))
require_session_read_access = require_permission(predicate=lambda u: u.is_authenticated and (is_admin(u) or _allowed(u, "view_inspection_reports") or ROLE_OPERATOR in roles(u)))
require_session_mutation_access = require_permission(predicate=lambda u: require_inspection_access_predicate(u))



def scope_logs(user, queryset):
    """Apply per-account record scope to a log queryset."""
    from django.db.models import Q
    if is_admin(user) or ROLE_QUALITY in roles(user):
        return queryset
    if ROLE_SUPERVISOR in roles(user):
        try: codes = [x.strip().upper() for x in user.vision_inspection_profile.station_scope.split(",") if x.strip()]
        except Exception: codes = []
        return queryset.filter(Q(session__station_id__in=codes) | Q(employee_code__in=codes)).distinct() if codes else queryset.none()
    if ROLE_OPERATOR in roles(user):
        return queryset.filter(Q(operator_id=user.pk) | Q(session__operator_id=user.pk) | Q(employee_code__iexact=user.get_username())).distinct()
    return queryset.none()


def scope_sessions(user, queryset):
    from django.db.models import Q
    if is_admin(user) or ROLE_QUALITY in roles(user):
        return queryset
    if ROLE_SUPERVISOR in roles(user):
        try: codes = [x.strip().upper() for x in user.vision_inspection_profile.station_scope.split(",") if x.strip()]
        except Exception: codes = []
        return queryset.filter(station_id__in=codes) if codes else queryset.none()
    if ROLE_OPERATOR in roles(user):
        return queryset.filter(Q(operator_id=user.pk) | Q(employee_code__iexact=user.get_username()))
    return queryset.none()


def _session_scoped(mutation=False):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not _session_allowed(request, mutation=mutation):
                return _forbidden(request)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator

require_session_read_access = _session_scoped(False)
require_session_mutation_access = _session_scoped(True)
