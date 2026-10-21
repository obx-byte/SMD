"""URL configuration for the VisionInspection application.

All URLs are relative to ``/vision-inspection/`` as set in the project's
root ``urls.py``.

Deployment model
----------------
This is a **dedicated factory inspection station** with no login: an operator
opens the application URL and lands directly on Inspection Master.  Routes are
split into two groups:

``station``  — operator routes that must work anonymously (inspection master,
    live inspection, camera, ROI, workflow, history, traceability, state, and
    the read-only Inspection Report).

``admin``    — privileged routes (configuration setup, model registry,
    master data, training, report exports, settings).  These are wrapped with
    :func:`~django.contrib.admin.views.decorators.staff_member_required`, so
    they keep a Django login and never become public.  Authentication
    middleware, CSRF protection and database permissions are left enabled.

Only the second group asks for a login; the station group never redirects.
"""

from django.urls import path

from VisionInspection import auth_views, dashboard_api, storage_views, ui_views, views
from VisionInspection.services.access_control import (
    require_administrator,
    require_audit_access,
    require_camera_roi_access,
    require_configuration_access,
    require_configuration_view,
    require_inspection_access,
    require_report_access,
    require_report_export,
    require_authenticated,
    require_session_mutation_access,
    require_session_read_access,
    require_user_management,
)

app_name = "VisionInspection"


_ACCESS_DECORATORS = {
    "authenticated": require_authenticated,
    "inspection": require_inspection_access,
    "reports": require_report_access,
    "report_export": require_report_export,
    "configuration": require_configuration_access,
    "configuration_view": require_configuration_view,
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


def admin(route, view, name):
    """Administrator-only route; staff status alone does not grant access."""
    return path(route, require_administrator(view), name=name)


urlpatterns = [
    public("login/", auth_views.employee_login, name="login"),
    station("logout/", auth_views.employee_logout, name="logout", access="authenticated"),
    station("account/password/", auth_views.change_password, name="password-change", access="authenticated"),
    station("users/", auth_views.user_management, name="user-management", access="user_management"),
    station("audit/", auth_views.audit_log, name="audit-log", access="audit"),
    # ------------------------------------------------------------------ #
    #  Station landing page — Dashboard (no login, no redirect).
    #  The sidebar "Dashboard" item and the workspace Home icon point here.
    # ------------------------------------------------------------------ #
    station("", ui_views.dashboard, name="dashboard", access="reports"),
    station("inspection/", views.inspection_view, name="inspection"),
    station("history/", views.history_view, name="history", access="reports"),
    station("storage/image/<int:metadata_id>/", storage_views.image, name="storage-image", access="reports"),
    station(
        "history/<int:inspection_id>/",
        views.history_detail,
        name="history-detail",
        access="reports",
    ),
    station("traceability/", ui_views.traceability, name="ui-traceability", access="reports"),
    station(
        "traceability/<int:inspection_id>/",
        ui_views.traceability_detail,
        name="traceability-detail",
        access="reports",
    ),
    # Scan-time validation (read-only; works without any account).
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
        access="configuration_view",
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
        access="configuration_view",
    ),
    station(
        "inspection-master/roi/",
        views.master_roi_get,
        name="master-roi-get",
        access="configuration_view",
    ),
    station(
        "inspection-master/preview/",
        views.api_master_roi_preview,
        name="master-roi-preview",
        access="configuration_view",
    ),
    station(
        "inspection-master/camera-resolution/",
        views.api_master_camera_resolution,
        name="master-camera-resolution",
        access="configuration_view",
    ),
    station(
        "inspection-master/<str:part_number>/",
        views.inspection_master_view,
        name="inspection-master-part",
        access="configuration_view",
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
    station("workflow/camera-config/", views.api_camera_config, name="api-camera-config", access="configuration_view"),
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
    #  Operations dashboard (aggregated JSON for dashboard.js) — staff only
    # ------------------------------------------------------------------ #
    admin(
        "operations/",
        ui_views.dashboard,
        name="dashboard-operations",
    ),
    admin(
        "api/dashboard/summary/",
        dashboard_api.dashboard_summary,
        name="api-dashboard-summary",
    ),
    # ------------------------------------------------------------------ #
    #  Employee & Part Master CRUD (JSON) — administration, staff only
    # ------------------------------------------------------------------ #
    admin("api/employees/", views.api_employee_list, name="api-employee-list"),
    admin("api/employees/create/", views.employee_create, name="employee-create"),
    admin(
        "api/employees/<int:employee_id>/update/",
        views.employee_update,
        name="employee-update",
    ),
    station("api/parts/", views.api_part_list, name="api-part-list", access="configuration_view"),
    admin("api/parts/list/", views.parts_list_api, name="api-parts-list"),
    admin("api/parts/create/", views.part_create, name="part-create"),
    admin("api/parts/<int:part_id>/update/", views.part_update, name="part-update"),
    # ------------------------------------------------------------------ #
    #  Configuration (preset / master image / trigger zone / ROI) — staff only
    # ------------------------------------------------------------------ #
    admin("configuration/", views.configuration_view, name="configuration"),
    admin(
        "configuration/master-image/",
        views.master_image_view,
        name="master-image",
    ),
    admin(
        "configuration/master-image/save/",
        views.save_master_image,
        name="master-image-save",
    ),
    admin(
        "configuration/trigger-zone/<int:preset_id>/",
        views.trigger_zone_config_view,
        name="trigger-zone-config",
    ),
    admin(
        "configuration/trigger-zone/save/",
        views.save_trigger_zone,
        name="trigger-zone-save",
    ),
    admin(
        "configuration/rois/<int:preset_id>/",
        views.roi_config_list,
        name="roi-config-list",
    ),
    admin("configuration/roi/create/", views.create_roi, name="roi-create"),
    admin("configuration/roi/<int:roi_id>/update/", views.update_roi, name="roi-update"),
    admin("configuration/roi/<int:roi_id>/delete/", views.delete_roi, name="roi-delete"),
    admin("configuration/roi/reorder/", views.reorder_rois, name="roi-reorder"),
    admin("configuration/preset/create/", views.create_preset, name="preset-create"),
    admin(
        "configuration/preset/list/",
        views.preset_list_api,
        name="preset-list",
    ),
    admin(
        "configuration/preset/<int:preset_id>/activate/",
        views.activate_preset,
        name="preset-activate",
    ),

    station(
        "parts/<int:part_id>/roi-setup/",
        ui_views.part_roi_setup,
        name="part-roi-setup",
        access="configuration_view",
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
    station("api/yolo/models/upload/", views.part_model_upload, name="part-model-upload", access="administrator"),
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
    admin("preset/<int:preset_id>/", views.preset_detail, name="preset-detail"),
    # ------------------------------------------------------------------ #
    #  YOLO Model registry — staff only (model uploads never public)
    # ------------------------------------------------------------------ #
    admin("yolo/models/", views.yolo_models_list, name="yolo-models"),
    admin("yolo/model/upload/", views.yolo_model_upload, name="yolo-model-upload"),
    admin("yolo/model/test/", views.yolo_model_test, name="yolo-model-test"),
    admin("yolo/detect/", views.yolo_detect, name="yolo-detect"),
    # ------------------------------------------------------------------ #
    #  YOLO Training (Celery + Redis + AJAX polling) — staff only
    # ------------------------------------------------------------------ #
    admin("training/start/", views.training_start, name="training-start"),
    admin(
        "training/status/<int:job_id>/",
        views.training_status,
        name="training-status",
    ),
    admin(
        "training/cancel/<int:job_id>/",
        views.training_cancel,
        name="training-cancel",
    ),
    admin("training/jobs/", views.training_jobs_list, name="training-jobs"),
    admin(
        "training/dataset-paths/",
        views.training_dataset_paths,
        name="training-dataset-paths",
    ),
    # ------------------------------------------------------------------ #
    #  Custom UI pages — administration, staff only
    # ------------------------------------------------------------------ #
    admin("employees/", ui_views.employee_list, name="ui-employees"),
    station("parts/", ui_views.part_list, name="ui-parts", access="configuration_view"),
    station(
        "parts/<int:part_id>/vision-configuration/",
        ui_views.part_vision_configuration,
        name="part-vision-configuration",
        access="configuration_view",
    ),
    admin("yolo-models/", ui_views.yolo_model_list, name="ui-yolo-models"),
    admin("presets/", ui_views.preset_list, name="ui-presets"),
    admin("state/", ui_views.state_list, name="ui-state"),
    admin("logs/", ui_views.log_list, name="ui-logs"),
    admin("rois/", ui_views.roi_list, name="ui-rois"),
    admin(
        "trigger-zones/",
        ui_views.trigger_zone_list,
        name="ui-trigger-zones",
    ),
    admin(
        "training-jobs/",
        ui_views.training_job_list,
        name="ui-training-jobs",
    ),
    # ------------------------------------------------------------------ #
    #  Inspection Report — operator page is available without login; exports
    #  remain staff-only because they download report data.
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
    admin("settings/", ui_views.settings_page, name="ui-settings"),
    station("settings/storage/", ui_views.storage_settings, name="storage-settings", access="administrator"),
    station("api/storage/state/", storage_views.state, name="storage-state", access="administrator"),
    station("api/storage/automatic-settings/", storage_views.automatic_settings, name="storage-automatic-settings", access="administrator"),
    station("api/storage/test/", storage_views.test_destination, name="storage-test", access="administrator"),
    station("api/storage/activate/", storage_views.activate_destination, name="storage-activate", access="administrator"),
    station("api/storage/migration/", storage_views.migration_start, name="storage-migration-start", access="administrator"),
    station("api/storage/migration/<int:job_id>/batch/", storage_views.migration_batch, name="storage-migration-batch", access="administrator"),
    station("api/storage/migration/<int:job_id>/", storage_views.migration_status, name="storage-migration-status", access="administrator"),
]
