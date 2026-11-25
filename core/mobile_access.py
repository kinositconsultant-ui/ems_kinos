"""Server-side gate for the Employee mobile (Android APK) area.

The APK is a WebView host: it relies on the existing Django session, CSRF and
per-view permissions. This module only decides whether the authenticated account
may enter the *mobile-only* Employee area. It never authorizes a client-supplied
employee id, user-agent, or custom header; the Employee is always resolved from
the authenticated account through the authoritative ``UserEmployeeLink``.

Entry requires all of the following:

1. An authenticated, active Django account (handled by ``LoginRequiredMixin``).
2. The existing Employee group role (see ``core.permissions.is_employee_group_user``).
3. A valid authoritative ``UserEmployeeLink`` to an Employee.
4. The existing per-operation permission and object-level scope checks of each
   reused view.

Employee-link-only accounts (a UserEmployeeLink without the Employee group role)
are not Employee-role authorization and are denied.
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from core.permissions import get_linked_employee, is_employee_group_user


def mobile_employee_access_denied_reason(user):
    """Return a human-readable denial reason, or ``None`` when access is allowed."""
    if not getattr(user, "is_authenticated", False):
        return "Authentication is required for the employee mobile area."
    if not getattr(user, "is_active", True):
        return "This account is inactive."
    if not is_employee_group_user(user):
        return (
            "The employee mobile area is restricted to accounts whose role is the "
            "Employee group. Administrative and reviewer roles must use the desktop "
            "application."
        )
    if get_linked_employee(user) is None:
        return (
            "Your account is not linked to an employee profile. Please contact an "
            "administrator."
        )
    return None


def mobile_employee_access_allowed(user):
    return mobile_employee_access_denied_reason(user) is None


def mobile_access_denied_response(request, reason):
    """Prefer the server-controlled login redirect for anonymous sessions."""
    from django.conf import settings

    if not getattr(request.user, "is_authenticated", False):
        return redirect(f"{settings.LOGIN_URL}?next={request.get_full_path()}")
    return None


def mobile_employee_required(view_func):
    """Decorator: require authenticated Employee-role account with a valid link."""

    @login_required
    def _wrapped(request, *args, **kwargs):
        reason = mobile_employee_access_denied_reason(request.user)
        if reason is not None:
            response = mobile_access_denied_response(request, reason)
            if response is not None:
                return response
            raise PermissionDenied(reason)
        return view_func(request, *args, **kwargs)

    return _wrapped