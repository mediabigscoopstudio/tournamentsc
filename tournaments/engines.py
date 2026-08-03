"""Format engines (FOUND-02).

A shared `FormatEngine` interface with four concrete implementations. A new
sport is added by mapping it to one of these engines in constants.SPORTS — no
new engine code required.

    generate_fixtures(entrants) -> int   # number of fixtures created
    record_result(fixture, data)         # write a result to one fixture
    compute_standings()                  # recompute derived leaderboard(s)
"""
import itertools
import math
from decimal import Decimal, InvalidOperation

from django.db import transaction

from . import constants as C
from .models import Fixture, FixtureParticipant, Standing


def _num(value):
    """Coerce a posted score to a Decimal.

    Scores arrive from the scoring form as *strings*. Comparing them directly
    ranks them lexicographically — '9' > '10' — which silently awards the match
    to the wrong competitor. Everything that compares a score goes through here.
    """
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---- entrant normalisation --------------------------------------------
def _entrants_for(tournament):
    """Return a list of {'team','player','label','seed','stats'} for approved
    entries, ordered by seed then registration.

    `stats` denormalises the few registration facts a scoreboard needs (bib,
    rating) onto the fixture participant, so a leaderboard row never has to walk
    back to the registration table.
    """
    out = []
    if tournament.is_team_based:
        for e in tournament.team_entries.filter(status='APPROVED').select_related('team'):
            out.append({'team': e.team, 'player': None, 'label': e.team.name,
                        'seed': e.seed or 9999, 'stats': {}})
    else:
        for r in tournament.registrations.filter(status='APPROVED').select_related('player__user'):
            stats = {}
            if r.bib_number:
                stats['bib'] = r.bib_number
            if r.player and r.player.rating:
                stats['rating'] = r.player.rating
            out.append({'team': None, 'player': r.player, 'label': r.name,
                        'seed': r.seed or 9999, 'stats': stats})
    out.sort(key=lambda d: (d['seed'], d['label'].lower()))
    return out


def _make_participant(fixture, entrant, slot):
    if entrant is None:
        return FixtureParticipant.objects.create(fixture=fixture, slot=slot, label='TBD')
    has_account = entrant['team'] or entrant['player']
    return FixtureParticipant.objects.create(
        fixture=fixture, slot=slot,
        team=entrant['team'], player=entrant['player'],
        # Account-less entrants (organizer-typed names) keep their label so they
        # stay distinct in standings and scoreboards.
        label='' if has_account else entrant.get('label', ''),
        stats=dict(entrant.get('stats') or {}),
    )


def _round_name(entrants_in_round):
    """Label a round by how many entrants actually start it — not the next
    power of two the bracket happens to be sized for — so a 10-team round 1
    reads "Round of 10", never "Round of 16"."""
    return {2: 'Final', 4: 'Semifinal', 8: 'Quarterfinal'}.get(
        entrants_in_round, f'Round of {entrants_in_round}')


def _round_robin_rounds(entrants):
    """Circle-method round-robin schedule.

    Returns a list of rounds; each round is a list of (a, b) entrant pairs.
    Every pair meets exactly once, no entrant appears twice in the same round,
    and an odd field yields exactly one bye per round (the entrant drawn against
    the placeholder simply sits that round out). Correct for 2, 3, 4, and any
    odd or even field.
    """
    field = list(entrants)
    if len(field) % 2:               # odd field -> a placeholder that means "bye"
        field.append(None)
    n = len(field)
    rounds = []
    idx = list(range(n))
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = field[idx[i]], field[idx[n - 1 - i]]
            if a is not None and b is not None:  # skip the bye pairing
                pairs.append((a, b))
        rounds.append(pairs)
        # Rotate every position but the first (standard circle rotation).
        idx = [idx[0]] + [idx[-1]] + idx[1:-1]
    return rounds


class FormatEngine:
    format = None

    def __init__(self, tournament):
        self.tournament = tournament

    # -- interface --
    def generate_fixtures(self, entrants=None):
        raise NotImplementedError

    def record_result(self, fixture, data):
        raise NotImplementedError

    def compute_standings(self):
        return None

    # -- shared helpers --
    def _clear_fixtures(self):
        self.tournament.fixtures.all().delete()
        self.tournament.standings.all().delete()


class BracketEngine(FormatEngine):
    """Single-elimination with automatic byes and winner advancement (FIX-01)."""
    format = C.FORMAT_KNOCKOUT

    @transaction.atomic
    def generate_fixtures(self, entrants=None):
        entrants = entrants if entrants is not None else _entrants_for(self.tournament)
        n = len(entrants)
        if n < 2:
            return 0
        self._clear_fixtures()

        # Round-by-round slot counts, built forward from the actual team
        # count instead of padding the whole draw out to the next power of
        # two up front. sizes[0] = n; sizes[r] = number of fixture slots in
        # round r = ceil(entrants-into-that-round / 2). A bye only appears
        # in whichever round's own field happens to be odd, so an even
        # field (10, 20, 24 teams, ...) plays a full round of real matches —
        # Team 1 v Team 2, Team 3 v Team 4, ... — before any bye shows up
        # anywhere in the bracket, instead of front-loading every bye into
        # round 1 the way padding to the next power of two would.
        sizes = [n]
        while sizes[-1] > 1:
            sizes.append(math.ceil(sizes[-1] / 2))
        num_rounds = len(sizes) - 1

        # Create every fixture slot, round by round, and keep a grid for wiring.
        grid = {}
        for r in range(1, num_rounds + 1):
            round_name = _round_name(sizes[r - 1])  # actual entrants starting this round
            for i in range(sizes[r]):
                grid[(r, i)] = Fixture.objects.create(
                    tournament=self.tournament, round_no=r, sequence=i,
                    bracket_position=i, round_name=round_name,
                    created_by_id=getattr(self.tournament.organizer.user, 'id', None),
                )
        # Wire advancement r -> r+1. When a round's slot count is odd, its
        # last target slot in the next round only ever gets one fixture
        # wired into it here — that missing second wire is exactly what
        # `_advance` uses to recognise a bye-through slot and auto-advance
        # its lone entrant.
        for r in range(1, num_rounds):
            for i in range(sizes[r]):
                grid[(r, i)].advances_to = grid[(r + 1, i // 2)]
                grid[(r, i)].advances_slot = i % 2
                grid[(r, i)].save(update_fields=['advances_to', 'advances_slot'])

        # Populate round 1 directly from the entrant list, pairing them in
        # order (Team 1 vs Team 2, Team 3 vs Team 4, ...), and auto-advance
        # a trailing bye if the field itself is odd.
        matches_r1 = n // 2
        for i in range(matches_r1):
            fx = grid[(1, i)]
            _make_participant(fx, entrants[2 * i], 0)
            _make_participant(fx, entrants[2 * i + 1], 1)
        if n % 2:
            fx = grid[(1, matches_r1)]
            bye_p = _make_participant(fx, entrants[-1], 0)
            bye_p.is_winner = True
            bye_p.save(update_fields=['is_winner'])
            fx.status = 'COMPLETED'
            fx.summary = 'Bye'
            fx.save(update_fields=['status', 'summary'])
            self._advance(fx, bye_p)
        return Fixture.objects.filter(tournament=self.tournament).count()

    def _advance(self, fixture, winner_participant):
        target, slot = fixture.advances_to, fixture.advances_slot
        if not target:
            return
        tp, _ = FixtureParticipant.objects.get_or_create(fixture=target, slot=slot)
        tp.team = winner_participant.team
        tp.player = winner_participant.player
        tp.label = '' if (winner_participant.team or winner_participant.player) else 'TBD'
        tp.is_winner = None
        tp.score = None
        tp.save()

        # A target with only one fixture wired into it went odd on its own
        # round and will never get a second competitor — the lone entrant
        # advances immediately without a match, the same way a first-round
        # bye already does, cascading through as many rounds as needed.
        if Fixture.objects.filter(advances_to=target).count() == 1:
            tp.is_winner = True
            tp.save(update_fields=['is_winner'])
            target.status = 'COMPLETED'
            target.summary = 'Bye'
            target.save(update_fields=['status', 'summary'])
            self._advance(target, tp)

    @transaction.atomic
    def record_result(self, fixture, data):
        """data: {participant_id: {'score': number}}. Highest score wins."""
        parts = list(fixture.participants.all())
        best = None
        for p in parts:
            raw = _num(data.get(str(p.id), {}).get('score'))
            if raw is not None:
                p.score = raw
            p.is_winner = False
            p.save(update_fields=['score', 'is_winner'])
            # Compare as numbers, never as the raw posted strings.
            mine = _num(p.score)
            if mine is not None and (best is None or mine > _num(best.score)):
                best = p

        # A draw has no winner and cannot advance a bracket.
        if best is not None:
            top = _num(best.score)
            tied = [p for p in parts if _num(p.score) == top]
            if len(tied) > 1:
                best = None

        if data.get('finalize') and best is not None:
            best.is_winner = True
            best.save(update_fields=['is_winner'])
            fixture.status = 'COMPLETED'
            fixture.save(update_fields=['status'])
            self._advance(fixture, best)


class PointsTableEngine(FormatEngine):
    """Round-robin points table (chess, 2-team leagues) and battle-royale
    lobby scoring (esports) (FIX-03 / FIX-07)."""
    format = C.FORMAT_ROUND_ROBIN

    @property
    def is_lobby(self):
        return self.tournament.sport.slug == 'mobile-esports'

    @transaction.atomic
    def generate_fixtures(self, entrants=None):
        entrants = entrants if entrants is not None else _entrants_for(self.tournament)
        if len(entrants) < 2:
            return 0
        self._clear_fixtures()

        if self.is_lobby:
            # A configurable number of lobbies, each with every squad.
            num_matches = int(self.tournament.points_config.get('num_matches', 4) or 4)
            for m in range(num_matches):
                fx = Fixture.objects.create(
                    tournament=self.tournament, round_no=1, sequence=m,
                    round_name=f'Match {m + 1}',
                    created_by_id=getattr(self.tournament.organizer.user, 'id', None))
                for e in entrants:
                    _make_participant(fx, e, 0)
        elif self.tournament.sport.slug == 'basketball':
            # Basketball league: schedule the round-robin into balanced rounds
            # (circle method) instead of a flat combinations dump. Every team
            # still plays every other exactly once, but at most one game per
            # team per round, in a clear round-by-round order, with a bye per
            # round when the field is odd. Other round-robin sports are
            # untouched (they fall through to the branch below).
            seq = 0
            for rno, pairs in enumerate(_round_robin_rounds(entrants), start=1):
                for a, b in pairs:
                    fx = Fixture.objects.create(
                        tournament=self.tournament, round_no=rno, sequence=seq,
                        round_name=f'Round {rno}',
                        created_by_id=getattr(self.tournament.organizer.user, 'id', None))
                    _make_participant(fx, a, 0)
                    _make_participant(fx, b, 1)
                    seq += 1
        else:
            seq = 0
            for a, b in itertools.combinations(entrants, 2):
                fx = Fixture.objects.create(
                    tournament=self.tournament, round_no=1, sequence=seq,
                    round_name='Round Robin',
                    created_by_id=getattr(self.tournament.organizer.user, 'id', None))
                _make_participant(fx, a, 0)
                _make_participant(fx, b, 1)
                seq += 1
        self.compute_standings()
        return self.tournament.fixtures.count()

    @transaction.atomic
    def record_result(self, fixture, data):
        for p in fixture.participants.all():
            entry = data.get(str(p.id), {})
            if self.is_lobby:
                cfg = self.tournament.points_config
                kills = float(entry.get('kills') or 0)
                placement = str(int(entry.get('placement') or 0)) if entry.get('placement') else None
                place_pts = float(cfg.get('placement_points', {}).get(placement, 0)) if placement else 0
                kill_pts = kills * float(cfg.get('kill_points', 1))
                p.score = kill_pts + place_pts
                p.rank = int(entry['placement']) if entry.get('placement') else None
                # Keep the breakdown, not just the total — the public leaderboard
                # shows placement points and kills as separate columns.
                stats = dict(p.stats or {})
                stats.update({'kills': kills, 'placement': p.rank,
                              'kill_pts': kill_pts, 'place_pts': place_pts})
                p.stats = stats
            else:
                score = _num(entry.get('score'))
                if score is not None:
                    p.score = score
            p.save()
        if data.get('finalize'):
            fixture.status = 'COMPLETED'
            fixture.save(update_fields=['status'])
        self.compute_standings()

    @transaction.atomic
    def compute_standings(self):
        t = self.tournament
        t.standings.all().delete()
        cfg = t.points_config
        table = {}   # key -> dict

        def key_for(p):
            if p.team_id:
                return ('team', p.team_id)
            if p.player_id:
                return ('player', p.player_id)
            return ('label', p.name)

        def row_for(p):
            k = key_for(p)
            if k not in table:
                table[k] = {'team': p.team, 'player': p.player, 'label': p.name,
                            'played': 0, 'won': 0, 'lost': 0, 'drawn': 0, 'points': 0.0,
                            'kills': 0.0, 'place_pts': 0.0,
                            'rating': (p.stats or {}).get('rating'),
                            'opponents': []}
            return table[k]

        completed = t.fixtures.filter(status='COMPLETED', is_removed=False)
        for fx in completed:
            parts = list(fx.participants.all())
            if self.is_lobby:
                for p in parts:
                    r = row_for(p)
                    r['played'] += 1
                    r['points'] += float(p.score or 0)
                    stats = p.stats or {}
                    r['kills'] += float(stats.get('kills') or 0)
                    r['place_pts'] += float(stats.get('place_pts') or 0)
            else:
                if len(parts) != 2:
                    continue
                a, b = parts
                sa, sb = _num(a.score), _num(b.score)
                if sa is None or sb is None:
                    continue
                ra, rb = row_for(a), row_for(b)
                ra['played'] += 1
                rb['played'] += 1
                # Remember who played whom — Buchholz needs the opponents' finals.
                ra['opponents'].append(key_for(b))
                rb['opponents'].append(key_for(a))
                if sa > sb:
                    ra['won'] += 1; rb['lost'] += 1
                    ra['points'] += float(cfg.get('win', 3)); rb['points'] += float(cfg.get('loss', 0))
                elif sb > sa:
                    rb['won'] += 1; ra['lost'] += 1
                    rb['points'] += float(cfg.get('win', 3)); ra['points'] += float(cfg.get('loss', 0))
                else:
                    ra['drawn'] += 1; rb['drawn'] += 1
                    ra['points'] += float(cfg.get('draw', 1)); rb['points'] += float(cfg.get('draw', 1))

        # Buchholz: the sum of your opponents' final scores. The standard Swiss /
        # round-robin tie-break — two players on equal points are separated by
        # whoever faced the tougher field.
        for k, r in table.items():
            r['buchholz'] = round(sum(table[o]['points'] for o in r['opponents'] if o in table), 1)

        if self.is_lobby:
            rows = sorted(table.values(),
                          key=lambda r: (-r['points'], -r['kills'], r['label'].lower()))
        else:
            rows = sorted(table.values(),
                          key=lambda r: (-r['points'], -r['buchholz'], -r['won'],
                                         r['label'].lower()))

        for pos, r in enumerate(rows, start=1):
            Standing.objects.create(
                tournament=t, team=r['team'], player=r['player'], label=r['label'],
                played=r['played'], won=r['won'], lost=r['lost'], drawn=r['drawn'],
                points=round(r['points'], 1), position=pos,
                extra_stats={'buchholz': r['buchholz'], 'kills': int(r['kills']),
                             'place_pts': int(r['place_pts']), 'rating': r['rating']})


class _LeaderboardEngine(FormatEngine):
    """Shared base for time-trial and single-event: participants ranked by
    finish time / position across the field, DNF/DSQ sink to the bottom."""

    def _rank_participants(self, participants):
        def sort_key(p):
            state_rank = {'OK': 0, 'DNF': 1, 'DSQ': 2}.get(p.result_state, 0)
            # lower time first; fall back to score; unfinished sink to the bottom
            score = _num(p.score)
            metric = p.time_ms if p.time_ms is not None else (
                float(score) if score is not None else math.inf)
            return (state_rank, metric)
        ordered = sorted(participants, key=sort_key)
        for i, p in enumerate(ordered, start=1):
            p.rank = i if p.result_state == 'OK' else None
            p.save(update_fields=['rank'])
        return ordered

    @transaction.atomic
    def record_result(self, fixture, data):
        for p in fixture.participants.all():
            entry = data.get(str(p.id), {})
            if 'time_ms' in entry:
                p.time_ms = entry['time_ms']
            score = _num(entry.get('score'))
            if score is not None:
                p.score = score
            if entry.get('result_state'):
                p.result_state = entry['result_state']
            p.save()
        self._rank_participants(list(fixture.participants.all()))
        if data.get('finalize'):
            fixture.status = 'COMPLETED'
            fixture.save(update_fields=['status'])


class TimeTrialEngine(_LeaderboardEngine):
    """Racing: multiple timed sessions (qualifying + race), best/last combined."""
    format = C.FORMAT_TIME_TRIAL

    @transaction.atomic
    def generate_fixtures(self, entrants=None):
        entrants = entrants if entrants is not None else _entrants_for(self.tournament)
        if not entrants:
            return 0
        self._clear_fixtures()
        sessions = self.tournament.points_config.get('sessions') or ['Qualifying', 'Race']
        for idx, label in enumerate(sessions, start=1):
            fx = Fixture.objects.create(
                tournament=self.tournament, round_no=idx, sequence=idx, session_no=idx,
                round_name=label,
                created_by_id=getattr(self.tournament.organizer.user, 'id', None))
            for e in entrants:
                _make_participant(fx, e, 0)
        return self.tournament.fixtures.count()


class SingleEventEngine(_LeaderboardEngine):
    """Marathon: one mass-start event, ranked once by finish time/category."""
    format = C.FORMAT_SINGLE_EVENT

    @transaction.atomic
    def generate_fixtures(self, entrants=None):
        entrants = entrants if entrants is not None else _entrants_for(self.tournament)
        if not entrants:
            return 0
        self._clear_fixtures()
        categories = list(self.tournament.categories.all()) or [None]
        seq = 0
        for cat in categories:
            fx = Fixture.objects.create(
                tournament=self.tournament, round_no=1, sequence=seq, event_category=cat,
                round_name=(cat.name if cat else 'Race'),
                created_by_id=getattr(self.tournament.organizer.user, 'id', None))
            for e in entrants:
                _make_participant(fx, e, 0)
            seq += 1
        return self.tournament.fixtures.count()


_ENGINES = {
    C.FORMAT_KNOCKOUT: BracketEngine,
    C.FORMAT_ROUND_ROBIN: PointsTableEngine,
    C.FORMAT_TIME_TRIAL: TimeTrialEngine,
    C.FORMAT_SINGLE_EVENT: SingleEventEngine,
}


def get_engine(tournament):
    return _ENGINES[tournament.format](tournament)
