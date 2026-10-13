from django.contrib import admin

from .models import (
    VisionInspectionLog,
    VisionInspectionMasterROI,
    VisionInspectionPreset,
    VisionInspectionState,
    VisionROI,
    VisionTrainingJob,
    VisionTriggerZone,
    VisionYOLOModel,
)


@admin.register(VisionInspectionMasterROI)
class VisionInspectionMasterROIAdmin(admin.ModelAdmin):
    list_display = ("part_number", "ref_width", "ref_height", "is_active", "updated_at")
    search_fields = ("part_number",)
    list_filter = ("is_active",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(VisionInspectionState)
class VisionInspectionStateAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "current_state",
        "employee_code",
        "employee_name",
        "part_number",
        "camera_status",
        "final_result",
        "updated_at",
    )

    search_fields = (
        "employee_code",
        "employee_name",
        "part_number",
        "preset_name",
    )

    list_filter = (
        "current_state",
        "camera_status",
        "final_result",
    )

    readonly_fields = ("updated_at",)
    ordering = ("-updated_at",)


@admin.register(VisionInspectionLog)
class VisionInspectionLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "employee_code",
        "part_number",
    )

    search_fields = (
        "employee_code",
        "part_number",
    )

    ordering = ("-id",)


@admin.register(VisionInspectionPreset)
class VisionInspectionPresetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
    )

    search_fields = ("name",)


@admin.register(VisionTrainingJob)
class VisionTrainingJobAdmin(admin.ModelAdmin):
    list_display = ("id",)

    ordering = ("-id",)


@admin.register(VisionYOLOModel)
class VisionYOLOModelAdmin(admin.ModelAdmin):
    list_display = ("id",)

    ordering = ("-id",)


@admin.register(VisionROI)
class VisionROIAdmin(admin.ModelAdmin):
    list_display = ("id",)

    ordering = ("-id",)


@admin.register(VisionTriggerZone)
class VisionTriggerZoneAdmin(admin.ModelAdmin):
    list_display = ("id",)

    ordering = ("-id",)
