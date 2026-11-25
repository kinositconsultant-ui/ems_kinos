from django.apps import AppConfig


class AudittrailConfig(AppConfig):
    name = 'audittrail'

    def ready(self):
        import audittrail.signals  # noqa
