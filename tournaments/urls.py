"""Organizer management routes + the public live-score API.

Everything an organizer can do is namespaced under `/organizer/`. Previously
these patterns sat at the URL root (`/<slug>/manage`), which made the root a
greedy catch-all and put organizer tools one guessed path away from every public
page. Player participation lives under `/player/`.
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- Public live-score JSON (polled by audience pages, no auth) ---
    path('api/fixtures/<int:fixture_id>/live', views.fixture_live_json, name='fixture_live_json'),

    # --- Player participation ---
    path('player/join/<slug:slug>', views.tournament_join, name='tournament_join'),
    path('player/follow/<slug:slug>', views.tournament_follow, name='tournament_follow'),
    path('player/following', views.my_following, name='my_following'),

    # --- Organizer dashboard ---
    path('organizer/', views.organizer_dashboard, name='organizer_dashboard'),
    path('organizer/tournaments/new', views.tournament_create, name='tournament_create'),

    # --- Per-tournament organizer management ---
    path('organizer/t/<slug:slug>/', views.tournament_manage, name='tournament_manage'),
    path('organizer/t/<slug:slug>/edit', views.tournament_edit, name='tournament_edit'),
    path('organizer/t/<slug:slug>/publish', views.tournament_publish, name='tournament_publish'),
    path('organizer/t/<slug:slug>/cancel', views.tournament_cancel, name='tournament_cancel'),
    path('organizer/t/<slug:slug>/delete', views.tournament_delete, name='tournament_delete'),

    # Participants / teams / registrations
    path('organizer/t/<slug:slug>/participants', views.participants_manage, name='participants_manage'),
    path('organizer/t/<slug:slug>/participants/team/add', views.team_add, name='team_add'),
    path('organizer/t/<slug:slug>/participants/team/<int:team_id>/member/add',
         views.team_member_add, name='team_member_add'),
    path('organizer/t/<slug:slug>/participants/member/<int:membership_id>/<str:decision>',
         views.member_decide, name='member_decide'),
    path('organizer/t/<slug:slug>/participants/individual/add',
         views.individual_add, name='individual_add'),
    path('organizer/t/<slug:slug>/participants/<str:kind>/<int:entry_id>/<str:decision>',
         views.entry_decide, name='entry_decide'),

    # Fixtures & scheduling
    path('organizer/t/<slug:slug>/fixtures', views.fixtures_manage, name='fixtures_manage'),
    path('organizer/t/<slug:slug>/fixtures/generate', views.fixtures_generate, name='fixtures_generate'),
    path('organizer/t/<slug:slug>/fixtures/<int:fixture_id>/schedule',
         views.fixture_schedule, name='fixture_schedule'),
    path('organizer/t/<slug:slug>/fixtures/<int:fixture_id>/score',
         views.score_fixture, name='score_fixture'),
    path('organizer/t/<slug:slug>/fixtures/<int:fixture_id>/highlight',
         views.highlight_manage, name='highlight_manage'),
]
