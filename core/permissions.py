from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.db.models import Q

HR_GROUP_NAME = "HR Office"
MINISTRO_GROUP = "Ministro"
VICE_GROUP = "Vice"


def can_manage_groups(user):
    return bool(user.is_authenticated and user.is_active and user.is_superuser)


def is_hr_user(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    try:
        return user.groups.filter(name=HR_GROUP_NAME).exists()
    except Exception:
        return False

def require_office_scoped_admin(user):
    if not user.is_superuser:
        raise PermissionDenied(
            "Office-level access assignments are not configured. "
            "Only a superuser can perform this office-scoped action."
        )


def offices_for_user(user):
    """Offices this user may manage/report schedules for.

    Branch HR/Staff HR: own resolved office only. Executive institution users
    (Ministro/Vice): all active offices within authorized institution scope when
    explicitly configured; fails closed (no offices) if scope is unresolved.
    Other permitted users retain existing behavior; views enforce permissions.
    """
    from organization.models import Office

    if not user.is_authenticated:
        return Office.objects.none()
    if is_executive_institution_user(user):
        scope, _error = resolve_executive_institution_scope(user)
        if scope is None:
            return Office.objects.none()
        return scope["offices_qs"]
    if is_branch_hr_user(user):
        office = resolve_branch_hr_office(user)
        if office is None:
            return Office.objects.none()
        return Office.objects.filter(pk=office.pk)
    return Office.objects.filter(is_active=True).order_by("office_name")
def resolve_hr_office(user):
    if not is_hr_user(user):
        return None

    employee = get_linked_employee(user)
    if employee is None:
        return None

    try:
        from employees.models import EmployeeAssignment
    except Exception:
        return None

    today = timezone.localdate()
    candidates = list(
        EmployeeAssignment.objects.filter(
            employee=employee,
            is_primary=True,
            start_date__lte=today,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .filter(Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today))
        .select_related("position__office")
    )
    if len(candidates) != 1:
        return None

    assignment = candidates[0]
    position = getattr(assignment, "position", None)
    if position is None:
        return None
    office = getattr(position, "office", None)
    if office is None:
        return None
    return office
def hr_office_or_none(user):
    return resolve_hr_office(user)


def require_hr_office(user):
    office = resolve_hr_office(user)
    if office is None:
        raise PermissionDenied("Branch office access could not be resolved.")
    return office
EMPLOYEE_GROUP_NAMES = {"employees", "employess"}

EMPLOYEES_GROUP = "Employess"
XEFE_GROUP = "Xefe_Departamentu"
DIRECTOR_GROUP = "Director"
STAFF_HR_GROUP = "Staff HR"
IT_ADMIN_GROUP = "IT Admin"


def effective_primary_assignments():
    from employees.models import EmployeeAssignment

    today = timezone.localdate()
    return (
        EmployeeAssignment.objects.filter(
            is_primary=True,
            start_date__lte=today,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .filter(Q(employee__contract_end_date__isnull=True) | Q(employee__contract_end_date__gte=today))
    )


def current_primary_assignment(employee):
    """Resolve exactly one currently valid primary assignment, or None."""
    if employee is None:
        return None
    candidates = list(
        effective_primary_assignments().filter(employee=employee)
        .select_related("position__office", "position__department")
    )
    if len(candidates) != 1:
        return None
    if candidates[0].position is None or candidates[0].position.office_id is None:
        return None
    return candidates[0]


def role_assignment_for_user(user):
    if not getattr(user, "is_authenticated", False) or getattr(user, "is_superuser", False):
        return None
    employee = get_linked_employee(user)
    if employee is None:
        return None
    return current_primary_assignment(employee)


def user_has_exact_group(user, group_name):
    return bool(
        getattr(user, "is_authenticated", False)
        and user.groups.filter(name=group_name).exists()
    )


def resolve_director_office(user):
    if not user_has_exact_group(user, DIRECTOR_GROUP):
        return None
    assignment = role_assignment_for_user(user)
    return assignment.position.office if assignment is not None else None


def resolve_xefe_assignment(user):
    if not user_has_exact_group(user, XEFE_GROUP):
        return None
    assignment = role_assignment_for_user(user)
    if assignment is None:
        return None
    position = assignment.position
    if (
        not position.is_management
        or position.department_id is None
        or position.office_id != position.department.office_id
    ):
        return None
    return assignment


def resolve_staff_hr_office(user):
    """Resolve the authorized office for a Staff HR user.

    Returns the Office object from the user's current primary assignment,
    or None if the profile/assignment is missing, invalid, or ambiguous.
    Uses the project's existing date/status rules for current assignments.
    Does not fall back to historical assignments or grant global access.
    """
    if not user_has_exact_group(user, STAFF_HR_GROUP):
        return None
    assignment = role_assignment_for_user(user)
    return assignment.position.office if assignment is not None else None


def _normalized_group_names(user):
    try:
        return {
            (name or "").strip().lower()
            for name in user.groups.values_list("name", flat=True)
        }
    except Exception:
        return set()


def is_employee_group_user(user):
    """True for users whose only role is the existing Employee group.

    Higher-privilege users (superusers, HR Office, staff, or users with any
    additional group) keep their existing dashboard behaviour.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    if getattr(user, "is_staff", False):
        return False
    if is_hr_user(user):
        return False
    names = _normalized_group_names(user)
    employee_groups = names & EMPLOYEE_GROUP_NAMES
    if not employee_groups:
        return False
    if names - employee_groups:
        return False
    return True


def get_linked_employee(user):
    """Compatibility accessor: returns the Employee linked to this User, or None.
    Uses UserEmployeeLink (supports one employee with multiple user accounts)."""
    try:
        from employees.models import get_linked_employee as _get
        return _get(user)
    except Exception:
        return getattr(user, "employee_profile", None)


def resolve_employee_assignment(user):
    """Return the authenticated Employee's one active primary assignment."""
    if not is_employee_group_user(user):
        return None

    employee = get_linked_employee(user)
    if employee is None:
        return None

    try:
        from employees.models import EmployeeAssignment
    except (ImportError, RuntimeError):
        return None

    today = timezone.localdate()
    candidates = list(
        EmployeeAssignment.objects.filter(
            employee=employee,
            is_primary=True,
            start_date__lte=today,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .select_related("position__office")
    )
    if len(candidates) != 1:
        return None

    assignment = candidates[0]
    if getattr(assignment, "position", None) is None:
        return None
    if getattr(assignment.position, "office", None) is None:
        return None
    return assignment


def resolve_employee_office(user):
    assignment = resolve_employee_assignment(user)
    return assignment.position.office if assignment is not None else None


def _is_role_user_excluding_admin(user, group_name):
    return bool(
        getattr(user, "is_authenticated", False)
        and not getattr(user, "is_superuser", False)
        and not is_it_admin_user(user)
        and user_has_exact_group(user, group_name)
    )


def is_director_user(user):
    """True for Director group members who are not superusers or IT Admins.

    IT Admin and superuser accounts retain administrative filters and
    unrestricted data scope even when they also belong to the Director group.
    """
    return _is_role_user_excluding_admin(user, DIRECTOR_GROUP)


def is_xefe_user(user):
    """True for Xefe_Departamentu group members who are not superusers or IT Admins.

    IT Admin and superuser accounts retain administrative filters and
    unrestricted data scope even when they also belong to the Xefe group.
    """
    return _is_role_user_excluding_admin(user, XEFE_GROUP)


def is_staff_hr_user(user):
    return bool(getattr(user, "is_authenticated", False)
                and not getattr(user, "is_superuser", False)
                and user_has_exact_group(user, STAFF_HR_GROUP))


def is_it_admin_user(user):
    """True for members of the existing "IT Admin" group (administrative data scope).

    The IT Admin group is authorized to see all system data across all branches
    and departments, equivalent to superuser for DATA SCOPE. It does not require
    a linked Employee, branch assignment, or institution-scope resolver. is_staff
    alone is not treated as unrestricted access. Superusers are handled separately
    by callers; this predicate targets the configured IT Admin group only and does
    not bypass Django feature/model action permissions.
    """
    return bool(getattr(user, "is_authenticated", False)
                and user_has_exact_group(user, IT_ADMIN_GROUP))


def is_branch_hr_user(user):
    return is_staff_hr_user(user) or is_hr_user(user)


def is_executive_institution_user(user):
    """True for Ministro/Vice accounts (read-only institution-wide reporting scope).

    Group names are verified against configured auth groups in this deployment.
    Executive scope does not grant mutation permissions; each feature still checks
    effective Django permissions.
    """
    if not getattr(user, "is_authenticated", False) or getattr(user, "is_superuser", False):
        return False
    return user_has_exact_group(user, MINISTRO_GROUP) or user_has_exact_group(user, VICE_GROUP)


def resolve_executive_institution_scope(user):
    """Resolve Ministro/Vice authorized institution and office scope.

    Returns (scope_dict, error_message). scope_dict contains institution and an
    Office queryset limited to that institution; it never returns unrestricted data.

    Policy:
    - In this deployment, InstitutionProfile is an explicit singleton (pk=1) and
      Office has no institution FK. A verified single-institution deployment is
      recognized only when the singleton InstitutionProfile exists AND the explicit
      setting ``EXECUTIVE_SINGLE_INSTITUTION_SCOPE`` is truthy. All active offices
      then belong to that authorized institution for executive reporting only.
    - If an executive account is linked to an Employee with a resolvable current
      Office, that Office is used only to confirm the singleton institution context
      in a single-institution deployment; it never narrows executive scope to that
      office/department.
    - If no authoritative explicit mapping/configuration exists, fail closed.
    """
    if not is_executive_institution_user(user):
        return None, None
    from django.conf import settings as django_settings
    from organization.models import InstitutionProfile, Office

    # Explicit single-institution deployment configuration.
    if getattr(django_settings, "EXECUTIVE_SINGLE_INSTITUTION_SCOPE", False):
        institution = InstitutionProfile.objects.filter(pk=1).first()
        if institution is None:
            return None, (
                "Executive institution scope is enabled for a single-institution deployment, "
                "but no InstitutionProfile (singleton) exists. Configure the Institution Profile first."
            )
        offices = Office.objects.filter(is_active=True).order_by("office_name")
        return {
            "kind": "institution",
            "institution": institution,
            "institution_id": institution.pk,
            "offices_qs": offices,
            "office_ids": None,  # None means all offices in authorized institution; filters may narrow.
        }, None

    # Future/multi-institution path: require explicit verified mapping.
    # No User/Employee->Institution authorization model exists in current schema.
    employee = get_linked_employee(user)
    if employee is not None:
        assignment = current_primary_assignment(employee)
        if assignment is not None and getattr(assignment.position, "office_id", None):
            # Office exists but no authoritative institution mapping field/model is available.
            return None, (
                "Ministro/Vice institution scope requires explicit configuration. "
                "Set EXECUTIVE_SINGLE_INSTITUTION_SCOPE=True in settings for this verified "
                "single-institution deployment (all offices belong to InstitutionProfile singleton), "
                "or implement an explicit account/office-to-institution authorization mapping before "
                "enabling executive scope in a multi-institution deployment."
            )

    return None, (
        "Ministro/Vice institution scope is not configured. "
        "For a verified single-institution deployment, set EXECUTIVE_SINGLE_INSTITUTION_SCOPE=True "
        "in settings and ensure InstitutionProfile exists. For multi-institution deployments, "
        "add an explicit administrator-configured account-to-institution authorization mapping; "
        "do not use default/first/arbitrary institutions."
    )


def resolve_branch_hr_office(user):
    # Staff HR scope takes precedence if both groups are assigned.
    return resolve_staff_hr_office(user) if is_staff_hr_user(user) else resolve_hr_office(user)


def staff_hr_employee_ids(user):
    from employees.models import EmployeeAssignment
    office = resolve_staff_hr_office(user)
    today = timezone.localdate()
    return EmployeeAssignment.objects.filter(
        position__office=office, is_primary=True, start_date__lte=today,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).values("employee_id") if office else EmployeeAssignment.objects.none().values("employee_id")
