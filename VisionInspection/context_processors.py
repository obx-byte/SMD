from VisionInspection.services.access_control import is_admin, roles

def access_flags(request):
    user = request.user
    groups = roles(user) if user.is_authenticated else set()
    admin = is_admin(user)
    return {"vi_access": {
        "administrator": admin,
        "reports": bool(user.is_authenticated and (admin or "Operator" in groups or user.has_perm("vision_inspection.view_inspection_reports"))),
        "configuration_view": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.view_part_vision_configuration"))),
        "camera_roi_edit": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.change_camera_roi_settings"))),
        "user_management": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.manage_inspection_users"))),
        "report_export": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.export_inspection_reports"))),
        "configuration_edit": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.change_part_vision_configuration"))),
        "audit": bool(user.is_authenticated and (admin or user.has_perm("vision_inspection.view_inspection_audit_log"))),
        "operator": "Operator" in groups, "quality": "Quality User" in groups, "supervisor": "Supervisor" in groups,
    }}
