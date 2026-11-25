from django.contrib import admin
from .models import InstitutionProfile, Office, Department, Position


@admin.register(InstitutionProfile)
class InstitutionProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "institution_code", "institution_type", "timezone", "updated_at")

    def has_add_permission(self, request):
        return not InstitutionProfile.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Office)
class OfficeAdmin(admin.ModelAdmin):
    list_display = ("office_code", "office_name", "office_type", "municipality", "parent_office", "is_active")
    list_filter = ("office_type", "municipality", "is_active")
    search_fields = ("office_code", "office_name", "municipality", "administrative_post", "suco")
    autocomplete_fields = ("parent_office",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("department_code", "department_name", "office", "parent_department", "is_active")
    list_filter = ("office", "is_active")
    search_fields = ("department_code", "department_name", "office__office_name")
    autocomplete_fields = ("office", "parent_department")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("position_code", "position_title", "office", "department", "reports_to_position", "is_active")
    list_filter = ("office", "is_management", "is_active")
    search_fields = ("position_code", "position_title", "office__office_name", "department__department_name")
    autocomplete_fields = ("office", "department", "reports_to_position")

    def has_delete_permission(self, request, obj=None):
        return False
