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


def extract_info(url):
    """Resolve an Instagram URL, retrying public embed variants on known failures."""
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
            # A successful HTTP response can still contain no useful media.
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
            if attempt == 0 and not is_empty_media_error(exc):
                break
            time.sleep(0.25)

    # Keep the most useful extractor error, but present it as a single JSON error
    # to the frontend rather than allowing an HTML/traceback response to leak out.
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
