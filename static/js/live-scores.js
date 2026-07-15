/* Live-score polling. Polls only while a match is LIVE, stops on completion, and
   never runs for upcoming/finished matches. Flashes a changed score green for
   ~450ms — the one signature micro-interaction — and keeps the win-probability
   bar in step with the score. */
(function () {
  const board = document.querySelector('[data-live-fixture]');
  if (!board) return;
  const fixtureId = board.getAttribute('data-live-fixture');
  if (board.getAttribute('data-live-status') !== 'LIVE') return;

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const endpoint = '/api/fixtures/' + fixtureId + '/live';
  const POLL_MS = 12000;

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

  async function poll() {
    board.classList.add('is-updating');
    try {
      const res = await fetch(endpoint, { headers: { 'X-Requested-With': 'fetch' } });
      if (!res.ok) return;
      const data = await res.json();

      data.participants.forEach((p) => {
        setText(document.querySelector('[data-score="' + p.id + '"]'),
                p.score !== null ? p.score : (p.time || '—'));
        const rank = document.querySelector('[data-rank="' + p.id + '"]');
        if (rank && p.rank) setText(rank, p.rank);
      });

      updateProbability(data.win_probability);

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
