from django.contrib import admin
from .models import LeaveType, EmployeeRequest, RequestApproval, Notification


@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "basis", "paid_status", "unit", "requires_approval", "is_active")
    list_filter = ("category", "basis", "paid_status", "unit", "requires_approval", "requires_attachment", "tracks_balance", "is_active")
    search_fields = ("code", "name", "description")
    fieldsets = (
        (None, {"fields": ("code", "name", "description", "category", "is_active")}),
        ("Configuration", {"fields": ("basis", "paid_status", "unit", "requires_approval", "requires_attachment", "attachment_instructions", "tracks_balance", "max_days_per_year")}),
    )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeRequest)
class EmployeeRequestAdmin(admin.ModelAdmin):
    list_display = ("request_number", "employee_assignment", "request_type", "status", "submitted_at", "processed_by")
    list_filter = ("request_type", "status", "submitted_at")
    search_fields = ("request_number", "employee_assignment__employee__employee_id", "employee_assignment__employee__full_name")
    readonly_fields = ("submitted_at", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RequestApproval)
class RequestApprovalAdmin(admin.ModelAdmin):
    list_display = ("request", "approver", "step_number", "decision", "assigned_at", "decided_at")
    list_filter = ("decision", "step_number")
    search_fields = ("request__request_number", "approver__username")
    readonly_fields = ("assigned_at",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient_user", "notification_type", "title", "channel", "delivery_status", "created_at", "read_at")
    list_filter = ("notification_type", "channel", "delivery_status", "created_at")
    search_fields = ("recipient_user__username", "title", "message")
    readonly_fields = ("created_at", "sent_at")

    def has_delete_permission(self, request, obj=None):
        return False
