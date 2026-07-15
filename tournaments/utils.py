import re

_YT_PATTERNS = [
    re.compile(r'(?:youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})'),
    re.compile(r'(?:youtu\.be/)([A-Za-z0-9_-]{11})'),
    re.compile(r'(?:youtube\.com/live/)([A-Za-z0-9_-]{11})'),
    re.compile(r'(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})'),
]


def youtube_id(url):
    """Extract an 11-char video id from any common YouTube URL shape, else None."""
    if not url:
        return None
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def is_probable_youtube_url(url):
    if not url:
        return False
    return ('youtube.com' in url) or ('youtu.be' in url)


def format_ms(ms):
    """Milliseconds -> H:MM:SS.mmm style clock for time-trial/marathon display."""
    if ms is None:
        return '—'
    total_seconds, millis = divmod(int(ms), 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}'
    return f'{minutes}:{seconds:02d}.{millis:03d}'


def parse_time_to_ms(text):
    """Parse 'HH:MM:SS(.mmm)' / 'MM:SS(.mmm)' / 'SS(.mmm)' into milliseconds."""
    if not text:
        return None
    text = text.strip()
    try:
        parts = text.split(':')
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return int(round(seconds * 1000))
