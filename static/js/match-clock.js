/* Basketball score-page clocks only (loaded only when is_basketball).
   Two countdowns, ONE timer engine:
   - Quarter clock ([data-quarter-clock]): remaining = quarter_length_seconds
     + extra_time_seconds - elapsed_since(live_started_at). While paused, the
     server has already computed the frozen remaining value as
     data-paused-remaining-seconds — render it once and don't tick.
   - Shot clock ([data-shot-clock]): auto-synced to the quarter clock's
     running/paused state (see pause_clock/resume_clock/reset_quarter_clock
     in views.py) — ticks down only while data-running="1", from its own
     started-at/remaining baseline, and auto-wraps back to
     data-duration-seconds the instant it would go below 0, repeating for as
     long as it keeps running. The wrap math mirrors
     Fixture.shot_clock_remaining_at() server-side so a page refresh mid-cycle
     renders the same value the client was already showing.

   Root cause of the old shot clock lag/jitter/desync: it ran on its OWN
   `setInterval`, entirely separate from the quarter clock's `setInterval`.
   Two independent timer loops meant (a) their renders could land at
   different instants within the same second, so the two clocks never
   visually ticked in lockstep, and (b) a plain `setInterval(fn, 1000)`
   schedules its next call 1000ms after the *previous* call was due, not
   after it actually finished — so under any main-thread contention (a
   score/foul AJAX patch landing, GC, a busy tab) callbacks bunch up or
   slip, which reads as the shot clock "skipping" a number or updating late.

   The fix: a single scheduler (scheduleTicks) drives both clocks from one
   authoritative setTimeout chain. Every firing re-derives BOTH displayed
   values straight from real elapsed time (never from a counter, so no
   drift accumulates) and then re-aims itself at whichever clock's next
   integer-boundary comes soonest — so both clocks render together, on the
   same call stack, every single time, and neither can drift out of phase
   with the other.

   Exposed as window.bballInitClocks() so basketball-score.js can re-run it
   after an AJAX DOM patch (see that file) — the previous timer chain is
   cancelled first so it doesn't keep ticking against detached elements. */
(function () {
  function pad(n) { return String(n).padStart(2, '0'); }

  function formatClock(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  }

  let timerId = null;

  function readQuarterTicker() {
    const el = document.querySelector('[data-quarter-clock]');
    if (!el || el.getAttribute('data-clock-status') !== 'LIVE') return null;

    const quarterLength = parseInt(el.getAttribute('data-quarter-length-seconds') || '0', 10) || 0;
    const extraSeconds = parseInt(el.getAttribute('data-extra-seconds') || '0', 10) || 0;

    if (el.getAttribute('data-paused') === '1') {
      const remaining = parseInt(el.getAttribute('data-paused-remaining-seconds') || '0', 10) || 0;
      el.textContent = formatClock(remaining);
      return null;
    }

    const startedAt = el.getAttribute('data-started-at');
    const startMs = startedAt ? new Date(startedAt).getTime() : NaN;
    if (Number.isNaN(startMs)) return null;

    return { el, quarterLength, extraSeconds, startMs, last: null };
  }

  function readShotTicker() {
    const el = document.querySelector('[data-shot-clock]');
    if (!el) return null;

    const duration = parseInt(el.getAttribute('data-duration-seconds') || '24', 10) || 24;
    const remainingBaseline = parseInt(el.getAttribute('data-remaining-seconds') || '0', 10) || 0;

    if (el.getAttribute('data-running') !== '1') {
      el.textContent = String(Math.max(0, remainingBaseline));
      return null;
    }

    const startedAt = el.getAttribute('data-started-at');
    const startMs = startedAt ? new Date(startedAt).getTime() : NaN;
    if (Number.isNaN(startMs)) return null;

    return { el, duration, remainingBaseline, startMs, cycleLength: duration + 1, last: null };
  }

  /* Renders both active tickers off the current instant and returns the
     number of milliseconds until whichever one's displayed value is next
     due to change — the point of scheduling *there* instead of "1000ms from
     now" is that it's self-correcting: however late this call itself ran,
     the next one is aimed at the real boundary, not an accumulated offset. */
  function renderAndGetNextDelay(quarterTicker, shotTicker) {
    const now = Date.now();
    let nextDelay = Infinity;

    if (quarterTicker) {
      const elapsed = (now - quarterTicker.startMs) / 1000;
      const remaining = quarterTicker.quarterLength + quarterTicker.extraSeconds - elapsed;
      const flooredRemaining = Math.max(0, Math.floor(remaining));
      if (flooredRemaining !== quarterTicker.last) {
        quarterTicker.el.textContent = formatClock(flooredRemaining);
        quarterTicker.last = flooredRemaining;
      }
      if (remaining > 0) {
        const frac = remaining - Math.floor(remaining);
        nextDelay = Math.min(nextDelay, frac * 1000);
      }
    }

    if (shotTicker) {
      const elapsed = (now - shotTicker.startMs) / 1000;
      const elapsedInCycle = (shotTicker.duration - shotTicker.remainingBaseline) + elapsed;
      const flooredCycle = Math.floor(elapsedInCycle);
      const remaining = shotTicker.duration - (flooredCycle % shotTicker.cycleLength);
      if (remaining !== shotTicker.last) {
        shotTicker.el.textContent = String(remaining);
        shotTicker.last = remaining;
      }
      const msIntoCurrentSecond = (elapsedInCycle - flooredCycle) * 1000;
      nextDelay = Math.min(nextDelay, 1000 - msIntoCurrentSecond);
    }

    return nextDelay;
  }

  function initClocks() {
    if (timerId) { clearTimeout(timerId); timerId = null; }

    const quarterTicker = readQuarterTicker();
    const shotTicker = readShotTicker();
    if (!quarterTicker && !shotTicker) return;

    const tick = function () {
      const nextDelay = renderAndGetNextDelay(quarterTicker, shotTicker);
      if (Number.isFinite(nextDelay)) {
        timerId = setTimeout(tick, Math.max(1, nextDelay));
      }
    };
    tick();
  }

  initClocks();
  window.bballInitClocks = initClocks;
})();
