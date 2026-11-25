from django.contrib import admin
from .models import (
    Shift, EmployeeSchedule, AttendanceDevice, FaceTemplate,
    FaceVerificationLog, AttendancePolicy, AttendanceRecord,
    AttendanceEvent, Holiday,
)


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("shift_code", "name", "office", "start_time", "end_time", "is_active")
    list_filter = ("office", "is_active", "crosses_midnight")
    search_fields = ("shift_code", "name", "office__office_name")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeSchedule)
class EmployeeScheduleAdmin(admin.ModelAdmin):
    list_display = ("work_date", "employee_assignment", "shift", "scheduled_start_at", "scheduled_end_at", "status")
    list_filter = ("work_date", "status", "shift__office")
    search_fields = ("employee_assignment__employee__employee_id", "employee_assignment__employee__full_name")
    autocomplete_fields = ("employee_assignment", "shift", "assigned_by")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceDevice)
class AttendanceDeviceAdmin(admin.ModelAdmin):
    list_display = ("device_code", "device_name", "office", "device_type", "status", "last_seen_at")
    list_filter = ("office", "device_type", "status")
    search_fields = ("device_code", "device_name", "office__office_name")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FaceTemplate)
class FaceTemplateAdmin(admin.ModelAdmin):
    list_display = ("employee", "provider", "model_version", "status", "enrolled_at", "retention_until")
    list_filter = ("provider", "status", "model_version")
    search_fields = ("employee__employee_id", "employee__full_name")
    exclude = ("encrypted_template",)

    def has_add_permission(self, request):
        # Biometric enrollment should use a dedicated, permission-checked workflow.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FaceVerificationLog)
class FaceVerificationLogAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "employee", "device", "event_type", "match_result", "liveness_result", "liveness_score")
    list_filter = ("event_type", "match_result", "liveness_result", "device__office")
    search_fields = ("employee__employee_id", "device__device_code", "failure_reason")
    readonly_fields = tuple(field.name for field in FaceVerificationLog._meta.fields)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendancePolicy)
class AttendancePolicyAdmin(admin.ModelAdmin):
    list_display = ("policy_name", "office", "effective_from", "effective_until", "require_liveness", "is_active")
    list_filter = ("office", "require_liveness", "is_active")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("schedule", "status", "first_check_in_at", "last_check_out_at", "late_minutes", "review_status")
    list_filter = ("status", "review_status", "schedule__shift__office", "schedule__work_date")
    search_fields = ("schedule__employee_assignment__employee__employee_id", "schedule__employee_assignment__employee__full_name")
    readonly_fields = tuple(field.name for field in AttendanceRecord._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceEvent)
class AttendanceEventAdmin(admin.ModelAdmin):
    list_display = ("attendance_record", "event_type", "occurred_at", "source", "created_by_user")
    list_filter = ("event_type", "source", "occurred_at")
    readonly_fields = tuple(field.name for field in AttendanceEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("name", "holiday_type", "office", "start_date", "end_date", "is_active")
    list_filter = ("holiday_type", "office", "is_active")
    search_fields = ("name",)

    def has_delete_permission(self, request, obj=None):
        return False
