import json
import re
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import Group, User
from django.shortcuts import redirect, render, get_object_or_404
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from VisionInspection.models import VisionInspectionUserProfile, VisionInspectionAuditLog
from VisionInspection.services.access_control import ROLE_OPERATOR, ROLE_QUALITY, ROLE_SUPERVISOR, ROLE_ADMIN, is_admin

ROLES = (ROLE_OPERATOR, ROLE_QUALITY, ROLE_SUPERVISOR, ROLE_ADMIN)

def employee_login(request):
    if request.user.is_authenticated:
        return redirect("VisionInspection:inspection")
    error = ""
    if request.method == "POST":
        employee_id = (request.POST.get("username") or "").strip().upper()
        password = request.POST.get("password") or ""
        matches = User.objects.filter(username__iexact=employee_id)[:2]
        candidate = matches[0] if len(matches) == 1 else None
        user = authenticate(request, username=candidate.username, password=password) if candidate else None
        if user is not None and user.is_active:
            login(request, user)
            next_url = request.POST.get("next") or ""
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect("VisionInspection:inspection")
        error = "Employee ID or password is incorrect, or this account is inactive."
    return render(request, "VisionInspection/ui/login.html", {"error": error, "next": request.GET.get("next", "")})

@require_POST
def employee_logout(request):
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
        return redirect("VisionInspection:inspection")
    return render(request, "VisionInspection/ui/password_change.html", {"form": form})

def _audit(request, user, action, message):
    VisionInspectionAuditLog.objects.create(
        actor=request.user, action=action, target_type="auth.User",
        target_id=str(user.pk), details={"employee_id": user.username, "message": message},
    )

def user_management(request):
    if request.method != "POST":
        users = User.objects.prefetch_related("groups").order_by("username")
        return render(request, "VisionInspection/ui/user_management.html", {"users": users, "roles": ROLES})
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
            user = User.objects.create_user(username=employee_id, password=password, is_staff=(role == ROLE_ADMIN), first_name=display_name)
            user.groups.add(Group.objects.get_or_create(name=role)[0])
            VisionInspectionUserProfile.objects.create(user=user, station_scope=", ".join(scope))
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
            user.groups.clear(); user.groups.add(Group.objects.get_or_create(name=role)[0])
            profile, _ = VisionInspectionUserProfile.objects.get_or_create(user=user)
            profile.station_scope = ", ".join(x.strip().upper() for x in (request.POST.get("scope") or "").split(",") if x.strip())
            profile.save()
        if action == "change_role" and not user.is_superuser:
            user.is_staff = role == ROLE_ADMIN
        user.save()
        _audit(request, user, "ACCOUNT_UPDATED", "action=" + action)
        messages.success(request, "Account updated.")
    return redirect("VisionInspection:user-management")

@require_GET
def audit_log(request):
    entries = VisionInspectionAuditLog.objects.select_related("actor").order_by("-created_at")[:500]
    return render(request, "VisionInspection/ui/audit_log.html", {"entries": entries})
