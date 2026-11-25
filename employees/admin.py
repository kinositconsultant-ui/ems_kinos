from django.contrib import admin
from django.contrib import messages

from .models import Employee, EmployeeAssignment, UserEmployeeLink
from .services.contract_scheduling import snapshot_employee_state, synchronize_employee


class UserEmployeeLinkInline(admin.TabularInline):
    model = UserEmployeeLink
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "is_primary", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "full_name", "employment_type", "employment_status", "linked_accounts_count")
    list_filter = ("employment_type", "employment_status")
    search_fields = ("employee_id", "contract_number", "full_name", "work_email")
    inlines = [UserEmployeeLinkInline]
    exclude = ("user",)

    @admin.display(description="Linked accounts")
    def linked_accounts_count(self, obj):
        try:
            return obj.user_account_links.count()
        except Exception:
            return 0

    def save_model(self, request, obj, form, change):
        previous = snapshot_employee_state(Employee.objects.get(pk=obj.pk)) if change else None
        super().save_model(request, obj, form, change)
        try:
            result = synchronize_employee(obj, actor_user=request.user, previous=previous)
        except Exception:
            self.message_user(request, "Automatic contract scheduling failed; see the server log.", level=messages.ERROR)
            return
        if result is None:
            return
        if result.pending_reasons:
            self.message_user(request, f"Automatic scheduling pending: {result.pending_reason}", level=messages.WARNING)
        if result.created_count:
            self.message_user(request, f"Automatic scheduling created {result.created_count} schedule(s).", level=messages.SUCCESS)
        if result.cancelled_count:
            self.message_user(request, f"Automatic scheduling cancelled {result.cancelled_count} future schedule(s).")
        for blocker in result.blocking_conflicts:
            self.message_user(request, blocker, level=messages.ERROR)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeAssignment)
class EmployeeAssignmentAdmin(admin.ModelAdmin):
    list_display = ("employee", "position", "assignment_type", "start_date", "end_date", "is_primary")
    list_filter = ("assignment_type", "is_primary", "position__office")
    search_fields = ("employee__employee_id", "employee__full_name", "position__position_title", "position__office__office_name")
    autocomplete_fields = ("employee", "position", "assigned_by")

    def save_model(self, request, obj, form, change):
        previous = snapshot_employee_state(obj.employee) if change else None
        super().save_model(request, obj, form, change)
        try:
            result = synchronize_employee(obj.employee, actor_user=request.user, previous=previous)
        except Exception:
            self.message_user(request, "Automatic contract scheduling failed; see the server log.", level=messages.ERROR)
            return
        if result is None:
            return
        if result.pending_reasons:
            self.message_user(request, f"Automatic scheduling pending: {result.pending_reason}", level=messages.WARNING)
        if result.created_count:
            self.message_user(request, f"Automatic scheduling created {result.created_count} schedule(s).", level=messages.SUCCESS)
        for blocker in result.blocking_conflicts:
            self.message_user(request, blocker, level=messages.ERROR)

    def has_delete_permission(self, request, obj=None):
        return False
