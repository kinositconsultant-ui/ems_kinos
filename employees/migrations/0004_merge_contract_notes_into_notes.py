"""Data-only migration: preserve contract_notes (and non-derivable values of the
soon-to-be-removed contract fields) inside ``notes`` before 0005 drops the columns.

Nothing is overwritten: existing ``notes`` text is always kept and labelled.
"""
from django.db import migrations

MARKER = "[Contract notes - migrated]"
ARCHIVE_MARKER = "[Archived contract attributes]"


def merge_contract_notes(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    alias = schema_editor.connection.alias

    for employee in Employee.objects.using(alias).all().order_by("pk").iterator():
        notes = (employee.notes or "").strip()
        contract_notes = (employee.contract_notes or "").strip()

        archived = []
        probation = (employee.probation_status or "").strip()
        if probation:
            archived.append(f"Probation status: {probation}")
        contract_type = (employee.contract_type or "").strip()
        # "permanent" is the column default and is already covered by employment_type.
        if contract_type and contract_type != "permanent":
            archived.append(f"Contract type: {contract_type}")
        # active/expired/terminated are derivable now; "draft" is not.
        if (employee.contract_status or "").strip() == "draft":
            archived.append("Contract status: draft")

        if not contract_notes and not archived:
            continue
        if MARKER in notes or ARCHIVE_MARKER in notes:
            continue  # already migrated; keep the migration idempotent

        if contract_notes and notes:
            merged = f"[Notes]\n{notes}\n\n{MARKER}\n{contract_notes}"
        elif contract_notes:
            merged = f"{MARKER}\n{contract_notes}"
        else:
            merged = notes
        if archived:
            block = ARCHIVE_MARKER + "\n" + "\n".join(f"- {line}" for line in archived)
            merged = f"{merged}\n\n{block}" if merged else block

        Employee.objects.using(alias).filter(pk=employee.pk).update(notes=merged)


def noop_reverse(apps, schema_editor):
    # The merged text intentionally stays in ``notes``; it cannot be split back safely.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0003_employeecertification"),
    ]

    operations = [
        migrations.RunPython(merge_contract_notes, noop_reverse),
    ]
