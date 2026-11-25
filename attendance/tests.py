import base64
import json
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import cv2
import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audittrail.models import AuditLog
from employees.models import Employee, EmployeeAssignment
from organization.models import Office, Position

from .models import (
    AttendanceDevice,
    AttendanceRecord,
    EmployeeSchedule,
    FaceTemplate,
    FaceVerificationLog,
    Shift,
)


class WorkSchedulePageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="schedule-admin",
            email="schedule-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)
        self.office = Office.objects.create(
            office_code="DIL",
            office_name="Dili Office",
        )
        self.employee = Employee.objects.create(
            employee_id="EMP-001",
            full_name="Maria",
            hire_date=date(2024, 1, 1),
        )
        self.position = Position.objects.create(
            office=self.office,
            position_code="ADM",
            position_title="Administrator",
        )
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.employee,
            position=self.position,
            start_date=date(2024, 1, 1),
        )
        self.shift = Shift.objects.create(
            office=self.office,
            shift_code="DAY",
            name="Day shift",
            start_time=time(9),
            end_time=time(17),
        )

    def test_shift_list_and_detail_render(self):
        list_response = self.client.get(reverse("attendance:shift-list"))
        detail_response = self.client.get(
            reverse("attendance:shift-detail", kwargs={"pk": self.shift.pk})
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Day shift")
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Dili Office")

    def test_night_shift_midnight_flag_is_derived_not_entered(self):
        response = self.client.post(
            reverse("attendance:shift-create"),
            {
                "office": self.office.pk,
                "shift_code": "NIGHT",
                "name": "Night shift",
                "start_time": "22:00",
                "break_start_time": "01:00",
                "break_end_time": "02:00",
                "end_time": "06:00",
                "grace_period_minutes": 0,
                "early_checkin_window_minutes": 60,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302, response.content[:300])
        night = Shift.objects.get(shift_code="NIGHT")
        self.assertTrue(night.crosses_midnight)
        self.assertEqual(night.break_duration_minutes, 60)

    def test_shift_without_break_times_is_rejected(self):
        response = self.client.post(
            reverse("attendance:shift-create"),
            {
                "office": self.office.pk,
                "shift_code": "NOBREAK",
                "name": "No break",
                "start_time": "08:00",
                "end_time": "16:00",
                "grace_period_minutes": 0,
                "early_checkin_window_minutes": 60,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Shift.objects.filter(shift_code="NOBREAK").exists())

    def test_schedule_rejects_shift_from_another_office(self):
        other_office = Office.objects.create(
            office_code="BAU",
            office_name="Baucau Office",
        )
        other_shift = Shift.objects.create(
            office=other_office,
            shift_code="OTHER",
            name="Other shift",
            start_time=time(9),
            end_time=time(17),
            break_start_time=time(12),
            break_end_time=time(13),
        )

        response = self.client.post(
            reverse("attendance:schedule-create"),
            {
                "office": self.office.pk,
                "employee_assignment": self.assignment.pk,
                "shift": other_shift.pk,
                "work_date": "2025-04-01",
                "status": "scheduled",
                "notes": "",
                "action": "save",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "different office")
        self.assertFalse(EmployeeSchedule.objects.exists())

    def test_scheduled_shift_can_be_cancelled_with_post(self):
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=self.assignment,
            shift=self.shift,
            work_date=date(2025, 4, 1),
            scheduled_start_at=timezone.make_aware(datetime(2025, 4, 1, 9)),
            scheduled_end_at=timezone.make_aware(datetime(2025, 4, 1, 17)),
        )
        cancel_url = reverse(
            "attendance:schedule-cancel", kwargs={"pk": schedule.pk}
        )
        self.assertContains(
            self.client.get(reverse("attendance:schedule-list")),
            cancel_url,
        )
        self.assertContains(
            self.client.get(reverse("attendance:schedule-detail", kwargs={"pk": schedule.pk})),
            cancel_url,
        )
        self.assertEqual(self.client.get(cancel_url).status_code, 200)

        response = self.client.post(
            cancel_url
        )

        self.assertRedirects(
            response, reverse("attendance:schedule-detail", kwargs={"pk": schedule.pk})
        )
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, EmployeeSchedule.Status.CANCELLED)

    def test_schedule_linked_to_attendance_cannot_be_cancelled(self):
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=self.assignment,
            shift=self.shift,
            work_date=date(2025, 4, 1),
            scheduled_start_at=timezone.make_aware(datetime(2025, 4, 1, 9)),
            scheduled_end_at=timezone.make_aware(datetime(2025, 4, 1, 17)),
        )
        AttendanceRecord.objects.create(schedule=schedule)

        self.client.post(
            reverse("attendance:schedule-cancel", kwargs={"pk": schedule.pk})
        )

        schedule.refresh_from_db()
        self.assertEqual(schedule.status, EmployeeSchedule.Status.SCHEDULED)

    def test_unused_shift_can_be_deleted_with_audit(self):
        url = reverse("attendance:shift-delete", kwargs={"pk": self.shift.pk})

        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Day shift")

        response = self.client.post(url)

        self.assertRedirects(response, reverse("attendance:shift-list"))
        self.assertFalse(Shift.objects.filter(pk=self.shift.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.DELETE,
                entity_type="Shift",
                object_id=str(self.shift.pk),
            ).exists()
        )

    def test_shift_with_schedule_cannot_be_deleted(self):
        schedule = EmployeeSchedule.objects.create(
            employee_assignment=self.assignment,
            shift=self.shift,
            work_date=date(2025, 4, 1),
            scheduled_start_at=timezone.make_aware(datetime(2025, 4, 1, 9)),
            scheduled_end_at=timezone.make_aware(datetime(2025, 4, 1, 17)),
        )

        response = self.client.post(
            reverse("attendance:shift-delete", kwargs={"pk": self.shift.pk})
        )

        self.assertRedirects(response, reverse("attendance:shift-list"))
        self.assertTrue(Shift.objects.filter(pk=self.shift.pk).exists())
        self.assertTrue(EmployeeSchedule.objects.filter(pk=schedule.pk).exists())

    def test_unused_device_can_be_deleted(self):
        device = AttendanceDevice.objects.create(
            office=self.office,
            device_code="DIL-KIOSK-03",
            device_name="Unused kiosk",
        )

        response = self.client.post(
            reverse("attendance:device-delete", kwargs={"pk": device.pk})
        )

        self.assertRedirects(response, reverse("attendance:device-list"))
        self.assertFalse(AttendanceDevice.objects.filter(pk=device.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                entity_type="AttendanceDevice",
                object_id=str(device.pk),
                action=AuditLog.Action.DELETE,
            ).exists()
        )

    def test_device_deactivation_preserves_verification_history(self):
        device = AttendanceDevice.objects.create(
            office=self.office,
            device_code="DIL-KIOSK-01",
            device_name="Main kiosk",
        )
        verification = FaceVerificationLog.objects.create(
            device=device,
            event_type=FaceVerificationLog.EventType.OTHER,
            match_result=FaceVerificationLog.MatchResult.ERROR,
            occurred_at=timezone.now(),
        )
        delete_response = self.client.post(
            reverse("attendance:device-delete", kwargs={"pk": device.pk})
        )
        self.assertRedirects(delete_response, reverse("attendance:device-list"))
        self.assertTrue(AttendanceDevice.objects.filter(pk=device.pk).exists())
        self.assertTrue(FaceVerificationLog.objects.filter(pk=verification.pk).exists())
        url = reverse("attendance:device-deactivate", kwargs={"pk": device.pk})

        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Main kiosk")

        response = self.client.post(url)

        self.assertRedirects(response, reverse("attendance:device-list"))
        device.refresh_from_db()
        self.assertEqual(device.status, AttendanceDevice.Status.INACTIVE)
        self.assertTrue(FaceVerificationLog.objects.filter(pk=verification.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                entity_type="AttendanceDevice",
                object_id=str(device.pk),
                action=AuditLog.Action.UPDATE,
            ).exists()
        )

    def test_device_deactivation_requires_superuser_office_scope(self):
        device = AttendanceDevice.objects.create(
            office=self.office,
            device_code="DIL-KIOSK-02",
            device_name="Reception kiosk",
        )
        user = get_user_model().objects.create_user(username="device-scoped-user")
        from django.contrib.auth.models import Permission

        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="attendance",
                codename="change_attendancedevice",
            )
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("attendance:device-deactivate", kwargs={"pk": device.pk})
        )

        self.assertEqual(response.status_code, 403)
        device.refresh_from_db()
        self.assertEqual(device.status, AttendanceDevice.Status.ACTIVE)

    def test_face_template_revoke_is_confirmed_and_audited_without_template_data(self):
        template = FaceTemplate.objects.create(
            employee=self.employee,
            provider="test",
            encrypted_template=b"private-encrypted-payload",
            model_version="test-model",
            status=FaceTemplate.Status.ACTIVE,
        )
        url = reverse("attendance:face-revoke", kwargs={"pk": template.pk})

        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "encrypted biometric data")

        response = self.client.post(url)

        self.assertRedirects(
            response,
            reverse("attendance:face-template-detail", kwargs={"pk": template.pk}),
        )
        template.refresh_from_db()
        self.assertEqual(template.status, FaceTemplate.Status.REVOKED)
        self.assertEqual(bytes(template.encrypted_template), b"private-encrypted-payload")
        audit_entry = AuditLog.objects.get(
            entity_type="FaceTemplate",
            object_id=str(template.pk),
            action=AuditLog.Action.REVOKE_FACE,
        )
        self.assertNotIn("template", str(audit_entry.before_data).casefold())
        self.assertNotIn("encrypted", str(audit_entry.after_data).casefold())

    def test_device_form_and_page_do_not_expose_credentials(self):
        device = self.client.get(reverse("attendance:device-create"))

        self.assertEqual(device.status_code, 200)
        self.assertNotContains(device, "credential_hash")
        self.assertNotContains(device, "credential")

    def test_face_enrollment_reports_runtime_status_truthfully(self):
        response = self.client.get(reverse("attendance:face-enrollment"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select employee")
        self.assertContains(response, "Start camera")
        self.assertContains(response, "Backend status")
        self.assertContains(response, "startCameraBtn.addEventListener('click', startCamera)")
        self.assertContains(response, "Requesting camera permission")
        self.assertContains(response, "capture: { step, image }")
        self.assertContains(response, "challenge.next_step = data.next_step")
        self.assertContains(response, "data-capture-slot=\"center\"")
        self.assertContains(response, "data-capture-slot=\"left\"")
        self.assertContains(response, "data-capture-slot=\"right\"")

    @staticmethod
    def _face_capture(step, image_data_url):
        return {"step": step, "image": image_data_url}

    def _set_enrollment_challenge(self, *, expires_at=None):
        session = self.client.session
        session["face_enrollment_challenge"] = {
            "id": "test-challenge",
            "employee_id": self.employee.pk,
            "user_id": self.user.pk,
            "sequence": ["center", "left", "right"],
            "expires_at": (expires_at or timezone.now() + timedelta(minutes=2)).isoformat(),
            "used": False,
            "accepted_steps": [],
        }
        session.save()
        self.challenge_id = "test-challenge"

    def _start_enrollment_challenge(self):
        response = self.client.post(
            reverse("attendance:face-enrollment-challenge"),
            data=json.dumps({"employee_id": self.employee.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sequence"], ["center", "left", "right"])
        self.challenge_id = response.json()["challenge_id"]
        return response.json()

    def _encoded_test_image(self, pixel_value=127):
        success, encoded = cv2.imencode(".png", np.full((200, 200, 3), pixel_value, dtype=np.uint8))
        self.assertTrue(success)
        return f"data:image/png;base64,{base64.b64encode(encoded.tobytes()).decode('ascii')}"

    def _submit_capture(self, step, image_data_url):
        payload = {
            "employee_id": self.employee.pk,
            "challenge_id": self.challenge_id,
            "capture": self._face_capture(step, image_data_url),
        }
        return self.client.post(
            reverse("attendance:face-enrollment-submit"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    @staticmethod
    def _face_detection(yaw, x=60):
        row = np.array(
            [x, 60, 80, 80, 80, 100, 120, 100, 100 + yaw * 40, 120, 90, 145, 110, 145, 0.91],
            dtype=np.float32,
        )
        return {"bbox": (x, 60, 80, 80), "confidence": 0.91, "face_row": row}

    def test_face_enrollment_accepts_center_left_right_in_order_and_persists(self):
        self._start_enrollment_challenge()
        images = {
            "center": self._encoded_test_image(20),
            "left": self._encoded_test_image(80),
            "right": self._encoded_test_image(140),
        }
        detected_frames = []
        detections = [self._face_detection(0), self._face_detection(-0.2), self._face_detection(0.2)]

        def detect_frame(frame):
            detected_frames.append(frame)
            return [detections[len(detected_frames) - 1]]

        with patch("attendance.views.FaceEngine.detect_faces", side_effect=detect_frame), patch(
            "attendance.views.build_enrollment_embedding", return_value=np.array([0.1, 0.2], dtype=np.float32)
        ), patch("attendance.views.encrypt_embedding", return_value=b"encrypted-template"):
            center = self._submit_capture("center", images["center"])
            left = self._submit_capture("left", images["left"])
            right = self._submit_capture("right", images["right"])

        self.assertEqual(center.status_code, 200)
        self.assertEqual(center.json()["next_step"], "left")
        self.assertEqual(left.status_code, 200)
        self.assertEqual(left.json()["next_step"], "right")
        self.assertEqual(right.status_code, 200)
        self.assertTrue(right.json()["complete"])
        self.assertEqual([center.json()["step"], left.json()["step"], right.json()["step"]], ["center", "left", "right"])
        self.assertEqual(len(detected_frames), 3)
        self.assertTrue(all(frame.shape == (200, 200, 3) for frame in detected_frames))
        self.assertEqual([int(frame[0, 0, 0]) for frame in detected_frames], [20, 80, 140])
        template = FaceTemplate.objects.get(employee=self.employee, status=FaceTemplate.Status.ACTIVE)
        self.assertEqual(bytes(template.encrypted_template), b"encrypted-template")

    def test_rejected_left_frame_does_not_advance_and_retry_uses_new_frame(self):
        self._start_enrollment_challenge()
        detected_frames = []

        def detect_frame(frame):
            detected_frames.append(frame)
            yaw_by_pixel = {20: 0, 60: 0, 100: -0.2}
            return [self._face_detection(yaw_by_pixel[int(frame[0, 0, 0])])]

        with patch("attendance.views.FaceEngine.detect_faces", side_effect=detect_frame):
            center = self._submit_capture("center", self._encoded_test_image(20))
            rejected_left = self._submit_capture("left", self._encoded_test_image(60))
            session_after_rejection = self.client.session["face_enrollment_challenge"]
            accepted_left = self._submit_capture("left", self._encoded_test_image(100))

        self.assertTrue(center.json()["accepted"])
        self.assertEqual(rejected_left.status_code, 400)
        self.assertIn("nose shifts toward the left", rejected_left.json()["error"])
        self.assertEqual(session_after_rejection["accepted_steps"], ["center"])
        self.assertEqual(accepted_left.status_code, 200)
        self.assertEqual(accepted_left.json()["next_step"], "right")
        self.assertEqual([int(frame[0, 0, 0]) for frame in detected_frames], [20, 60, 100])

    def test_failed_final_embedding_does_not_complete_or_advance_challenge(self):
        self._start_enrollment_challenge()
        detections = [self._face_detection(0), self._face_detection(-0.2), self._face_detection(0.2)]
        with patch("attendance.views.FaceEngine.detect_faces", side_effect=[[item] for item in detections]), patch(
            "attendance.views.build_enrollment_embedding", side_effect=RuntimeError("Embedding extraction failed.")
        ):
            self.assertEqual(self._submit_capture("center", self._encoded_test_image(20)).status_code, 200)
            self.assertEqual(self._submit_capture("left", self._encoded_test_image(80)).status_code, 200)
            rejected_right = self._submit_capture("right", self._encoded_test_image(140))

        self.assertEqual(rejected_right.status_code, 400)
        self.assertIn("Embedding extraction failed", rejected_right.json()["error"])
        self.assertEqual(
            self.client.session["face_enrollment_challenge"]["accepted_steps"],
            ["center", "left"],
        )
        self.assertFalse(FaceTemplate.objects.exists())

    def test_face_enrollment_rejects_zero_faces_with_specific_message(self):
        self._start_enrollment_challenge()
        with patch("attendance.views.FaceEngine.detect_faces", return_value=[]):
            response = self._submit_capture("center", self._encoded_test_image())

        self.assertEqual(response.status_code, 400)
        self.assertIn("No face was detected in the center", response.json()["error"])
        self.assertFalse(FaceTemplate.objects.exists())

    def test_face_enrollment_rejects_multiple_faces_with_specific_message(self):
        self._start_enrollment_challenge()
        detections = [
            self._face_detection(0),
            self._face_detection(0.2, x=120),
        ]
        with patch("attendance.views.FaceEngine.detect_faces", return_value=detections):
            response = self._submit_capture("center", self._encoded_test_image())

        self.assertEqual(response.status_code, 400)
        self.assertIn("Multiple faces were detected in the center", response.json()["error"])
        self.assertFalse(FaceTemplate.objects.exists())

    def test_face_enrollment_rejects_invalid_image_data(self):
        self._start_enrollment_challenge()
        response = self._submit_capture("center", "data:image/png;base64,not-valid-base64")

        self.assertEqual(response.status_code, 400)
        self.assertIn("malformed base64 image data", response.json()["error"])
        self.assertFalse(FaceTemplate.objects.exists())

    def test_expired_challenge_is_rejected_before_processing_frame(self):
        self._set_enrollment_challenge(expires_at=timezone.now() - timedelta(seconds=1))
        with patch("attendance.views.FaceEngine.detect_faces") as detect_faces:
            response = self._submit_capture("center", self._encoded_test_image())

        self.assertEqual(response.status_code, 400)
        self.assertIn("challenge has expired", response.json()["error"])
        detect_faces.assert_not_called()

    def test_out_of_order_step_does_not_advance_challenge(self):
        self._start_enrollment_challenge()
        response = self._submit_capture("left", self._encoded_test_image())

        self.assertEqual(response.status_code, 400)
        self.assertIn("next required challenge frame is center", response.json()["error"])
        self.assertEqual(self.client.session["face_enrollment_challenge"]["accepted_steps"], [])
