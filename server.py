from flask import Flask, request, jsonify, Response, send_file
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from pathlib import Path

import yt_dlp
try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

app = Flask(__name__, static_folder='static', static_url_path='')
SITE_URL = os.environ.get('SITE_URL', 'https://instamax-whbk.onrender.com').rstrip('/')
ALLOWED_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com'}
RESOLVE_CACHE = {}
CACHE_TTL = 180


def cache_key(url: str) -> str:
    """Build a stable cache key for an Instagram URL.

    Tracking query parameters such as utm_source/stkn do not change the
    underlying Instagram media, so strip the query/fragment to avoid duplicate
    extraction work while keeping the path that identifies the post/reel.
    """
    try:
        p = urlparse(url.strip())
        host = (p.hostname or '').lower()
        path = p.path.rstrip('/')
        return f'{p.scheme.lower()}://{host}{path}'
    except Exception:
        return url.strip()


def valid_instagram_url(url: str) -> bool:
    try:
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
    """Return alternate public embed URLs for the same Instagram media."""
    p = urlparse(url)
    path = p.path.rstrip('/')
    parts = [x for x in path.split('/') if x]
    if len(parts) < 2:
        return []
    kind, shortcode = parts[-2], parts[-1]
    if kind not in {'reel', 'p', 'tv'} or not shortcode:
        return []
    # Instagram's public embed endpoints can expose media when the normal page
    # extractor receives an empty media response. No account cookies are used.
    base = f'https://www.instagram.com/{kind}/{shortcode}/embed/'
    legacy = f'https://www.instagram.com/{kind}/{shortcode}/embed/captioned/'
    return [base, legacy]


def is_empty_media_error(exc):
    text = str(exc).lower()
    markers = (
        'empty media response',
        'unable to extract',
        'no media',
        'login required',
        'not found',
    )
    return any(m in text for m in markers)


def extract_info_once(url):
    with yt_dlp.YoutubeDL(extractor_opts()) as ydl:
        return ydl.extract_info(url, download=False)



def _decode_instagram_escaped(value):
    """Decode the JSON/HTML escaping Instagram commonly uses in page state."""
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
    """Return a balanced JSON array/object beginning at start, or None."""
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
    """Find all JSON arrays/objects immediately following a named key."""
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
        # Instagram's video_versions are direct playable video files. Keep them
        # as a direct source rather than asking yt-dlp to rediscover the post.
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
            return html.unescape(re.sub(r'\s+', ' ', m.group(1)).strip())
    return None


def _public_page_og_image(html_text):
    patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    )
    for pattern in patterns:
        m = re.search(pattern, html_text, re.I)
        if m:
            return _decode_instagram_escaped(m.group(1))
    return None


def extract_public_instagram_media(url):
    """Extract public media URLs from Instagram's HTML/embed representation.

    This is a fallback for cases where yt-dlp recognizes the post but returns no
    video formats, especially image-only posts and carousels. It does not use
    account cookies or attempt to bypass private access controls.
    """
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
            req = Request(candidate, headers=headers)
            with urlopen(req, timeout=25) as r:
                html_text = r.read().decode('utf-8', errors='ignore')
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
                        if f:
                            formats.append(f)
                    for n, c in enumerate(image_candidates, 1):
                        f = _format_from_image_candidate(c, n)
                        if f:
                            formats.append(f)
                    if not formats:
                        continue
                    thumb = (_largest_image_candidate(image_candidates) or {}).get('url')
                    entries.append({
                        'title': item.get('accessibility_caption') or title,
                        'thumbnail': thumb,
                        'formats': formats,
                        'duration': item.get('video_duration'),
                    })

                if entries:
                    return {'title': title, 'webpage_url': url, 'entries': entries, '_public_fallback': True}

            # Single public post/reel fallback. Prefer direct video, then og:image.
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
                    if f:
                        formats.append(f)
                if formats:
                    thumb = _public_page_og_image(html_text)
                    return {
                        'title': title,
                        'webpage_url': url,
                        'entries': [{'title': title, 'thumbnail': thumb, 'formats': formats}],
                        '_public_fallback': True,
                    }

            # Single-photo fallback. Prefer a real image_versions2 candidate
            # from Instagram page state, then fall back to og:image.
            image_lists = []
            for key in ('"image_versions2"', 'image_versions2'):
                image_lists.extend(_json_values_after_key(html_text, key))
            image_candidates = []
            for value in image_lists:
                if isinstance(value, dict):
                    image_candidates.extend([x for x in (value.get('candidates') or []) if isinstance(x, dict)])
            if image_candidates:
                # Keep all distinct image candidates as selectable quality variants.
                formats = []
                seen_urls = set()
                for n, c in enumerate(sorted(image_candidates, key=lambda x: ((x.get('height') or 0) * (x.get('width') or 0)), reverse=True), 1):
                    if c.get('url') in seen_urls:
                        continue
                    seen_urls.add(c.get('url'))
                    f = _format_from_image_candidate(c, n)
                    if f:
                        formats.append(f)
                if formats:
                    thumb = (formats[0] or {}).get('url')
                    return {
                        'title': title,
                        'webpage_url': url,
                        'entries': [{
                            'title': title,
                            'thumbnail': thumb,
                            'formats': formats,
                        }],
                        '_public_fallback': True,
                    }

            image_url = _public_page_og_image(html_text)
            if image_url:
                return {
                    'title': title,
                    'webpage_url': url,
                    'entries': [{
                        'title': title,
                        'thumbnail': image_url,
                        'formats': [_format_from_image_candidate({'url': image_url}, 1)],
                    }],
                    '_public_fallback': True,
                }
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise RuntimeError(f'Public Instagram page fallback failed: {last_error}')
    raise RuntimeError('Instagram public HTML did not contain an accessible media URL.')

def extract_info(url):
    """Resolve Instagram media, with a public HTML/embed fallback for image/carousel posts."""
    key = cache_key(url)
    now = time.time()
    hit = RESOLVE_CACHE.get(key)
    if hit and now - hit['created'] < CACHE_TTL:
        return hit['info']

    errors = []
    candidates = [url]
    candidates.extend(instagram_embed_candidates(url))

    for attempt, candidate in enumerate(candidates):
        try:
            info = extract_info_once(candidate)
            entries = entries_from(info)
            if not entries:
                raise RuntimeError('Instagram returned no accessible media.')
            if not any((e.get('formats') or e.get('url')) for e in entries):
                raise RuntimeError('Instagram returned an empty media response.')
            RESOLVE_CACHE[key] = {'created': time.time(), 'info': info}
            if len(RESOLVE_CACHE) > 20:
                oldest = min(RESOLVE_CACHE, key=lambda k: RESOLVE_CACHE[k]['created'])
                RESOLVE_CACHE.pop(oldest, None)
            return info
        except Exception as exc:
            errors.append(str(exc))
            time.sleep(0.2)

    # yt-dlp currently reports image-only Instagram carousels as having no video
    # formats. A public HTML/embed fallback can still expose image/video URLs.
    try:
        fallback = extract_public_instagram_media(url)
        entries = entries_from(fallback)
        if entries and any((e.get('formats') or e.get('url')) for e in entries):
            RESOLVE_CACHE[key] = {'created': time.time(), 'info': fallback}
            if len(RESOLVE_CACHE) > 20:
                oldest = min(RESOLVE_CACHE, key=lambda k: RESOLVE_CACHE[k]['created'])
                RESOLVE_CACHE.pop(oldest, None)
            return fallback
    except Exception as exc:
        errors.append(str(exc))

    if errors:
        raise RuntimeError(errors[-1][:1400])
    raise RuntimeError('Instagram media could not be resolved.')

def entries_from(info):
    raw = info.get('entries')
    return [e for e in raw if e] if raw else [info]


def actual_label(width, height):
    w = int(width or 0)
    h = int(height or 0)
    if not w or not h:
        return 'Unknown'
    # Match downloader-style naming by using the larger dimension.
    long_edge = max(w, h)
    return f'{long_edge}p'


def bitrate_label(kbps):
    if not kbps:
        return 'Bitrate n/a'
    if kbps >= 1000:
        return f'{kbps / 1000:.2f} Mbps'
    return f'{round(kbps):d} kbps'


def codec_short(codec):
    if not codec or codec == 'none':
        return None
    return codec.split('.', 1)[0]


def video_variants(formats):
    variants = []
    seen = set()
    for f in formats:
        if not f.get('url') or f.get('vcodec') in (None, 'none'):
            continue
        w = int(f.get('width') or 0)
        h = int(f.get('height') or 0)
        if not w or not h:
            continue
        tbr = float(f.get('tbr') or 0)
        fps = float(f.get('fps') or 0)
        size = int(filesize(f) or 0)
        key = (
            w, h, round(tbr, 2), round(fps, 2), codec_short(f.get('vcodec')),
            codec_short(f.get('acodec')), f.get('ext'), f.get('protocol'),
        )
        # Keep distinct bitrate/resolution/codec representations, while removing exact duplicates.
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            'format_id': str(f.get('format_id') or ''),
            'label': actual_label(w, h),
            'width': w,
            'height': h,
            'resolution': f'{w} × {h}',
            'long_edge': max(w, h),
            'short_edge': min(w, h),
            'fps': fps or None,
            'tbr': tbr or None,
            'bitrate_label': bitrate_label(tbr),
            'filesize': size or None,
            'filesize_label': fmt_bytes(size),
            'ext': f.get('ext') or 'media',
            'vcodec': codec_short(f.get('vcodec')),
            'acodec': codec_short(f.get('acodec')),
            'has_audio': f.get('acodec') not in (None, 'none'),
            'format_note': f.get('format_note'),
            'protocol': f.get('protocol'),
        })
    variants.sort(key=lambda v: (
        v['long_edge'], v['short_edge'], float(v['tbr'] or 0),
        float(v['fps'] or 0), int(v['filesize'] or 0)
    ), reverse=True)
    return variants


def image_variants(formats):
    variants = []
    for f in formats:
        if not f.get('url') or f.get('vcodec') not in (None, 'none'):
            continue
        w = int(f.get('width') or 0)
        h = int(f.get('height') or 0)
        variants.append({
            'format_id': str(f.get('format_id') or ''),
            'label': 'Original image' if not (w and h) else f'{w} × {h}',
            'width': w or None,
            'height': h or None,
            'resolution': f'{w} × {h}' if w and h else None,
            'long_edge': max(w, h) if w and h else 0,
            'short_edge': min(w, h) if w and h else 0,
            'fps': None,
            'tbr': None,
            'bitrate_label': 'Image',
            'filesize': int(filesize(f) or 0) or None,
            'filesize_label': fmt_bytes(filesize(f)),
            'ext': f.get('ext') or 'jpg',
            'vcodec': None,
            'acodec': None,
            'has_audio': False,
            'format_note': f.get('format_note'),
            'protocol': f.get('protocol'),
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


def best_variant_obj(variants):
    return variants[0] if variants else None


def choose_format_by_id(formats, format_id):
    if not format_id:
        return None
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
        'index': idx,
        'type': 'video' if is_video else 'image',
        'title': item.get('title') or fallback_title or f'Instagram media {idx}',
        'thumbnail': thumb,
        'width': (chosen or {}).get('width'),
        'height': (chosen or {}).get('height'),
        'fps': (chosen or {}).get('fps'),
        'duration': item.get('duration'),
        'formats_count': len(formats),
        'variant_count': len(variants),
        'filesize': filesize(chosen or {}),
        'tbr': (chosen or {}).get('tbr'),
        'abr': (best_audio or {}).get('abr'),
        'vcodec': (chosen or {}).get('vcodec'),
        'acodec': (chosen or {}).get('acodec'),
        'ext': (chosen or {}).get('ext') or item.get('ext'),
        'format_note': (chosen or {}).get('format_note'),
        'best_video_id': (best_video or {}).get('format_id'),
        'best_audio_id': (best_audio or {}).get('format_id'),
        'best_progressive_id': (best_progressive or {}).get('format_id'),
        'variants': variants,
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
    '/about': 'about.html',
    '/privacy': 'privacy.html',
    '/terms': 'terms.html',
    '/copyright': 'copyright.html',
    '/contact': 'contact.html',
}

for _path, _filename in PAGE_MAP.items():
    app.add_url_rule(_path, endpoint='page_' + _filename.replace('.', '_'), view_func=lambda _filename=_filename: app.send_static_file(_filename))

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
            'ok': True,
            'media_count': len(media),
            'media': media,
            'title': info.get('title') or 'Instagram media',
            'webpage_url': info.get('webpage_url') or url,
            'max_dimension': max_dimension,
            'total_variants': total_variants,
            'note': 'Every distinct video representation exposed by the extractor is shown. Selecting a source downloads that exact video representation and, when needed, muxes the best accessible audio without re-encoding the video.',
        })
    except Exception as exc:
        message = str(exc)[:1400]
        if 'empty media response' in message.lower():
            message = ('Instagram did not expose media to the anonymous extractor for this URL. '
                       'The same public post may still work later or through a different Instagram representation.')
        return jsonify({'error': message}), 502


def header_blob(headers):
    if not headers:
        return ''
    lines = []
    for k, v in headers.items():
        if k.lower() in {'host', 'content-length'}:
            continue
        lines.append(f'{k}: {v}')
    return '\r\n'.join(lines) + ('\r\n' if lines else '')


def http_stream(url, headers=None, chunk_size=256 * 1024):
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=60) as r:
        while True:
            chunk = r.read(chunk_size)
            if not chunk:
                return
            yield chunk


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
        # Re-resolve immediately before download so CDN URLs and their headers are fresh.
        info = extract_info(url)
        item, chosen, best_audio = selected_media(info, media_index, format_id)

        if not chosen or not chosen.get('url'):
            return jsonify({'error': 'The selected source variant is no longer available. Analyze the URL again and retry.'}), 502

        title = safe_name(item.get('title') or info.get('title') or f'instagram-media-{media_index}')

        # If the selected representation already contains audio, stream that exact URL.
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

        # Video-only formats are downloaded with yt-dlp itself. This is more reliable
        # than handing Instagram CDN URLs directly to ffmpeg because yt-dlp preserves
        # the extractor's per-format HTTP headers and CDN access parameters.
        if chosen.get('vcodec') not in (None, 'none'):
            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                return jsonify({'error': 'ffmpeg is not available on the server.'}), 502

            temp_dir = tempfile.mkdtemp(prefix='instamax-')
            outtmpl = os.path.join(temp_dir, 'media.%(ext)s')

            # First attempt: exact selected video representation + best accessible audio.
            # yt-dlp/ffmpeg will remux without re-encoding when the streams are compatible.
            format_expr = f'{format_id}+bestaudio' if format_id else 'bestvideo+bestaudio/best'
            opts = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'format': format_expr,
                'outtmpl': outtmpl,
                'merge_output_format': 'mp4',
                'ffmpeg_location': ffmpeg,
                'overwrites': True,
            }

            downloaded_path = None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                files = [
                    os.path.join(temp_dir, name)
                    for name in os.listdir(temp_dir)
                    if os.path.isfile(os.path.join(temp_dir, name))
                    and not name.endswith(('.part', '.ytdl'))
                ]
                if files:
                    downloaded_path = max(files, key=os.path.getsize)
            except Exception:
                # If combining audio fails, fall back to the exact selected video stream.
                opts['format'] = str(format_id) if format_id else 'bestvideo'
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                files = [
                    os.path.join(temp_dir, name)
                    for name in os.listdir(temp_dir)
                    if os.path.isfile(os.path.join(temp_dir, name))
                    and not name.endswith(('.part', '.ytdl'))
                ]
                if files:
                    downloaded_path = max(files, key=os.path.getsize)

            if not downloaded_path:
                raise RuntimeError('The selected media could not be downloaded from Instagram.')

            ext = Path(downloaded_path).suffix.lower() or '.mp4'
            filename = title + ('.mp4' if ext in ('.mp4', '.m4v', '.mov') else ext)
            mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

            response = send_file(
                downloaded_path,
                mimetype=mime,
                as_attachment=True,
                download_name=filename,
                conditional=True,
                max_age=0,
            )

            def cleanup():
                try:
                    for name in os.listdir(temp_dir):
                        try:
                            os.remove(os.path.join(temp_dir, name))
                        except OSError:
                            pass
                    os.rmdir(temp_dir)
                except OSError:
                    pass

            response.call_on_close(cleanup)
            return response

        return jsonify({'error': 'The selected source variant cannot be downloaded from the exposed formats.'}), 502

    except Exception as exc:
        if temp_dir:
            try:
                for name in os.listdir(temp_dir):
                    try:
                        os.remove(os.path.join(temp_dir, name))
                    except OSError:
                        pass
                os.rmdir(temp_dir)
            except OSError:
                pass
        message = str(exc)[:1200]
        if 'empty media response' in message.lower():
            message = ('Instagram did not expose this media to the anonymous extractor. '
                       'Try analyzing again or use another publicly accessible post.')
        return jsonify({'error': 'Download failed: ' + message}), 502

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8787'))
    host = os.environ.get('HOST', '127.0.0.1')
    app.run(host=host, port=port, debug=False, threaded=True)
