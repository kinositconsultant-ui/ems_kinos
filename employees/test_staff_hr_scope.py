from datetime import timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from core.context_processors import attendance_menu_permissions
from core.permissions import offices_for_user
from employees.forms import EmployeeForm, EmployeeAssignmentForm
from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Office, Department, Position


class StaffHRBranchRepairTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='staff-scope')
        cls.group = Group.objects.create(name='Staff HR')
        cls.user.groups.add(cls.group)
        cls.group.permissions.set(Permission.objects.filter(content_type__app_label__in=['employees', 'organization', 'employee_requests']))
        cls.offices = []
        cls.departments = []
        cls.positions = []
        cls.employees = []
        cls.assignments = []
        for code in ('A', 'B'):
            office = Office.objects.create(office_code=code, office_name='Branch '+code)
            department = Department.objects.create(office=office, department_code=code, department_name='Department '+code)
            position = Position.objects.create(office=office, department=department, position_code=code, position_title='Position '+code)
            employee = Employee.objects.create(employee_id=code, full_name='Employee '+code)
            if code == 'A':
                UserEmployeeLink.objects.create(user=cls.user, employee=employee)
            assignment = EmployeeAssignment.objects.create(employee=employee, position=position, is_primary=True, start_date=timezone.localdate()-timedelta(days=10))
            cls.offices.append(office); cls.departments.append(department); cls.positions.append(position); cls.employees.append(employee); cls.assignments.append(assignment)

    def setUp(self):
        self.client.force_login(self.user)

    def test_employee_list_and_filters_stay_in_branch(self):
        response = self.client.get(reverse('employees:employee-list'), {'office_id': self.offices[1].pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['employees']), [self.employees[0]])
        for item in response.context['filters']:
            if item['name']=='department_id':
                self.assertEqual(list(item['choices']), [(self.departments[0].pk, self.departments[0].department_name)])

    def test_foreign_detail_and_edit_get_post_denied(self):
        for name, pk in [('employee-detail',self.employees[1].pk), ('employee-update',self.employees[1].pk), ('assignment-detail',self.assignments[1].pk), ('assignment-update',self.assignments[1].pk)]:
            url=reverse('employees:'+name, args=[pk])
            self.assertEqual(self.client.get(url).status_code, 404)
            if name.endswith('update'):
                self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_own_create_and_edit_pages_render(self):
        self.assertEqual(self.client.get(reverse('employees:employee-create')).status_code, 200)
        self.assertEqual(self.client.get(reverse('employees:employee-update',args=[self.employees[0].pk])).status_code, 200)

    def test_forms_reject_foreign_choices(self):
        form=EmployeeForm(assigned_by=self.user)
        for name, value in [('office',self.offices[1].pk), ('department',self.departments[1].pk), ('position',self.positions[1].pk)]:
            with self.assertRaises(ValidationError):
                form.fields[name].clean(value)
        form=EmployeeAssignmentForm(user=self.user)
        self.assertFalse(form.fields['employee'].queryset.filter(pk=self.employees[1].pk).exists())
        self.assertFalse(form.fields['position'].queryset.filter(pk=self.positions[1].pk).exists())

    def test_organization_scope_and_mutation_denial(self):
        for name, pk in [('office-detail',self.offices[1].pk),('department-detail',self.departments[1].pk),('position-detail',self.positions[1].pk)]:
            self.assertEqual(self.client.get(reverse('organization:'+name,args=[pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('organization:department-list')).status_code,200)
        self.assertEqual(self.client.post(reverse('organization:department-update',args=[self.departments[0].pk]),{}).status_code,403)

    def test_missing_assignment_fails_closed(self):
        self.assignments[0].delete()
        self.assertEqual(self.client.get(reverse('employees:employee-list')).status_code,403)
        self.assertEqual(self.client.get(reverse('organization:department-list')).status_code,403)
        self.assertFalse(offices_for_user(self.user).exists())
        self.assertFalse(EmployeeForm(assigned_by=self.user).fields['office'].queryset.exists())

    def test_my_requests_hidden_and_direct_access_denied(self):
        request=RequestFactory().get('/'); request.user=self.user
        self.assertTrue(attendance_menu_permissions(request)['director_my_requests_hidden'])
        self.assertEqual(self.client.get(reverse('employee_requests:request-mine')).status_code,403)
        self.assertEqual(self.client.post(reverse('employee_requests:request-create'),{}).status_code,403)

    def test_permission_required_and_superuser_preserved(self):
        self.group.permissions.clear()
        self.assertEqual(self.client.get(reverse('employees:employee-list')).status_code,403)
        self.user.is_superuser=True; self.user.save()
        self.assertEqual(self.client.get(reverse('employees:employee-detail',args=[self.employees[1].pk])).status_code,200)
        self.assertEqual(offices_for_user(self.user).count(),2)

    def test_historical_assignment_does_not_grant_current_employee_access(self):
        EmployeeAssignment.objects.create(employee=self.employees[1], position=self.positions[0], is_primary=True,
            start_date=timezone.localdate()-timedelta(days=30), end_date=timezone.localdate()-timedelta(days=20))
        response=self.client.get(reverse('employees:employee-list'))
        self.assertEqual(list(response.context['employees']),[self.employees[0]])
        self.assertEqual(self.client.get(reverse('employees:employee-detail',args=[self.employees[1].pk])).status_code,404)

    def test_schedule_edit_and_preview_cannot_expand_branch_scope(self):
        from datetime import time
        from attendance.models import Shift, EmployeeSchedule
        from attendance.services.scheduling import build_schedule_snapshot
        from attendance.forms import EmployeeScheduleForm
        from django.core.exceptions import PermissionDenied
        self.group.permissions.add(*Permission.objects.filter(content_type__app_label='attendance',codename__in=['view_employeeschedule','add_employeeschedule','change_employeeschedule']))
        shift=Shift.objects.create(office=self.offices[1],shift_code='FOREIGN',name='Foreign shift',start_time=time(8),end_time=time(17),break_start_time=time(12),break_end_time=time(13))
        day=timezone.localdate()
        schedule=EmployeeSchedule(employee_assignment=self.assignments[1],shift=shift,work_date=day)
        schedule.apply_snapshot(build_schedule_snapshot(shift,day))
        schedule.save()
        self.assertEqual(self.client.get(reverse('attendance:schedule-update',args=[schedule.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('attendance:schedule-update',args=[schedule.pk]),{}).status_code,404)
        with self.assertRaises(PermissionDenied):
            EmployeeScheduleForm(instance=schedule,user=self.user)
        response=self.client.get(reverse('attendance:schedule-preview'),{'office':self.offices[1].pk,'employee_assignment':self.assignments[1].pk,'shift':shift.pk,'schedule':schedule.pk,'work_date':str(day)})
        self.assertEqual(response.status_code,403)
