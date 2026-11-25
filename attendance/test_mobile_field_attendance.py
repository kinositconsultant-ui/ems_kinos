import json
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from attendance.models import (
    AttendanceEvent,
    AttendancePersistenceFailure,
    AttendanceRecord,
    FaceVerificationLog,
)
from attendance.views import MobileFaceAttendanceVerifyView
from employees.models import UserEmployeeLink
from .test_kiosk import KioskFixtureMixin


class MobileFieldAttendanceLocationTests(SimpleTestCase):
    def setUp(self):
        self.view = MobileFaceAttendanceVerifyView()
        self.factory = RequestFactory()

    def test_mobile_location_coordinates_are_parsed(self):
        request = self.factory.post(
            "/attendance/mobile/verify/",
            {
                "latitude": "-8.556",
                "longitude": "125.560",
                "accuracy": "12.5",
                "location_source": "arbitrary-browser-value",
            },
        )

        location, error = self.view._parse_location(request)

        self.assertIsNone(error)
        self.assertEqual(location["latitude"], Decimal("-8.556000"))
        self.assertEqual(location["longitude"], Decimal("125.560000"))
        self.assertEqual(location["accuracy"], 12.5)
        self.assertEqual(location["source"], "mobile_browser")

    def test_mobile_location_is_required(self):
        location, error = self.view._parse_location(self.factory.post("/attendance/mobile/verify/"))

        self.assertIsNone(location)
        self.assertEqual(error, "Location coordinates are required for mobile attendance.")

    def test_mobile_coordinates_are_normalized_to_database_precision(self):
        request = self.factory.post(
            "/attendance/mobile/verify/",
            {
                "latitude": "-8.556123789",
                "longitude": "125.560987654",
                "accuracy": "12.5",
            },
        )

        location, error = self.view._parse_location(request)

        self.assertIsNone(error)
        self.assertEqual(location["latitude"], Decimal("-8.556124"))
        self.assertEqual(location["longitude"], Decimal("125.560988"))
        self.assertEqual(location["latitude"].as_tuple().exponent, -6)
        self.assertEqual(location["longitude"].as_tuple().exponent, -6)

    def test_mobile_coordinate_boundaries_are_accepted(self):
        for latitude, longitude in (
            ("-90", "-180"),
            ("90", "180"),
        ):
            request = self.factory.post(
                "/attendance/mobile/verify/",
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": "12.5",
                },
            )
            location, error = self.view._parse_location(request)
            self.assertIsNone(error)
            self.assertTrue(location["latitude"].is_finite())
            self.assertTrue(location["longitude"].is_finite())

    def test_invalid_mobile_coordinates_are_rejected(self):
        invalid_coordinates = (
            ("-90.000001", "125"),
            ("90.000001", "125"),
            ("-8.556123789", "-180.000001"),
            ("-8.556123789", "180.000001"),
            ("not-a-number", "125"),
            ("NaN", "125"),
            ("Infinity", "125"),
        )
        for latitude, longitude in invalid_coordinates:
            request = self.factory.post(
                "/attendance/mobile/verify/",
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": "12.5",
                },
            )
            location, error = self.view._parse_location(request)
            self.assertIsNone(location)
            self.assertIsNotNone(error)

    def test_dynamic_location_does_not_depend_on_office_geofence(self):
        status, distance, reason = self.view._location_status(
            {"latitude": -8.556, "longitude": 125.560, "accuracy": 12.5},
            object(),
        )

        self.assertEqual(status, "dynamic")
        self.assertIsNone(distance)
        self.assertIn("Dynamic field location", reason)

    def test_mobile_assignment_lookup_has_initialized_server_date(self):
        request = self.factory.post(
            "/attendance/mobile/verify/",
            {"latitude": "-8.556", "longitude": "125.560", "accuracy": "12.5"},
        )
        with patch("employees.models.get_linked_employee", return_value=SimpleNamespace(pk=1)):
            request.user = SimpleNamespace(is_authenticated=True)
        office = SimpleNamespace(pk=1)
        assignment = SimpleNamespace(
            position=SimpleNamespace(office=office, office_id=office.pk),
        )

        with (
            patch("employees.models.get_linked_employee", return_value=SimpleNamespace(pk=1)),
            patch("attendance.views.cv2", SimpleNamespace(error=Exception, imdecode=lambda *args: None)),
            patch("attendance.views.np", object()),
            patch("attendance.views.EmployeeAssignment.objects.filter") as queryset,
        ):
            queryset.return_value.filter.return_value.select_related.return_value.first.return_value = assignment
            response = self.view.post(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["stage"], "image")

    def test_mobile_request_without_valid_assignment_is_rejected(self):
        request = self.factory.post(
            "/attendance/mobile/verify/",
            {
                "latitude": "-8.556",
                "longitude": "125.560",
                "accuracy": "12.5",
                "image": SimpleUploadedFile(
                    "capture.jpg", b"image-bytes", content_type="image/jpeg"
                ),
            },
        )
        request.user = SimpleNamespace(is_authenticated=True)
        with patch("employees.models.get_linked_employee", return_value=None):
            response = self.view.post(request)

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(json.loads(response.content)["stage"], "employee")


class MobileFrontendResponseTests(SimpleTestCase):
    def test_mobile_template_checks_non_json_responses_before_parsing(self):
        template = (
            Path(__file__).resolve().parent
            / "templates"
            / "attendance"
            / "mobile_attendance.html"
        ).read_text(encoding="utf-8")

        self.assertIn("response.headers.get('content-type')", template)
        self.assertIn("unexpected response", template.lower())
        self.assertIn("JSON.parse(responseText)", template)


class MobileAuthorizationTests(TestCase):
    def setUp(self):
        self.mobile_permission = Permission.objects.get(
            codename="use_mobile_face_attendance",
            content_type__app_label="attendance",
        )

    def _create_employee(self, username):
        from employees.models import Employee
        employee = Employee.objects.create(
            employee_id=f"{username}-id",
            full_name=username,
            hire_date="2020-01-01",
        )
        user = get_user_model().objects.create_user(username=username, password="test-password")
        UserEmployeeLink.objects.create(user=user, employee=employee)
        return user, employee

    def test_unauthenticated_mobile_request_returns_json_401(self):
        response = self.client.post(reverse("attendance:mobile-verify"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertFalse(response.json()["ok"])

    def test_employee_without_permission_is_denied_with_json_403(self):
        user, _ = self._create_employee("mobile-no-permission")
        self.client.force_login(user)
        response = self.client.post(reverse("attendance:mobile-verify"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["stage"], "authorization")

    def test_employee_with_permission_and_profile_is_allowed(self):
        user, _ = self._create_employee("mobile-employee")
        user.user_permissions.add(self.mobile_permission)
        self.client.force_login(user)
        response = self.client.post(
            reverse("attendance:mobile-verify"),
            {"latitude": "-8.556", "longitude": "125.560"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["stage"], "employee")

    def test_hr_with_permission_and_profile_is_allowed(self):
        user, _ = self._create_employee("mobile-hr")
        user.user_permissions.add(self.mobile_permission)
        hr_group, _ = Group.objects.get_or_create(name="HR Office")
        user.groups.add(hr_group)
        self.client.force_login(user)
        response = self.client.post(
            reverse("attendance:mobile-verify"),
            {"latitude": "-8.556", "longitude": "125.560"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["stage"], "employee")

    def test_authenticated_user_without_employee_profile_is_denied(self):
        user = get_user_model().objects.create_user(
            username="mobile-no-profile",
            password="test-password",
        )
        user.user_permissions.add(self.mobile_permission)
        self.client.force_login(user)
        response = self.client.post(reverse("attendance:mobile-verify"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["stage"], "employee")

    def test_mobile_verification_remains_csrf_protected(self):
        user, _ = self._create_employee("mobile-csrf")
        user.user_permissions.add(self.mobile_permission)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(user)
        response = csrf_client.post(
            reverse("attendance:mobile-verify"),
            {
                "latitude": "-8.556",
                "longitude": "125.560",
            },
        )
        self.assertEqual(response.status_code, 403)


class MobilePersistenceTests(KioskFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        UserEmployeeLink.objects.create(user=self.user, employee=self.employee)

    def test_mobile_check_in_persists_event_record_and_verification(self):
        with patch(
            "attendance.views.verify_attendance_frame",
            return_value={
                "employee_id": self.employee.pk,
                "score": 0.99,
                "match_result": "matched",
                "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                "liveness_score": 0.99,
            },
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556123789",
                    "longitude": "125.560987654",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        verification = FaceVerificationLog.objects.get(
            employee=self.employee,
            schedule=self.schedule,
        )
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        event = AttendanceEvent.objects.get(attendance_record=record)
        self.assertIsNone(verification.device)
        self.assertEqual(event.event_type, AttendanceEvent.EventType.CHECK_IN)
        self.assertEqual(event.verification_log_id, verification.pk)
        self.assertEqual(event.latitude, Decimal("-8.556124"))
        self.assertEqual(event.longitude, Decimal("125.560988"))
        self.assertEqual(str(event.location_accuracy_meters), "12.50")
        self.assertEqual(event.location_source, "mobile_browser")
        self.assertEqual(event.location_status, AttendanceEvent.LocationStatus.DYNAMIC)
        self.assertEqual(record.first_check_in_at, event.occurred_at)

    def test_mixed_kiosk_check_in_break_out_mobile_break_in_reuses_record(self):
        check_in = self._post_capture(now=self.now)
        self.assertEqual(check_in.status_code, 200, check_in.content)

        break_out = self._post_capture(now=self.now + timedelta(minutes=30))
        self.assertEqual(break_out.status_code, 200, break_out.content)

        mobile_now = self.now + timedelta(minutes=90)
        with (
            patch("attendance.views.timezone.now", return_value=mobile_now),
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": FaceVerificationLog.MatchResult.MATCHED,
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
        ):
            break_in = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556123789",
                    "longitude": "125.560987654",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(break_in.status_code, 200, break_in.content)
        self.assertEqual(break_in.json()["action"], "break_end")

        record = AttendanceRecord.objects.get(schedule=self.schedule)
        events = list(
            AttendanceEvent.objects.filter(attendance_record=record).order_by("occurred_at")
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                AttendanceEvent.EventType.CHECK_IN,
                AttendanceEvent.EventType.BREAK_START,
                AttendanceEvent.EventType.BREAK_END,
            ],
        )
        mobile_event = events[-1]
        self.assertEqual(mobile_event.event_type, AttendanceEvent.EventType.BREAK_END)
        self.assertEqual(mobile_event.source, AttendanceEvent.Source.FACE_RECOGNITION)
        self.assertEqual(mobile_event.verification_log.event_type, AttendanceEvent.EventType.BREAK_END)
        self.assertEqual(mobile_event.verification_log.employee, self.employee)
        self.assertEqual(mobile_event.verification_log.schedule, self.schedule)
        self.assertEqual(mobile_event.latitude, Decimal("-8.556124"))
        self.assertEqual(mobile_event.longitude, Decimal("125.560988"))
        self.assertEqual(str(mobile_event.location_accuracy_meters), "12.50")
        self.assertEqual(mobile_event.location_source, "mobile_browser")
        self.assertEqual(AttendanceRecord.objects.filter(schedule=self.schedule).count(), 1)
        self.assertEqual(
            AttendanceEvent.objects.filter(attendance_record__schedule=self.schedule).count(),
            3,
        )

    def test_mobile_check_in_rolls_back_successful_attendance_writes(self):
        with (
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": "matched",
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
            patch.object(
                AttendanceEvent,
                "save",
                side_effect=ValidationError("Simulated event persistence failure."),
            ),
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556000",
                    "longitude": "125.560000",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["stage"], "attendance")
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        self.assertFalse(AttendanceEvent.objects.filter(attendance_record__schedule=self.schedule).exists())
        self.assertFalse(FaceVerificationLog.objects.filter(schedule=self.schedule).exists())

    def test_mobile_check_in_rolls_back_on_workflow_application_failure(self):
        """A failure in apply_event_to_record must roll back all writes."""
        with (
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": "matched",
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
            patch(
                "attendance.views.apply_event_to_record",
                side_effect=ValidationError("Simulated workflow application failure."),
            ),
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556000",
                    "longitude": "125.560000",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["stage"], "attendance")
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        self.assertFalse(AttendanceEvent.objects.filter(attendance_record__schedule=self.schedule).exists())
        self.assertFalse(FaceVerificationLog.objects.filter(schedule=self.schedule).exists())

    def test_mobile_check_in_rolls_back_on_database_save_failure(self):
        """A DatabaseError from AttendanceEvent.save() must roll back all writes."""
        from django.db import DatabaseError

        with (
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": "matched",
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
            patch.object(
                AttendanceEvent,
                "save",
                side_effect=DatabaseError("Simulated database save failure."),
            ),
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556000",
                    "longitude": "125.560000",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["stage"], "attendance")
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        self.assertFalse(AttendanceEvent.objects.filter(attendance_record__schedule=self.schedule).exists())
        self.assertFalse(FaceVerificationLog.objects.filter(schedule=self.schedule).exists())

    def test_mobile_check_in_logs_persistence_stage_on_failure(self):
        """Server-side diagnostics must identify the failing persistence stage."""
        with (
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": "matched",
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
            patch.object(
                AttendanceEvent,
                "save",
                side_effect=ValidationError("Simulated event persistence failure."),
            ),
            self.assertLogs("attendance.views", level="WARNING") as log_cm,
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556000",
                    "longitude": "125.560000",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["stage"], "attendance")
        logged = "\n".join(log_cm.output)
        self.assertIn("attendance_event_save", logged)
        self.assertIn("channel mobile", logged)
        self.assertIn("verification_log", logged)
        incident = AttendancePersistenceFailure.objects.get()
        self.assertEqual(incident.employee, self.employee)
        self.assertEqual(incident.schedule, self.schedule)
        self.assertEqual(incident.channel, AttendancePersistenceFailure.Channel.MOBILE)
        self.assertEqual(incident.expected_event_type, AttendanceEvent.EventType.CHECK_IN)
        self.assertEqual(incident.persistence_stage, "attendance_event_save")
        self.assertEqual(incident.exception_type, ValidationError.__name__)

    def test_mobile_check_in_rolls_back_on_event_validation_failure(self):
        """A ValidationError from AttendanceEvent.full_clean() must roll back all writes."""
        with (
            patch(
                "attendance.views.verify_attendance_frame",
                return_value={
                    "employee_id": self.employee.pk,
                    "score": 0.99,
                    "match_result": "matched",
                    "liveness_result": FaceVerificationLog.LivenessResult.PASSED,
                    "liveness_score": 0.99,
                },
            ),
            patch.object(
                AttendanceEvent,
                "full_clean",
                side_effect=ValidationError("Simulated event validation failure."),
            ),
        ):
            response = self.client.post(
                reverse("attendance:mobile-verify"),
                {
                    "image": self._encoded_image(),
                    "latitude": "-8.556000",
                    "longitude": "125.560000",
                    "accuracy": "12.5",
                },
            )

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["stage"], "attendance")
        self.assertFalse(AttendanceRecord.objects.filter(schedule=self.schedule).exists())
        self.assertFalse(AttendanceEvent.objects.filter(attendance_record__schedule=self.schedule).exists())
        self.assertFalse(FaceVerificationLog.objects.filter(schedule=self.schedule).exists())
