from flask import Flask, request, jsonify, Response, send_file, make_response
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import json
import socket
import ipaddress
import sys
import logging
from urllib.parse import urlparse, urljoin
from pathlib import Path

# Security: do not use urllib.request directly to avoid SSRF
import requests

import yt_dlp
try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

# Protect against common DOS
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

SITE_URL = os.environ.get('SITE_URL', 'https://instamax-whbk.onrender.com').rstrip('/')
ALLOWED_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com'}
RESOLVE_CACHE = {}
CACHE_TTL = 180

# Configure Flask to drop large requests to prevent DOS
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 MB max request size

import threading
rate_limiter = {}
lock = threading.Lock()

def check_rate_limit(ip):
    # Simple in-memory rate limiting (max 10 requsts per 10 seconds)
    with lock:
        now = time.time()
        requests = rate_limiter.get(ip, [])
        requests = [r for r in requests if now - r < 10]
        if len(requests) >= 20:
            return False
        requests.append(now)
        rate_limiter[ip] = requests
        return True

@app.before_request
def before_request():
    ip = request.remote_addr
    if ip and not check_rate_limit(ip):
        return jsonify({'error': 'Rate limit exceeded. Please wait.'}), 429

@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

def is_safe_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)
    except Exception:
        return False

def validate_url_safety(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f"Disallowed scheme: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("No hostname in URL")
    try:
        ip_addr = socket.gethostbyname(hostname)
        if not is_safe_ip(ip_addr):
            raise ValueError(f"URL resolves to disallowed IP: {ip_addr}")
    except socket.gaierror:
        raise ValueError("Host resolution failed")
        
def secure_request(url, headers=None, stream=False, timeout=30, max_redirects=5):
    current_url = url
    s = requests.Session()
    for _ in range(max_redirects):
        validate_url_safety(current_url)
        resp = s.get(current_url, headers=headers, stream=stream, allow_redirects=False, timeout=timeout)
        if resp.status_code in (301, 302, 303, 307, 308):
            current_url = urljoin(current_url, resp.headers['Location'])
        else:
            return resp
    raise ValueError("Too many redirects")

def cache_key(url: str) -> str:
    try:
        p = urlparse(url.strip())
        host = (p.hostname or '').lower()
        path = p.path.rstrip('/')
        return f'{p.scheme.lower()}://{host}{path}'
    except Exception:
        return url.strip()

def valid_instagram_url(url: str) -> bool:
    try:
        if len(url) > 500:
            return False
        p = urlparse(url.strip())
        host = (p.hostname or '').lower()
        return p.scheme in ('http', 'https') and (host in ALLOWED_HOSTS or host.endswith('.instagram.com'))
    except Exception:
        return False

def filesize(f):
    return f.get('filesize') or f.get('filesize_approx') or 0

def fmt_bytes(n):
    if not n:
        return None
    units = ['B', 'KB', 'MB', 'GB']
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f'{x:.1f} {u}' if u != 'B' else f'{int(x)} B'
        x /= 1024

def format_score(f):
    return (
        int(f.get('height') or 0),
        int(f.get('width') or 0),
        float(f.get('tbr') or 0),
        float(f.get('fps') or 0),
        int(filesize(f) or 0),
    )

def get_ffmpeg_path():
    if imageio_ffmpeg is not None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
    return shutil.which('ffmpeg')

def safe_name(name):
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', name or 'instagram-media').strip(' .')
    return (name[:90] or 'instagram-media')

def extractor_opts():
    return {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': False,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.instagram.com/',
        },
    }

def instagram_embed_candidates(url: str):
    p = urlparse(url)
    path = p.path.rstrip('/')
    parts = [x for x in path.split('/') if x]
    if len(parts) < 2:
        return []
    kind, shortcode = parts[-2], parts[-1]
    if kind not in {'reel', 'p', 'tv'} or not shortcode:
        return []
    base = f'https://www.instagram.com/{kind}/{shortcode}/embed/'
    legacy = f'https://www.instagram.com/{kind}/{shortcode}/embed/captioned/'
    return [base, legacy]

class ExtractionFailure(RuntimeError):
    """Sanitized, user-facing extraction failure with the original exception retained server-side."""

    def __init__(self, category, message, cause=None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.cause = cause


def _exception_status_code(exc):
    """Best-effort status extraction without exposing exception internals to clients."""
    seen = set()
    current = exc
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        for attr in ('status', 'status_code', 'code'):
            value = getattr(current, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
        text_value = str(current)
        match = re.search(r'\bHTTP(?: Error)?[\s:]+(\d{3})\b', text_value, re.I)
        if not match:
            match = re.search(r'\b(?:status|status code)[\s:=]+(\d{3})\b', text_value, re.I)
        if match:
            return int(match.group(1))
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
    return None


def _sanitize_extractor_error(exc):
    """Return a short, safe single-line extractor message for the browser."""
    raw = str(exc or '').strip()
    if not raw:
        return 'The extractor returned an unspecified error.'
    if 'Traceback (most recent call last)' in raw:
        raw = raw.split('Traceback (most recent call last)', 1)[0].strip()
    raw = raw.splitlines()[0].strip()
    raw = re.sub(r'(?i)(?:cookie|authorization|proxy-authorization|set-cookie|x-api-key)\s*[:=]\s*[^;\n]+', '[redacted]', raw)
    raw = re.sub(r'(?i)(?:[A-Za-z]:\\|(?:/tmp|/var|/home|/workspace|/mnt)(?:/|\\))[^\s]+', '[redacted-path]', raw)
    raw = re.sub(r'(?i)(?:127\.0\.0\.1|0\.0\.0\.0|localhost)(?::\d+)?', '[redacted-host]', raw)
    raw = raw.replace('\x00', '')
    raw = re.sub(r'\s+', ' ', raw)
    return raw[:320] or 'The extractor returned an unspecified error.'


def classify_extractor_error(exc):
    """Map extractor failures to stable, user-safe categories."""
    text = str(exc or '').strip()
    lower = text.lower()
    status = _exception_status_code(exc)

    # Explicit upstream HTTP status always wins over message-text heuristics.
    if status == 429 or re.search(r'\b429\b|too many requests|rate[- ]limit', lower):
        return {
            'category': 'rate_limited',
            'message': 'Instagram is rate-limiting requests right now. Please retry later.',
            'use_media_fallback': False,
        }

    if status == 403 or re.search(r'\b403\b|forbidden|access denied', lower):
        return {
            'category': 'access_denied',
            'message': 'Instagram denied access to this media.',
            'use_media_fallback': False,
        }

    if status == 404 or re.search(r'\b404\b|not found|does not exist', lower):
        return {
            'category': 'not_found',
            'message': 'Instagram could not find this media.',
            'use_media_fallback': False,
        }

    if re.search(r'no video formats found|there is no video in this post', lower):
        return {
            'category': 'image_or_carousel_fallback',
            'message': 'No video formats were found. This post may be an image or carousel.',
            'use_media_fallback': True,
        }

    if 'instagram sent an empty media response' in lower:
        return {
            'category': 'extractor_unavailable',
            'message': 'Instagram did not expose this media to the extractor.',
            'use_media_fallback': True,
        }

    if re.search(r'login required|login is required|log in to view|login to view|please log in|sign in to continue|authentication required', lower):
        return {
            'category': 'login_required',
            'message': 'Instagram requires login to access this media.',
            'use_media_fallback': False,
        }

    if re.search(r'private account|account is private|post is private|content is private|media is private', lower):
        return {
            'category': 'private',
            'message': 'This Instagram content is private.',
            'use_media_fallback': False,
        }

    return {
        'category': 'extractor_error',
        'message': _sanitize_extractor_error(exc),
        'use_media_fallback': True,
    }


def extract_info_once(url):
    with yt_dlp.YoutubeDL(extractor_opts()) as ydl:
        return ydl.extract_info(url, download=False)

def _decode_instagram_escaped(value):
    import html
    value = html.unescape(value)
    value = value.replace('\\/', '/')
    def repl(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    value = re.sub(r'\\u([0-9a-fA-F]{4})', repl, value)
    return value

def _balanced_json_fragment(text, start):
    if start >= len(text) or text[start] not in '[{':
        return None
    opener = text[start]
    closer = ']' if opener == '[' else '}'
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None

def _json_values_after_key(text, key):
    values = []
    pos = 0
    while True:
        pos = text.find(key, pos)
        if pos < 0:
            break
        colon = text.find(':', pos + len(key), pos + len(key) + 80)
        if colon < 0:
            pos += len(key)
            continue
        i = colon + 1
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] in '[{':
            raw = _balanced_json_fragment(text, i)
            if raw:
                for candidate in (raw, _decode_instagram_escaped(raw)):
                    try:
                        values.append(json.loads(candidate))
                        break
                    except Exception:
                        pass
        pos = pos + len(key)
    return values

def _largest_image_candidate(candidates):
    valid = [c for c in (candidates or []) if isinstance(c, dict) and c.get('url')]
    if not valid:
        return None
    return max(valid, key=lambda c: ((c.get('height') or 0) * (c.get('width') or 0), c.get('width') or 0))

def _format_from_image_candidate(c, idx=1):
    url = c.get('url') if isinstance(c, dict) else None
    if not url:
        return None
    w = int(c.get('width') or 0)
    h = int(c.get('height') or 0)
    return {
        'format_id': f'img-{idx}',
        'url': url,
        'width': w or None,
        'height': h or None,
        'ext': 'jpg',
        'vcodec': 'none',
        'acodec': 'none',
        'filesize': None,
        'http_headers': extractor_opts()['http_headers'],
    }

def _format_from_video_candidate(c, idx=1):
    url = c.get('url') if isinstance(c, dict) else None
    if not url:
        return None
    w = int(c.get('width') or 0)
    h = int(c.get('height') or 0)
    return {
        'format_id': f'vid-{idx}',
        'url': url,
        'width': w or None,
        'height': h or None,
        'fps': c.get('fps'),
        'tbr': c.get('bitrate') or c.get('bandwidth'),
        'ext': 'mp4',
        'vcodec': 'h264',
        'acodec': 'aac',
        'filesize': None,
        'http_headers': extractor_opts()['http_headers'],
    }

def _public_page_title(html_text):
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)',
        r'<title[^>]*>(.*?)</title>',
    ):
        m = re.search(pattern, html_text, re.I | re.S)
        if m:
            import html
            return html.unescape(re.sub(r'\\s+', ' ', m.group(1)).strip())
    return None

def _public_page_og_image(html_text):
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html_text, re.I)
    return _decode_instagram_escaped(m.group(1)) if m else None

def extract_public_instagram_media(url):
    candidates = [url]
    p = urlparse(url)
    path_parts = [x for x in p.path.rstrip('/').split('/') if x]
    if len(path_parts) >= 2:
        kind, shortcode = path_parts[-2], path_parts[-1]
        if kind in {'p', 'reel', 'reels', 'tv'} and shortcode:
            candidates.extend([
                f'https://www.instagram.com/{kind}/{shortcode}/embed/',
                f'https://www.instagram.com/{kind}/{shortcode}/embed/captioned/',
            ])

    last_error = None
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.instagram.com/',
    }

    for candidate in candidates:
        try:
            r = secure_request(candidate, headers=headers, timeout=20)
            html_text = r.text
            if not html_text or len(html_text) < 200:
                continue

            title = _public_page_title(html_text) or 'Instagram media'
            entries = []

            carousel_lists = []
            for key in ('"carousel_media"', 'carousel_media'):
                carousel_lists.extend(_json_values_after_key(html_text, key))
            carousel = max((x for x in carousel_lists if isinstance(x, list)), key=len, default=None)

            if carousel:
                for item in carousel:
                    if not isinstance(item, dict):
                        continue
                    image_versions = item.get('image_versions2') or {}
                    image_candidates = image_versions.get('candidates') or []
                    video_candidates = item.get('video_versions') or []
                    formats = []
                    for n, c in enumerate(video_candidates, 1):
                        f = _format_from_video_candidate(c, n)
                        if f: formats.append(f)
                    for n, c in enumerate(image_candidates, 1):
                        f = _format_from_image_candidate(c, n)
                        if f: formats.append(f)
                    if formats:
                        thumb = (_largest_image_candidate(image_candidates) or {}).get('url')
                        entries.append({
                            'title': item.get('accessibility_caption') or title,
                            'thumbnail': thumb,
                            'formats': formats,
                            'duration': item.get('video_duration'),
                        })
                if entries:
                    return {'title': title, 'webpage_url': url, 'entries': entries, '_public_fallback': True}

            video_lists = []
            for key in ('"video_versions"', 'video_versions'):
                video_lists.extend(_json_values_after_key(html_text, key))
            video_candidates = []
            for value in video_lists:
                if isinstance(value, list):
                    video_candidates.extend([x for x in value if isinstance(x, dict)])

            if video_candidates:
                formats = []
                for n, c in enumerate(video_candidates, 1):
                    f = _format_from_video_candidate(c, n)
                    if f: formats.append(f)
                if formats:
                    thumb = _public_page_og_image(html_text)
                    return {
                        'title': title, 'webpage_url': url, 'entries': [{'title': title, 'thumbnail': thumb, 'formats': formats}], '_public_fallback': True
                    }

            image_url = _public_page_og_image(html_text)
            if image_url:
                return {
                    'title': title, 'webpage_url': url,
                    'entries': [{'title': title, 'thumbnail': image_url, 'formats': [_format_from_image_candidate({'url': image_url}, 1)]}],
                    '_public_fallback': True
                }
        except Exception as exc:
            last_error = exc

    if last_error:
        raise RuntimeError(f'Public HTML fallback failed.')
    raise RuntimeError('Instagram HTML did not contain accessible media.')

def _gallery_media_url_lines(url, resolve=True):
    cmd = [sys.executable, '-m', 'gallery_dl', '-G' if resolve else '-g', '--no-input', '--quiet', url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    urls = []
    for stream in (proc.stdout or '', proc.stderr or ''):
        for line in stream.splitlines():
            line = line.strip()
            if line.startswith(('http://', 'https://')):
                # Validate URL before exposing it
                try:
                    validate_url_safety(line)
                    urls.append(line)
                except ValueError:
                    pass
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    if not out:
        raise RuntimeError('gallery-dl failed to resolve usable URLs')
    return out

def _gallery_ext(media_url):
    ext = Path(urlparse(media_url).path.lower()).suffix.lstrip('.')
    return ext if ext in {'jpg','jpeg','png','webp','gif','avif','heic','mp4','m4v','mov','webm','m3u8'} else 'bin'

def gallery_dl_public_media(url):
    urls, errors = [], []
    for resolve in (True, False):
        try:
            urls = _gallery_media_url_lines(url, resolve=resolve)
            if urls: break
        except Exception as exc:
            errors.append(str(exc))
    if not urls:
        raise RuntimeError('gallery-dl could not resolve public Instagram media.')

    headers = extractor_opts()['http_headers']
    entries = []
    for idx, media_url in enumerate(urls, 1):
        ext = _gallery_ext(media_url)
        is_video = ext in {'mp4','m4v','mov','webm','m3u8'} or any(t in media_url.lower() for t in ('.mp4', 'video', 'videoplayback'))
        fmt = {
            'format_id': f'gallery-{idx}',
            'url': media_url,
            'ext': ext if ext != 'bin' else ('mp4' if is_video else 'jpg'),
            'vcodec': 'h264' if is_video else 'none',
            'acodec': 'aac' if is_video else 'none',
            'width': None, 'height': None, 'fps': None, 'filesize': None,
            'http_headers': headers,
        }
        entries.append({
            'title': f'Instagram media {idx}',
            'thumbnail': None if is_video else media_url,
            'formats': [fmt],
        })
    return {'title': 'Instagram media', 'webpage_url': url, 'entries': entries, '_gallery_fallback': True}

def _has_usable_media(info):
    if not info: return False
    entries = entries_from(info)
    if not entries: return False
    for e in entries:
        if e.get('formats') or e.get('url'):
            # Only consider usable if there are formats that have a URL.
            for f in e.get('formats', []):
                if f.get('url'): return True
            if e.get('url'): return True
    return False

def extract_info(url):
    key = cache_key(url)
    now = time.time()
    hit = RESOLVE_CACHE.get(key)
    if hit and now - hit['created'] < CACHE_TTL:
        return hit['info']

    primary_failures = []
    fallback_failures = []

    # 1. Primary Strategy: yt-dlp first. Better for videos/Reels.
    candidates = [url]
    candidates.extend(instagram_embed_candidates(url))
    for candidate in candidates:
        try:
            info = extract_info_once(candidate)
            if _has_usable_media(info):
                RESOLVE_CACHE[key] = {'created': time.time(), 'info': info}
                return info
        except Exception as exc:
            classification = classify_extractor_error(exc)
            primary_failures.append((classification, exc, candidate))
            logger.exception('yt-dlp extraction failed for candidate %s', candidate)

    # 2. Secondary Strategy: gallery-dl. Better for image/photo/carousel media.
    # No-video and empty-media extractor failures explicitly flow through this fallback.
    try:
        fallback = gallery_dl_public_media(url)
        if _has_usable_media(fallback):
            RESOLVE_CACHE[key] = {'created': time.time(), 'info': fallback}
            return fallback
    except Exception as exc:
        fallback_failures.append((classify_extractor_error(exc), exc, url))
        logger.exception('gallery-dl fallback failed for %s', url)

    # 3. Tertiary Strategy: HTML scraping.
    try:
        fallback = extract_public_instagram_media(url)
        if _has_usable_media(fallback):
            RESOLVE_CACHE[key] = {'created': time.time(), 'info': fallback}
            return fallback
    except Exception as exc:
        fallback_failures.append((classify_extractor_error(exc), exc, url))
        logger.exception('HTML Instagram fallback failed for %s', url)

    failures = primary_failures or fallback_failures
    if failures:
        # Prefer the original yt-dlp failure over downstream fallback errors.
        classification, cause, candidate = failures[0]
        raise ExtractionFailure(
            classification['category'],
            classification['message'],
            cause=cause,
        ) from cause

    raise ExtractionFailure(
        'extractor_error',
        'The extractor could not resolve this Instagram media.',
    )

def entries_from(info):
    raw = info.get('entries')
    return [e for e in raw if e] if raw else [info]

def actual_label(width, height):
    w = int(width or 0); h = int(height or 0)
    if not w or not h: return 'Unknown'
    return f'{max(w, h)}p'

def bitrate_label(kbps):
    if not kbps: return 'Bitrate n/a'
    if kbps >= 1000: return f'{kbps / 1000:.2f} Mbps'
    return f'{round(kbps):d} kbps'

def codec_short(codec):
    if not codec or codec == 'none': return None
    return codec.split('.', 1)[0]

def video_variants(formats):
    variants = []; seen = set()
    for f in formats:
        if not f.get('url') or f.get('vcodec') in (None, 'none'): continue
        w = int(f.get('width') or 0); h = int(f.get('height') or 0)
        if not w or not h: continue
        tbr = float(f.get('tbr') or 0); fps = float(f.get('fps') or 0)
        size = int(filesize(f) or 0)
        key = (w, h, round(tbr, 2), round(fps, 2), codec_short(f.get('vcodec')), codec_short(f.get('acodec')), f.get('ext'), f.get('protocol'))
        if key in seen: continue
        seen.add(key)
        variants.append({
            'format_id': str(f.get('format_id') or ''),
            'label': actual_label(w, h), 'width': w, 'height': h, 'resolution': f'{w} × {h}',
            'long_edge': max(w, h), 'short_edge': min(w, h),
            'fps': fps or None, 'tbr': tbr or None, 'bitrate_label': bitrate_label(tbr),
            'filesize': size or None, 'filesize_label': fmt_bytes(size),
            'ext': f.get('ext') or 'media', 'vcodec': codec_short(f.get('vcodec')),
            'acodec': codec_short(f.get('acodec')), 'has_audio': f.get('acodec') not in (None, 'none'),
            'format_note': f.get('format_note'), 'protocol': f.get('protocol'),
        })
    variants.sort(key=lambda v: (v['long_edge'], v['short_edge'], float(v['tbr'] or 0), float(v['fps'] or 0), int(v['filesize'] or 0)), reverse=True)
    return variants

def image_variants(formats):
    variants = []
    for f in formats:
        if not f.get('url') or f.get('vcodec') not in (None, 'none'): continue
        w = int(f.get('width') or 0); h = int(f.get('height') or 0)
        variants.append({
            'format_id': str(f.get('format_id') or ''),
            'label': 'Original image' if not (w and h) else f'{w} × {h}',
            'width': w or None, 'height': h or None, 'resolution': f'{w} × {h}' if w and h else None,
            'long_edge': max(w, h) if w and h else 0, 'short_edge': min(w, h) if w and h else 0,
            'fps': None, 'tbr': None, 'bitrate_label': 'Image',
            'filesize': int(filesize(f) or 0) or None, 'filesize_label': fmt_bytes(filesize(f)),
            'ext': f.get('ext') or 'jpg', 'vcodec': None, 'acodec': None, 'has_audio': False,
            'format_note': f.get('format_note'), 'protocol': f.get('protocol'),
        })
    return variants

def best_format(formats):
    usable = [f for f in formats if f.get('url')]
    video = [f for f in usable if f.get('vcodec') not in (None, 'none')]
    audio = [f for f in usable if f.get('acodec') not in (None, 'none')]
    progressive = [f for f in video if f.get('acodec') not in (None, 'none')]
    best_video = max(video, key=format_score, default=None)
    best_audio = max(audio, key=lambda f: (float(f.get('abr') or 0), float(f.get('asr') or 0), int(filesize(f) or 0)), default=None)
    best_progressive = max(progressive, key=format_score, default=None)
    return best_video, best_audio, best_progressive

def choose_format_by_id(formats, format_id):
    if not format_id: return None
    for f in formats:
        if str(f.get('format_id')) == str(format_id) and f.get('url'):
            return f
    return None

def media_row(idx, item, fallback_title):
    formats = item.get('formats') or []
    best_video, best_audio, best_progressive = best_format(formats)
    variants = video_variants(formats)
    if not variants:
        variants = image_variants(formats)
    chosen = choose_format_by_id(formats, variants[0]['format_id']) if variants else (best_progressive or best_video)
    thumb = item.get('thumbnail')
    if not thumb:
        thumbs = item.get('thumbnails') or []
        if thumbs:
            thumb = max(thumbs, key=lambda t: ((t.get('height') or 0) * (t.get('width') or 0))).get('url')
    is_video = bool(best_video or item.get('duration') or any(v.get('vcodec') for v in variants))
    return {
        'index': idx, 'type': 'video' if is_video else 'image',
        'title': item.get('title') or fallback_title or f'Instagram media {idx}',
        'thumbnail': thumb, 'width': (chosen or {}).get('width'), 'height': (chosen or {}).get('height'),
        'fps': (chosen or {}).get('fps'), 'duration': item.get('duration'),
        'formats_count': len(formats), 'variant_count': len(variants),
        'filesize': filesize(chosen or {}), 'tbr': (chosen or {}).get('tbr'), 'abr': (best_audio or {}).get('abr'),
        'vcodec': (chosen or {}).get('vcodec'), 'acodec': (chosen or {}).get('acodec'),
        'ext': (chosen or {}).get('ext') or item.get('ext'), 'format_note': (chosen or {}).get('format_note'),
        'best_video_id': (best_video or {}).get('format_id'), 'best_audio_id': (best_audio or {}).get('format_id'),
        'best_progressive_id': (best_progressive or {}).get('format_id'), 'variants': variants,
    }

@app.get('/')
def index():
    return app.send_static_file('index.html')

PAGE_MAP = {
    '/instagram-video-downloader': 'instagram-video-downloader.html',
    '/instagram-reel-downloader': 'instagram-reel-downloader.html',
    '/instagram-photo-downloader': 'instagram-photo-downloader.html',
    '/instagram-story-downloader': 'instagram-story-downloader.html',
    '/instagram-carousel-downloader': 'instagram-carousel-downloader.html',
    '/about': 'about.html', '/privacy': 'privacy.html', '/terms': 'terms.html',
    '/copyright': 'copyright.html', '/contact': 'contact.html',
}

for _path, _filename in PAGE_MAP.items():
    app.add_url_rule(_path, endpoint='page_' + _filename.replace('.', '_'), view_func=lambda _f=_filename: app.send_static_file(_f))

@app.get('/robots.txt')
def robots():
    return Response(f'User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {SITE_URL}/sitemap.xml\n', mimetype='text/plain')

@app.get('/sitemap.xml')
def sitemap():
    pages = ['/', *PAGE_MAP.keys()]
    urls = []
    for p in pages:
        urls.append(f'<url><loc>{SITE_URL}{p}</loc></url>')
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(urls) + '</urlset>'
    return Response(xml, mimetype='application/xml')

@app.get('/api/health')
def health():
    return jsonify({'ok': True})

@app.post('/api/resolve')
def resolve():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not valid_instagram_url(url):
        return jsonify({'error': 'Paste a valid public Instagram URL.'}), 400
    try:
        info = extract_info(url)
        entries = entries_from(info)
        media = [media_row(i, item, info.get('title')) for i, item in enumerate(entries, 1)]
        max_dimension = max((max(m.get('width') or 0, m.get('height') or 0) for m in media), default=0)
        total_variants = sum(m.get('variant_count', 0) for m in media)
        return jsonify({
            'ok': True, 'media_count': len(media), 'media': media,
            'title': info.get('title') or 'Instagram media', 'webpage_url': info.get('webpage_url') or url,
            'max_dimension': max_dimension, 'total_variants': total_variants,
            'note': 'Media distinct representation exposed by the extractor is shown.',
        })
    except ExtractionFailure as exc:
        logger.exception('Resolve extraction failed for %s', url)
        return jsonify({
            'error': exc.message,
            'error_category': exc.category,
        }), 502
    except Exception as exc:
        logger.exception('Unexpected resolve failure for %s', url)
        classification = classify_extractor_error(exc)
        return jsonify({
            'error': classification['message'],
            'error_category': classification['category'],
        }), 502

def http_stream(url, headers=None, chunk_size=256 * 1024):
    try:
        resp = secure_request(url, headers=headers, stream=True, timeout=30)
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk: yield chunk
    except Exception as e:
        raise RuntimeError("Proxy backend stream failed")

def cd(filename):
    return 'attachment; filename="' + filename.replace('"', '') + '"'

def selected_media(info, index, format_id=None):
    entries = entries_from(info)
    item = entries[min(max(index, 1), len(entries)) - 1]
    formats = item.get('formats') or []
    chosen = choose_format_by_id(formats, format_id)
    if chosen is None:
        best_video, _, best_progressive = best_format(formats)
        chosen = best_progressive or best_video
        if not chosen and formats:
            # Fallback to image formats
            images = image_variants(formats)
            if images:
                chosen = choose_format_by_id(formats, images[0]['format_id'])

    best_audio = best_format(formats)[1]
    return item, chosen, best_audio

@app.get('/api/download')
def download():
    url = (request.args.get('url') or '').strip()
    try:
        media_index = max(1, int(request.args.get('index', '1')))
    except ValueError:
        media_index = 1
    format_id = request.args.get('format_id', '').strip() or None

    if not valid_instagram_url(url):
        return jsonify({'error': 'Invalid Instagram URL.'}), 400

    temp_dir = None
    try:
        info = extract_info(url)
        item, chosen, best_audio = selected_media(info, media_index, format_id)

        if not chosen or not chosen.get('url'):
            return jsonify({'error': 'The selected source variant is no longer available. Analyze the URL again and retry.'}), 502

        title = safe_name(item.get('title') or info.get('title') or f'instagram-media-{media_index}')
        
        # If it's an image or progressive audio/video, stream directly. No ffmpeg shell needed.
        if chosen.get('acodec') not in (None, 'none') or chosen.get('vcodec') in (None, 'none'):
            ext = (chosen.get('ext') or ('jpg' if chosen.get('vcodec') in (None, 'none') else 'mp4')).lower()
            filename = title + '.' + ext
            mime = mimetypes.guess_type(filename)[0] or ('video/mp4' if ext == 'mp4' else 'application/octet-stream')
            headers = {
                'Content-Disposition': cd(filename),
                'Content-Type': mime,
                'Cache-Control': 'no-store',
            }
            if chosen.get('filesize'):
                headers['Content-Length'] = str(chosen['filesize'])
            return Response(http_stream(chosen['url'], chosen.get('http_headers')), headers=headers, direct_passthrough=True)

        if chosen.get('vcodec') not in (None, 'none'):
            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                return jsonify({'error': 'ffmpeg is not available on the server.'}), 502

            temp_dir = tempfile.mkdtemp(prefix='instamax-')
            outtmpl = os.path.join(temp_dir, 'media.%(ext)s')
            
            # Using yt-dlp arguments list. 
            # Note: No arbitrary string formatting that executes shell code.
            opts = {
                'quiet': True, 'no_warnings': True, 'noplaylist': True,
                'format': f'{format_id}+bestaudio' if format_id else 'bestvideo+bestaudio/best',
                'outtmpl': outtmpl, 'merge_output_format': 'mp4',
                'ffmpeg_location': ffmpeg, 'overwrites': True,
            }

            downloaded_path = None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                files = [os.path.join(temp_dir, name) for name in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, name)) and not name.endswith(('.part', '.ytdl'))]
                if files: downloaded_path = max(files, key=os.path.getsize)
            except Exception:
                opts['format'] = str(format_id) if format_id else 'bestvideo'
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                files = [os.path.join(temp_dir, name) for name in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, name)) and not name.endswith(('.part', '.ytdl'))]
                if files: downloaded_path = max(files, key=os.path.getsize)

            if not downloaded_path:
                raise RuntimeError('The selected media could not be downloaded from Instagram.')

            ext = Path(downloaded_path).suffix.lower() or '.mp4'
            filename = title + ('.mp4' if ext in ('.mp4', '.m4v', '.mov') else ext)
            mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

            response = send_file(downloaded_path, mimetype=mime, as_attachment=True, download_name=filename, conditional=True, max_age=0)
            def cleanup():
                try:
                    for name in os.listdir(temp_dir):
                        try: os.remove(os.path.join(temp_dir, name))
                        except: pass
                    os.rmdir(temp_dir)
                except:
                    pass
            response.call_on_close(cleanup)
            return response

        return jsonify({'error': 'Variant could not be downloaded.'}), 502

    except Exception as exc:
        if temp_dir:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except: pass
        return jsonify({'error': 'Download failed. The media may be private.'}), 502

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8787')) 
    host = os.environ.get('HOST', '0.0.0.0')
    app.run(host=host, port=port, debug=False, threaded=True)
