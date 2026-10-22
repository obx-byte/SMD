from VisionInspection.services.access_control import has_permission, is_admin, roles


def access_flags(request):
    user = request.user
    active = bool(user.is_authenticated and user.is_active)
    groups = roles(user) if active else set()
    flags = {
        "administrator": is_admin(user),
        "dashboard": has_permission(user, "view_inspection_dashboard"),
        "inspection": has_permission(user, "perform_inspection"),
        "history": has_permission(user, "view_inspection_history"),
        "reports": has_permission(user, "view_inspection_reports"),
        "report_export": has_permission(user, "export_inspection_reports"),
        "master_data_view": has_permission(user, "view_master_data"),
        "master_data_create": has_permission(user, "create_master_data"),
        "master_data_change": has_permission(user, "change_master_data"),
        "master_data_delete": has_permission(user, "delete_master_data"),
        "part_vision_view": has_permission(user, "view_part_vision_configuration"),
        "configuration_view": has_permission(user, "view_part_vision_configuration"),
        "configuration_edit": has_permission(user, "change_part_vision_configuration"),
        "camera_setup_view": has_permission(user, "view_camera_setup"),
        "camera_roi_edit": has_permission(user, "change_camera_roi_settings"),
        "storage_view": has_permission(user, "view_storage_settings"),
        "storage_change": has_permission(user, "change_storage_settings"),
        "model_configuration_view": has_permission(user, "view_model_configuration"),
        "model_configuration_edit": has_permission(user, "change_model_configuration"),
        "approved_models_view": has_permission(user, "view_approved_models"),
        "yolo_models_view": has_permission(user, "view_yolo_models"),
        "training_jobs_view": has_permission(user, "view_training_jobs"),
        "user_management": is_admin(user)
        and has_permission(user, "manage_inspection_users"),
        "permission_management": is_admin(user)
        and has_permission(user, "manage_inspection_permissions"),
        "audit": has_permission(user, "view_inspection_audit_log"),
        "operator": "Operator" in groups,
        "supervisor": "Supervisor" in groups,
    }
    return {"vi_access": flags}
