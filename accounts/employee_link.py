from django.contrib.auth import get_user_model
from employees.models import get_linked_employee


def _employee_profile(self):
    return get_linked_employee(self)


# Attach as a normal instance attribute-like property on the user model class.
# It returns Employee or None and does not raise RelatedObjectDoesNotExist.
get_user_model().employee_profile = property(_employee_profile)
