/* Shared UI behaviour: mobile menu, seamless ticker, tabbed detail panels. */
(function () {
  // --- mobile menu ---
  var menu = document.getElementById('mmenu');
  function setMenu(open) {
    if (!menu) return;
    menu.classList.toggle('on', open);
    menu.setAttribute('aria-hidden', open ? 'false' : 'true');
    document.body.style.overflow = open ? 'hidden' : '';
  }
  document.querySelectorAll('[data-menu-open]').forEach(function (b) { b.addEventListener('click', function () { setMenu(true); }); });
  document.querySelectorAll('[data-menu-close]').forEach(function (b) { b.addEventListener('click', function () { setMenu(false); }); });

  // --- seamless ticker (duplicate track once) ---
  var track = document.querySelector('[data-ticker]');
  if (track && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    track.innerHTML += track.innerHTML;
  }

  // --- tabbed detail panels ---
  document.querySelectorAll('[data-tabs]').forEach(function (group) {
    var tabs = group.querySelectorAll('.tab');
    var panels = group.querySelectorAll('.tabpanel');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
        tab.classList.add('active'); tab.setAttribute('aria-selected', 'true');
        var name = tab.getAttribute('data-tab');
        panels.forEach(function (p) { p.classList.toggle('on', p.getAttribute('data-panel') === name); });
      });
    });
  });
})();
