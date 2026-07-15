"""Shared choices and the sport -> format-engine mapping.

The PRD (§5) is explicit that the *four format engines* are the real
architecture, not the seven sports. A new sport ships by mapping it to an
existing engine here — no new engine code.
"""

# --- Tournament formats (each maps to exactly one engine) ---------------
FORMAT_KNOCKOUT = 'KNOCKOUT'          # single-elimination bracket
FORMAT_ROUND_ROBIN = 'ROUND_ROBIN'    # points-table (multi-match)
FORMAT_TIME_TRIAL = 'TIME_TRIAL'      # time/position leaderboard, multi-session
FORMAT_SINGLE_EVENT = 'SINGLE_EVENT'  # one mass-start event, ranked once

FORMAT_CHOICES = [
    (FORMAT_KNOCKOUT, 'Single-elimination bracket'),
    (FORMAT_ROUND_ROBIN, 'Round-robin points table'),
    (FORMAT_TIME_TRIAL, 'Time-trial / leaderboard'),
    (FORMAT_SINGLE_EVENT, 'Single-event ranking'),
]

# --- The 7 seeded sports, with their default + allowed formats ----------
# slug: (name, icon, color-token, format_type, default_format, [allowed_formats])
SPORTS = {
    'basketball': ('Basketball', '🏀', 'sport-basketball', 'TEAM', FORMAT_KNOCKOUT,
                   [FORMAT_KNOCKOUT, FORMAT_ROUND_ROBIN]),
    'badminton': ('Badminton', '🏸', 'sport-badminton', 'BOTH', FORMAT_KNOCKOUT,
                  [FORMAT_KNOCKOUT, FORMAT_ROUND_ROBIN]),
    'wrestling': ('Wrestling', '🤼', 'sport-wrestling', 'INDIVIDUAL', FORMAT_KNOCKOUT,
                  [FORMAT_KNOCKOUT]),
    'chess': ('Chess', '♟️', 'sport-chess', 'INDIVIDUAL', FORMAT_ROUND_ROBIN,
              [FORMAT_ROUND_ROBIN, FORMAT_KNOCKOUT]),
    'mobile-esports': ('Mobile Esports', '🎮', 'sport-esports', 'TEAM', FORMAT_ROUND_ROBIN,
                       [FORMAT_ROUND_ROBIN]),
    'racing': ('Racing', '🏎️', 'sport-racing', 'BOTH', FORMAT_TIME_TRIAL,
               [FORMAT_TIME_TRIAL]),
    'pickleball': ('Pickleball', '🥒', 'sport-pickleball', 'BOTH', FORMAT_KNOCKOUT,
                   [FORMAT_KNOCKOUT, FORMAT_ROUND_ROBIN]),
    'marathon': ('Marathon', '🏃', 'sport-marathon', 'INDIVIDUAL', FORMAT_SINGLE_EVENT,
                 [FORMAT_SINGLE_EVENT]),
}

TEAM_BASED_FORMAT_TYPES = {'TEAM'}   # sports that always use teams
# Note: BOTH/INDIVIDUAL sports use individual registration by default.

# --- Statuses -----------------------------------------------------------
TOURNAMENT_STATUS = [
    ('DRAFT', 'Draft'),
    ('PUBLISHED', 'Upcoming'),
    ('ONGOING', 'Live'),
    ('COMPLETED', 'Completed'),
    ('CANCELLED', 'Cancelled'),
]

FIXTURE_STATUS = [
    ('SCHEDULED', 'Scheduled'),
    ('LIVE', 'Live'),
    ('COMPLETED', 'Completed'),
    ('POSTPONED', 'Postponed'),
    ('CANCELLED', 'Cancelled'),
]

ENTRY_STATUS = [
    ('PENDING', 'Pending'),
    ('APPROVED', 'Approved'),
    ('REJECTED', 'Rejected'),
]

RESULT_STATE = [
    ('OK', 'Finished'),
    ('DNF', 'Did not finish'),
    ('DSQ', 'Disqualified'),
]


def default_points_config():
    """Per-tournament scoring knobs (FIX-03 / FIX-07)."""
    return {
        'win': 3, 'draw': 1, 'loss': 0,          # round-robin match points
        'kill_points': 1,                         # esports: points per kill
        'placement_points': {'1': 10, '2': 6, '3': 5, '4': 4, '5': 3, '6': 2},
    }
