from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import GroupDetailView, GroupListView, SystemLoginView, UserCreateView, UserDeactivateView, UserListView, UserUpdateView

urlpatterns = [
    path('login/', SystemLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('users/', UserListView.as_view(), name='user-list'),
    path('users/new/', UserCreateView.as_view(), name='user-create'),
    path('users/<int:pk>/edit/', UserUpdateView.as_view(), name='user-update'),
    path('users/<int:pk>/deactivate/', UserDeactivateView.as_view(), name='user-deactivate'),
    path('groups/', GroupListView.as_view(), name='group-list'),
    path('groups/<int:pk>/', GroupDetailView.as_view(), name='group-detail'),
]