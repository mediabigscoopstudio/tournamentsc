/* Live-score polling. Polls only while a match is LIVE, stops on completion, and
   never runs for upcoming/finished matches. Flashes a changed score green for
   ~450ms — the one signature micro-interaction — and keeps the win-probability
   bar in step with the score. */
(function () {
  // Individual-scoring team switcher (mirrors the organizer scoring page's
  // own switcher — see static/js/basketball-score.js). Runs unconditionally,
  // even for a COMPLETED match with polling never starting below, since a
  // finished match's box score should still be browsable by team. The
  // currently-selected team is kept on #individual-scoring's own
  // data-active-team attribute — that element is never replaced by a poll
  // refresh (only #individual-scoring-body's innerHTML is), so re-running
  // this after a refresh restores whichever team the visitor had open
  // instead of silently snapping back to Team A.
  function initIndividualScoringSwitcher() {
    const panel = document.getElementById('individual-scoring');
    if (!panel) return;
    const switchBtns = panel.querySelectorAll('[data-team-switch]');
    const teamPanels = panel.querySelectorAll('[data-team-panel]');
    if (!switchBtns.length) return;

    function activate(teamId) {
      switchBtns.forEach(function (btn) {
        const active = btn.getAttribute('data-team-switch') === teamId;
        btn.classList.toggle('is-active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      teamPanels.forEach(function (p) {
        p.classList.toggle('is-active', p.getAttribute('data-team-panel') === teamId);
      });
      panel.setAttribute('data-active-team', teamId);
    }

    switchBtns.forEach(function (btn) {
      btn.addEventListener('click', function () { activate(btn.getAttribute('data-team-switch')); });
    });

    activate(panel.getAttribute('data-active-team'));
  }
  initIndividualScoringSwitcher();

  const board = document.querySelector('[data-live-fixture]');
  if (!board) return;
  const fixtureId = board.getAttribute('data-live-fixture');
  if (board.getAttribute('data-live-status') !== 'LIVE') return;

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const endpoint = '/api/fixtures/' + fixtureId + '/live';
  // Short enough that a pause/resume on the organizer's clock reaches this
  // page well within a few seconds — no manual reload needed to stay in
  // sync. setText() only flashes on an actual value change, so polling more
  // often doesn't make the score flash any more than before.
  const POLL_MS = 4000;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function setText(el, value) {
    if (!el || value === null || value === undefined) return;
    const text = String(value);
    if (el.textContent.trim() === text) return;
    el.textContent = text;
    if (!prefersReduced) {
      el.classList.add('flash');
      setTimeout(() => el.classList.remove('flash'), 450);
    }
  }

  function updateProbability(wp) {
    const bar = document.querySelector('[data-prob-bar]');
    const wrap = document.querySelector('[data-prob-wrap]');
    if (!bar || !wrap) return;
    if (wp === null || wp === undefined) { wrap.hidden = true; return; }
    wrap.hidden = false;
    bar.style.width = wp + '%';
    setText(document.querySelector('[data-prob-a]'), wp);
    setText(document.querySelector('[data-prob-b]'), 100 - wp);
  }

  function stop() {
    clearInterval(timer);
    const badge = document.querySelector('[data-live-badge]');
    if (badge) badge.outerHTML = '<span class="badge badge-completed">Final</span>';
    board.classList.remove('is-live');
  }

  // Keeps the public page's quarter clock AND shot clock (both rendered and
  // ticked by static/js/match-clock.js) in step with the organizer's — same
  // elements, same data-* attributes the organizer page itself writes, just
  // refreshed from this same poll instead of a second timer implementation.
  // `data.clock` is only present for basketball fixtures (see fixture_live_json).
  function updateClock(status, clock) {
    if (!clock) return;

    const clockEl = document.querySelector('[data-quarter-clock]');
    if (clockEl) {
      clockEl.setAttribute('data-clock-status', status);
      clockEl.setAttribute('data-started-at', clock.started_at || '');
      clockEl.setAttribute('data-extra-seconds', clock.extra_seconds);
      clockEl.setAttribute('data-quarter-length-seconds', clock.quarter_length_seconds);
      clockEl.setAttribute('data-paused', clock.paused ? '1' : '0');
      clockEl.setAttribute('data-paused-remaining-seconds',
        clock.paused_remaining_seconds !== null && clock.paused_remaining_seconds !== undefined
          ? clock.paused_remaining_seconds : '');
    }

    const pausedBadge = document.querySelector('[data-clock-paused-badge]');
    if (pausedBadge) pausedBadge.hidden = !clock.paused;

    const periodPill = document.querySelector('[data-period-pill]');
    if (periodPill && clock.period_display) periodPill.textContent = clock.period_display;

    // The shot clock auto-resets on its own (see Fixture.shot_clock_remaining_at)
    // — this just carries forward whichever running/paused state and baseline
    // the organizer's actions (start/pause/resume/reset/duration change) most
    // recently produced, exactly like the quarter clock above.
    const shotEl = document.querySelector('[data-shot-clock]');
    if (shotEl && clock.shot_duration_seconds !== undefined) {
      shotEl.setAttribute('data-running', clock.shot_running ? '1' : '0');
      shotEl.setAttribute('data-started-at', clock.shot_started_at || '');
      shotEl.setAttribute('data-remaining-seconds', clock.shot_remaining_seconds);
      shotEl.setAttribute('data-duration-seconds', clock.shot_duration_seconds);
    }

    if (window.bballInitClocks) window.bballInitClocks();
  }

  // Server-rendered by the exact same partial the initial page load used
  // (template/public/_individual_scoring.html, fed by
  // _basketball_scoring_context() — the organizer page's own source of
  // truth), so a poll refresh can never drift from what that function
  // would compute right now: +1/+2/+3, fouls, and Undo on the organizer
  // side all show up here within one poll cycle, already correctly
  // totalled — nothing here re-derives player stats a second way.
  function updateIndividualScoring(html) {
    if (html === undefined || html === null) return;
    const body = document.getElementById('individual-scoring-body');
    if (!body) return;
    if (body.innerHTML === html) return;
    body.innerHTML = html;
    initIndividualScoringSwitcher();
  }

  async function poll() {
    board.classList.add('is-updating');
    try {
      const res = await fetch(endpoint, { headers: { 'X-Requested-With': 'fetch' }, cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();

      data.participants.forEach((p) => {
        setText(document.querySelector('[data-score="' + p.id + '"]'),
                p.score !== null ? p.score : (p.time || '—'));
        const rank = document.querySelector('[data-rank="' + p.id + '"]');
        if (rank && p.rank) setText(rank, p.rank);
      });

      updateProbability(data.win_probability);
      updateClock(data.status, data.clock);
      updateIndividualScoring(data.individual_scoring_html);

      const feed = document.querySelector('[data-feed]');
      if (feed && data.events) {
        feed.innerHTML = data.events.length
          ? data.events.map((e) =>
              '<li><span class="t">' + escapeHtml(e.at) + '</span><span>' +
              escapeHtml(e.text) + '</span></li>').join('')
          : '<li class="dim">No commentary yet.</li>';
      }

      if (data.status !== 'LIVE') stop();
    } catch (e) {
      /* transient network error — keep polling */
    } finally {
      board.classList.remove('is-updating');
    }
  }

  const timer = setInterval(poll, POLL_MS);
  poll();
})();
