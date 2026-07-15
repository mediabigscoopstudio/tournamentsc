"""Real-browser UI audit.

Loads every page at desktop / tablet / mobile widths and reports:
  * JavaScript console errors and failed network requests
  * horizontal overflow (the page scrolling sideways)
  * elements overflowing the viewport
  * empty action containers (the "missing buttons" class of bug)
  * buttons that render with no size (collapsed / invisible)

Run the dev server on :8099 first.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8099'

VIEWPORTS = [
    ('desktop', 1440, 900),
    ('laptop', 1280, 800),
    ('tablet', 820, 1180),
    ('mobile', 390, 844),
]

PUBLIC = ['/', '/browse', '/search?q=chess', '/live', '/results', '/highlights',
          '/teams', '/players', '/news', '/sports/basketball', '/sports/pickleball',
          '/t/city-basketball-open', '/t/gdg-chess-open', '/t/kalinga-half-marathon',
          '/t/bgmi-city-scrims', '/t/city-basketball-open/standings',
          '/t/city-basketball-open/schedule', '/login', '/signup',
          '/player/login', '/organizer/login']

ORGANIZER = ['/organizer/', '/organizer/t/city-basketball-open/',
             '/organizer/t/city-basketball-open/participants',
             '/organizer/t/city-basketball-open/fixtures',
             '/organizer/t/city-basketball-open/edit']

PLAYER = ['/player/', '/player/tournaments', '/player/following', '/player/profile']

ADMIN = ['/dashboard/', '/dashboard/tournaments/', '/dashboard/users/',
         '/dashboard/analytics/', '/dashboard/settings/', '/dashboard/highlights/',
         '/dashboard/follows/', '/dashboard/registrations/']

issues = []
checked = 0


def audit(page, url, label, vw):
    global checked
    checked += 1
    errors = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    failed = []
    page.on('requestfailed', lambda r: failed.append(r.url))

    resp = page.goto(BASE + url, wait_until='networkidle')
    if resp and resp.status >= 400:
        issues.append(f'[{label} {vw}] {url} -> HTTP {resp.status}')
        return

    # horizontal overflow of the document itself
    over = page.evaluate(
        '() => document.documentElement.scrollWidth - document.documentElement.clientWidth')
    if over > 2:
        issues.append(f'[{label} {vw}] {url} -> page scrolls horizontally by {over}px')

    # elements sticking out past the viewport (ignore intentional scrollers)
    bad = page.evaluate('''(vw) => {
        const out = [];
        const scrollers = ['.rail','.table-wrap','.bwrap','.chips','.tabs','.ticker__view',
                           '.ticker','.spark'];
        document.querySelectorAll('body *').forEach(el => {
            if (scrollers.some(s => el.closest(s))) return;
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') return;
            const r = el.getBoundingClientRect();
            if (r.width === 0) return;
            if (r.right > vw + 2) out.push(el.tagName.toLowerCase() + '.' +
                (el.className.toString().split(' ')[0] || '?') + ' +' + Math.round(r.right - vw) + 'px');
        });
        return [...new Set(out)].slice(0, 3);
    }''', vw)
    for b in bad:
        issues.append(f'[{label} {vw}] {url} -> overflows viewport: {b}')

    # Buttons that are *rendered* but collapsed to nothing. A button inside a
    # closed mobile menu or a collapsed sidebar has no client rects at all — it
    # is deliberately not rendered, so it is not a bug.
    dead = page.evaluate('''() => {
        const out = [];
        document.querySelectorAll('.btn').forEach(el => {
            if (el.getClientRects().length === 0) return;   // not rendered at all
            if (el.offsetParent === null) return;           // hidden ancestor
            const r = el.getBoundingClientRect();
            if (r.width < 8 || r.height < 8) out.push(el.textContent.trim().slice(0,24) || '(icon)');
        });
        return [...new Set(out)].slice(0, 3);
    }''')
    for d in dead:
        issues.append(f'[{label} {vw}] {url} -> zero-size button: "{d}"')

    # action containers that rendered empty
    empty = page.evaluate('''() => {
        const out = [];
        document.querySelectorAll('.rowacts,.btn-group,.dhero__actions,.hero__actions').forEach(el => {
            if (el.children.length === 0 && el.getBoundingClientRect().height > 0)
                out.push(el.className);
        });
        return [...new Set(out)].slice(0, 3);
    }''')
    for e in empty:
        issues.append(f'[{label} {vw}] {url} -> empty action container: .{e}')

    for e in errors:
        if 'favicon' in e.lower():
            continue
        issues.append(f'[{label} {vw}] {url} -> JS console error: {e[:110]}')
    for f in failed:
        if 'favicon' in f.lower():
            continue
        issues.append(f'[{label} {vw}] {url} -> failed request: {f[:90]}')


def login(page, url, ident, pw):
    page.goto(BASE + url)
    page.fill('input[name=identifier]', ident)
    page.fill('input[name=password]', pw)
    page.click('button[type=submit]')
    page.wait_for_load_state('networkidle')


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for label, w, h in VIEWPORTS:
        # --- public (anonymous) ---
        ctx = browser.new_context(viewport={'width': w, 'height': h})
        page = ctx.new_page()
        for url in PUBLIC:
            audit(page, url, 'public', w)
        ctx.close()

        # --- organizer ---
        ctx = browser.new_context(viewport={'width': w, 'height': h})
        page = ctx.new_page()
        login(page, '/organizer/login', 'organizer@tournamentsc.app', 'organizer12345')
        for url in ORGANIZER:
            audit(page, url, 'organizer', w)
        ctx.close()

        # --- player ---
        ctx = browser.new_context(viewport={'width': w, 'height': h})
        page = ctx.new_page()
        login(page, '/player/login', 'ravi.kumar@example.com', 'player12345')
        for url in PLAYER:
            audit(page, url, 'player', w)
        ctx.close()

        # --- admin ---
        ctx = browser.new_context(viewport={'width': w, 'height': h})
        page = ctx.new_page()
        login(page, '/dashboard/login', 'admin@tournamentsc.app', 'admin12345')
        for url in ADMIN:
            audit(page, url, 'admin', w)
        ctx.close()

    browser.close()

print(f'\nAudited {checked} page-renders across {len(VIEWPORTS)} viewports.')
if issues:
    print(f'\n{len(issues)} ISSUE(S):')
    for i in issues:
        print('  -', i)
    sys.exit(1)
print('\nNo UI issues: no overflow, no console errors, no collapsed buttons, '
      'no empty action containers.')
