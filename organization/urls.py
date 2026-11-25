from django.urls import path

from . import views

app_name = "organization"

urlpatterns = [
    path("institution/", views.institution_profile, name="institution-detail"),
    path("institution/edit/", views.institution_profile_edit, name="institution-update"),
    path("offices/", views.OfficeListView.as_view(), name="office-list"),
    path("offices/create/", views.OfficeCreateView.as_view(), name="office-create"),
    path("offices/<int:pk>/", views.OfficeDetailView.as_view(), name="office-detail"),
    path("offices/<int:pk>/edit/", views.OfficeUpdateView.as_view(), name="office-update"),
    path("offices/<int:pk>/delete/", views.OfficeDeleteView.as_view(), name="office-delete"),
    path("offices/<int:pk>/deactivate/", views.OfficeDeactivateView.as_view(), name="office-deactivate"),
    path("departments/", views.DepartmentListView.as_view(), name="department-list"),
    path("departments/create/", views.DepartmentCreateView.as_view(), name="department-create"),
    path("departments/<int:pk>/", views.DepartmentDetailView.as_view(), name="department-detail"),
    path("departments/<int:pk>/edit/", views.DepartmentUpdateView.as_view(), name="department-update"),
    path("departments/<int:pk>/delete/", views.DepartmentDeleteView.as_view(), name="department-delete"),
    path("departments/<int:pk>/deactivate/", views.DepartmentDeactivateView.as_view(), name="department-deactivate"),
    path("positions/", views.PositionListView.as_view(), name="position-list"),
    path("positions/create/", views.PositionCreateView.as_view(), name="position-create"),
    path("positions/<int:pk>/", views.PositionDetailView.as_view(), name="position-detail"),
    path("positions/<int:pk>/edit/", views.PositionUpdateView.as_view(), name="position-update"),
    path("positions/<int:pk>/delete/", views.PositionDeleteView.as_view(), name="position-delete"),
    path("positions/<int:pk>/deactivate/", views.PositionDeactivateView.as_view(), name="position-deactivate"),
]
