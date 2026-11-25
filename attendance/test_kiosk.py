from datetime import timedelta
import os
from unittest.mock import Mock, patch

import cv2
import numpy as np
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, EmployeeAssignment, UserEmployeeLink
from organization.models import Office, Position

from .models import (
    AttendanceDevice,
    AttendanceEvent,
    AttendancePolicy,
    AttendanceRecord,
    EmployeeSchedule,
    FaceTemplate,
    FaceVerificationLog,
    Shift,
)
from .services.face_encryption import encrypt_embedding
from .services.face_engine import FaceEngineError


class KioskFixtureMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.encryption_key = Fernet.generate_key().decode("ascii")

    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"FACE_TEMPLATE_ENCRYPTION_KEY": self.encryption_key},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        user_model = get_user_model()
        self.user = user_model.objects.create_superuser(
            username="kiosk-admin",
            email="kiosk-admin@example.invalid",
            password="test-password",
        )
        self.client.force_login(self.user)

        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        self.office = Office.objects.create(
            office_code="KSK",
            office_name="Kiosk Office",
        )
        self.employee = Employee.objects.create(
            employee_id="KSK-EMP-001",
            full_name='Maria Soares',
            hire_date=self.today - timedelta(days=400),
            employment_status=Employee.EmploymentStatus.ACTIVE,
        )
        self.position = Position.objects.create(
            office=self.office,
            position_code="KSK-POS",
            position_title="Kiosk Employee",
        )
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee,
            position=self.position,
            start_date=self.today - timedelta(days=30),
        )
        self.shift = Shift.objects.create(
            office=self.office,
            shift_code="KSK-DAY",
            name="Kiosk Day Shift",
            start_time=self.now.time().replace(second=0, microsecond=0),
            end_time=(self.now + timedelta(hours=4)).time().replace(second=0, microsecond=0),
            grace_period_minutes=5,
        )
        self.device = AttendanceDevice.objects.create(
            office=self.office,
            device_code="KSK-DEVICE-1",
            device_name="Kiosk Device",
            device_type=AttendanceDevice.DeviceType.KIOSK,
            status=AttendanceDevice.Status.ACTIVE,
        )
        AttendancePolicy.objects.create(
            office=self.office,
            policy_name="Kiosk policy",
            effective_from=self.today - timedelta(days=1),
            effective_until=self.today + timedelta(days=30),
            early_checkin_window_minutes=30,
            absent_after_minutes=180,
            require_liveness=True,
        )
        self.schedule = self._create_schedule()
        self._create_template(self.employee, np.array([1.0, 0.0, 0.0], dtype=np.float32))

    def _create_schedule(self):
        start = self.now - timedelta(minutes=1)
        return EmployeeSchedule.objects.create(
            employee_assignment=self.assignment,
            shift=self.shift,
            work_date=self.today,
            scheduled_start_at=start,
            scheduled_end_at=start + timedelta(hours=8),
            status=EmployeeSchedule.Status.SCHEDULED,
        )

    @staticmethod
    def _create_template(employee, embedding):
        return FaceTemplate.objects.create(
            employee=employee,
            provider="YuNet+SFace",
            encrypted_template=encrypt_embedding(embedding),
            model_version="YuNet/SFace-2021dec",
            status=FaceTemplate.Status.ACTIVE,
        )

    @staticmethod
    def _encoded_image():
        success, encoded = cv2.imencode(
            ".jpg", np.full((160, 200, 3), 120, dtype=np.uint8)
        )
        if not success:
            raise AssertionError("OpenCV could not encode the test image.")
        return SimpleUploadedFile(
            "camera-capture.jpg",
            encoded.tobytes(),
            content_type="image/jpeg",
        )

    def _post_capture(
        self,
        *,
        image=None,
        device_id=None,
        embedding=None,
        detections=None,
        now=None,
        engine_failure=None,
    ):
        face_row = np.zeros(15, dtype=np.float32)
        engine = Mock()
        engine.detect_faces.return_value = (
            detections
            if detections is not None
            else [{"face_row": face_row, "bbox": (10, 10, 80, 80), "confidence": 0.99}]
        )
        engine.extract_embedding_from_face.return_value = (
            embedding
            if embedding is not None
            else np.array([1.0, 0.0, 0.0], dtype=np.float32)
        )
        data = {
            "device_id": str(device_id or self.device.pk),
            "image": image or self._encoded_image(),
        }
        engine_patch = (
            patch(
                "attendance.services.face_attendance.get_face_engine",
                side_effect=engine_failure,
            )
            if engine_failure
            else patch(
                "attendance.services.face_attendance.get_face_engine",
                return_value=engine,
            )
        )
        liveness_patch = patch(
            "attendance.services.face_attendance.check_face_liveness",
            return_value={
                "result": FaceVerificationLog.LivenessResult.PASSED,
                "score": 0.99,
                "reason": "Test liveness fixture passed.",
                "model_version": "test",
            },
        )
        with engine_patch, liveness_patch:
            if now is None:
                return self.client.post(reverse("attendance:kiosk-verify"), data=data)
            with patch("attendance.views.timezone.now", return_value=now):
                return self.client.post(reverse("attendance:kiosk-verify"), data=data)



class FaceAttendanceKioskTests(KioskFixtureMixin, TestCase):
    def test_employee_with_attendance_permissions_cannot_access_kiosk(self):
        employee_user = get_user_model().objects.create_user(
            username="employee-kiosk",
            password="test-password",
        )
        employee_group, _ = Group.objects.get_or_create(name="employees")
        employee_user.groups.add(employee_group)
        employee_user.user_permissions.add(*Permission.objects.filter(
            codename__in={
                "view_attendancerecord",
                "add_attendanceevent",
                "add_attendancerecord",
                "change_attendancerecord",
                "add_faceverificationlog",
            },
            content_type__app_label="attendance",
        ))
        UserEmployeeLink.objects.create(user=employee_user, employee=self.employee)
        self.client.force_login(employee_user)

        self.assertEqual(self.client.get(reverse("attendance:kiosk")).status_code, 403)
        self.assertEqual(self.client.get(reverse("attendance:device-list")).status_code, 403)
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, "Face Attendance / Attendance Kiosk")
    def test_kiosk_url_and_login_permission(self):
        response = self.client.get(reverse("attendance:kiosk"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Face Attendance Kiosk")
        self.assertContains(response, "Start Camera")
        self.assertContains(response, "Capture &amp; Verify Attendance")
        self.assertContains(response, "audio: false")
        self.assertContains(response, "YuNet + SFace")

        self.client.logout()
        self.assertEqual(self.client.get(reverse("attendance:kiosk")).status_code, 302)

        user = get_user_model().objects.create_user(
            username="kiosk-no-permission",
            password="test-password",
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("attendance:kiosk")).status_code, 403)
        response = self.client.post(
            reverse("attendance:kiosk-verify"),
            {"device_id": self.device.pk, "image": self._encoded_image()},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FaceVerificationLog.objects.exists())

    def test_capture_requires_post_and_csrf(self):
        self.assertEqual(
            self.client.get(reverse("attendance:kiosk-verify")).status_code, 405
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(
            csrf_client.post(
                reverse("attendance:kiosk-verify"),
                {"device_id": self.device.pk, "image": self._encoded_image()},
            ).status_code,
            403,
        )

    def test_invalid_image_is_rejected_without_attendance_or_verification_log(self):
        invalid = SimpleUploadedFile(
            "camera-capture.jpg", b"not an image", content_type="image/jpeg"
        )
        response = self._post_capture(image=invalid)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["stage"], "image")
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(FaceVerificationLog.objects.exists())

    def test_frame_without_face_is_logged_and_does_not_record_attendance(self):
        response = self._post_capture(detections=[])

        self.assertEqual(response.status_code, 422)
        self.assertIn("No face was detected", response.json()["message"])
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.NOT_MATCHED,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_multiple_faces_are_rejected_and_logged(self):
        multiple_faces = [
            {"face_row": np.zeros(15, dtype=np.float32)},
            {"face_row": np.ones(15, dtype=np.float32)},
        ]
        response = self._post_capture(detections=multiple_faces)

        self.assertEqual(response.status_code, 422)
        self.assertIn("More than one face", response.json()["message"])
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.NOT_MATCHED,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_unknown_face_is_logged_and_does_not_record_attendance(self):
        response = self._post_capture(
            embedding=np.array([-1.0, 0.0, 0.0], dtype=np.float32)
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.NOT_MATCHED,
        )
        self.assertIsNone(FaceVerificationLog.objects.get().employee_id)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_ambiguous_match_is_logged_without_selecting_an_employee(self):
        other_employee = Employee.objects.create(
            employee_id="KSK-EMP-002",
            full_name='Jose Lopes',
            hire_date=self.today - timedelta(days=200),
        )
        self._create_template(
            other_employee,
            np.array([0.99, 0.1, 0.0], dtype=np.float32),
        )

        response = self._post_capture()

        self.assertEqual(response.status_code, 422)
        verification = FaceVerificationLog.objects.get()
        self.assertEqual(verification.match_result, FaceVerificationLog.MatchResult.AMBIGUOUS)
        self.assertIsNone(verification.employee_id)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_inactive_employee_is_blocked(self):
        self.employee.employment_status = Employee.EmploymentStatus.INACTIVE
        self.employee.save(update_fields=["employment_status"])

        response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "employee")
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.MATCHED,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_inactive_device_is_blocked_and_logged(self):
        self.device.status = AttendanceDevice.Status.INACTIVE
        self.device.save(update_fields=["status"])

        response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "device")
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.ERROR,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_missing_valid_schedule_is_blocked(self):
        EmployeeSchedule.objects.filter(pk=self.schedule.pk).update(
            work_date=self.today - timedelta(days=3),
            scheduled_start_at=self.now - timedelta(hours=5),
            scheduled_end_at=self.now - timedelta(hours=1),
        )

        response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertEqual(FaceVerificationLog.objects.get().match_result, "matched")

    def test_check_in_saves_record_event_and_verification_log(self):
        response = self._post_capture()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["action"], "check_in")
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertIsNotNone(record.first_check_in_at)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)
        event = AttendanceEvent.objects.get(attendance_record=record)
        self.assertEqual(event.event_type, AttendanceEvent.EventType.CHECK_IN)
        self.assertEqual(event.source, AttendanceEvent.Source.FACE_RECOGNITION)
        self.assertEqual(event.verification_log.match_result, FaceVerificationLog.MatchResult.MATCHED)
        self.assertEqual(
            FaceVerificationLog.objects.get().liveness_result,
            FaceVerificationLog.LivenessResult.PASSED,
        )

    def _four_events(self):
        offsets = (0, 30, 90, 180)  # minutes after the first scan
        return [self._post_capture(now=self.now + timedelta(minutes=m)) for m in offsets]

    def test_four_scans_record_the_four_events_in_order(self):
        responses = self._four_events()

        for response in responses:
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [r.json()["action"] for r in responses],
            ["check_in", "break_start", "break_end", "check_out"],
        )
        self.assertEqual(responses[0].json()["next_action_label"], "Break Out")
        self.assertTrue(responses[3].json()["session_complete"])
        record = AttendanceRecord.objects.get(schedule=self.schedule)
        self.assertIsNotNone(record.first_check_in_at)
        self.assertIsNotNone(record.last_check_out_at)
        self.assertEqual(
            list(
                AttendanceEvent.objects.filter(attendance_record=record)
                .order_by("occurred_at")
                .values_list("event_type", flat=True)
            ),
            [
                AttendanceEvent.EventType.CHECK_IN,
                AttendanceEvent.EventType.BREAK_START,
                AttendanceEvent.EventType.BREAK_END,
                AttendanceEvent.EventType.CHECK_OUT,
            ],
        )
        # 30 minutes before the break + 90 minutes after it; the 60 minute break is not work.
        self.assertEqual(record.total_work_minutes, 30 + 90)

    def test_scan_after_all_four_events_is_rejected_as_duplicate(self):
        self._four_events()

        duplicate = self._post_capture(now=self.now + timedelta(minutes=181 + 60))

        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["stage"], "duplicate")
        self.assertEqual(AttendanceEvent.objects.count(), 4)
        self.assertEqual(AttendanceRecord.objects.count(), 1)

    def test_verification_log_is_saved_for_successful_attendance(self):
        response = self._post_capture()

        self.assertEqual(response.status_code, 200)
        verification = FaceVerificationLog.objects.get()
        self.assertEqual(verification.employee, self.employee)
        self.assertEqual(verification.device, self.device)
        self.assertEqual(verification.schedule, self.schedule)
        self.assertEqual(verification.event_type, FaceVerificationLog.EventType.CHECK_IN)

    def test_policy_requires_liveness_but_no_liveness_engine_blocks_attendance(self):
        from unittest.mock import patch
        AttendancePolicy.objects.filter(office=self.office).update(require_liveness=True)

        with patch(
            "attendance.services.face_liveness.check_face_liveness",
            return_value={
                "result": FaceVerificationLog.LivenessResult.INCONCLUSIVE,
                "score": None,
                "reason": "simulated missing PAD provider",
                "model_version": "",
            },
        ):
            response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "liveness")
        self.assertEqual(response.json()["liveness_result"], "inconclusive")
        self.assertEqual(
            FaceVerificationLog.objects.get().liveness_result,
            FaceVerificationLog.LivenessResult.INCONCLUSIVE,
        )
        self.assertFalse(AttendanceEvent.objects.exists())
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_engine_failure_is_logged_without_attendance(self):
        response = self._post_capture(
            engine_failure=FaceEngineError("SFace engine unavailable.")
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["stage"], "engine")
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.ERROR,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_encryption_failure_is_logged_without_attendance(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self._post_capture()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["stage"], "engine")
        self.assertEqual(
            FaceVerificationLog.objects.get().match_result,
            FaceVerificationLog.MatchResult.ERROR,
        )
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_event_failure_rolls_back_log_and_attendance_record(self):
        with patch.object(
            AttendanceEvent,
            "save",
            side_effect=ValidationError("Simulated event persistence failure."),
        ):
            response = self._post_capture()

        self.assertEqual(response.status_code, 500)
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(AttendanceEvent.objects.exists())
        self.assertFalse(FaceVerificationLog.objects.exists())

    def test_today_attendance_shows_schedule_derived_fields_and_filters(self):
        AttendanceRecord.objects.create(
            schedule=self.schedule,
            first_check_in_at=self.now,
            late_minutes=4,
            status=AttendanceRecord.Status.LATE,
        )

        response = self.client.get(
            reverse("attendance:today"),
            {
                "start_date": self.today.isoformat(),
                "end_date": self.today.isoformat(),
                "office_id": self.office.pk,
                "employee_id": self.employee.pk,
                "status": AttendanceRecord.Status.LATE,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.employee_id)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, self.office.office_name)
        self.assertContains(response, self.shift.name)
        self.assertContains(response, "Scheduled Time")
        self.assertContains(response, "Check-in Time")
        self.assertContains(response, "Check-out Time")
        self.assertContains(response, "Late Minutes")
        self.assertContains(response, "Attendance Status")

    def test_wrong_office_device_is_rejected(self):
        """Selecting a device in a different office than the employee's assignment must be rejected."""
        other_office = Office.objects.create(
            office_code="KSK-OTHER",
            office_name="Other Office",
        )
        other_device = AttendanceDevice.objects.create(
            office=other_office,
            device_code="KSK-DEV-OTHER",
            device_name="Other Device",
            device_type=AttendanceDevice.DeviceType.KIOSK,
            status=AttendanceDevice.Status.ACTIVE,
        )

        response = self._post_capture(device_id=other_device.pk)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceRecord.objects.exists())
        verification = FaceVerificationLog.objects.get()
        self.assertEqual(verification.match_result, FaceVerificationLog.MatchResult.MATCHED)
        self.assertIn("office", verification.failure_reason.lower())

    def test_expired_assignment_is_rejected(self):
        """An assignment whose end_date is in the past must be rejected."""
        self.assignment.end_date = self.today - timedelta(days=1)
        self.assignment.save(update_fields=["end_date"])

        response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_unpublished_schedule_is_rejected(self):
        """A schedule that is not in SCHEDULED status must be rejected."""
        self.schedule.status = EmployeeSchedule.Status.PLANNED
        self.schedule.save(update_fields=["status"])

        response = self._post_capture()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_outside_shift_window_is_rejected(self):
        """Attempting to check in before the shift window opens must be rejected."""
        from django.utils import timezone
        before_opens = self.schedule.scheduled_start_at - timedelta(hours=5)
        response = self._post_capture(now=before_opens)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["stage"], "schedule")
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_device_selection_validation(self):
        """device_id must be provided and must refer to an existing device."""
        # Missing device_id
        response = self.client.post(
            reverse("attendance:kiosk-verify"),
            {"image": self._encoded_image()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["stage"], "device")

        # Non-existent device
        response = self.client.post(
            reverse("attendance:kiosk-verify"),
            {"device_id": 99999, "image": self._encoded_image()},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["stage"], "device")

        # Non-integer device_id
        response = self.client.post(
            reverse("attendance:kiosk-verify"),
            {"device_id": "not-a-number", "image": self._encoded_image()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["stage"], "device")

        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(FaceVerificationLog.objects.exists())
