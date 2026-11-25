"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core.views import dashboard
from core.views import (
    mobile_attendance,
    mobile_home,
    mobile_logout,
    mobile_notifications,
    mobile_profile,
    mobile_requests,
)

urlpatterns = [
    path('', dashboard, name='dashboard'),
    # Employee mobile (Android APK) area. Employee-role gated; reuses the
    # existing session auth, permissions and object-level scope of each view.
    path('m/', mobile_home, name='mobile-home'),
    path('m/attendance/', mobile_attendance, name='mobile-attendance'),
    path('m/requests/', mobile_requests, name='mobile-requests'),
    path('m/notifications/', mobile_notifications, name='mobile-notifications'),
    path('m/profile/', mobile_profile, name='mobile-profile'),
    path('m/logout/', mobile_logout, name='mobile-logout'),
    path('', include('accounts.urls')),
    path('', include('organization.urls')),
    path('employees/', include('employees.urls')),
    path('', include('attendance.urls')),
    path('', include('employee_requests.urls')),
    path('audit/', include('audittrail.urls')),
    path('admin/', admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
