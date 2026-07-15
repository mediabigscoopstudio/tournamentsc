"""Admin-console routes — the whole second application.

Everything is under `/dashboard/`. The literal paths are declared before the
generic `<str:key>/` resource routes so a resource key can never shadow a
built-in page.
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- Branded admin auth (never Django's admin login) ---
    path('login', views.dash_login, name='dash_login'),
    path('logout', views.dash_logout, name='dash_logout'),

    # --- Fixed pages ---
    path('', views.dash_index, name='dash_index'),
    path('analytics/', views.analytics, name='dash_analytics'),
    path('reports/', views.reports, name='dash_reports'),
    path('reports/<str:key>.csv', views.report_export, name='dash_report_export'),
    path('logs/', views.audit_log, name='dash_audit'),
    path('permissions/', views.permissions_view, name='dash_permissions'),
    path('settings/', views.settings_view, name='dash_settings'),

    # --- Generic CRUD, one route set for every registered resource ---
    path('<str:key>/', views.resource_list, name='dash_resource_list'),
    path('<str:key>/new', views.resource_form, name='dash_resource_create'),
    path('<str:key>/<int:pk>/edit', views.resource_form, name='dash_resource_edit'),
    path('<str:key>/<int:pk>/delete', views.resource_delete, name='dash_resource_delete'),
    path('<str:key>/<int:pk>/<str:action>', views.resource_action, name='dash_resource_action'),
]
