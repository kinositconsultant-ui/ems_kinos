from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db.models import Q
from django.views.generic import ListView

from .forms import AuditLogFilterForm
from .models import AuditLog


class AuditLogListView(LoginRequiredMixin, PermissionRequiredMixin, ListView):
    model = AuditLog
    template_name = "audittrail/audit_log_list.html"
    context_object_name = "audit_logs"
    permission_required = "audittrail.view_auditlog"
    raise_exception = True
    paginate_by = 25

    def get_filter_form(self):
        return AuditLogFilterForm(data=self.request.GET or None)

    def get(self, request, *args, **kwargs):
        self.filter_form = self.get_filter_form()
        if self.filter_form.is_bound and not self.filter_form.is_valid():
            context = self.get_context_data(
                filter_form=self.filter_form,
                search_query=request.GET.get("q", ""),
            )
            return self.render_to_response(context, status=400)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("actor_user", "office")
        form = getattr(self, "filter_form", self.get_filter_form())
        if form.is_valid():
            data = form.cleaned_data
            if data.get("action"):
                queryset = queryset.filter(action=data["action"])
            if data.get("start_date"):
                queryset = queryset.filter(created_at__date__gte=data["start_date"])
            if data.get("end_date"):
                queryset = queryset.filter(created_at__date__lte=data["end_date"])
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(entity_type__icontains=query)
                | Q(object_id__icontains=query)
                | Q(reason__icontains=query)
                | Q(actor_user__username__icontains=query)
                | Q(office__office_name__icontains=query)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title="Audit Logs",
            filter_form=kwargs.get("filter_form", getattr(self, "filter_form", self.get_filter_form())),
            search_query=self.request.GET.get("q", ""),
        )
        return context
