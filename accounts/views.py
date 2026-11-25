from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Count
from django.urls import reverse, reverse_lazy
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from audittrail.models import AuditLog
from audittrail.services import log_action

from .forms import UserCreateForm, UserUpdateForm

User = get_user_model()


class SystemLoginView(LoginView):
    """Halaman login sistem (tema AdminLTE), pengganti login lewat /admin/."""

    template_name = "adminlte/auth/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        # Checkbox "Remember Me": jika tidak dicentang, sesi berakhir saat browser ditutup.
        if not self.request.POST.get("remember"):
            self.request.session.set_expiry(0)
        return response


class UserPermissionMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Belum login -> diarahkan ke /login/. Sudah login tapi tanpa izin -> 403."""


class UserListView(UserPermissionMixin, ListView):
    permission_required = "auth.view_user"
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 20

    def get_queryset(self):
        queryset = User.objects.prefetch_related("groups", "employee_account_link__employee").order_by("username")
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(username__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(email__icontains=query)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["search_query"] = self.request.GET.get("q", "")
        context["querystring"] = params.urlencode()
        context["can_create"] = self.request.user.has_perm("auth.add_user")
        context["can_deactivate"] = (
            self.request.user.is_superuser
            and self.request.user.has_perm("auth.change_user")
        )
        context["can_edit_users"] = (
            self.request.user.is_superuser
            and self.request.user.has_perm("auth.change_user")
        )
        return context


class UserCreateView(UserPermissionMixin, CreateView):
    permission_required = "auth.add_user"
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user-list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["back_url"] = reverse("user-list")
        context["page_title"] = "Add User"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            log_action(
                actor_user=self.request.user,
                action=AuditLog.Action.CREATE,
                entity_type="User",
                object_id=self.object.pk,
                after_data={
                    "username": self.object.username,
                    "email": self.object.email,
                    "is_active": self.object.is_active,
                    "groups": sorted(group.name for group in form.cleaned_data["groups"]),
                },
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
        messages.success(self.request, f'User "{self.object.username}" was created.')
        return response


class UserUpdateView(UserPermissionMixin, UpdateView):
    permission_required = "auth.change_user"
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user-list")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            raise PermissionDenied(
                "Only a superuser can edit user accounts."
            )
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        target = super().get_object(queryset)
        if target.is_superuser:
            raise PermissionDenied(
                "Privileged superuser accounts cannot be edited here."
            )
        return target

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["back_url"] = reverse("user-list")
        context["page_title"] = "Edit user"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            log_action(
                actor_user=self.request.user,
                action=AuditLog.Action.UPDATE,
                entity_type="User",
                object_id=self.object.pk,
                after_data={
                    "username": self.object.username,
                    "email": self.object.email,
                    "is_active": self.object.is_active,
                    "groups": sorted(group.name for group in form.cleaned_data["groups"]),
                },
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
        messages.success(self.request, f'User "{self.object.username}" was updated.')
        return response
class UserDeactivateView(UserPermissionMixin, View):
    permission_required = "auth.change_user"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            raise PermissionDenied(
                "Office-level account scopes are not configured. "
                "Only a superuser can deactivate user accounts."
            )
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def _allowed(self, target):
        return (
            target.is_active
            and target.pk != self.request.user.pk
            and not target.is_superuser
        )

    def get(self, request, *args, **kwargs):
        target = self.get_object()
        if not self._allowed(target):
            messages.error(request, "This account cannot be deactivated.")
            return redirect("user-list")
        return render(
            request,
            "core/action_confirm.html",
            {
                "page_title": "Deactivate user account",
                "prompt": "Deactivate this login account while preserving its audit and request history:",
                "object_label": target.username,
                "warning": "This action cannot be undone here. The account will no longer be able to sign in.",
                "confirm_label": "Deactivate account",
                "back_url": reverse("user-list"),
            },
        )

    def post(self, request, *args, **kwargs):
        target = self.get_object()
        if not self._allowed(target):
            messages.error(request, "This account cannot be deactivated.")
            return redirect("user-list")
        with transaction.atomic():
            log_action(
                actor_user=request.user,
                action=AuditLog.Action.UPDATE,
                entity_type="User",
                object_id=target.pk,
                before_data={"is_active": target.is_active},
                after_data={"is_active": False},
                reason="User login deactivated; account history retained.",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            target.is_active = False
            target.save(update_fields=["is_active"])
        messages.success(request, f'Account "{target.username}" was deactivated.')
        return redirect("user-list")


class GroupPermissionMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Base mixin for group views - requires auth.view_group permission."""
    permission_required = "auth.view_group"
    raise_exception = True


class GroupListView(GroupPermissionMixin, ListView):
    model = Group
    template_name = "accounts/group_list.html"
    context_object_name = "groups"
    paginate_by = 20

    def get_queryset(self):
        queryset = Group.objects.annotate(
            permission_count=Count("permissions")
        ).order_by("name")
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(name__icontains=query)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["search_query"] = self.request.GET.get("q", "")
        context["querystring"] = params.urlencode()
        return context


class GroupDetailView(GroupPermissionMixin, View):
    template_name = "accounts/group_detail.html"

    def get(self, request, pk):
        group = get_object_or_404(Group.objects.prefetch_related("permissions__content_type"), pk=pk)
        
        # Group permissions by app_label and model for readability
        permissions_by_app = {}
        for perm in group.permissions.select_related("content_type").order_by("content_type__app_label", "content_type__model", "codename"):
            app_label = perm.content_type.app_label
            model = perm.content_type.model
            if app_label not in permissions_by_app:
                permissions_by_app[app_label] = {}
            if model not in permissions_by_app[app_label]:
                permissions_by_app[app_label][model] = []
            permissions_by_app[app_label][model].append(perm)

        context = {
            "group": group,
            "permissions_by_app": permissions_by_app,
            "page_title": f"Group: {group.name}",
        }
        return render(request, self.template_name, context)
