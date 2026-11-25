from django.urls import path

from . import views

app_name = "employee_requests"

urlpatterns = [
    path("requests/mine/", views.MyRequestListView.as_view(), name="request-mine"),
    path("requests/my/", views.MyRequestListView.as_view(), name="request-my"),
    path("requests/pending/", views.PendingRequestListView.as_view(), name="request-pending"),
    path("requests/all/", views.RequestListView.as_view(), name="request-all"),
    path("requests/create/", views.RequestCreateView.as_view(), name="request-create"),
    path("requests/<int:pk>/", views.RequestDetailView.as_view(), name="request-detail"),
    path("requests/<int:pk>/cancel/", views.RequestCancelView.as_view(), name="request-cancel"),
    path("requests/<int:pk>/edit/", views.RequestUpdateView.as_view(), name="request-update"),
    path("requests/approvals/pending/", views.PendingApprovalListView.as_view(), name="approval-pending"),
    path("requests/approvals/", views.ApprovalListView.as_view(), name="approval-history"),
    path(
        "requests/schedule-processing/waiting/",
        views.ScheduleProcessingListView.as_view(processing_state="waiting"),
        name="schedule-processing-waiting",
    ),
    path(
        "requests/schedule-processing/processed/",
        views.ScheduleProcessingListView.as_view(processing_state="processed"),
        name="schedule-processing-processed",
    ),
    path("requests/leave-calendar/", views.LeaveCalendarView.as_view(), name="leave-calendar"),
    path("requests/leave-calendar/events/", views.LeaveCalendarEventsView.as_view(), name="leave-calendar-events"),
    path("requests/leave-calendar/events/<int:pk>/", views.LeaveCalendarEventDetailView.as_view(), name="leave-calendar-event-detail"),
    path("requests/approvals/<int:pk>/", views.ApprovalDetailView.as_view(), name="approval-detail"),
    path(
        "requests/approvals/<int:pk>/forward/",
        views.RequestForwardView.as_view(),
        name="approval-forward",
    ),
    path(
        "requests/approvals/<int:pk>/<str:decision>/",
        views.ApprovalDecisionView.as_view(),
        name="approval-decision",
    ),
    path(
        "requests/<int:pk>/process-preview/",
        views.StaffScheduleProcessPreviewView.as_view(),
        name="request-process-preview",
    ),
    path(
        "requests/<int:pk>/process-result/",
        views.StaffScheduleProcessView.as_view(),
        name="request-process-result",
    ),
    path("requests/leave-types/", views.LeaveTypeListView.as_view(), name="leave-type-list"),
    path("requests/leave-types/create/", views.LeaveTypeCreateView.as_view(), name="leave-type-create"),
    path("requests/leave-types/<int:pk>/", views.LeaveTypeDetailView.as_view(), name="leave-type-detail"),
    path("requests/leave-types/<int:pk>/edit/", views.LeaveTypeUpdateView.as_view(), name="leave-type-update"),
    path("requests/leave-types/<int:pk>/delete/", views.LeaveTypeDeleteView.as_view(), name="leave-type-delete"),
    path("requests/leave-types/<int:pk>/deactivate/", views.LeaveTypeDeactivateView.as_view(), name="leave-type-deactivate"),
    path("notifications/", views.NotificationListView.as_view(), name="notification-list"),
    path("notifications/unread/", views.UnreadNotificationListView.as_view(), name="notification-unread"),
    path("notifications/<int:pk>/", views.NotificationDetailView.as_view(), name="notification-detail"),
    path(
        "notifications/<int:pk>/<str:action>/",
        views.NotificationReadAction.as_view(),
        name="notification-action",
    ),
]
