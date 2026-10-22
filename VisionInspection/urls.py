"""URL configuration for the VisionInspection application.

All URLs are relative to ``/vision-inspection/`` as set in the project's
root ``urls.py``.

Access model
------------
The login page is public. Application pages and APIs are authenticated and
wrapped with explicit role/permission checks; ``admin`` is a convenience
wrapper for privileged routes, not a separate authentication mechanism.
Inspection sessions are additionally scoped to their owning operator or an
authorized Supervisor/Admin.
"""

from django.urls import path

from VisionInspection import auth_views, dashboard_api, storage_views, ui_views, views
from VisionInspection.services.access_control import (
    require_administrator,
    require_audit_access,
    require_camera_roi_access,
    require_camera_setup_view,
    require_configuration_access,
    require_configuration_view,
    require_dashboard_access,
    require_history_access,
    require_inspection_access,
    require_inspection_evidence_access,
    require_inspection_logs_view,
    require_inspection_state_view,
    require_master_data_change,
    require_master_data_create,
    require_master_data_delete,
    require_master_data_view,
    require_master_data_page,
    require_master_images_change,
    require_master_images_view,
    require_model_configuration_change,
    require_model_configuration_view,
    require_approved_models_change,
    require_approved_models_view,
    require_presets_change,
    require_presets_view,
    require_roi_configuration_change,
    require_roi_configuration_view,
    require_report_access,
    require_report_export,
    require_authenticated,
    require_storage_change,
    require_storage_view,
    require_system_settings_change,
    require_system_settings_view,
    require_session_mutation_access,
    require_session_read_access,
    require_training_jobs_change,
    require_training_jobs_view,
    require_trigger_zone_change,
    require_trigger_zone_view,
    require_user_management,
    require_yolo_models_change,
    require_yolo_models_view,
    require_yolo_models_page,
)

app_name = "VisionInspection"


_ACCESS_DECORATORS = {
    "authenticated": require_authenticated,
    "inspection": require_inspection_access,
    "inspection_evidence": require_inspection_evidence_access,
    "dashboard": require_dashboard_access,
    "history": require_history_access,
    "reports": require_report_access,
    "report_export": require_report_export,
    "master_data_view": require_master_data_view,
    "master_data_create": require_master_data_create,
    "master_data_change": require_master_data_change,
    "master_data_delete": require_master_data_delete,
    "master_data_page": require_master_data_page,
    "configuration": require_configuration_access,
    "configuration_view": require_configuration_view,
    "camera_setup_view": require_camera_setup_view,
    "storage_view": require_storage_view,
    "storage_change": require_storage_change,
    "model_view": require_model_configuration_view,
    "model_change": require_model_configuration_change,
    "approved_models_view": require_approved_models_view,
    "approved_models_change": require_approved_models_change,
    "yolo_models_view": require_yolo_models_view,
    "yolo_models_page": require_yolo_models_page,
    "yolo_models_change": require_yolo_models_change,
    "training_view": require_training_jobs_view,
    "training_change": require_training_jobs_change,
    "inspection_state_view": require_inspection_state_view,
    "inspection_logs_view": require_inspection_logs_view,
    "roi_view": require_roi_configuration_view,
    "roi_change": require_roi_configuration_change,
    "trigger_zone_view": require_trigger_zone_view,
    "trigger_zone_change": require_trigger_zone_change,
    "presets_view": require_presets_view,
    "presets_change": require_presets_change,
    "master_images_view": require_master_images_view,
    "master_images_change": require_master_images_change,
    "system_settings_view": require_system_settings_view,
    "system_settings_change": require_system_settings_change,
    "administrator": require_administrator,
    "user_management": require_user_management,
    "camera": require_camera_roi_access,
    "audit": require_audit_access,
    "session_read": require_session_read_access,
    "session_mutation": require_session_mutation_access,
}


def station(route, view, name, access="inspection"):
    """Authenticated route with explicit application-role authorization."""
    return path(route, _ACCESS_DECORATORS[access](view), name=name)


def public(route, view, name):
    return path(route, view, name=name)


def admin(route, view, name, access="administrator"):
    """Privileged route with explicit application-role authorization."""
    return station(route, view, name, access=access)


urlpatterns = [
    public("login/", auth_views.employee_login, name="login"),
    station("logout/", auth_views.employee_logout, name="logout", access="authenticated"),
    station("account/password/", auth_views.change_password, name="password-change", access="authenticated"),
    station("users/", auth_views.user_management, name="user-management", access="user_management"),
    station(
        "users/<int:user_id>/",
        auth_views.user_management_detail,
        name="user-management-detail",
        access="user_management",
    ),
    station("access-groups/", auth_views.access_groups, name="access-groups", access="user_management"),
    station(
        "access-groups/new/",
        auth_views.access_group_edit,
        name="access-group-create",
        access="user_management",
    ),
    station(
        "access-groups/<int:group_id>/edit/",
        auth_views.access_group_edit,
        name="access-group-edit",
        access="user_management",
    ),
    station(
        "access-groups/<int:group_id>/delete/",
        auth_views.access_group_delete,
        name="access-group-delete",
        access="user_management",
    ),
    station(
        "access-groups/<int:group_id>/permissions/",
        auth_views.access_group_permissions,
        name="access-group-permissions",
        access="user_management",
    ),
    station("audit/", auth_views.audit_log, name="audit-log", access="audit"),
    # ------------------------------------------------------------------ #
    #  Station landing page — role-gated dashboard.
    #  The sidebar "Dashboard" item and the workspace Home icon point here.
    # ------------------------------------------------------------------ #
    station("", ui_views.dashboard, name="dashboard", access="dashboard"),
    station("inspection/", views.inspection_view, name="inspection"),
    station("history/", views.history_view, name="history", access="history"),
    station(
        "storage/image/<int:metadata_id>/",
        storage_views.image,
        name="storage-image",
        access="inspection_evidence",
    ),
    station(
        "history/<int:inspection_id>/",
        views.history_detail,
        name="history-detail",
        access="history",
    ),
    station("traceability/", ui_views.traceability, name="ui-traceability", access="history"),
    station(
        "traceability/<int:inspection_id>/",
        ui_views.traceability_detail,
        name="traceability-detail",
        access="history",
    ),
    # Scan-time validation (read-only, inspection-permission protected).
    station(
        "api/employee/validate/",
        views.api_employee_validate,
        name="api-employee-validate",
    ),
    station("api/part/validate/", views.api_part_validate, name="api-part-validate"),
    # Live inspection state polling / control.
    station("api/state/", views.api_inspection_state, name="api-state"),
    station("api/state/reset/", views.api_reset_state, name="api-reset-state", access="administrator"),
    station("engine/start/", views.start_inspection_engine, name="engine-start", access="administrator"),
    station("engine/stop/", views.stop_inspection_engine, name="engine-stop", access="administrator"),
    # ------------------------------------------------------------------ #
    #  Inspection Master — continuous ROI inspection
    #  NOTE: static sub-routes must precede the <part_number> route so a
    #  sub-path like "save-roi" is not captured as a part number.
    # ------------------------------------------------------------------ #
    station(
        "inspection-master/",
        views.inspection_master_view,
        name="inspection-master",
        access="camera_setup_view",
    ),
    station(
        "inspection-master/save-roi/",
        views.master_roi_save,
        name="master-roi-save",
        access="camera",
    ),
    station(
        "inspection-master/start/",
        views.master_inspection_start,
        name="master-inspection-start",
        access="camera",
    ),
    station(
        "inspection-master/stop/",
        views.master_inspection_stop,
        name="master-inspection-stop",
        access="camera",
    ),
    station(
        "inspection-master/status/",
        views.api_master_inspection_status,
        name="master-inspection-status",
        access="camera_setup_view",
    ),
    station(
        "inspection-master/roi/",
        views.master_roi_get,
        name="master-roi-get",
        access="camera_setup_view",
    ),
    station(
        "inspection-master/preview/",
        views.api_master_roi_preview,
        name="master-roi-preview",
        access="camera_setup_view",
    ),
    station(
        "inspection-master/camera-resolution/",
        views.api_master_camera_resolution,
        name="master-camera-resolution",
        access="camera_setup_view",
    ),
    station(
        "inspection-master/<str:part_number>/",
        views.inspection_master_view,
        name="inspection-master-part",
        access="camera_setup_view",
    ),
    # ------------------------------------------------------------------ #
    #  Airbag Stitch Inspection workflow (barcode → model → front → back →
    #  result → save).  Sequential; nothing fabricated.
    # ------------------------------------------------------------------ #
    station("workflow/scan-part/", views.workflow_scan_part, name="workflow-scan-part"),
    station(
        "workflow/validate-model/",
        views.workflow_validate_model,
        name="workflow-validate-model",
    ),
    station("workflow/submit/", views.workflow_submit, name="workflow-submit"),
    station(
        "workflow/session/<uuid:session_id>/",
        views.workflow_progress,
        name="workflow-progress",
        access="session_read",
    ),
    station("workflow/activate-side/", views.workflow_activate_side, name="workflow-activate-side", access="session_mutation"),
    station(
        "workflow/inspect/<str:side>/",
        views.workflow_inspect_side,
        name="workflow-inspect-side",
        access="session_mutation",
    ),
    station(
        "workflow/compute-result/",
        views.workflow_compute_result,
        name="workflow-compute-result",
        access="session_mutation",
    ),
    station("workflow/save/", views.workflow_save, name="workflow-save", access="session_mutation"),
    station("workflow/cancel/", views.workflow_cancel, name="workflow-cancel", access="session_mutation"),
    station("workflow/camera-config/", views.api_camera_config, name="api-camera-config", access="camera_setup_view"),
    station(
        "workflow/camera-config/save/",
        views.api_camera_config_save,
        name="api-camera-config-save",
        access="camera",
    ),
    # ------------------------------------------------------------------ #
    #  Camera
    # ------------------------------------------------------------------ #
    station("camera/start/", views.camera_start, name="camera-start"),
    station("camera/stop/", views.camera_stop, name="camera-stop"),
    station("camera/status/", views.camera_status, name="camera-status"),
    station("camera/stream/", views.camera_stream, name="camera-stream"),
    station("camera/capture/", views.camera_capture, name="camera-capture"),
    # ------------------------------------------------------------------ #
    #  Legacy operations payload includes cross-station and configuration data.
    # ------------------------------------------------------------------ #
    admin(
        "operations/",
        ui_views.dashboard,
        name="dashboard-operations",
        access="dashboard",
    ),
    admin(
        "api/dashboard/summary/",
        dashboard_api.dashboard_summary,
        name="api-dashboard-summary",
        access="administrator",
    ),
    # ------------------------------------------------------------------ #
    #  Employee & Part Master CRUD (JSON) — administration, staff only
    # ------------------------------------------------------------------ #
    admin("api/employees/", views.api_employee_list, name="api-employee-list", access="master_data_view"),
    admin("api/employees/create/", views.employee_create, name="employee-create", access="master_data_create"),
    admin(
        "api/employees/<int:employee_id>/update/",
        views.employee_update,
        name="employee-update",
        access="master_data_change",
    ),
    station("api/parts/", views.api_part_list, name="api-part-list", access="master_data_view"),
    admin("api/parts/list/", views.parts_list_api, name="api-parts-list", access="master_data_view"),
    admin("api/parts/create/", views.part_create, name="part-create", access="master_data_create"),
    admin("api/parts/<int:part_id>/update/", views.part_update, name="part-update", access="master_data_change"),
    # ------------------------------------------------------------------ #
    #  Configuration (preset / master image / trigger zone / ROI) — staff only
    # ------------------------------------------------------------------ #
    admin("configuration/", views.configuration_view, name="configuration", access="model_view"),
    admin(
        "configuration/master-image/",
        views.master_image_view,
        name="master-image",
        access="master_images_view",
    ),
    admin(
        "configuration/master-image/save/",
        views.save_master_image,
        name="master-image-save",
        access="master_images_change",
    ),
    admin(
        "configuration/trigger-zone/<int:preset_id>/",
        views.trigger_zone_config_view,
        name="trigger-zone-config",
        access="trigger_zone_view",
    ),
    admin(
        "configuration/trigger-zone/save/",
        views.save_trigger_zone,
        name="trigger-zone-save",
        access="trigger_zone_change",
    ),
    admin(
        "configuration/rois/<int:preset_id>/",
        views.roi_config_list,
        name="roi-config-list",
        access="roi_view",
    ),
    admin("configuration/roi/create/", views.create_roi, name="roi-create", access="roi_change"),
    admin("configuration/roi/<int:roi_id>/update/", views.update_roi, name="roi-update", access="roi_change"),
    admin("configuration/roi/<int:roi_id>/delete/", views.delete_roi, name="roi-delete", access="roi_change"),
    admin("configuration/roi/reorder/", views.reorder_rois, name="roi-reorder", access="roi_change"),
    admin("configuration/preset/create/", views.create_preset, name="preset-create", access="presets_change"),
    admin(
        "configuration/preset/list/",
        views.preset_list_api,
        name="preset-list",
        access="presets_view",
    ),
    admin(
        "configuration/preset/<int:preset_id>/activate/",
        views.activate_preset,
        name="preset-activate",
        access="presets_change",
    ),

    station(
        "parts/<int:part_id>/roi-setup/",
        ui_views.part_roi_setup,
        name="part-roi-setup",
        access="camera_setup_view",
    ),
    station(
        "api/parts/<int:part_id>/vision-configuration/",
        views.api_part_vision_configuration,
        name="api-part-vision-configuration",
        access="configuration_view",
    ),
    station(
        "api/parts/<int:part_id>/vision-configuration/save/",
        views.api_part_vision_configuration_save,
        name="api-part-vision-configuration-save",
        access="configuration",
    ),
    station(
        "api/parts/<int:part_id>/vision-preview/",
        views.api_part_vision_preview,
        name="api-part-vision-preview",
        access="configuration_view",
    ),
    station(
        "api/yolo/models/<int:model_id>/classes/",
        views.api_yolo_model_classes,
        name="api-yolo-model-classes",
        access="configuration_view",
    ),
    station("api/yolo/models/upload/", views.part_model_upload, name="part-model-upload", access="yolo_models_change"),
    station(
        "api/parts/<int:part_id>/vision-roi/",
        views.api_part_vision_roi,
        name="api-part-vision-roi",
        access="configuration_view",
    ),
    station(
        "api/parts/<int:part_id>/vision-roi/save/",
        views.api_part_vision_roi_save,
        name="api-part-vision-roi-save",
        access="configuration",
    ),
    station(
        "api/parts/<int:part_id>/data-yaml/validate/",
        views.api_part_data_yaml_validate,
        name="api-part-data-yaml-validate",
        access="configuration_view",
    ),
    admin("preset/<int:preset_id>/", views.preset_detail, name="preset-detail", access="presets_view"),
    # ------------------------------------------------------------------ #
    #  YOLO Model registry — staff only (model uploads never public)
    # ------------------------------------------------------------------ #
    admin("yolo/models/", views.yolo_models_list, name="yolo-models", access="yolo_models_view"),
    admin("yolo/model/upload/", views.yolo_model_upload, name="yolo-model-upload", access="yolo_models_change"),
    admin("yolo/model/test/", views.yolo_model_test, name="yolo-model-test", access="yolo_models_change"),
    admin("yolo/detect/", views.yolo_detect, name="yolo-detect", access="yolo_models_change"),
    # ------------------------------------------------------------------ #
    #  YOLO Training (Celery + Redis + AJAX polling) — staff only
    # ------------------------------------------------------------------ #
    admin("training/start/", views.training_start, name="training-start", access="training_change"),
    admin(
        "training/status/<int:job_id>/",
        views.training_status,
        name="training-status",
        access="training_view",
    ),
    admin(
        "training/cancel/<int:job_id>/",
        views.training_cancel,
        name="training-cancel",
        access="training_change",
    ),
    admin("training/jobs/", views.training_jobs_list, name="training-jobs", access="training_view"),
    admin(
        "training/dataset-paths/",
        views.training_dataset_paths,
        name="training-dataset-paths",
        access="training_view",
    ),
    # ------------------------------------------------------------------ #
    #  Custom UI pages — administration, staff only
    # ------------------------------------------------------------------ #
    station("employees/", ui_views.employee_list, name="ui-employees", access="master_data_page"),
    station("parts/", ui_views.part_list, name="ui-parts", access="master_data_page"),
    station(
        "parts/<int:part_id>/vision-configuration/",
        ui_views.part_vision_configuration,
        name="part-vision-configuration",
        access="configuration_view",
    ),
    station("yolo-models/", ui_views.yolo_model_list, name="ui-yolo-models", access="yolo_models_page"),
    admin("presets/", ui_views.preset_list, name="ui-presets", access="presets_view"),
    admin("state/", ui_views.state_list, name="ui-state", access="inspection_state_view"),
    admin("logs/", ui_views.log_list, name="ui-logs", access="inspection_logs_view"),
    admin("rois/", ui_views.roi_list, name="ui-rois", access="roi_view"),
    admin(
        "trigger-zones/",
        ui_views.trigger_zone_list,
        name="ui-trigger-zones",
        access="trigger_zone_view",
    ),
    admin(
        "training-jobs/",
        ui_views.training_job_list,
        name="ui-training-jobs",
        access="training_view",
    ),
    # ------------------------------------------------------------------ #
    #  Inspection Report — view and export access are checked independently.
    # ------------------------------------------------------------------ #
    station(
        "reports/export/excel/",
        ui_views.report_export_excel,
        name="ui-report-export-excel",
        access="report_export",
    ),
    station(
        "reports/export/pdf/",
        ui_views.report_export_pdf,
        name="ui-report-export-pdf",
        access="report_export",
    ),
    station("reports/", ui_views.reports, name="ui-reports", access="reports"),
    station("settings/", ui_views.settings_page, name="ui-settings", access="storage_view"),
    station("settings/storage/", ui_views.storage_settings, name="storage-settings", access="storage_view"),
    station("api/storage-manager/browse/", storage_views.manager_browse, name="storage-manager-browse", access="storage_view"),
    station("api/storage-manager/create-folder/", storage_views.manager_create_folder, name="storage-manager-create-folder", access="storage_change"),
    station("api/storage-manager/merge/", storage_views.manager_merge, name="storage-manager-merge", access="storage_change"),
    admin("api/storage-manager/roots/", storage_views.manager_add_root, name="storage-manager-add-root", access="administrator"),
    admin("api/storage-manager/roots/<int:root_id>/deactivate/", storage_views.manager_deactivate_root, name="storage-manager-deactivate-root", access="administrator"),
    station("api/storage/state/", storage_views.state, name="storage-state", access="storage_view"),
    station("api/storage/automatic-settings/", storage_views.automatic_settings, name="storage-automatic-settings", access="storage_change"),
    station("api/storage/test/", storage_views.test_destination, name="storage-test", access="storage_change"),
    station("api/storage/activate/", storage_views.activate_destination, name="storage-activate", access="storage_change"),
    station("api/storage/migration/", storage_views.migration_start, name="storage-migration-start", access="storage_change"),
    station("api/storage/migration/<int:job_id>/batch/", storage_views.migration_batch, name="storage-migration-batch", access="storage_change"),
    station("api/storage/migration/<int:job_id>/", storage_views.migration_status, name="storage-migration-status", access="storage_view"),
    station("users/permissions/", auth_views.user_management_permissions, name="user-management-permissions", access="user_management"),
]
