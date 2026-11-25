"""Scoped dashboard data preparation for non-employee dashboard roles.

Every widget is independently authorized by effective Django model permissions.
Organizational scope is resolved with existing helpers in ``core.permissions``
and applied before filtering/aggregation, so unauthorized or out-of-scope data
is never computed, serialized, or sent to the browser.

Dashboard uses concise Daily/Monthly/Yearly reporting controls (server-derived
start/end). Existing report pages keep their own From/To controls.
"""

import calendar
from datetime import date

from django.conf import settings
from django.db.models import Count, Q
from django.http import QueryDict
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

from attendance.models import AttendanceRecord
from employee_requests.models import EmployeeRequest
from employees.models import Employee

from core.permissions import (
    DIRECTOR_GROUP,
    STAFF_HR_GROUP,
    XEFE_GROUP,
    is_branch_hr_user,
    is_executive_institution_user,
    is_it_admin_user,
    resolve_branch_hr_office,
    resolve_director_office,
    resolve_executive_institution_scope,
    resolve_staff_hr_office,
    resolve_xefe_assignment,
    user_has_exact_group,
)

REQUEST_LEAVE = EmployeeRequest.RequestType.LEAVE
REQUEST_APPROVED = EmployeeRequest.Status.APPROVED
RECENT_LIMIT = 10

STATUS_META = [
    (AttendanceRecord.Status.PRESENT, "Present", "#198754"),
    (AttendanceRecord.Status.LATE, "Late", "#d39e00"),
    (AttendanceRecord.Status.INCOMPLETE, "Incomplete", "#fd7e14"),
    (AttendanceRecord.Status.ABSENT, "Absent", "#dc3545"),
    (AttendanceRecord.Status.ON_LEAVE, "On approved leave", "#0dcaf0"),
    (AttendanceRecord.Status.HOLIDAY, "Holiday", "#6f42c1"),
    (AttendanceRecord.Status.REST_DAY, "Rest day", "#6c757d"),
    (AttendanceRecord.Status.PENDING_REVIEW, "Pending review", "#0d6efd"),
    (AttendanceRecord.Status.SCHEDULED, "Scheduled", "#adb5bd"),
]
STATUS_COLORS = {k: c for k, _l, c in STATUS_META}
STATUS_LABELS = {k: l for k, l, _c in STATUS_META}
CHART_STATUS_KEYS = [k for k, _l, _c in STATUS_META]


def _unavailable(role_label, reason, **extra):
    base = {
        "role": None, "office": None, "department": None, "institution": None,
        "scope_kind": "unavailable", "scope_label": role_label or "Account",
        "scope_available": False, "scope_unavailable_reason": reason,
    }
    base.update(extra)
    return base


def resolve_scope(user):
    if getattr(user, "is_superuser", False):
        return {
            "role": "superuser", "office": None, "department": None, "institution": None,
            "scope_kind": "superuser", "scope_label": "Institution-wide (superuser)",
            "scope_available": True, "scope_unavailable_reason": None,
        }
    if is_executive_institution_user(user):
        scope, error = resolve_executive_institution_scope(user)
        if scope is None:
            return _unavailable("Ministro/Vice", error or "Ministro/Vice institution scope is not configured.")
        institution = scope["institution"]
        return {
            "role": "executive", "office": None, "department": None, "institution": institution,
            "institution_offices_qs": scope["offices_qs"], "scope_kind": "institution",
            "scope_label": f"Institution: {getattr(institution, 'institution_name', '')} (all authorized branches)",
            "scope_available": True, "scope_unavailable_reason": None,
        }
    if user_has_exact_group(user, STAFF_HR_GROUP):
        office = resolve_staff_hr_office(user)
        if office is None:
            return _unavailable("Staff HR", "Your Staff HR branch scope could not be resolved. Ask an administrator to link your position to a branch office.")
        return {"role": "staff_hr", "office": office, "department": None, "institution": None, "scope_kind": "branch", "scope_label": f"Branch: {office.office_name}", "scope_available": True, "scope_unavailable_reason": None}
    if is_branch_hr_user(user):
        office = resolve_branch_hr_office(user)
        if office is None:
            return _unavailable("HR Office", "Your HR Office branch scope could not be resolved. Ask an administrator to configure your branch assignment.")
        return {"role": "branch_hr", "office": office, "department": None, "institution": None, "scope_kind": "branch", "scope_label": f"Branch: {office.office_name}", "scope_available": True, "scope_unavailable_reason": None}
    if user_has_exact_group(user, DIRECTOR_GROUP):
        office = resolve_director_office(user)
        if office is None:
            return _unavailable("Director", "Your Director branch scope could not be resolved. Ask an administrator to link your position to a branch office.")
        return {"role": "director", "office": office, "department": None, "institution": None, "scope_kind": "branch", "scope_label": f"Branch: {office.office_name}", "scope_available": True, "scope_unavailable_reason": None}
    if user_has_exact_group(user, XEFE_GROUP):
        assignment = resolve_xefe_assignment(user)
        if assignment is None:
            return _unavailable("Xefe_Departamentu", "Your department leadership assignment could not be resolved. Ask an administrator to configure your management position.")
        office = assignment.position.office
        department = assignment.position.department
        if department is None or getattr(department, "office_id", None) != getattr(office, "pk", None):
            return _unavailable("Xefe_Departamentu", "Your position is not linked to a department in your authorized branch office.")
        return {"role": "xefe", "office": office, "department": department, "institution": None, "scope_kind": "department", "scope_label": f"Branch: {office.office_name} · Department: {department.department_name}", "scope_available": True, "scope_unavailable_reason": None}
    return _unavailable("Account", "No verified organizational scope is configured for this account. Ask an administrator to configure an authorized branch/department or institution scope.")


def _active_assignment_q(today):
    return (
        Q(assignments__is_primary=True, assignments__start_date__lte=today)
        & (Q(assignments__end_date__isnull=True) | Q(assignments__end_date__gte=today))
        & (Q(assignments__employee__contract_end_date__isnull=True) | Q(assignments__employee__contract_end_date__gte=today))
    )


def _scope_employee_qs(queryset, scope, today, *, office_id=None, department_id=None):
    queryset = queryset.filter(_active_assignment_q(today))
    if scope["scope_kind"] == "institution":
        queryset = queryset.filter(assignments__position__office__in=scope["institution_offices_qs"])
        if office_id:
            queryset = queryset.filter(assignments__position__office_id=office_id)
    elif scope.get("office") is not None:
        queryset = queryset.filter(assignments__position__office=scope["office"])
    if scope.get("department") is not None:
        queryset = queryset.filter(assignments__position__department=scope["department"])
    elif department_id:
        queryset = queryset.filter(assignments__position__department_id=department_id)
    return queryset.distinct()


def _scope_attendance_qs(queryset, scope, *, office_id=None, department_id=None):
    if scope["scope_kind"] == "institution":
        queryset = queryset.filter(schedule__shift__office__in=scope["institution_offices_qs"])
        if office_id:
            queryset = queryset.filter(schedule__shift__office_id=office_id)
    elif scope.get("office") is not None:
        office = scope["office"]
        queryset = queryset.filter(schedule__employee_assignment__position__office=office, schedule__shift__office=office)
    if scope.get("department") is not None:
        queryset = queryset.filter(schedule__employee_assignment__position__department=scope["department"])
    elif department_id:
        queryset = queryset.filter(schedule__employee_assignment__position__department_id=department_id)
    return queryset


def _scope_request_qs(queryset, scope, *, office_id=None, department_id=None):
    if scope["scope_kind"] == "institution":
        queryset = queryset.filter(branch__in=scope["institution_offices_qs"])
        if office_id:
            queryset = queryset.filter(branch_id=office_id)
    elif scope.get("office") is not None and scope.get("department") is not None:
        queryset = queryset.filter(employee_assignment__position__office=scope["office"], employee_assignment__position__department=scope["department"])
    elif scope.get("office") is not None:
        queryset = queryset.filter(branch=scope["office"])
    if department_id and scope.get("department") is None:
        queryset = queryset.filter(employee_assignment__position__department_id=department_id)
    return queryset


def _parse_int(value, default=None, min_value=None, max_value=None):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if min_value is not None and n < min_value:
        return default
    if max_value is not None and n > max_value:
        return default
    return n


def _parse_period_mode(request, today):
    mode = (request.GET.get("period") or "monthly").strip().lower()
    if mode not in ("daily", "monthly", "yearly"):
        mode = "monthly"
    cy = today.year
    if mode == "daily":
        try:
            d = date.fromisoformat((request.GET.get("date") or today.isoformat()).strip())
        except ValueError:
            d = today
        return "daily", d, d, d.strftime("%d %b %Y"), {"period": "daily", "date": d.isoformat(), "month": d.month, "year": d.year}
    if mode == "yearly":
        year = _parse_int(request.GET.get("year"), cy, 2000, 2100)
        return "yearly", date(year, 1, 1), date(year, 12, 31), str(year), {"period": "yearly", "date": today.isoformat(), "month": 1, "year": year}
    year = _parse_int(request.GET.get("year"), cy, 2000, 2100)
    month = _parse_int(request.GET.get("month"), today.month, 1, 12)
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return "monthly", start, end, start.strftime("%b %Y"), {"period": "monthly", "date": today.isoformat(), "month": month, "year": year}


def _allowed_filters(scope):
    kind = scope["scope_kind"]
    role = scope.get("role")
    if kind == "institution" or role == "superuser":
        return {"office": True, "department": True}
    if kind == "branch":
        return {"office": False, "department": True}
    return {"office": False, "department": False}


def _parse_scope_filters(request, scope):
    selected_office_id = None
    selected_department_id = None
    allowed = _allowed_filters(scope)
    if scope["scope_kind"] == "institution":
        oid = _parse_int(request.GET.get("office"))
        if oid and scope["institution_offices_qs"].filter(pk=oid).exists():
            selected_office_id = oid
    if allowed["department"]:
        did = _parse_int(request.GET.get("department"))
        if did:
            from organization.models import Department
            q = Department.objects.filter(pk=did, is_active=True)
            if scope["scope_kind"] == "institution":
                q = q.filter(office__in=scope["institution_offices_qs"])
            elif scope.get("office") is not None:
                q = q.filter(office=scope["office"])
            if selected_office_id:
                q = q.filter(office_id=selected_office_id)
            if q.exists():
                selected_department_id = did
    return selected_office_id, selected_department_id, allowed


def _filter_choices(scope, selected_office_id):
    from organization.models import Department
    choices = {"offices": [], "departments": []}
    if scope["scope_kind"] == "institution":
        choices["offices"] = list(scope["institution_offices_qs"].values("id", "office_name"))
        dq = Department.objects.filter(office__in=scope["institution_offices_qs"], is_active=True)
        if selected_office_id:
            dq = dq.filter(office_id=selected_office_id)
        choices["departments"] = list(dq.select_related("office").order_by("office__office_name", "department_name").values("id", "department_name", "office__office_name"))
    elif scope.get("office") is not None and scope.get("department") is None:
        dq = Department.objects.filter(office=scope["office"], is_active=True)
        choices["departments"] = list(dq.order_by("department_name").values("id", "department_name"))
    return choices


def _report_params(start, end, office_id=None, department_id=None):
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    if office_id:
        params["office"] = office_id
    if department_id:
        params["department"] = department_id
    return params


def _safe_url(name, params=None):
    try:
        url = reverse(name)
    except NoReverseMatch:
        return None
    if params:
        q = QueryDict(mutable=True)
        for key, value in params.items():
            if value not in (None, ""):
                q[key] = value
        encoded = q.urlencode()
        if encoded:
            return f"{url}?{encoded}"
    return url


def _kpi(title, value, *, unit, icon, color, authorized=True, note=None, url=None):
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = None
    return {"title": title, "value": value, "unit": unit, "icon": icon, "color": color, "authorized": authorized, "zero": numeric == 0, "note": note, "url": url}


def _svg_text(value):
    return escape(str(value))


def _simple_table(headers, rows, caption=""):
    th = "".join(f'<th scope="col">{_svg_text(h)}</th>' for h in headers)
    body = ["<tr>" + "".join(f"<td>{_svg_text(cell)}</td>" for cell in row) + "</tr>" for row in rows]
    cap = f'<caption class="visually-hidden">{_svg_text(caption)}</caption>' if caption else ""
    return mark_safe(f'<table class="nd-chart-table">{cap}<thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')


def _status_rows_from_counts(counts):
    rows, total = [], 0
    for key, label, _color in STATUS_META:
        n = int(counts.get(key) or 0)
        if n:
            rows.append((key, label, n))
            total += n
    return rows, total


def _distribution_svg(rows, total, title, desc):
    if total <= 0:
        return None
    import math
    size, cx, cy, r_out, r_in = 300, 150, 150, 92, 58
    parts = [f'<svg role="img" aria-labelledby="nd-dist-title nd-dist-desc" viewBox="0 0 {size} {size}" width="100%" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" class="nd-svg-chart">', f'<title id="nd-dist-title">{_svg_text(title)}</title><desc id="nd-dist-desc">{_svg_text(desc)}</desc><rect x="0" y="0" width="{size}" height="{size}" fill="#ffffff" rx="10"/>']
    start = -math.pi / 2
    for key, label, value in rows:
        frac = value / total
        if frac <= 0:
            continue
        end = start + frac * math.tau
        large = 1 if frac > 0.5 else 0
        x1o, y1o = cx + r_out * math.cos(start), cy + r_out * math.sin(start)
        x2o, y2o = cx + r_out * math.cos(end), cy + r_out * math.sin(end)
        x1i, y1i = cx + r_in * math.cos(end), cy + r_in * math.sin(end)
        x2i, y2i = cx + r_in * math.cos(start), cy + r_in * math.sin(start)
        if frac >= 0.999999:
            mid = start + math.pi
            xm_o, ym_o = cx + r_out * math.cos(mid), cy + r_out * math.sin(mid)
            xm_i, ym_i = cx + r_in * math.cos(mid), cy + r_in * math.sin(mid)
            d = f"M {x1o:.2f} {y1o:.2f} A {r_out} {r_out} 0 1 1 {xm_o:.2f} {ym_o:.2f} A {r_out} {r_out} 0 1 1 {x1o:.2f} {y1o:.2f} L {x2i:.2f} {y2i:.2f} A {r_in} {r_in} 0 1 0 {xm_i:.2f} {ym_i:.2f} A {r_in} {r_in} 0 1 0 {x2i:.2f} {y2i:.2f} Z"
        else:
            d = f"M {x1o:.2f} {y1o:.2f} A {r_out} {r_out} 0 {large} 1 {x2o:.2f} {y2o:.2f} L {x1i:.2f} {y1i:.2f} A {r_in} {r_in} 0 {large} 0 {x2i:.2f} {y2i:.2f} Z"
        parts.append(f'<path d="{d}" fill="{STATUS_COLORS.get(key, "#6c757d")}"><title>{_svg_text(label)}: {value} records ({(value/total*100):.1f}%)</title></path>')
        start = end
    parts.append(f'<text x="{cx}" y="{cy-4}" text-anchor="middle" font-size="20" font-weight="700" fill="#212529">{total}</text><text x="{cx}" y="{cy+16}" text-anchor="middle" font-size="12" fill="#6c757d">records</text></svg>')
    return mark_safe(''.join(parts))


def _distribution_widget(rows, total, title, subtitle):
    table_rows = [[label, value, f"{(value/total*100):.1f}%" if total else "0.0%"] for _k, label, value in rows]
    table_rows.append(["Total", total, "100.0%" if total else "0.0%"])
    return {"title": title, "subtitle": subtitle, "svg": _distribution_svg(rows, total, title, subtitle), "table": _simple_table(["Status", "Records", "Share"], table_rows, caption=title), "authorized": True, "empty": total <= 0}


def _trend_widget(rec_qs, mode, start, end, period_label):
    if mode == "daily":
        buckets = [(start.strftime("%d %b"), start, end)]
        granularity = "daily (single day)"
        keys = CHART_STATUS_KEYS
    elif mode == "monthly":
        buckets = []
        d = start
        while d <= end:
            buckets.append((d.strftime("%d %b"), d, d))
            d = date.fromordinal(d.toordinal() + 1)
        granularity = "daily"
        keys = [AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE, AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.INCOMPLETE]
    else:
        buckets = [(date(start.year, m, 1).strftime("%b"), date(start.year, m, 1), date(start.year, m, calendar.monthrange(start.year, m)[1])) for m in range(1, 13)]
        granularity = "monthly"
        keys = [AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE, AttendanceRecord.Status.ABSENT, AttendanceRecord.Status.INCOMPLETE]
    series = {k: [0] * len(buckets) for k in keys}
    for row in rec_qs.values("schedule__work_date", "status").annotate(n=Count("pk")):
        status = row["status"]
        if status not in series:
            continue
        wd = row["schedule__work_date"]
        for i, (_l, b0, b1) in enumerate(buckets):
            if b0 <= wd <= b1:
                series[status][i] += int(row["n"])
                break
    has_data = any(sum(series[k][i] for k in keys) for i in range(len(buckets)))
    title = "Attendance trend"
    subtitle = f"{granularity} attendance records by status · {period_label}"
    if mode == "daily":
        subtitle += " · hourly trend omitted (no verified hourly attendance snapshot source)"
    svg = None
    if has_data:
        view_w, view_h = 680, 320
        ml, mr, mt, mb = 58, 18, 24, 82
        px0, px1, py0, py1 = ml, view_w - mr, mt, view_h - mb
        plot_h = py1 - py0
        n = len(buckets)
        group_w = (px1 - px0) / max(n, 1)
        # Allocate bar slots only to series with data in the selected period (legend/table keep all keys).
        plot_keys = [k for k in keys if any(series[k][i] for i in range(n))] or list(keys)
        bar_slot = (group_w * 0.8) / max(len(plot_keys), 1)
        bar_w = min(14, bar_slot)
        bar_draw_w = max(bar_w - 1.5, 1.0) if bar_w > 2 else max(bar_w - 0.5, 1.0)
        y_max = max((series[k][i] for k in keys for i in range(n)), default=0)
        y_max = max(1, int((y_max * 1.2) + 0.999))
        step = 1 if y_max <= 4 else max(1, round(y_max / 4))
        ticks = list(range(0, y_max + 1, step))
        if ticks[-1] != y_max:
            ticks.append(y_max)
        def y_px(v): return py1 - (float(v) / y_max) * plot_h
        parts = [f'<svg role="img" aria-labelledby="nd-trend-title nd-trend-desc" viewBox="0 0 {view_w} {view_h}" width="100%" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">', f'<title id="nd-trend-title">{_svg_text(title)}</title><desc id="nd-trend-desc">Attendance records by selected status and period bucket.</desc><rect x="0" y="0" width="{view_w}" height="{view_h}" fill="#ffffff" rx="10"/>']
        for tick in ticks:
            ty = y_px(tick)
            parts.append(f'<line x1="{ml}" y1="{ty:.2f}" x2="{px1}" y2="{ty:.2f}" stroke="#eef2f6"/><text x="{ml-8}" y="{ty+4:.2f}" text-anchor="end" font-size="11" fill="#6c757d">{tick}</text>')
        parts.append(f'<text x="16" y="{(py0+py1)/2:.2f}" text-anchor="middle" font-size="11" fill="#6c757d" transform="rotate(-90 16 {(py0+py1)/2:.2f})">Records</text>')
        show_every = 3 if n > 24 else (2 if n > 14 else 1)
        for i, (label, _b0, _b1) in enumerate(buckets):
            gx = px0 + i * group_w + group_w / 2
            group_left = gx - (bar_w * len(plot_keys) / 2)
            for j, key in enumerate(plot_keys):
                value = series[key][i]
                bx = group_left + j * bar_w
                by = y_px(value)
                parts.append(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bar_draw_w:.2f}" height="{py1-by:.2f}" fill="{STATUS_COLORS[key]}" rx="2"><title>{_svg_text(label)}: {_svg_text(STATUS_LABELS[key])} {int(value)} records</title></rect>')
            if i % show_every == 0 or i == n - 1:
                parts.append(f'<text x="{gx:.2f}" y="{py1+18}" text-anchor="middle" font-size="10.5" fill="#6c757d">{_svg_text(label)}</text>')
        lx = ml
        for key in keys:
            parts.append(f'<rect x="{lx}" y="{view_h-16}" width="12" height="12" rx="2" fill="{STATUS_COLORS[key]}"/><text x="{lx+17}" y="{view_h-6}" font-size="10.5" fill="#495057">{_svg_text(STATUS_LABELS[key])}</text>')
            lx += 108
        parts.append('</svg>')
        svg = mark_safe(''.join(parts))
    headers = ["Period"] + [STATUS_LABELS[k] for k in keys] + ["Total"]
    trows = []
    for i, (label, *_r) in enumerate(buckets):
        vals = [int(series[k][i]) for k in keys]
        trows.append([label] + vals + [sum(vals)])
    trows.append(["Total"] + [sum(int(v) for v in series[k]) for k in keys] + [sum(sum(int(v) for v in series[k]) for k in keys)])
    return {"title": title, "subtitle": subtitle, "svg": svg, "table": _simple_table(headers, trows, caption="Attendance trend data"), "authorized": True, "empty": not has_data}


def _comparison_widget(rec_qs, scope, office_id, department_id):
    if scope["scope_kind"] == "institution":
        if department_id or office_id:
            return None
        group_field = "schedule__shift__office__office_name"
        title, subtitle, entity = "Attendance comparison by branch", "Attendance records by branch in authorized institution (selected period)", "Branch"
    elif scope.get("office") is not None and scope.get("department") is None:
        group_field = "schedule__employee_assignment__position__department__department_name"
        title, subtitle, entity = "Attendance comparison by department", f"Attendance records by department in {scope['office'].office_name} (selected period)", "Department"
    else:
        return None
    # Present = records with a valid Check In (first_check_in_at IS NOT NULL)
    present_filter = Q(first_check_in_at__isnull=False)
    rows = [r for r in rec_qs.values(group_field).annotate(total=Count("pk"), present=Count("pk", filter=present_filter), late=Count("pk", filter=Q(status=AttendanceRecord.Status.LATE)), absent=Count("pk", filter=Q(status=AttendanceRecord.Status.ABSENT)), incomplete=Count("pk", filter=Q(status=AttendanceRecord.Status.INCOMPLETE))).order_by("-total", group_field)[:12] if r.get(group_field)]
    if not rows:
        return {"title": title, "subtitle": subtitle, "svg": None, "table": _simple_table([entity, "Records"], [["No records", 0]], caption=title), "authorized": True, "empty": True}
    labels = [r[group_field] or "—" for r in rows]
    vals = [int(r["total"] or 0) for r in rows]
    max_v = max(vals) or 1
    h, w, ml, mr = 56 + len(rows) * 44, 760, 200, 80
    parts = [f'<svg role="img" aria-labelledby="nd-cmp-title" viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" class="nd-svg-chart"><title id="nd-cmp-title">{_svg_text(title)}</title><rect width="{w}" height="{h}" fill="#fff" rx="10"/>']
    for i, (label, row) in enumerate(zip(labels, rows)):
        y = 36 + i * 44
        bw = (vals[i] / max_v) * (w - ml - mr)
        parts.append(f'<text x="{ml-10}" y="{y+15}" text-anchor="end" font-size="12" fill="#374151">{_svg_text(label[:26])}</text><rect x="{ml}" y="{y}" width="{max(bw,2):.1f}" height="24" rx="4" fill="#2563eb"><title>{_svg_text(label)}: {vals[i]} records (present {row["present"]}, late {row["late"]}, absent {row["absent"]}, incomplete {row["incomplete"]})</title></rect><text x="{ml+bw+8}" y="{y+16}" font-size="12" font-weight="700" fill="#2563eb">{vals[i]}</text>')
    parts.append('</svg>')
    table = _simple_table([entity, "Records", "Present", "Late", "Absent", "Incomplete"], [[r[group_field] or "—", r["total"], r["present"], r["late"], r["absent"], r["incomplete"]] for r in rows], caption=title)
    return {"title": title, "subtitle": subtitle, "svg": mark_safe(''.join(parts)), "table": table, "authorized": True, "empty": False}


def _employee_distribution_widget(scope, today, office_id, department_id, can_employees):
    if not can_employees:
        return {"authorized": False}
    qs = _scope_employee_qs(Employee.objects.filter(employment_status=Employee.EmploymentStatus.ACTIVE), scope, today, office_id=office_id, department_id=department_id)
    if scope["scope_kind"] == "institution":
        if office_id:
            rows = list(qs.values("assignments__position__department__department_name").annotate(n=Count("id", distinct=True)).order_by("-n", "assignments__position__department__department_name")[:12])
            title, entity, key = "Employee distribution by department", "Department", "assignments__position__department__department_name"
        else:
            rows = list(qs.values("assignments__position__office__office_name").annotate(n=Count("id", distinct=True)).order_by("-n", "assignments__position__office__office_name")[:12])
            title, entity, key = "Employee distribution by branch", "Branch", "assignments__position__office__office_name"
    elif scope.get("office") is not None and scope.get("department") is None:
        rows = list(qs.values("assignments__position__department__department_name").annotate(n=Count("id", distinct=True)).order_by("-n", "assignments__position__department__department_name")[:12])
        title, entity, key = "Employee distribution by department", "Department", "assignments__position__department__department_name"
    else:
        return None
    labels = [r[key] or "—" for r in rows]
    vals = [int(r["n"] or 0) for r in rows]
    total = sum(vals)
    subtitle = "Distinct active employees in authorized scope (current snapshot)"
    if total <= 0:
        return {"title": title, "subtitle": subtitle, "svg": None, "table": _simple_table([entity, "Employees"], [["No employees", 0]], caption=title), "authorized": True, "empty": True}
    max_v = max(vals) or 1
    h, w, ml, mr = 56 + len(rows) * 40, 760, 200, 60
    parts = [f'<svg role="img" aria-labelledby="nd-ed-title" viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" class="nd-svg-chart"><title id="nd-ed-title">{_svg_text(title)}</title><rect width="{w}" height="{h}" fill="#fff" rx="10"/>']
    for i, (label, v) in enumerate(zip(labels, vals)):
        y = 34 + i * 40
        bw = (v / max_v) * (w - ml - mr)
        parts.append(f'<text x="{ml-10}" y="{y+15}" text-anchor="end" font-size="12" fill="#374151">{_svg_text(str(label)[:26])}</text><rect x="{ml}" y="{y}" width="{max(bw,2):.1f}" height="22" rx="4" fill="#16a34a"><title>{_svg_text(label)}: {v} employees</title></rect><text x="{ml+bw+8}" y="{y+15}" font-size="12" font-weight="700" fill="#16a34a">{v}</text>')
    parts.append('</svg>')
    table = _simple_table([entity, "Employees"], [[l, v] for l, v in zip(labels, vals)] + [["Total", total]], caption=title)
    return {"title": title, "subtitle": subtitle, "svg": mark_safe(''.join(parts)), "table": table, "authorized": True, "empty": False}


def _leave_summary_widget(scope, start, end, office_id, department_id, can_requests, can_leave_calendar, calendar_url):
    if not can_requests and not can_leave_calendar:
        return {"authorized": False}
    # Use approved+processed LEAVE overlap summary; authorized scope applied server-side.
    if can_requests:
        leave_qs = _scope_request_qs(
            EmployeeRequest.objects.filter(request_type=REQUEST_LEAVE, status=REQUEST_APPROVED, schedule_processed_at__isnull=False, start_date__lte=end, end_date__gte=start),
            scope, office_id=office_id, department_id=department_id,
        )
        distinct_employees = leave_qs.values("employee_assignment__employee_id").distinct().count()
        request_count = leave_qs.count()
    else:
        # Calendar permission can show already-authorized aggregate only when available via
        # request access in this summary context; fail to neutral unavailable rather than expose data.
        distinct_employees = "—"
        request_count = "—"
    subtitle = "Approved, HR-processed leave requests overlapping selected period (authorized scope). No private reasons/attachments/review notes."
    rows = [["Distinct employees on approved processed leave (overlap)", distinct_employees], ["Approved processed leave requests (overlap)", request_count]]
    return {"title": "Approved leave summary", "subtitle": subtitle, "table": _simple_table(["Metric", "Count"], rows, caption="Leave summary"), "authorized": True, "empty": (distinct_employees == 0 and request_count == 0), "svg": None, "calendar_url": calendar_url if can_leave_calendar else None}


def build_dashboard_context(request):
    user = request.user
    today = timezone.localdate()
    mode, start, end, period_label, controls = _parse_period_mode(request, today)
    scope = resolve_scope(user)

    can_attendance = user.has_perm("attendance.view_attendancerecord")
    can_requests = user.has_perm("employee_requests.view_employeerequest")
    can_employees = user.has_perm("employees.view_employee")
    can_leave_calendar = user.has_perm("employee_requests.view_leave_calendar")

    selected_office_id, selected_department_id, allowed_filters = (None, None, {"office": False, "department": False})
    filter_choices = {"offices": [], "departments": []}
    if scope["scope_available"]:
        selected_office_id, selected_department_id, allowed_filters = _parse_scope_filters(request, scope)
        filter_choices = _filter_choices(scope, selected_office_id)

    report_params = _report_params(start, end, selected_office_id, selected_department_id)
    report_url = _safe_url("attendance:report-daily", report_params) if can_attendance and scope["scope_available"] else None
    calendar_url = _safe_url("employee_requests:leave-calendar") if can_leave_calendar else None

    context = {
        "today": today, "period_mode": mode, "period_start": start.isoformat(), "period_end": end.isoformat(),
        "period_label": period_label, "period_is_today": start == end == today, "period_controls": controls,
        "year_choices": list(range(today.year - 5, today.year + 2)),
        "month_choices": [(m, calendar.month_name[m]) for m in range(1, 13)],
        "scope_label": scope["scope_label"], "scope_available": scope["scope_available"], "scope_unavailable_reason": scope["scope_unavailable_reason"],
        "is_admin_dashboard": scope["scope_kind"] == "superuser" or is_it_admin_user(user),
        "selected_office_id": selected_office_id, "selected_department_id": selected_department_id,
        "filter_offices": filter_choices["offices"] if allowed_filters["office"] else [],
        "filter_departments": filter_choices["departments"] if allowed_filters["department"] else [],
        "show_office_filter": allowed_filters["office"], "show_department_filter": allowed_filters["department"],
        "can_view_attendance": can_attendance, "can_view_requests": can_requests,
        "kpi_cards": [], "trend_chart": None, "distribution_chart": None, "comparison_chart": None,
        "employee_distribution_chart": None, "leave_summary": None, "recent_records": [],
        "report_url": report_url, "calendar_url": calendar_url,
        "RECENT_LIMIT": RECENT_LIMIT, "time_zone": getattr(settings, "TIME_ZONE", ""),
    }
    if not scope["scope_available"]:
        return context

    if can_employees:
        emp_qs = _scope_employee_qs(Employee.objects.filter(employment_status=Employee.EmploymentStatus.ACTIVE), scope, today, office_id=selected_office_id, department_id=selected_department_id)
        employee_list_params = {"employment_status": Employee.EmploymentStatus.ACTIVE}
        if selected_office_id:
            employee_list_params["office_id"] = selected_office_id
        if selected_department_id:
            employee_list_params["department_id"] = selected_department_id
        context["kpi_cards"].append(_kpi(
            "Active employees", emp_qs.count(), unit="employees", icon="bi bi-people-fill", color="blue",
            note="Distinct active employees in authorized scope (current snapshot)",
            url=_safe_url("employees:employee-list", employee_list_params),
        ))

    if can_attendance:
        rec_qs = _scope_attendance_qs(
            AttendanceRecord.objects.filter(schedule__work_date__gte=start, schedule__work_date__lte=end),
            scope, office_id=selected_office_id, department_id=selected_department_id,
        )
        # Present = records with a valid Check In (first_check_in_at IS NOT NULL)
        # This includes PRESENT, LATE, and INCOMPLETE statuses
        present_filter = Q(first_check_in_at__isnull=False)
        counts = rec_qs.aggregate(
            present_checked_in=Count("id", filter=present_filter),
            **{AttendanceRecord.Status.PRESENT: Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)),
               AttendanceRecord.Status.LATE: Count("id", filter=Q(status=AttendanceRecord.Status.LATE)),
               AttendanceRecord.Status.ABSENT: Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
               AttendanceRecord.Status.INCOMPLETE: Count("id", filter=Q(status=AttendanceRecord.Status.INCOMPLETE)),
               AttendanceRecord.Status.ON_LEAVE: Count("id", filter=Q(status=AttendanceRecord.Status.ON_LEAVE)),
               AttendanceRecord.Status.HOLIDAY: Count("id", filter=Q(status=AttendanceRecord.Status.HOLIDAY)),
               AttendanceRecord.Status.REST_DAY: Count("id", filter=Q(status=AttendanceRecord.Status.REST_DAY)),
               AttendanceRecord.Status.PENDING_REVIEW: Count("id", filter=Q(status=AttendanceRecord.Status.PENDING_REVIEW)),
               AttendanceRecord.Status.SCHEDULED: Count("id", filter=Q(status=AttendanceRecord.Status.SCHEDULED)),
               "total": Count("id")}
        )
        def _status_report_url(status):
            return _safe_url("attendance:report-daily", {**report_params, "status": status})
        context["kpi_cards"].extend([
            _kpi("Present", counts["present_checked_in"] or 0, unit="attendance records", icon="bi bi-check-circle-fill", color="green", url=_safe_url("attendance:report-daily", {**report_params, "present": "true"}), note="Checked in (Present, Late, or Incomplete)"),
            _kpi("Late", counts[AttendanceRecord.Status.LATE] or 0, unit="attendance records", icon="bi bi-clock-history", color="amber", url=_status_report_url(AttendanceRecord.Status.LATE)),
            _kpi("Absent", counts[AttendanceRecord.Status.ABSENT] or 0, unit="attendance records", icon="bi bi-x-circle-fill", color="red", url=_status_report_url(AttendanceRecord.Status.ABSENT), note="Recorded Absent status only; missing records not auto-counted"),
            _kpi("Incomplete", counts[AttendanceRecord.Status.INCOMPLETE] or 0, unit="attendance records", icon="bi bi-exclamation-circle-fill", color="cyan", url=_status_report_url(AttendanceRecord.Status.INCOMPLETE)),
        ])
        rows, total = _status_rows_from_counts(counts)
        context["distribution_chart"] = _distribution_widget(rows, total, "Attendance status distribution", "Attendance-record status breakdown for the selected period and authorized scope.")
        context["trend_chart"] = _trend_widget(rec_qs, mode, start, end, period_label)
        context["comparison_chart"] = _comparison_widget(rec_qs, scope, selected_office_id, selected_department_id)
        context["employee_distribution_chart"] = _employee_distribution_widget(scope, today, selected_office_id, selected_department_id, can_employees)

        recent_rows = []
        for r in rec_qs.select_related("schedule__employee_assignment__employee", "schedule__employee_assignment__position__department").order_by("-schedule__work_date", "-id")[:RECENT_LIMIT]:
            assignment = getattr(r.schedule, "employee_assignment", None)
            employee = getattr(assignment, "employee", None) if assignment else None
            department_obj = getattr(getattr(assignment, "position", None), "department", None)
            try:
                detail_url = reverse("attendance:record-detail", args=[r.pk])
            except NoReverseMatch:
                detail_url = None
            recent_rows.append({
                "employee_id": getattr(employee, "employee_id", "") or "—",
                "employee_name": getattr(employee, "full_name", "") or "—",
                "department": getattr(department_obj, "department_name", None) or "—",
                "work_date": r.schedule.work_date, "status": r.get_status_display(), "status_code": r.status, "url": detail_url,
            })
        context["recent_records"] = recent_rows
    else:
        context["trend_chart"] = {"authorized": False}
        context["distribution_chart"] = {"authorized": False}
        context["recent_records"] = []

    if can_requests or can_leave_calendar:
        context["leave_summary"] = _leave_summary_widget(scope, start, end, selected_office_id, selected_department_id, can_requests, can_leave_calendar, calendar_url)

    return context
