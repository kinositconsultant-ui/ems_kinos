from core.permissions import (
    DIRECTOR_GROUP,
    STAFF_HR_GROUP,
    XEFE_GROUP,
)


def attendance_menu_permissions(request):
    user = getattr(request, "user", None)
    return {
        "attendance_kiosk_allowed": bool(getattr(user, "is_superuser", False)),
        "director_my_requests_hidden": bool(
            getattr(user, "is_authenticated", False)
            and not getattr(user, "is_superuser", False)
            and (user.groups.filter(name__in=[DIRECTOR_GROUP, XEFE_GROUP]).exists()
                 or (user.groups.filter(name=STAFF_HR_GROUP).exists()
                     and not user.groups.filter(name="Employess").exists()))
        ),
    }


def mobile_employee_area(request):
    """Flag set by the mobile views so the desktop sidebar is suppressed.

    Read by the shared templates (master.html, sidebar.html, navbar.html) via
    this context processor. It ensures the Employee mobile area never inherits
    administrative menus from another group membership, regardless of which
    reused view renders the page.
    """
    return {
        "mobile_employee_area": bool(getattr(request, "_mobile_employee_area", False)),
    }