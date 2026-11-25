from django.urls import path

from . import views

app_name = "employees"

urlpatterns = [
    path("", views.EmployeeListView.as_view(), name="employee-list"),
    path("profile/edit/", views.EmployeeSelfServiceProfileView.as_view(), name="employee-self-service-profile"),
    path("create/", views.EmployeeCreateView.as_view(), name="employee-create"),
    path("<int:pk>/deactivate/", views.EmployeeDeactivateView.as_view(), name="employee-deactivate"),
    path("<int:pk>/", views.EmployeeDetailView.as_view(), name="employee-detail"),
    path("<int:pk>/edit/", views.EmployeeUpdateView.as_view(), name="employee-update"),
    path("certifications/<int:pk>/download/", views.EmployeeCertificationDownloadView.as_view(), name="certification-download"),
    path("assignments/", views.AssignmentListView.as_view(), name="assignment-list"),
    path("assignments/create/", views.AssignmentCreateView.as_view(), name="assignment-create"),
    path("assignments/<int:pk>/", views.AssignmentDetailView.as_view(), name="assignment-detail"),
    path("assignments/<int:pk>/edit/", views.AssignmentUpdateView.as_view(), name="assignment-update"),
]
