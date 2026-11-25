from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # Register user.employee_profile as a compat property backed by the
        # authoritative UserEmployeeLink (migration 0006 replaced the legacy
        # Employee.user related_name). Import here so the property is available
        # once models are loaded.
        import accounts.employee_link  # noqa: F401