from flask import Flask, request, jsonify, Response
import mimetypes
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yt_dlp
try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

app = Flask(__name__, static_folder='static', static_url_path='')
ALLOWED_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com'}
RESOLVE_CACHE = {}
CACHE_TTL = 180


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
    }


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


def cache_key(url):
    return url.split('#', 1)[0].strip()


def extract_info(url):
    key = cache_key(url)
    now = time.time()
    hit = RESOLVE_CACHE.get(key)
    if hit and now - hit['created'] < CACHE_TTL:
        return hit['info']
    with yt_dlp.YoutubeDL(extractor_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    RESOLVE_CACHE[key] = {'created': now, 'info': info}
    if len(RESOLVE_CACHE) > 20:
        oldest = min(RESOLVE_CACHE, key=lambda k: RESOLVE_CACHE[k]['created'])
        RESOLVE_CACHE.pop(oldest, None)
    return info


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
        return jsonify({'error': str(exc)[:1000]}), 502


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
        return Response('Invalid Instagram URL', status=400)
    try:
        info = extract_info(url)
        item, chosen, best_audio = selected_media(info, media_index, format_id)
        if not chosen or not chosen.get('url'):
            return Response('The selected source variant is no longer available. Analyze the URL again and retry.', status=502)

        title = safe_name(item.get('title') or info.get('title') or f'instagram-media-{media_index}')

        # Progressive media or image can be streamed directly without touching/re-encoding it.
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

        # Video-only source: merge with best accessible audio while copying both streams.
        ffmpeg = get_ffmpeg_path()
        if chosen.get('vcodec') not in (None, 'none') and best_audio and best_audio.get('url'):
            if not ffmpeg:
                return Response('ffmpeg is required to combine the selected video with audio. Run start.bat again.', status=502)
            args = [ffmpeg, '-hide_banner', '-loglevel', 'error']
            vh = header_blob(chosen.get('http_headers'))
            ah = header_blob(best_audio.get('http_headers'))
            if vh:
                args += ['-headers', vh]
            args += ['-i', chosen['url']]
            if ah:
                args += ['-headers', ah]
            args += [
                '-i', best_audio['url'],
                '-map', '0:v:0', '-map', '1:a:0?',
                '-c:v', 'copy', '-c:a', 'copy',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-f', 'mp4', 'pipe:1'
            ]
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

            def generate():
                stderr_data = b''
                try:
                    while True:
                        chunk = proc.stdout.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                    rc = proc.wait(timeout=20)
                    stderr_data = proc.stderr.read(2000) if proc.stderr else b''
                    if rc != 0:
                        err = stderr_data.decode('utf-8', 'ignore')[:700]
                        raise RuntimeError(err or 'ffmpeg failed')
                finally:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                    try:
                        proc.stderr.close()
                    except Exception:
                        pass

            headers = {
                'Content-Disposition': cd(title + '.mp4'),
                'Content-Type': 'video/mp4',
                'Cache-Control': 'no-store',
            }
            return Response(generate(), headers=headers, direct_passthrough=True)

        return Response('The selected source variant cannot be downloaded from the exposed formats.', status=502)
    except Exception as exc:
        return Response('Download failed: ' + str(exc)[:900], status=502)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8787'))
    host = os.environ.get('HOST', '127.0.0.1')
    app.run(host=host, port=port, debug=False, threaded=True)
