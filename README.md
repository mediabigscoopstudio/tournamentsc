# TournamentSC

One home for every local tournament — from an organizer creating one, to a player
joining it, to an audience watching it live. Seven sports, four format engines,
**two completely separate applications**, one Django codebase.

---

## Quick start

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_sports        # the 7 fixed sports (run in every env)
python manage.py seed_demo          # optional: demo data across all 4 engines
python manage.py runserver
```

Open http://127.0.0.1:8000

### Demo logins (created by `seed_demo`)

| Role | Login page | Email | Password |
|------|-----------|-------|----------|
| **Platform admin** | `/dashboard/login` | `admin@tournamentsc.app` | `admin12345` |
| **Organizer** | `/organizer/login` | `organizer@tournamentsc.app` | `organizer12345` |
| **Player** | `/player/login` | `ravi.kumar@example.com` | `player12345` |

**Audience needs no login at all** — browsing, live scores, standings, schedules,
results, highlights and news are entirely public.

### Validate the whole thing

```bash
python smoke_test.py     # 201 checks: every flow, every permission boundary
```

Runs against a throwaway database. It drives all four workflows end to end and
asserts the isolation guarantees below.

---

## The two applications

### 1. Public application — `/`

| Who | Front door | Can do |
|-----|-----------|--------|
| **Audience** | *none — never asked to log in* | Browse home, tournaments, sports, matches, teams, players, standings, schedules, results, highlights, live scores, news, search |
| **Player** | `/player/login` · `/player/register` | Profile, join tournaments, register with a team, see fixtures / live matches / results |
| **Organizer** | `/organizer/login` · `/organizer/register` | Tournament CRUD, teams, matches, fixtures, schedules, banners, highlights, registrations, approve/reject teams, publish |

### 2. Admin application — `/dashboard/`

A dedicated, custom-branded admin panel. **Not Django Admin.** Its own login,
its own layout, its own permission gate (`is_staff`).

| Group | Sections |
|-------|----------|
| **Competition** | Tournaments · Matches & fixtures · Teams · Team members · Team registrations · Player registrations |
| **People** | Users · Organizers · Organizer applications · Players |
| **Content** | News · Announcements · Highlights · Media library |
| **Catalogue** | Sports · Venues · Tournament categories |
| **System** | Roles · Permissions · Analytics · Reports · Activity log · Settings |

Full CRUD everywhere, plus lifecycle actions: **approve, reject, suspend,
activate, verify, publish, unpublish, feature, cancel, archive, restore, delete**
— including over content organizers created. Every section exports to CSV, and
every administrative action is written to the activity log.

---

## Isolation guarantees

These are enforced in the view layer (`accounts/decorators.py`), not by hiding
buttons, and each one is asserted in `smoke_test.py`:

- An **audience** visitor is never redirected to a login page.
- A **player** hitting `/dashboard/…` gets a hard **403** — never a redirect that
  hints the console exists.
- An **organizer** cannot reach the admin panel, platform settings, the global
  user list, or site configuration.
- An organizer cannot manage another organizer's tournament (**403**), and admins
  do not get a back door into the organizer UI — they manage everything from the
  console.
- Each login page **rejects the other roles' accounts**: an admin cannot sign in at
  `/player/login` or `/organizer/login`, and an organizer or player cannot sign in
  at `/dashboard/login`.
- `is_staff` can only ever be granted from the admin console. The public sign-up
  forms hard-code it to `False`.
- Suspending an account logs it out on its very next request.

### Django's built-in admin

Not installed and not routed by default — `django.contrib.admin` is only added to
`INSTALLED_APPS` when a developer sets `ENABLE_DJANGO_ADMIN=True`, and it then
mounts at the env-controlled `DJANGO_ADMIN_URL` (default `/django-admin-dev/`),
never a guessable path. `/admin/` is a 404 in every configuration.

---

## Routing

Four independent namespaces. Nothing sits at the bare URL root except the public
pages themselves, so a tournament slug can never shadow an organizer or admin
route:

```
/                    public audience site
/player/…            player application      (login: /player/login)
/organizer/…         organizer application   (login: /organizer/login)
/dashboard/…         admin application       (login: /dashboard/login)
/t/<slug>            public tournament + match pages
/api/fixtures/<id>/live   public live-score JSON
```

---

## Architecture

- **`accounts`** — custom `User` (email login), the three isolated auth flows,
  the role guards every view is decorated with, organizer applications,
  notifications, moderation primitives.
- **`tournaments`** — the format-agnostic core: `Fixture` → many
  `FixtureParticipant` (one row per competitor), the four **format engines**
  (`BracketEngine`, `PointsTableEngine`, `TimeTrialEngine`, `SingleEventEngine`),
  scoring services and standings. A new sport is added by mapping it to an engine
  in `constants.SPORTS` — no new engine code.
- **`dash`** — the admin application. `resources.py` is a **declarative registry**:
  each of the ~18 managed resources declares its model, form, columns, filters and
  lifecycle actions, and three generic views (`resource_list`, `resource_form`,
  `resource_delete`) serve all of them. Adding a managed resource is a
  declaration, not a new view + URL + template. Also owns News, Announcements,
  Media and the singleton `SiteSetting`.
- **`main`** — the public audience site and the player dashboard.

Sport → engine map: basketball / badminton / wrestling → bracket · chess /
mobile esports → points-table · racing → time-trial · marathon → single-event.

### Settings that actually do something

`/dashboard/settings/` (superuser only) drives real behaviour: closing player or
organizer registration disables those sign-up pages; auto-approve skips the
organizer review queue; maintenance mode shows a site-wide notice; site name,
tagline and meta description are used across the public templates.

### Live scores

Public match pages poll `/api/fixtures/<id>/live` every ~12s **only while a match
is LIVE**, and flash a changed score green. No WebSockets. The endpoint refuses to
serve a draft or archived tournament.

### Database

SQLite by default. Set `DATABASE_URL=postgres://…` for PostgreSQL. All config is
env-driven — see `.env.example`.
