/* Basketball score-page clocks only (loaded only when is_basketball).
   Two independent countdowns:
   - Quarter clock ([data-quarter-clock]): remaining = quarter_length_seconds
     + extra_time_seconds - elapsed_since(live_started_at). The server is the
     source of truth for all of those (see Fixture model + score_fixture
     view) — this only renders the arithmetic against the current time, so a
     page refresh never loses the clock. While paused, the server has
     already computed the frozen remaining value as
     data-paused-remaining-seconds — render it once and don't tick.
   - Shot clock ([data-shot-clock]): fully organizer-controlled (start/pause/
     reset are explicit actions, never auto-run) — ticks down only while
     data-running="1", from its own independent started-at/remaining.

   Exposed as window.bballInitClocks() so basketball-score.js can re-run it
   after an AJAX DOM patch (see that file) — the old intervals are cleared
   first so they don't keep ticking against detached elements. */
(function () {
  function pad(n) { return String(n).padStart(2, '0'); }

  function formatClock(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  }

  let quarterIntervalId = null;
  let shotIntervalId = null;

  function initClocks() {
    if (quarterIntervalId) { clearInterval(quarterIntervalId); quarterIntervalId = null; }
    if (shotIntervalId) { clearInterval(shotIntervalId); shotIntervalId = null; }

    const quarterEl = document.querySelector('[data-quarter-clock]');
    if (quarterEl && quarterEl.getAttribute('data-clock-status') === 'LIVE') {
      const quarterLength = parseInt(quarterEl.getAttribute('data-quarter-length-seconds') || '0', 10) || 0;
      const extraSeconds = parseInt(quarterEl.getAttribute('data-extra-seconds') || '0', 10) || 0;

      if (quarterEl.getAttribute('data-paused') === '1') {
        const remaining = parseInt(quarterEl.getAttribute('data-paused-remaining-seconds') || '0', 10) || 0;
        quarterEl.textContent = formatClock(remaining);
      } else {
        const startedAt = quarterEl.getAttribute('data-started-at');
        const startMs = startedAt ? new Date(startedAt).getTime() : NaN;
        if (!Number.isNaN(startMs)) {
          const tick = function () {
            const elapsed = (Date.now() - startMs) / 1000;
            quarterEl.textContent = formatClock(quarterLength + extraSeconds - elapsed);
          };
          tick();
          quarterIntervalId = setInterval(tick, 1000);
        }
      }
    }

    const shotEl = document.querySelector('[data-shot-clock]');
    if (shotEl) {
      const remainingBaseline = parseInt(shotEl.getAttribute('data-remaining-seconds') || '0', 10) || 0;
      if (shotEl.getAttribute('data-running') === '1') {
        const startedAt = shotEl.getAttribute('data-started-at');
        const startMs = startedAt ? new Date(startedAt).getTime() : NaN;
        if (!Number.isNaN(startMs)) {
          const tick = function () {
            const elapsed = (Date.now() - startMs) / 1000;
            shotEl.textContent = String(Math.max(0, Math.round(remainingBaseline - elapsed)));
          };
          tick();
          shotIntervalId = setInterval(tick, 1000);
        }
      } else {
        shotEl.textContent = String(Math.max(0, remainingBaseline));
      }
    }
  }

  initClocks();
  window.bballInitClocks = initClocks;
})();
