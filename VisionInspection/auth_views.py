import json
import re
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import Group, Permission, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.core.paginator import Paginator
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from VisionInspection.models import (
    AccessGroupProfile,
    VisionInspectionAuditLog,
    VisionInspectionPermissionOverride,
    VisionInspectionUserProfile,
)
from VisionInspection.services.access_control import (
    ADMIN_ONLY_PERMISSION_CODENAMES,
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_CATALOG,
    SCREEN_PERMISSION_GROUPS,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_QUALITY,
    ROLE_SUPERVISOR,
    ROLE_PERMISSION_ROWS,
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


def _is_administrator_account(user):
    return user.is_superuser or user.groups.filter(name=ROLE_ADMIN).exists()


def _has_other_active_administrator(user):
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(groups__name=ROLE_ADMIN)
    ).exclude(pk=user.pk).distinct().exists()


def user_management(request):
    if request.method != "POST":
        query = (request.GET.get("q") or "").strip()
        role_filter = request.GET.get("role", "")
        group_filter = request.GET.get("access_group", "")
        status_filter = request.GET.get("status", "")
        users = User.objects.prefetch_related(
            "groups", "groups__access_group_profile"
        ).order_by("username")
        if query:
            users = users.filter(
                Q(username__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
            )
        if role_filter in ROLES:
            users = users.filter(groups__name=role_filter)
        if group_filter.isdigit():
            users = users.filter(groups__access_group_profile__pk=group_filter)
        if status_filter == "active":
            users = users.filter(is_active=True)
        elif status_filter == "inactive":
            users = users.filter(is_active=False)
        users = users.distinct()
        page_obj = Paginator(users, 25).get_page(request.GET.get("page"))
        for account in page_obj.object_list:
            custom_groups = []
            for group in account.groups.all():
                try:
                    custom_profile = group.access_group_profile
                except AccessGroupProfile.DoesNotExist:
                    continue
                custom_groups.append(custom_profile)
            account.access_group_names = [item.group.name for item in custom_groups]
            account.access_group_id = custom_groups[0].pk if custom_groups else None
            account.role_names = [
                group.name
                for group in account.groups.all()
                if group.name in ROLES + (ROLE_QUALITY,)
            ]
        return render(
            request,
            "VisionInspection/ui/user_management.html",
            {
                "users": page_obj.object_list,
                "page_obj": page_obj,
                "query": query,
                "role_filter": role_filter,
                "group_filter": group_filter,
                "status_filter": status_filter,
                "roles": ROLES,
                "access_groups": AccessGroupProfile.objects.select_related(
                    "group"
                ).filter(is_active=True).order_by("group__name"),
                "all_access_groups": AccessGroupProfile.objects.select_related(
                    "group"
                ).order_by("group__name"),
                "can_manage_permissions": is_admin(request.user)
                and has_permission(request.user, "manage_inspection_permissions"),
            },
        )
    action = request.POST.get("action")
    if action == "create":
        employee_id = (request.POST.get("employee_id") or "").strip().upper()
        password = request.POST.get("password") or ""
        display_name = (request.POST.get("display_name") or "").strip()[:150]
        role = request.POST.get("role") or ROLE_OPERATOR
        access_group_id = request.POST.get("access_group", "")
        candidate = User(username=employee_id, first_name=display_name)
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            password_error = " ".join(exc.messages)
        else:
            password_error = ""
        access_group = None
        if access_group_id:
            access_group = AccessGroupProfile.objects.filter(
                pk=access_group_id, is_active=True
            ).select_related("group").first()
        if not re.fullmatch(r"[A-Z0-9._-]{1,150}", employee_id) or role not in ROLES:
            messages.error(request, "Enter a valid Employee ID and role.")
        elif password_error:
            messages.error(request, password_error)
        elif access_group_id and access_group is None:
            messages.error(request, "Select an active Access Group.")
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
                if access_group:
                    user.groups.add(access_group.group)
                VisionInspectionUserProfile.objects.create(user=user)
            _audit(request, user, "ACCOUNT_CREATED", "role=" + role)
            messages.success(request, "Account created.")
    elif action in ("activate", "deactivate", "reset_password", "change_role"):
        user = get_object_or_404(User, pk=request.POST.get("user_id"))
        if action == "activate":
            user.is_active = True
        elif action == "deactivate":
            if (
                user.is_active
                and _is_administrator_account(user)
                and not _has_other_active_administrator(user)
            ):
                messages.error(request, "The last active Administrator cannot be deactivated.")
                return redirect("VisionInspection:user-management")
            user.is_active = False
        elif action == "reset_password":
            password = request.POST.get("password") or ""
            try:
                validate_password(password, user)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return redirect("VisionInspection:user-management")
            user.set_password(password)
        elif action == "change_role":
            role = request.POST.get("role") or ""
            if role not in ROLES:
                messages.error(request, "Select a valid role.")
                return redirect("VisionInspection:user-management")
            if user.is_superuser:
                messages.error(request, "Superuser accounts cannot be changed from this page.")
                return redirect("VisionInspection:user-management")
            if (
                _is_administrator_account(user)
                and role != ROLE_ADMIN
                and user.is_active
                and not _has_other_active_administrator(user)
            ):
                messages.error(request, "The last active Administrator cannot be demoted.")
                return redirect("VisionInspection:user-management")
            selected_group_id = request.POST.get("access_group")
            selected_access_group = None
            if selected_group_id:
                selected_access_group = AccessGroupProfile.objects.filter(
                    pk=selected_group_id, is_active=True
                ).select_related("group").first()
                if selected_access_group is None:
                    messages.error(request, "Select an active Access Group.")
                    return redirect("VisionInspection:user-management")
            with transaction.atomic():
                user.groups.remove(
                    *Group.objects.filter(name__in=ROLES + (ROLE_QUALITY,))
                )
                user.groups.add(_role_group(role))
                if selected_group_id is not None:
                    custom_groups = Group.objects.filter(
                        access_group_profile__isnull=False
                    )
                    user.groups.remove(*custom_groups)
                    if selected_access_group:
                        user.groups.add(selected_access_group.group)
                VisionInspectionUserProfile.objects.get_or_create(user=user)
                if "display_name" in request.POST:
                    user.first_name = (request.POST.get("display_name") or "").strip()[:150]
                if role != ROLE_SUPERVISOR:
                    VisionInspectionPermissionOverride.objects.filter(user=user).delete()
                user.is_staff = role == ROLE_ADMIN
                user.save()
        if action != "change_role":
            user.save()
        _audit(request, user, "ACCOUNT_UPDATED", "action=" + action)
        messages.success(request, "Account updated.")
    return redirect("VisionInspection:user-management")


def user_management_detail(request, user_id):
    account = get_object_or_404(
        User.objects.prefetch_related("groups", "groups__access_group_profile"),
        pk=user_id,
    )
    custom_groups = []
    for group in account.groups.all():
        try:
            custom_groups.append(group.access_group_profile)
        except AccessGroupProfile.DoesNotExist:
            continue
    overrides = VisionInspectionPermissionOverride.objects.filter(
        user=account
    ).select_related("permission").order_by("permission__codename")
    return render(
        request,
        "VisionInspection/ui/user_management_detail.html",
        {
            "account": account,
            "access_groups": custom_groups,
            "overrides": overrides,
        },
    )


def user_management_permissions(request):
    permission_codes = [codename for codename, _, _ in PERMISSION_CATALOG]
    role_matrix_codes = {row[3] for row in ROLE_PERMISSION_ROWS}
    role_editable_codes = role_matrix_codes - ADMIN_ONLY_PERMISSION_CODENAMES
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
    selected_user_id = request.POST.get("user_id") if request.method == "POST" else None

    if request.method == "POST":
        action = request.POST.get("action")
        with transaction.atomic():
            if action == "save_role_permissions":
                role = request.POST.get("role")
                if role not in (ROLE_OPERATOR, ROLE_SUPERVISOR):
                    messages.error(request, "Select a configurable role.")
                    return redirect(
                        "VisionInspection:user-management-permissions"
                    )
                selected_codes = set(request.POST.getlist("permission"))
                if selected_codes.difference(role_editable_codes):
                    messages.error(request, "The permission selection contains unsupported access.")
                    return redirect(
                        f"{reverse('VisionInspection:user-management-permissions')}?role={role}"
                    )
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
                    permission__codename__in=role_editable_codes,
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
                    "before": sorted(before.intersection(role_editable_codes)),
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
                return redirect(
                    f"{reverse('VisionInspection:user-management-permissions')}?role={role}"
                )
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

    selected_role = request.GET.get("role", ROLE_OPERATOR)
    if selected_role not in ROLES:
        selected_role = ROLE_OPERATOR
    role_group = Group.objects.filter(name=selected_role).first()
    selected_codes = (
        set(role_matrix_codes)
        if selected_role == ROLE_ADMIN
        else set(
            role_group.permissions.filter(
                content_type__app_label="vision_inspection",
                codename__in=role_matrix_codes,
            ).values_list("codename", flat=True)
        )
        if role_group
        else set()
    )
    permission_rows = [
        {
            "codename": codename,
            "module": module,
            "screen": screen,
            "action": action,
            "checked": codename in selected_codes,
            "locked": (
                selected_role == ROLE_ADMIN
                or codename in ADMIN_ONLY_PERMISSION_CODENAMES
            ),
        }
        for module, screen, action, codename in ROLE_PERMISSION_ROWS
    ]
    return render(
        request,
        "VisionInspection/ui/user_permissions.html",
        {
            "roles": ROLES,
            "selected_role": selected_role,
            "role_is_admin": selected_role == ROLE_ADMIN,
            "permission_rows": permission_rows,
        },
    )


def access_groups(request):
    query = (request.GET.get("q") or "").strip()
    groups = AccessGroupProfile.objects.select_related("group").order_by("group__name")
    if query:
        groups = groups.filter(group__name__icontains=query)
    return render(
        request,
        "VisionInspection/ui/access_groups.html",
        {"access_groups": groups, "query": query},
    )


def _access_group_permission_data(selected_codes):
    catalog = {
        codename: (screen, action)
        for _, screen, action, codename in ROLE_PERMISSION_ROWS
    }
    return [
        {
            "name": module,
            "items": [
                {
                    "codename": codename,
                    "screen": catalog[codename][0],
                    "action": catalog[codename][1],
                    "checked": codename in selected_codes,
                }
                for codename in codenames
            ],
        }
        for module, codenames in SCREEN_PERMISSION_GROUPS
    ]


def _save_access_group_permissions(group, selected_codes, permissions_by_code):
    managed_codes = {
        codename for _, codenames in SCREEN_PERMISSION_GROUPS for codename in codenames
    }
    through = Group.permissions.through
    through.objects.filter(
        group_id=group.pk,
        permission__content_type__app_label="vision_inspection",
        permission__codename__in=managed_codes,
    ).delete()
    through.objects.bulk_create(
        [
            through(group_id=group.pk, permission_id=permissions_by_code[codename].pk)
            for codename in sorted(selected_codes)
        ],
        ignore_conflicts=True,
    )


def access_group_edit(request, group_id=None):
    profile = None
    if group_id is not None:
        profile = get_object_or_404(
            AccessGroupProfile.objects.select_related("group"), pk=group_id
        )
    protected_names = {name.casefold() for name in ROLES + (ROLE_QUALITY,)}
    if profile and (
        profile.group.name.casefold() in protected_names
        or profile.group.permissions.filter(
            content_type__app_label="vision_inspection",
            codename__in=ADMIN_ONLY_PERMISSION_CODENAMES,
        ).exists()
    ):
        messages.error(request, "System-critical groups cannot be edited as Access Groups.")
        return redirect("VisionInspection:access-groups")
    editable_codes = {
        codename for _, codenames in SCREEN_PERMISSION_GROUPS for codename in codenames
    }
    permissions_by_code = {
        permission.codename: permission
        for permission in Permission.objects.filter(
            content_type__app_label="vision_inspection",
            codename__in=editable_codes,
        )
    }
    if set(permissions_by_code) != editable_codes:
        raise RuntimeError("Screen permission definitions are incomplete; apply migrations.")

    checked_codes = (
        set(
            profile.group.permissions.filter(
                content_type__app_label="vision_inspection",
                codename__in=editable_codes,
            ).values_list("codename", flat=True)
        )
        if profile
        else set()
    )
    form_name = profile.group.name if profile else ""
    form_description = profile.description if profile else ""
    form_is_active = profile.is_active if profile else True
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()[:500]
        is_active = request.POST.get("is_active") == "on"
        selected_codes = set(request.POST.getlist("permission"))
        checked_codes = selected_codes.intersection(editable_codes)
        form_name = name
        form_description = description
        form_is_active = is_active
        duplicate = Group.objects.filter(name__iexact=name)
        if profile:
            duplicate = duplicate.exclude(pk=profile.group_id)
        if selected_codes.difference(editable_codes):
            messages.error(request, "The permission selection contains unsupported access.")
        elif not name or len(name) > 150:
            messages.error(request, "Enter an Access Group name up to 150 characters.")
        elif name.casefold() in protected_names:
            messages.error(request, "System role groups cannot be used as Access Groups.")
        elif duplicate.exists():
            messages.error(request, "An Access Group with that name already exists.")
        else:
            try:
                with transaction.atomic():
                    if profile is None:
                        group = Group.objects.create(name=name)
                        profile = AccessGroupProfile.objects.create(
                            group=group,
                            description=description,
                            is_active=is_active,
                            created_by=request.user,
                            updated_by=request.user,
                        )
                        audit_action = "ACCESS_GROUP_CREATED"
                    else:
                        profile.group.name = name
                        profile.group.save(update_fields=("name",))
                        profile.description = description
                        profile.is_active = is_active
                        profile.updated_by = request.user
                        profile.save(
                            update_fields=(
                                "description", "is_active", "updated_by", "updated_at"
                            )
                        )
                        audit_action = "ACCESS_GROUP_UPDATED"
                    _save_access_group_permissions(
                        profile.group, selected_codes, permissions_by_code
                    )
                    VisionInspectionAuditLog.objects.create(
                        actor=request.user,
                        action=audit_action,
                        target_type="auth.Group",
                        target_id=str(profile.group_id),
                        details={
                            "name": name,
                            "description": description,
                            "is_active": is_active,
                            "permissions": sorted(selected_codes),
                        },
                    )
            except IntegrityError:
                messages.error(request, "An Access Group with that name already exists.")
            else:
                messages.success(request, "Access Group saved.")
                return redirect("VisionInspection:access-groups")
    return render(
        request,
        "VisionInspection/ui/access_group_form.html",
        {
            "access_group": profile,
            "is_edit": profile is not None,
            "form_name": form_name,
            "form_description": form_description,
            "form_is_active": form_is_active,
            "permission_groups": _access_group_permission_data(checked_codes),
        },
    )


@require_POST
def access_group_delete(request, group_id):
    profile = get_object_or_404(
        AccessGroupProfile.objects.select_related("group"), pk=group_id
    )
    group = profile.group
    protected_names = {name.casefold() for name in ROLES + (ROLE_QUALITY,)}
    has_admin_permissions = group.permissions.filter(
        content_type__app_label="vision_inspection",
        codename__in=ADMIN_ONLY_PERMISSION_CODENAMES,
    ).exists()
    if group.name.casefold() in protected_names or has_admin_permissions:
        messages.error(request, "This system-critical Access Group cannot be deleted.")
    elif group.user_set.exists():
        messages.error(request, "Remove all assigned users before deleting this Access Group.")
    else:
        group_id_value = group.pk
        group_name = group.name
        with transaction.atomic():
            group.delete()
            VisionInspectionAuditLog.objects.create(
                actor=request.user,
                action="ACCESS_GROUP_DELETED",
                target_type="auth.Group",
                target_id=str(group_id_value),
                details={"name": group_name},
            )
        messages.success(request, "Access Group deleted.")
    return redirect("VisionInspection:access-groups")


@require_GET
def access_group_permissions(request, group_id):
    profile = get_object_or_404(
        AccessGroupProfile.objects.select_related("group"), pk=group_id
    )
    editable_codes = {
        codename for _, codenames in SCREEN_PERMISSION_GROUPS for codename in codenames
    }
    current_codes = set(
        profile.group.permissions.filter(
            content_type__app_label="vision_inspection",
            codename__in=editable_codes,
        ).values_list("codename", flat=True)
    )
    return render(
        request,
        "VisionInspection/ui/access_group_form.html",
        {
            "access_group": profile,
            "is_edit": True,
            "read_only": True,
            "form_name": profile.group.name,
            "form_description": profile.description,
            "form_is_active": profile.is_active,
            "permission_groups": _access_group_permission_data(current_codes),
        },
    )


@require_GET
def audit_log(request):
    entries = VisionInspectionAuditLog.objects.select_related("actor").order_by("-created_at")[:500]
    return render(request, "VisionInspection/ui/audit_log.html", {"entries": entries})
