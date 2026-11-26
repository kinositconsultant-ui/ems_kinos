# Attendance System

This Django application manages a single institution with multiple offices and departments. It includes employee assignments, attendance, requests, approvals, notifications, and audit logs. The apps and their migrations are already in this repository. The project uses Django's built-in `auth.User`.

## Local development on Windows

Use a separate checkout for a **new local database**. Run these commands from the directory containing `manage.py`. The dependency baseline is 64-bit CPython 3.14 on Windows. Use that interpreter to create the project's `.venv` and select it in your IDE. Confirm which interpreter a launcher or scheduled job uses before relying on the installed packages.

```powershell
python -VV
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item .env.example .env
```

Run the copy command only when `.env` is absent. Edit the ignored `.env` locally or supply the values in the process environment. Existing process values take precedence.

- `SECRET_KEY` is required for Django startup. Generate a local value with Django's `get_random_secret_key()`.
- `FACE_TEMPLATE_ENCRYPTION_KEY` is required for face-template encryption and decryption. For a fresh local database, generate a local Fernet key with `Fernet.generate_key()`. Preserve the corresponding key for any existing enrolled templates.

Use synthetic development data. Django uses `db.sqlite3` and `media/` in the checkout. Verify that the checkout has its own empty database and private local media storage before initializing it. Apply the migrations already supplied with the source.

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Visit `http://localhost:8000/admin/` to configure the new database.

## Local face model setup

Run `.\.venv\Scripts\python.exe scripts/download_face_models.py` to provision YuNet and SFace in `models/face_recognition/`. See [model sources and attribution](models/face_recognition/README.md).

Liveness requires ONNX Runtime from `requirements.txt` and a separately provisioned `models/face_liveness/minifasnet_v2.onnx`. The downloader only provisions the two recognition files. The exact liveness ONNX export, its acquisition instructions, and its provenance are unresolved. Preserve local model files and backups outside Git.

## Production deployment

The setup above uses the development server and local SQLite database. Production requires separate validation of the target OS, architecture, Python wheels and system libraries, settings, HTTPS, database and media storage, backups, scheduled jobs, model provenance, and face liveness before use. A clean installation was verified on Windows x64 with CPython 3.14.8. Target hosting compatibility remains unverified.

## First setup order in Django Admin

1. Create the single `InstitutionProfile` record.
2. Create the headquarters office, then branch offices. Choose `parent_office` to show their reporting hierarchy.
3. Create departments for each office.
4. Create positions, select their office/department, and set `reports_to_position` according to the institution's actual organizational chart.
5. Create Users and Employees.
6. Assign each employee to a Position with an `EmployeeAssignment` and set the correct dates.
7. Create Django Groups for model permissions. Office scope also depends on the relevant view and role-specific scope rules.
8. Configure shifts and attendance policies, then create schedules.

## Important implementation notes

- Model `clean()` methods are validation helpers; calling `.save()` directly does not automatically execute `full_clean()`. Services/forms should run validation, and database constraints should protect rules that can be expressed at the database level.
- Django Groups grant model permissions. Office scope is enforced by the relevant views and helpers in `core/permissions.py`, including branch HR office resolution. A Group by itself does not restrict data to one branch.
- Create a Group called `direktur_utama` with view/report permissions only. Do not give it request approval or request-processing permissions. The request-processing service/view must explicitly reject this Group even if someone attempts to call the endpoint directly. This rule is not fully enforced by database models alone.
- `AttendanceEvent` is intended as a controlled event log. Create it only through a backend service after a successful, trusted verification or an approved correction. The model's `clean()` is not a substitute for checking that service flow.
- `FaceTemplate` stores an encrypted template or an external provider reference; do not store raw face images by default. The face-recognition engine and the experimental MiniFASNetV2 liveness provider are separate integrations; the liveness provider is uncalibrated, must be security-tested, and must never be treated as a certified presentation-attack defence.
- In production, review file-upload security, biometric consent/retention, backups, encryption/key management, audit-log protection, rate limiting, device authentication, and applicable Timor-Leste privacy/employment requirements.

## Application interfaces

- The Face Attendance Kiosk is available at `/attendance/kiosk/` (`attendance:kiosk`). A signed-in user needs attendance-view permissions and the required attendance mutation permissions; configure an active device, encrypted active face templates, the encryption key, a valid schedule, and an applicable attendance policy. The browser requests camera-only access over HTTPS or localhost, and sends a single temporary JPEG to the backend. YuNet/SFace matching determines the employee; the backend then records check-in or check-out against the matching schedule.
- Liveness/anti-spoofing is **mandatory for all face attendance**. Every face-based attendance check-in or event accepted through the existing face-recognition flow must pass liveness/PAD verification before it is recorded as a successful face attendance event. This includes kiosk face attendance and mobile face attendance. The `require_liveness` field on the `AttendancePolicy` model is enforced at the runtime level: a policy's `require_liveness` value of `False` does not allow face attendance to bypass PAD. MiniFASNetV2 ONNX provider (`attendance/services/face_liveness.py`) runs a single-frame presentation-attack check that follows the upstream Silent-Face-Anti-Spoofing pipeline: raw BGR values in `[0, 255]`, the upstream boundary-adjusted shifted 2.7x crop resized to 80x80, a stable softmax over the raw logits, and acceptance only when class `1` (the bona-fide class) is the highest-scoring class. It is an additional gate, not a certification: the detection threshold is a provisional development value, `production_validated` is reported as `false`, and the result must be calibrated on representative live faces, printed photos, and screen replays before it is relied upon. An `inconclusive` or missing PAD result is treated as unverified — face attendance is rejected, not silently permitted. Attendance events are never recorded as system-created after face matching when liveness is required; verification history records the actual PAD outcome.
- Today's Attendance and Attendance History show persisted attendance records through their schedule, assignment, employee, shift, and office relations. Today's Attendance supports date, office, employee, and attendance-status filters.
- The attendance pages provide read-only daily, monthly, office, and employee reports with date, office, employee, and status filters. The report view implements CSV and XLSX exports.
- The audit page is permission-gated and read-only. Its interface does not display before/after snapshots; the `audittrail.services.log_action()` helper filters sensitive snapshot keys, and request approval decisions are recorded in the same transaction as the decision.
- User accounts are listed and created in the custom account interface. Deactivation is available to superusers and preserves the User row and audit history; Django Admin deletion is disabled. Active Super Admins can create groups, rename groups, and assign or remove existing permissions through Users & Access → Groups & Permissions in the main application.
- Approval steps must be assigned to approvers by a separately defined workflow. Request creation intentionally does not guess approvers or create approval steps automatically.

## Safe removal and retention

- Institution profiles, employee assignments, attendance schedules/records/events, face verification logs/templates, request approvals/processed requests, notifications, and audit logs have no general-purpose Delete action. Existing schedule cancellation and face-template revocation are explicit, audited status changes; biometric ciphertext is retained on revoke and is not included in the audit snapshot.
- Unused Departments, Positions, Shifts, Attendance Devices, and Leave Types can be permanently deleted after an object-specific confirmation. Foreign-key references are checked and protected; used organization records, shifts, devices, and leave types must instead be deactivated. Offices are deletable only when no related or audit-history records refer to them. The singleton InstitutionProfile cannot be deleted.
- Employees and user accounts are deactivated rather than deleted. Pending requests can be cancelled only by their submitter before approval steps exist. Active attendance policies and holidays are deactivated so their historical context remains available.
- The project has no `UserRoleAssignment` model. Where a view uses `require_office_scoped_admin()`, that helper restricts the operation to superusers.
