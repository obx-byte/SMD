"""Models for the VisionInspection application.

This module defines the data models for the automated visual inspection system.
The application is intentionally self-contained for employee and part masters —
these models store inspection presets, ROIs, trigger zones, YOLO model metadata,
and historical inspection logs.
"""

import re
import uuid

from django.db import models
from django.utils import timezone


def normalize_token(value: str) -> str:
    """Canonical form used for case/whitespace-insensitive uniqueness.

    Collapses internal whitespace runs, strips ends and lower-cases so that
    ``"Ravi Kumar"``, ``"ravi  kumar"``, ``" Ravi Kumar "`` and ``"RAVI KUMAR"``
    are treated as the same value.
    """
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()




class VisionInspectionEmployee(models.Model):
    """Local employee master for the VisionInspection admin."""

    employee_code = models.CharField(max_length=50, unique=True)
    employee_name = models.CharField(max_length=100)
    department = models.CharField(max_length=100, blank=True, null=True)
    designation = models.CharField(max_length=100, blank=True, null=True)
    name_normalized = models.CharField(
        max_length=100,
        unique=True,
        editable=False,
        help_text="Case/space-normalized employee name (unique). Maintained on save().",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_employee"
        ordering = ["employee_name"]
        verbose_name = "Vision Inspection Employee"
        verbose_name_plural = "Vision Inspection Employees"

    def __str__(self):
        return f"{self.employee_code} — {self.employee_name}"

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_token(self.employee_name)
        super().save(*args, **kwargs)


class VisionInspectionPart(models.Model):
    """Local part master for the VisionInspection admin."""

    part_number = models.CharField(max_length=40, unique=True)
    part_name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    part_number_normalized = models.CharField(
        max_length=40,
        unique=True,
        editable=False,
        help_text="Case-normalized part number (unique). Maintained on save().",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_part"
        ordering = ["part_number"]
        verbose_name = "Vision Inspection Part"
        verbose_name_plural = "Vision Inspection Parts"

    def __str__(self):
        return f"{self.part_number} — {self.part_name}"

    def save(self, *args, **kwargs):
        self.part_number_normalized = normalize_token(self.part_number)
        super().save(*args, **kwargs)


class VisionYOLOModel(models.Model):
    """Stores metadata for YOLO .pt models available for inspection."""

    name = models.CharField(max_length=100)
    model_path = models.FilePathField(
        max_length=500, help_text="Full filesystem path to the .pt model file."
    )
    version = models.CharField(max_length=50, blank=True, null=True)
    part_class = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="YOLO class name that represents the whole part (e.g. 'PART-001').",
    )
    num_classes = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_yolo_model"
        ordering = ["-created_at"]
        verbose_name = "Vision YOLO Model"
        verbose_name_plural = "Vision YOLO Models"

    def __str__(self):
        return f"{self.name} v{self.version or '1.0'}"


class VisionInspectionPreset(models.Model):
    """Configuration preset for inspecting a specific part number.

    Only one *active* preset may exist per ``part_number``.
    Historical versions are preserved; new versions are created rather than
    overwriting existing ones.
    """

    part_number = models.CharField(
        max_length=40,
        help_text="Part number from the existing Shopfloor PartNo master.",
    )
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    master_image = models.ImageField(
        upload_to="vision_inspection/master_images/",
        blank=True,
        null=True,
        help_text="Captured reference image of the correct part.",
    )
    camera_index = models.IntegerField(default=0)
    yolo_model = models.ForeignKey(
        VisionYOLOModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="presets",
    )
    part_class = models.CharField(
        max_length=100,
        help_text="Expected YOLO class label that identifies this part (e.g. 'PART-001').",
    )
    confidence_threshold = models.FloatField(default=0.5)
    stable_frames = models.PositiveIntegerField(default=3)
    trigger_cooldown = models.FloatField(default=2.0)
    trigger_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional trigger zone configuration (containment threshold, etc.).",
    )
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_preset"
        ordering = ["-created_at"]
        unique_together = ("part_number", "version")
        verbose_name = "Vision Inspection Preset"
        verbose_name_plural = "Vision Inspection Presets"

    def __str__(self):
        return f"{self.name} (v{self.version})"

    def save(self, *args, **kwargs):
        if self.is_active:
            VisionInspectionPreset.objects.filter(
                part_number=self.part_number, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)


class VisionTriggerZone(models.Model):
    """Four-corner polygon defining the trigger zone (part entry detection)."""

    preset = models.ForeignKey(
        VisionInspectionPreset,
        on_delete=models.CASCADE,
        related_name="trigger_zones",
    )
    p1 = models.JSONField(help_text="Corner 1 as [x, y] (normalized 0-1).")
    p2 = models.JSONField(help_text="Corner 2 as [x, y] (normalized 0-1).")
    p3 = models.JSONField(help_text="Corner 3 as [x, y] (normalized 0-1).")
    p4 = models.JSONField(help_text="Corner 4 as [x, y] (normalized 0-1).")
    reference_width = models.FloatField(help_text="Master image width in pixels.")
    reference_height = models.FloatField(help_text="Master image height in pixels.")

    class Meta:
        db_table = "vision_trigger_zone"
        verbose_name = "Vision Trigger Zone"
        verbose_name_plural = "Vision Trigger Zones"

    @property
    def polygon(self):
        """Return the four corners as an (N, 2) numpy-compatible list."""
        return [list(self.p1), list(self.p2), list(self.p3), list(self.p4)]


class VisionROI(models.Model):
    """A custom four-corner ROI area for inspection within the part bounding box."""

    preset = models.ForeignKey(
        VisionInspectionPreset,
        on_delete=models.CASCADE,
        related_name="rois",
    )
    roi_name = models.CharField(max_length=100, default="ROI")
    sequence = models.PositiveIntegerField(default=0)
    p1 = models.JSONField(help_text="Corner 1 as [x, y] (normalized 0-1).")
    p2 = models.JSONField(help_text="Corner 2 as [x, y] (normalized 0-1).")
    p3 = models.JSONField(help_text="Corner 3 as [x, y] (normalized 0-1).")
    p4 = models.JSONField(help_text="Corner 4 as [x, y] (normalized 0-1).")
    normalized_coordinates = models.JSONField(
        default=dict,
        help_text="Full normalized polygon stored as {polygon: [[x,y],...], ref_w, ref_h}.",
    )
    expected_class = models.CharField(
        max_length=100, help_text="YOLO class expected inside this ROI."
    )
    min_count = models.PositiveIntegerField(default=0)
    max_count = models.PositiveIntegerField(default=0)
    confidence_threshold = models.FloatField(default=0.5)
    inspection_rule = models.CharField(
        max_length=50,
        choices=[
            ("count", "Count Matching"),
            ("presence", "Presence"),
            ("absence", "Absence"),
        ],
        default="count",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_roi"
        ordering = ["preset", "sequence"]
        verbose_name = "Vision ROI"
        verbose_name_plural = "Vision ROIs"

    @property
    def polygon(self):
        return [list(self.p1), list(self.p2), list(self.p3), list(self.p4)]

    def __str__(self):
        return f"{self.roi_name} (#{self.sequence})"


class VisionInspectionLog(models.Model):
    """Historical record of a completed inspection cycle."""

    employee_code = models.CharField(max_length=100, blank=True, null=True)
    part_number = models.CharField(max_length=40, blank=True, null=True)
    preset_name = models.CharField(max_length=200, blank=True, null=True)
    preset_version = models.PositiveIntegerField(blank=True, null=True)
    yolo_model_name = models.CharField(max_length=100, blank=True, null=True)
    yolo_model_version = models.CharField(max_length=50, blank=True, null=True)

    timestamp = models.DateTimeField(auto_now_add=True)
    result = models.CharField(
        max_length=20,
        choices=[
            ("OK", "OK"),
            ("NOT_OK", "NOT OK"),
            ("ERROR", "INSPECTION ERROR"),
        ],
        default="ERROR",
    )
    roi_results = models.JSONField(
        default=list,
        help_text='[{"roi_name": "ROI-01", "result": "MATCH", "expected": "bolt x4", "detected": "bolt x4"}]',
    )
    detected_objects = models.JSONField(
        default=list,
        help_text='[{"class": "bolt", "confidence": 0.92, "bbox": [x1,y1,x2,y2]}]',
    )
    inspection_image = models.ImageField(
        upload_to="vision_inspection/results/", blank=True, null=True
    )
    error_details = models.TextField(blank=True, null=True)
    stable_frames_used = models.PositiveIntegerField(default=0)
    part_bbox = models.JSONField(blank=True, null=True)
    trigger_zone_polygon = models.JSONField(blank=True, null=True)

    class Meta:
        db_table = "vision_inspection_log"
        ordering = ["-timestamp"]
        verbose_name = "Vision Inspection Log"
        verbose_name_plural = "Vision Inspection Logs"
        indexes = [
            # Chronological ordering + range filters (traceability, reports, trends).
            models.Index(fields=["timestamp"], name="vi_log_ts_idx"),
            # Part filter + dashboard part-wise aggregation + inspection filter.
            models.Index(fields=["part_number"], name="vi_log_part_idx"),
            # Result filter + dashboard OK/NOT_OK aggregation.
            models.Index(fields=["result"], name="vi_log_result_idx"),
            # Traceability search by employee/operator code.
            models.Index(fields=["employee_code"], name="vi_log_emp_idx"),
        ]

    def __str__(self):
        return f"Inspection #{self.pk} — {self.result} @ {self.timestamp}"


class VisionInspectionState(models.Model):
    """Singleton-style model storing the current real-time state of the inspection engine.

    Updated by the background inspection thread; read by the UI via AJAX polling.
    """

    STATE_CHOICES = [
        ("WAITING_EMPLOYEE", "Waiting Employee"),
        ("EMPLOYEE_VALID", "Employee Valid"),
        ("WAITING_PART", "Waiting Part"),
        ("PART_VALID", "Part Valid"),
        ("WAITING_FOR_PART_DETECTION", "Waiting for Part Detection"),
        ("PART_DETECTED", "Part Detected"),
        ("PART_IN_TRIGGER_ZONE", "Part in Trigger Zone"),
        ("STABILITY_CHECK", "Stability Check"),
        ("AUTO_TRIGGER", "Auto Trigger"),
        ("INSPECTING", "Inspecting"),
        ("RESULT_OK", "Result OK"),
        ("RESULT_NOT_OK", "Result Not OK"),
        ("WAIT_PART_LEAVE", "Wait Part Leave"),
        ("RE_ARM", "Re-Arm"),
        ("ERROR", "Error"),
    ]

    current_state = models.CharField(
        max_length=50, choices=STATE_CHOICES, default="WAITING_EMPLOYEE"
    )
    employee_code = models.CharField(max_length=100, blank=True, null=True)
    employee_valid = models.BooleanField(default=False)
    employee_name = models.CharField(max_length=100, blank=True, null=True)
    part_number = models.CharField(max_length=40, blank=True, null=True)
    part_valid = models.BooleanField(default=False)
    preset_name = models.CharField(max_length=200, blank=True, null=True)
    preset_version = models.PositiveIntegerField(blank=True, null=True)
    camera_status = models.CharField(
        max_length=20,
        choices=[
            ("CONNECTING", "Connecting"),
            ("ON", "On"),
            ("OFF", "Off"),
            ("ERROR", "Error"),
        ],
        default="OFF",
    )
    trigger_auto = models.BooleanField(default=True)
    error_message = models.TextField(blank=True, null=True)
    roi_results = models.JSONField(default=list)
    final_result = models.CharField(max_length=20, default="PENDING")
    detected_objects = models.JSONField(default=list)
    last_inspection_id = models.PositiveIntegerField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_state"
        verbose_name = "Vision Inspection State"
        verbose_name_plural = "Vision Inspection States"

    def __str__(self):
        return f"State: {self.get_current_state_display()} | Final: {self.final_result}"


class VisionTrainingJob(models.Model):
    """Tracks a YOLO model training job processed via Celery + Redis.

    The Celery task updates this record during training.  The frontend polls
    ``GET /vision-inspection/training/status/<id>/`` to read these values.
    """

    STATUS_QUEUED = "QUEUED"
    STATUS_STARTING = "STARTING"
    STATUS_PREPARING = "PREPARING_DATASET"
    STATUS_TRAINING = "TRAINING"
    STATUS_VALIDATING = "VALIDATING"
    STATUS_EVALUATING = "EVALUATING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_STARTING, "Starting"),
        (STATUS_PREPARING, "Preparing Dataset"),
        (STATUS_TRAINING, "Training"),
        (STATUS_VALIDATING, "Validating"),
        (STATUS_EVALUATING, "Evaluating"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    id = models.AutoField(primary_key=True)
    training_project = models.CharField(
        max_length=200, help_text="Name/ID of the training project"
    )
    celery_task_id = models.CharField(
        max_length=100, blank=True, null=True, help_text="Celery task ID"
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
    progress = models.IntegerField(
        default=0, help_text="Overall progress percentage (0-100)"
    )
    current_epoch = models.IntegerField(default=0, help_text="Current training epoch")
    total_epochs = models.IntegerField(default=100, help_text="Total epochs to train")
    message = models.TextField(blank=True, help_text="Current status message")
    precision = models.FloatField(default=0.0)
    recall = models.FloatField(default=0.0)
    map50 = models.FloatField(default=0.0)
    map50_95 = models.FloatField(default=0.0)
    model_path = models.CharField(
        max_length=500, blank=True, null=True, help_text="Path to trained model"
    )
    model_version = models.CharField(
        max_length=50, blank=True, null=True, help_text="New model version string"
    )
    error_message = models.TextField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_training_job"
        ordering = ["-created_at"]
        verbose_name = "Vision Training Job"
        verbose_name_plural = "Vision Training Jobs"

    def __str__(self):
        return f"TrainingJob #{self.pk} — {self.status} ({self.progress}%)"


class VisionInspectionMasterROI(models.Model):
    """Manually drawn inspection ROI for the live-camera Inspection Master.

    Stores one rectangular region of interest per ``part_number`` + camera
    ``view`` (front/back) as *normalized* coordinates (0..1) so the same ROI can
    be re-projected onto any future frame size — allowing inspection to be
    restarted without redrawing, surviving camera resolution changes, and
    keeping front and back ROIs independent.

    ``ref_width`` / ``ref_height`` record the frame dimensions at draw time so
    the UI can display the original pixel X / Y / W / H while the pipeline crops
    using the live resolution.
    """

    VIEW_FRONT = "front"
    VIEW_BACK = "back"
    VIEW_CHOICES = ((VIEW_FRONT, "Front"), (VIEW_BACK, "Back"))

    part_number = models.CharField(max_length=40)
    view = models.CharField(max_length=8, choices=VIEW_CHOICES, default=VIEW_FRONT)
    x = models.FloatField(help_text="Normalized ROI left edge (0..1).")
    y = models.FloatField(help_text="Normalized ROI top edge (0..1).")
    width = models.FloatField(help_text="Normalized ROI width (0..1).")
    height = models.FloatField(help_text="Normalized ROI height (0..1).")
    ref_width = models.PositiveIntegerField(help_text="Frame width at draw time.")
    ref_height = models.PositiveIntegerField(help_text="Frame height at draw time.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_master_roi"
        unique_together = (("part_number", "view"),)
        verbose_name = "Inspection Master ROI"
        verbose_name_plural = "Inspection Master ROIs"

    def __str__(self):
        return f"Master ROI — {self.part_number} ({self.view})"


class VisionInspectionCameraConfig(models.Model):
    """One-time camera + ROI configuration, reused across all parts.

    Coordinates are normalized (0..1) against the stored ``frame_width`` /
    ``frame_height`` (camera-frame pixels — never physical mm, which would need
    calibration).  One active config per camera *view* (front/back).  Versioned
    so historical sessions can record which configuration produced them.
    """

    VIEW_FRONT = "front"
    VIEW_BACK = "back"
    VIEW_CHOICES = ((VIEW_FRONT, "Front"), (VIEW_BACK, "Back"))

    view = models.CharField(max_length=8, choices=VIEW_CHOICES, default=VIEW_FRONT)
    camera_source = models.CharField(max_length=20, default="opencv")
    camera_index = models.IntegerField(default=0)
    frame_width = models.PositiveIntegerField()
    frame_height = models.PositiveIntegerField()
    roi_x = models.FloatField(help_text="Normalized ROI left (0..1).")
    roi_y = models.FloatField(help_text="Normalized ROI top (0..1).")
    roi_w = models.FloatField(help_text="Normalized ROI width (0..1).")
    roi_h = models.FloatField(help_text="Normalized ROI height (0..1).")
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    updated_by = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_inspection_camera_config"
        ordering = ["view", "-version"]

    def active_for(self):
        return self.view

    def __str__(self):
        return f"Camera config [{self.view}] v{self.version} ({self.frame_width}x{self.frame_height})"


class VisionInspectionSession(models.Model):
    """Sequential front→back Airbag Stitch Inspection session.

    Backs the end-to-end workflow: barcode → model validation → submit → front
    inspection → back inspection → overall result → save.  Stages and per-side
    statuses are persisted so the progress panel and traceability survive
    refresh/direct navigation.  Session id is unique and ``is_saved`` makes the
    save operation idempotent.
    """

    STAGE_CHOICES = (
        ("scan", "Scan Part"),
        ("model", "Model Loaded"),
        ("front", "Front Inspection"),
        ("back", "Back Inspection"),
        ("result", "Final Result"),
        ("saved", "Saved"),
        ("cancelled", "Cancelled"),
    )
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    OVERALL_OK = "OK"
    OVERALL_NOT_OK = "NOT_OK"
    OVERALL_INCOMPLETE = "INCOMPLETE"

    session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    part_number = models.CharField(max_length=40, db_index=True)
    barcode = models.CharField(max_length=80, blank=True, default="")
    model = models.ForeignKey(
        VisionYOLOModel, on_delete=models.SET_NULL, null=True, blank=True
    )
    model_version = models.CharField(max_length=50, blank=True, default="")
    model_classes = models.JSONField(default=list, blank=True)
    # Part vision configuration used for this session.  ``config_version`` and
    # ``config_snapshot`` freeze the exact rules (models, classes, expected
    # counts, thresholds, priorities, sides, ROI, camera) so history never
    # changes when the part's current configuration is edited later.
    configuration = models.ForeignKey(
        "VisionPartConfiguration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
    )
    config_version = models.PositiveIntegerField(default=0)
    config_snapshot = models.JSONField(default=dict, blank=True)
    roi_version = models.PositiveIntegerField(default=0)
    # Combined per-class results of this session (one row per configured class).
    class_results = models.JSONField(default=list, blank=True)
    # Front + Back on a single camera requires an explicit reposition
    # confirmation before the Back capture is accepted.
    reposition_confirmed = models.BooleanField(default=False)

    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default="scan", db_index=True)
    front_status = models.CharField(max_length=20, default="PENDING", db_index=True)
    back_status = models.CharField(max_length=20, default="PENDING")
    front_verdict = models.CharField(max_length=20, default="PENDING")
    back_verdict = models.CharField(max_length=20, default="PENDING")
    overall = models.CharField(max_length=20, default="PENDING", db_index=True)

    front_roi = models.ForeignKey(
        VisionInspectionMasterROI, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="front_sessions",
    )
    back_roi = models.ForeignKey(
        VisionInspectionMasterROI, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="back_sessions",
    )
    # ROI frozen for this session (normalized 0..1 + ref), resolved from the
    # active camera config (preferred) or the per-part ROI at Submit time.
    front_roi_norm = models.JSONField(default=dict, blank=True)
    back_roi_norm = models.JSONField(default=dict, blank=True)

    front_image = models.ImageField(
        upload_to="vision_inspection/sessions/front/", null=True, blank=True
    )
    back_image = models.ImageField(
        upload_to="vision_inspection/sessions/back/", null=True, blank=True
    )
    front_annotated = models.ImageField(
        upload_to="vision_inspection/sessions/front_annotated/", null=True, blank=True
    )
    back_annotated = models.ImageField(
        upload_to="vision_inspection/sessions/back_annotated/", null=True, blank=True
    )
    front_results = models.JSONField(default=list, blank=True)
    back_results = models.JSONField(default=list, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    front_completed_at = models.DateTimeField(null=True, blank=True)
    back_completed_at = models.DateTimeField(null=True, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    is_saved = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        db_table = "vision_inspection_session"
        ordering = ["-started_at"]

    def __str__(self):
        return f"Session {self.session_id} — {self.part_number} ({self.stage}/{self.overall})"


# =========================================================================== #
#  Part-wise vision configuration (Airbag Stitch Inspection)
#
#  One configuration per part owns every setting used at inspection time:
#  the models attached to the part, the per-class side/expected-count/
#  confidence/priority rules, the exclusive camera backend per role, and the
#  ONE common ROI shared by every enabled model and both sides.
# =========================================================================== #


class VisionPartConfiguration(models.Model):
    """Part-wise vision configuration (versioned; one active per part).

    ``status`` is derived from real validation (see
    :mod:`VisionInspection.services.part_configuration`) and stored so the UI
    can show Draft / Validation Failed / Ready without re-validating on every
    render.  A configuration is never marked READY unless every required
    setting was actually validated.
    """

    STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
    STATUS_DRAFT = "DRAFT"
    STATUS_VALIDATION_FAILED = "VALIDATION_FAILED"
    STATUS_READY = "READY"
    STATUS_INACTIVE = "INACTIVE"
    STATUS_CHOICES = (
        (STATUS_NOT_CONFIGURED, "Not Configured"),
        (STATUS_DRAFT, "Draft"),
        (STATUS_VALIDATION_FAILED, "Validation Failed"),
        (STATUS_READY, "Ready for Inspection"),
        (STATUS_INACTIVE, "Inactive"),
    )

    MODE_FRONT_ONLY = "FRONT_ONLY"
    MODE_BACK_ONLY = "BACK_ONLY"
    MODE_FRONT_BACK = "FRONT_BACK"
    MODE_CHOICES = (
        (MODE_FRONT_ONLY, "Front Only"),
        (MODE_BACK_ONLY, "Back Only"),
        (MODE_FRONT_BACK, "Front + Back"),
    )

    SIDE_FRONT = "FRONT"
    SIDE_BACK = "BACK"
    SIDE_BOTH = "BOTH"
    SIDE_CHOICES = (
        (SIDE_FRONT, "Front"),
        (SIDE_BACK, "Back"),
        (SIDE_BOTH, "Both"),
    )

    SOURCE_OPENCV = "OPENCV"
    SOURCE_RPI = "RPI"
    SOURCE_CHOICES = (
        (SOURCE_OPENCV, "OpenCV Camera"),
        (SOURCE_RPI, "Raspberry Pi Camera"),
    )

    part = models.ForeignKey(
        VisionInspectionPart,
        on_delete=models.CASCADE,
        related_name="vision_configurations",
    )
    name = models.CharField(max_length=200, blank=True, default="")
    version = models.PositiveIntegerField(default=1)

    inspection_mode = models.CharField(
        max_length=20, choices=MODE_CHOICES, default=MODE_FRONT_BACK
    )
    # Exactly one backend per configured camera role — never an ambiguous
    # "both booleans true" selection.
    front_camera_source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_OPENCV
    )
    front_camera_index = models.PositiveIntegerField(default=0)
    back_camera_source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_OPENCV
    )
    back_camera_index = models.PositiveIntegerField(default=1)
    # A single physical camera used for Front + Back requires an explicit
    # reposition confirmation before the Back capture is taken.
    single_camera_reposition_confirmation = models.BooleanField(default=True)

    # Acceptance rule applied to every configured class unless the class
    # overrides it.  Nothing is invented: an empty rule keeps a class
    # unruled and the configuration incomplete.
    acceptance_rule = models.CharField(
        max_length=30,
        choices=[
            ("count", "Expected detection count"),
            ("presence", "Required class presence"),
            ("confidence", "Confidence threshold"),
            ("count_confidence", "Count and confidence"),
        ],
        default="count_confidence",
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_NOT_CONFIGURED
    )
    validation_errors = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=False)
    configured_by = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_part_configuration"
        ordering = ["part__part_number", "-version"]
        unique_together = (("part", "version"),)
        verbose_name = "Part Vision Configuration"
        verbose_name_plural = "Part Vision Configurations"
        indexes = [
            models.Index(fields=["part", "is_active"], name="vi_partcfg_active_idx"),
        ]

    def __str__(self):
        return f"{self.part.part_number} configuration v{self.version} ({self.status})"

    def save(self, *args, **kwargs):
        if self.is_active:
            VisionPartConfiguration.objects.filter(
                part=self.part, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    @property
    def requires_front(self):
        return self.inspection_mode in (self.MODE_FRONT_ONLY, self.MODE_FRONT_BACK)

    @property
    def requires_back(self):
        return self.inspection_mode in (self.MODE_BACK_ONLY, self.MODE_FRONT_BACK)


class VisionPartModel(models.Model):
    """A YOLO model attached to a part configuration.

    Parts may carry any number of models; each keeps its own display name,
    version, task type and the *actual* class list discovered from the model
    file.  Classes of different models are never merged.
    """

    TASK_DETECT = "detect"
    TASK_SEGMENT = "segment"
    TASK_CHOICES = (
        (TASK_DETECT, "Detection"),
        (TASK_SEGMENT, "Segmentation"),
    )

    configuration = models.ForeignKey(
        VisionPartConfiguration,
        on_delete=models.CASCADE,
        related_name="part_models",
    )
    model = models.ForeignKey(
        VisionYOLOModel,
        on_delete=models.CASCADE,
        related_name="part_associations",
    )
    display_name = models.CharField(max_length=200, blank=True, default="")
    version = models.CharField(max_length=50, blank=True, default="")
    task_type = models.CharField(
        max_length=20, choices=TASK_CHOICES, default=TASK_SEGMENT
    )
    # Actual class names/ids discovered from the uploaded model file.
    classes = models.JSONField(default=list, blank=True)
    num_classes = models.PositiveIntegerField(default=0)
    is_enabled = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False)
    validation_status = models.CharField(max_length=20, default="PENDING")
    validation_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_part_model"
        ordering = ["configuration", "id"]
        unique_together = (("configuration", "model"),)
        verbose_name = "Part Model"
        verbose_name_plural = "Part Models"

    def __str__(self):
        return f"{self.display_name or self.model.name} → {self.configuration.part.part_number}"


class VisionClassConfiguration(models.Model):
    """Per-class inspection rule attached to one part model.

    ``class_id`` / ``class_name`` always come from the model that was
    uploaded — never invented and never replaced by a YAML file.  Two models
    that share a class name stay separate rows distinguished by model + id.
    """

    RULE_COUNT = "count"
    RULE_PRESENCE = "presence"
    RULE_CONFIDENCE = "confidence"
    RULE_COUNT_CONFIDENCE = "count_confidence"
    RULE_CHOICES = (
        (RULE_COUNT, "Expected detection count"),
        (RULE_PRESENCE, "Required class presence"),
        (RULE_CONFIDENCE, "Confidence threshold"),
        (RULE_COUNT_CONFIDENCE, "Count and confidence"),
    )

    part_model = models.ForeignKey(
        VisionPartModel, on_delete=models.CASCADE, related_name="class_configs"
    )
    class_id = models.IntegerField()
    class_name = models.CharField(max_length=200)
    is_enabled = models.BooleanField(default=True)
    side = models.CharField(
        max_length=10,
        choices=VisionPartConfiguration.SIDE_CHOICES,
        default=VisionPartConfiguration.SIDE_FRONT,
    )
    expected_front = models.PositiveIntegerField(default=0)
    expected_back = models.PositiveIntegerField(default=0)
    front_threshold = models.FloatField(default=0.5)
    back_threshold = models.FloatField(default=0.5)
    priority = models.PositiveIntegerField(default=1)
    is_required = models.BooleanField(default=True)
    acceptance_rule = models.CharField(
        max_length=20, choices=RULE_CHOICES, blank=True, default=""
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_class_configuration"
        ordering = ["part_model", "priority", "class_id"]
        unique_together = (("part_model", "class_id"),)
        verbose_name = "Class Configuration"
        verbose_name_plural = "Class Configurations"
        indexes = [
            models.Index(fields=["part_model", "side"], name="vi_classcfg_side_idx"),
        ]

    def __str__(self):
        return f"{self.class_name} [{self.side}] P{self.priority}"

    def expected_for(self, side):
        return int(self.expected_front if side == "front" else self.expected_back)

    def threshold_for(self, side):
        return float(self.front_threshold if side == "front" else self.back_threshold)

    def applies_to(self, side):
        """True when this class must be evaluated on *side*."""
        if self.side == VisionPartConfiguration.SIDE_BOTH:
            return True
        return self.side.upper() == side.upper()


class VisionPartROI(models.Model):
    """The ONE common ROI of a part configuration.

    A part configuration has a single saved ROI — never one per model, per
    class or per stitch type.  It is reused by every enabled model and for
    both the Front and Back images of that part.  Coordinates are stored in
    the *source frame* pixel system together with the frame size they were
    drawn against, so they can be mapped to any later frame resolution.
    """

    configuration = models.ForeignKey(
        VisionPartConfiguration,
        on_delete=models.CASCADE,
        related_name="rois",
    )
    version = models.PositiveIntegerField(default=1)
    x = models.FloatField(help_text="ROI left edge in source-frame pixels.")
    y = models.FloatField(help_text="ROI top edge in source-frame pixels.")
    width = models.FloatField(help_text="ROI width in source-frame pixels.")
    height = models.FloatField(help_text="ROI height in source-frame pixels.")
    ref_width = models.PositiveIntegerField(help_text="Source frame width at draw time.")
    ref_height = models.PositiveIntegerField(help_text="Source frame height at draw time.")
    is_active = models.BooleanField(default=True)
    configured_by = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vision_part_roi"
        ordering = ["configuration", "-version"]
        unique_together = (("configuration", "version"),)
        verbose_name = "Part ROI"
        verbose_name_plural = "Part ROIs"

    def __str__(self):
        return (
            f"{self.configuration.part.part_number} ROI v{self.version} "
            f"({int(self.width)}x{int(self.height)} @ {self.ref_width}x{self.ref_height})"
        )

    @property
    def normalized(self):
        """Normalised (0..1) rectangle against the stored source frame."""
        rw = float(self.ref_width or 0) or 1.0
        rh = float(self.ref_height or 0) or 1.0
        return {
            "x": max(0.0, min(1.0, self.x / rw)),
            "y": max(0.0, min(1.0, self.y / rh)),
            "width": max(0.0, min(1.0, self.width / rw)),
            "height": max(0.0, min(1.0, self.height / rh)),
        }

    def pixel_rect(self, frame_width, frame_height):
        """ROI rectangle mapped onto a frame of *frame_width* x *frame_height*.

        The mapping scales the stored source-frame rectangle to the live frame
        and clamps it inside the frame so an incompatible numeric rectangle can
        never produce an out-of-bounds crop.  Callers must still verify the
        mapped rectangle is usable (see the configuration compatibility check).
        """
        try:
            fw = float(frame_width or 0)
            fh = float(frame_height or 0)
        except (TypeError, ValueError):
            return None
        if fw <= 0 or fh <= 0:
            return None
        norm = self.normalized
        x = int(round(norm["x"] * fw))
        y = int(round(norm["y"] * fh))
        width = int(round(norm["width"] * fw))
        height = int(round(norm["height"] * fh))
        width = max(1, min(width, int(fw) - max(0, x)))
        height = max(1, min(height, int(fh) - max(0, y)))
        if width <= 1 or height <= 1:
            return None
        return {"x": x, "y": y, "width": width, "height": height}






