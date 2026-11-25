from django.urls import path

from . import views

app_name = "audittrail"

urlpatterns = [
    path("", views.AuditLogListView.as_view(), name="audit-list"),
]
