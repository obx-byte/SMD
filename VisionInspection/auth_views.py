import json
import re
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import Group, Permission, User
from django.db import transaction
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from VisionInspection.models import (
    VisionInspectionAuditLog,
    VisionInspectionPermissionOverride,
    VisionInspectionUserProfile,
)
from VisionInspection.services.access_control import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_CATALOG,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    cancel_user_workflows,
    has_permission,
    is_admin,
)

ROLES = (ROLE_OPERATOR, ROLE_SUPERVISOR, ROLE_ADMIN)


def _role_group(role):
    group, created = Group.objects.get_or_create(name=role)
    if created and role in DEFAULT_ROLE_PERMISSIONS:
        permissions = Permission.objects.filter(
            content_type__app_label="vision_inspection",
            codename__in=DEFAULT_ROLE_PERMISSIONS[role],
        )
        if permissions.count() != len(DEFAULT_ROLE_PERMISSIONS[role]):
            raise RuntimeError(f"Default permissions for {role} have not been migrated.")
        group.permissions.add(*permissions)
    return group


def _post_login_url(user):
    if has_permission(user, "perform_inspection"):
        return reverse("VisionInspection:inspection")
    if has_permission(user, "view_inspection_dashboard"):
        return reverse("VisionInspection:dashboard")
    if has_permission(user, "view_inspection_reports"):
        return reverse("VisionInspection:ui-reports")
    return reverse("VisionInspection:password-change")

def employee_login(request):
    if request.user.is_authenticated:
        return redirect(_post_login_url(request.user))
    error = ""
    if request.method == "POST":
        employee_id = (request.POST.get("username") or "").strip().upper()
        password = request.POST.get("password") or ""
        matches = User.objects.filter(username__iexact=employee_id)[:2]
        candidate = matches[0] if len(matches) == 1 else None
        user = authenticate(request, username=candidate.username, password=password) if candidate else None
        if user is not None and user.is_active:
            request.session.flush()
            login(request, user)
            next_url = request.POST.get("next") or ""
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect(_post_login_url(user))
        error = "Employee ID or password is incorrect, or this account is inactive."
    return render(request, "VisionInspection/ui/login.html", {"error": error, "next": request.GET.get("next", "")})

@require_POST
def employee_logout(request):
    cancel_user_workflows(request.user)
    logout(request)
    return redirect("VisionInspection:login")

def change_password(request):
    if request.method != "POST":
        return render(request, "VisionInspection/ui/password_change.html", {"form": PasswordChangeForm(request.user)})
    form = PasswordChangeForm(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        from django.contrib.auth import update_session_auth_hash
        update_session_auth_hash(request, user)
        messages.success(request, "Password changed.")
        return redirect(_post_login_url(user))
    return render(request, "VisionInspection/ui/password_change.html", {"form": form})

def _audit(request, user, action, message):
    VisionInspectionAuditLog.objects.create(
        actor=request.user, action=action, target_type="auth.User",
        target_id=str(user.pk), details={"employee_id": user.username, "message": message},
    )

def user_management(request):
    if request.method != "POST":
        users = User.objects.prefetch_related("groups").order_by("username")
        return render(
            request,
            "VisionInspection/ui/user_management.html",
            {
                "users": users,
                "roles": ROLES,
                "can_manage_permissions": is_admin(request.user),
            },
        )
    action = request.POST.get("action")
    if action == "create":
        employee_id = (request.POST.get("employee_id") or "").strip().upper()
        password = request.POST.get("password") or ""
        display_name = (request.POST.get("display_name") or "").strip()[:150]
        role = request.POST.get("role") or ROLE_OPERATOR
        scope = [x.strip().upper() for x in (request.POST.get("scope") or "").split(",") if x.strip()]
        if not re.fullmatch(r"[A-Z0-9._-]{1,150}", employee_id) or role not in ROLES or len(password) < 12:
            messages.error(request, "Enter a valid Employee ID and role, and a password of at least 12 characters.")
        elif User.objects.filter(username__iexact=employee_id).exists():
            messages.error(request, "That Employee ID already has an account.")
        else:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=employee_id,
                    password=password,
                    is_staff=(role == ROLE_ADMIN),
                    first_name=display_name,
                )
                user.groups.add(_role_group(role))
                VisionInspectionUserProfile.objects.create(
                    user=user, station_scope=", ".join(scope)
                )
            _audit(request, user, "ACCOUNT_CREATED", "role=" + role)
            messages.success(request, "Account created.")
    elif action in ("activate", "deactivate", "reset_password", "change_role"):
        user = get_object_or_404(User, pk=request.POST.get("user_id"))
        if action == "activate": user.is_active = True
        elif action == "deactivate": user.is_active = False
        elif action == "reset_password":
            password = request.POST.get("password") or ""
            if len(password) < 12:
                messages.error(request, "The new password must be at least 12 characters.")
                return redirect("VisionInspection:user-management")
            user.set_password(password)
        elif action == "change_role":
            role = request.POST.get("role") or ""
            if role not in ROLES:
                messages.error(request, "Select a valid role.")
                return redirect("VisionInspection:user-management")
            with transaction.atomic():
                user.groups.clear()
                user.groups.add(_role_group(role))
                profile, _ = VisionInspectionUserProfile.objects.get_or_create(user=user)
                profile.station_scope = ", ".join(
                    x.strip().upper()
                    for x in (request.POST.get("scope") or "").split(",")
                    if x.strip()
                )
                profile.save()
                if role != ROLE_SUPERVISOR:
                    VisionInspectionPermissionOverride.objects.filter(user=user).delete()
        if action == "change_role" and not user.is_superuser:
            user.is_staff = role == ROLE_ADMIN
        user.save()
        _audit(request, user, "ACCOUNT_UPDATED", "action=" + action)
        messages.success(request, "Account updated.")
    return redirect("VisionInspection:user-management")


def user_management_permissions(request):
    permission_codes = [codename for codename, _, _ in PERMISSION_CATALOG]
    permissions_by_code = {
        item.codename: item
        for item in Permission.objects.filter(
            content_type__app_label="vision_inspection",
            codename__in=permission_codes,
        )
    }
    if set(permissions_by_code) != set(permission_codes):
        raise RuntimeError("Permission catalog is incomplete; apply VisionInspection migrations.")

    manageable_users = User.objects.filter(
        is_active=True,
        groups__name__in=(ROLE_OPERATOR, ROLE_SUPERVISOR),
    ).exclude(
        is_superuser=True
    ).exclude(
        groups__name=ROLE_ADMIN
    ).prefetch_related("groups").order_by("username").distinct()
    selected_user_id = (
        request.POST.get("user_id")
        if request.method == "POST"
        else request.GET.get("user_id")
    )
    selected_user = (
        manageable_users.filter(pk=selected_user_id).first()
        if selected_user_id else manageable_users.first()
    )

    if request.method == "POST":
        action = request.POST.get("action")
        with transaction.atomic():
            if action == "save_role_permissions":
                role = request.POST.get("role")
                if role not in (ROLE_OPERATOR, ROLE_SUPERVISOR):
                    messages.error(request, "Select a configurable role.")
                    return redirect("VisionInspection:user-management-permissions")
                role_key = role.lower().replace(" ", "_")
                selected_codes = set(
                    request.POST.getlist(f"permission_{role_key}")
                )
                unknown = selected_codes.difference(permission_codes)
                admin_only = {
                    "manage_inspection_users",
                    "manage_inspection_permissions",
                }
                if unknown or selected_codes.intersection(admin_only):
                    messages.error(request, "The permission selection contains unsupported access.")
                    return redirect("VisionInspection:user-management-permissions")
                group = _role_group(role)
                before = set(
                    group.permissions.filter(
                        content_type__app_label="vision_inspection"
                    ).values_list("codename", flat=True)
                )
                through = Group.permissions.through
                through.objects.filter(
                    group_id=group.pk,
                    permission__content_type__app_label="vision_inspection",
                ).delete()
                through.objects.bulk_create(
                    [
                        through(
                            group_id=group.pk,
                            permission_id=permissions_by_code[codename].pk,
                        )
                        for codename in sorted(selected_codes)
                    ],
                    ignore_conflicts=True,
                )
                changed = {
                    "role": role,
                    "before": sorted(before),
                    "after": sorted(selected_codes),
                }
                VisionInspectionAuditLog.objects.create(
                    actor=request.user,
                    action="ROLE_PERMISSIONS_UPDATED",
                    target_type="auth.Group",
                    target_id=str(group.pk),
                    details=changed,
                )
                messages.success(request, f"{role} permissions updated.")
            elif action == "save_user_overrides":
                target = manageable_users.filter(pk=selected_user_id).first()
                if target is None:
                    messages.error(request, "Choose an active Operator or Supervisor account.")
                    return redirect("VisionInspection:user-management-permissions")
                decisions = {
                    codename: request.POST.get(f"override_{codename}", "inherit")
                    for codename in permission_codes
                }
                admin_only = {
                    "manage_inspection_users",
                    "manage_inspection_permissions",
                }
                if any(
                    decision not in ("inherit", "grant", "deny")
                    for decision in decisions.values()
                ):
                    messages.error(request, "An unsupported permission decision was submitted.")
                    return redirect(
                        f"{reverse('VisionInspection:user-management-permissions')}?user_id={target.pk}"
                    )
                if any(
                    decisions[codename] == "grant" for codename in admin_only
                ):
                    messages.error(
                        request,
                        "User and permission management remain Admin-only.",
                    )
                    return redirect(
                        f"{reverse('VisionInspection:user-management-permissions')}?user_id={target.pk}"
                    )
                changes = []
                for codename in permission_codes:
                    decision = decisions[codename]
                    permission = permissions_by_code[codename]
                    previous = VisionInspectionPermissionOverride.objects.filter(
                        user=target, permission=permission
                    ).first()
                    before = (
                        "inherit"
                        if previous is None
                        else "grant"
                        if previous.granted
                        else "deny"
                    )
                    if decision == "inherit":
                        if previous is not None:
                            previous.delete()
                    else:
                        VisionInspectionPermissionOverride.objects.update_or_create(
                            user=target,
                            permission=permission,
                            defaults={
                                "granted": decision == "grant",
                                "updated_by": request.user,
                            },
                        )
                    if before != decision:
                        changes.append(
                            {"permission": codename, "from": before, "to": decision}
                        )
                if changes:
                    VisionInspectionAuditLog.objects.create(
                        actor=request.user,
                        action="USER_PERMISSION_OVERRIDES_UPDATED",
                        target_type="auth.User",
                        target_id=str(target.pk),
                        details={
                            "employee_id": target.username,
                            "permissions": changes,
                        },
                    )
                messages.success(request, "User permission exceptions updated.")
                return redirect(
                    f"{reverse('VisionInspection:user-management-permissions')}?user_id={target.pk}"
                )
            else:
                messages.error(request, "Unsupported permission management action.")
        return redirect("VisionInspection:user-management-permissions")

    groups = {
        group.name: group
        for group in Group.objects.filter(name__in=ROLES).prefetch_related("permissions")
    }
    role_rows = [
        {
            "name": role,
            "key": role.lower().replace(" ", "_"),
            "editable": role != ROLE_ADMIN,
            "permissions": (
                set(permission_codes)
                if role == ROLE_ADMIN
                else set(
                    groups[role].permissions.filter(
                        content_type__app_label="vision_inspection"
                    ).values_list("codename", flat=True)
                )
                if role in groups
                else set()
            ),
        }
        for role in ROLES
    ]
    overrides = {}
    if selected_user:
        overrides = {
            item.permission.codename: ("grant" if item.granted else "deny")
            for item in VisionInspectionPermissionOverride.objects.filter(
                user=selected_user,
                permission__content_type__app_label="vision_inspection",
                permission__codename__in=permission_codes,
            ).select_related("permission")
        }
    permission_rows = [
        {
            "codename": codename,
            "module": module,
            "action": action,
            "permission": permissions_by_code[codename],
            "decision": overrides.get(codename, "inherit"),
        }
        for codename, module, action in PERMISSION_CATALOG
    ]
    return render(
        request,
        "VisionInspection/ui/user_permissions.html",
        {
            "roles": role_rows,
            "permission_rows": permission_rows,
            "manageable_users": manageable_users,
            "selected_user": selected_user,
            "overrides": overrides,
        },
    )

@require_GET
def audit_log(request):
    entries = VisionInspectionAuditLog.objects.select_related("actor").order_by("-created_at")[:500]
    return render(request, "VisionInspection/ui/audit_log.html", {"entries": entries})
