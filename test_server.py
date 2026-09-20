import pytest
import server
from server import app, is_safe_ip, valid_instagram_url, classify_extractor_error
from yt_dlp.utils import DownloadError

@pytest.fixture
def client():
    app.config['TESTING'] = True
    server.RESOLVE_CACHE.clear()
    with app.test_client() as client:
        yield client


def test_health(client):
    rv = client.get('/api/health')
    assert rv.status_code == 200
    assert rv.get_json() == {'ok': True}


def test_safe_ip():
    assert is_safe_ip('8.8.8.8') == True
    assert is_safe_ip('127.0.0.1') == False
    assert is_safe_ip('10.0.0.5') == False
    assert is_safe_ip('192.168.1.1') == False


def test_valid_instagram_url():
    assert valid_instagram_url('https://www.instagram.com/p/CwYmC56okA8/') == True
    assert valid_instagram_url('ftp://www.instagram.com/p') == False
    assert valid_instagram_url('https://evil.com/p') == False
    assert valid_instagram_url('http://127.0.0.1') == False


def test_resolve_invalid(client):
    rv = client.post('/api/resolve', json={'url': 'http://evil.com/'})
    assert rv.status_code == 400


def test_resolve_empty(client):
    rv = client.post('/api/resolve', json={})
    assert rv.status_code == 400


def test_security_headers(client):
    rv = client.get('/api/health')
    assert rv.headers['X-Frame-Options'] == 'DENY'
    assert rv.headers['X-Content-Type-Options'] == 'nosniff'


def test_rate_limit(client):
    for i in range(25):
        rv = client.get('/api/health', environ_base={'REMOTE_ADDR': '127.0.0.5'})
        if i >= 20:
            assert rv.status_code == 429
        else:
            assert rv.status_code == 200


def _image_fallback():
    return {
        'title': 'Test image post',
        'webpage_url': 'https://www.instagram.com/p/abc123/',
        '_gallery_fallback': True,
        'entries': [{
            'title': 'Test image',
            'thumbnail': 'https://cdn.example.test/image.jpg',
            'formats': [{
                'format_id': 'img-1',
                'url': 'https://cdn.example.test/image.jpg',
                'width': 1080,
                'height': 1350,
                'ext': 'jpg',
                'vcodec': 'none',
                'acodec': 'none',
            }],
        }],
    }


def test_no_video_formats_routes_to_instaloader_carousel_fallback(monkeypatch):
    server.RESOLVE_CACHE.clear()

    monkeypatch.setattr(
        server,
        'extract_info_once',
        lambda _url: (_ for _ in ()).throw(DownloadError('No video formats found')),
    )

    fallback = {
        'title': 'Carousel',
        'webpage_url': 'https://www.instagram.com/p/carousel123/',
        '_instaloader_fallback': True,
        'entries': [
            {
                'title': 'Carousel',
                'thumbnail': 'https://cdn.example.test/1.jpg',
                'formats': [{
                    'format_id': 'img-instaloader-1',
                    'url': 'https://cdn.example.test/1.jpg',
                    'width': 1080,
                    'height': 1080,
                    'ext': 'jpg',
                    'vcodec': 'none',
                    'acodec': 'none',
                }],
            },
            {
                'title': 'Carousel',
                'thumbnail': 'https://cdn.example.test/2.jpg',
                'formats': [{
                    'format_id': 'img-instaloader-2',
                    'url': 'https://cdn.example.test/2.jpg',
                    'width': 1080,
                    'height': 1350,
                    'ext': 'jpg',
                    'vcodec': 'none',
                    'acodec': 'none',
                }],
            },
        ],
    }

    monkeypatch.setattr(server, 'instaloader_public_media', lambda _url: fallback)
    monkeypatch.setattr(
        server,
        'gallery_dl_public_media',
        lambda _url: pytest.fail('gallery-dl should not be reached when Instaloader succeeds'),
    )
    monkeypatch.setattr(
        server,
        'extract_public_instagram_media',
        lambda _url: pytest.fail('HTML fallback should not be reached when Instaloader succeeds'),
    )

    info = server.extract_info('https://www.instagram.com/p/carousel123/')

    assert info['_instaloader_fallback'] is True
    assert len(info['entries']) == 2
    assert all(e['formats'][0]['vcodec'] == 'none' for e in info['entries'])


def test_fallback_failure_is_not_hidden_by_no_video_error(monkeypatch):
    server.RESOLVE_CACHE.clear()

    monkeypatch.setattr(
        server,
        'extract_info_once',
        lambda _url: (_ for _ in ()).throw(DownloadError('No video formats found')),
    )
    monkeypatch.setattr(
        server,
        'instaloader_public_media',
        lambda _url: (_ for _ in ()).throw(RuntimeError('Instaloader fallback is not installed.')),
    )
    monkeypatch.setattr(
        server,
        'gallery_dl_public_media',
        lambda _url: (_ for _ in ()).throw(RuntimeError('gallery-dl failed to resolve usable URLs')),
    )
    monkeypatch.setattr(
        server,
        'extract_public_instagram_media',
        lambda _url: (_ for _ in ()).throw(RuntimeError('Instagram HTML did not contain accessible media.')),
    )

    with pytest.raises(server.ExtractionFailure) as caught:
        server.extract_info('https://www.instagram.com/p/fallbackfail123/')

    assert caught.value.category == 'extractor_error'
    assert 'No video formats found' not in caught.value.message
    assert 'HTML did not contain accessible media' in caught.value.message


def test_no_video_formats_routes_to_image_fallback(monkeypatch):
    server.RESOLVE_CACHE.clear()

    monkeypatch.setattr(
        server,
        'extract_info_once',
        lambda _url: (_ for _ in ()).throw(DownloadError('No video formats found')),
    )
    calls = []

    def fake_gallery(url):
        calls.append(url)
        return _image_fallback()

    monkeypatch.setattr(server, 'gallery_dl_public_media', fake_gallery)
    monkeypatch.setattr(server, 'extract_public_instagram_media', lambda _url: pytest.fail('HTML fallback should not be needed'))

    info = server.extract_info('https://www.instagram.com/p/abc123/')

    assert calls == ['https://www.instagram.com/p/abc123/']
    assert info['_gallery_fallback'] is True
    assert info['entries'][0]['formats'][0]['vcodec'] == 'none'



def test_empty_media_response_survives_extraction_classification(monkeypatch):
    server.RESOLVE_CACHE.clear()

    def fail(_url):
        raise DownloadError('Instagram sent an empty media response')

    monkeypatch.setattr(server, 'extract_info_once', fail)
    monkeypatch.setattr(server, 'gallery_dl_public_media', lambda _url: (_ for _ in ()).throw(RuntimeError('gallery unavailable')))
    monkeypatch.setattr(server, 'extract_public_instagram_media', lambda _url: (_ for _ in ()).throw(RuntimeError('html unavailable')))

    with pytest.raises(server.ExtractionFailure) as caught:
        server.extract_info('https://www.instagram.com/p/empty123/')

    assert caught.value.category == 'extractor_unavailable'
    assert caught.value.message == 'Instagram did not expose this media to the extractor.'


def test_resolve_success_contract_is_preserved(monkeypatch, client):
    info = {
        'title': 'Test post',
        'webpage_url': 'https://www.instagram.com/p/contract123/',
        'entries': [{
            'title': 'Test video',
            'thumbnail': 'https://cdn.example.test/thumb.jpg',
            'duration': 3,
            'formats': [{
                'format_id': 'vid-1',
                'url': 'https://cdn.example.test/video.mp4',
                'width': 720,
                'height': 1280,
                'ext': 'mp4',
                'vcodec': 'h264',
                'acodec': 'aac',
            }],
        }],
    }
    monkeypatch.setattr(server, 'extract_info', lambda _url: info)

    rv = client.post('/api/resolve', json={'url': 'https://www.instagram.com/p/contract123/'})

    assert rv.status_code == 200
    body = rv.get_json()
    for key in ('ok', 'media', 'media_count', 'title', 'webpage_url', 'total_variants'):
        assert key in body
    assert body['ok'] is True


def test_empty_media_response_is_extractor_unavailable(monkeypatch, client):
    def fail(_url):
        raise DownloadError('Instagram sent an empty media response')

    monkeypatch.setattr(server, 'extract_info', fail)

    rv = client.post('/api/resolve', json={'url': 'https://www.instagram.com/p/empty123/'})

    assert rv.status_code == 502
    body = rv.get_json()
    assert body['error_category'] == 'extractor_unavailable'
    assert body['error'] == 'Instagram did not expose this media to the extractor.'


def test_classifies_explicit_statuses_and_access_conditions():
    cases = [
        (DownloadError('ERROR: HTTP Error 429: Too Many Requests'), 'rate_limited'),
        (DownloadError('HTTP Error 429: Too Many Requests; No video formats found'), 'rate_limited'),
        (DownloadError('ERROR: HTTP Error 403: Forbidden'), 'access_denied'),
        (DownloadError('ERROR: HTTP Error 404: Not Found'), 'not_found'),
        (DownloadError('Login required to view this post'), 'login_required'),
        (DownloadError('This post is from a private account'), 'private'),
    ]
    for exc, category in cases:
        assert classify_extractor_error(exc)['category'] == category


def test_unknown_error_is_sanitized_without_internal_details():
    exc = RuntimeError('Extractor exploded at /tmp/secret/server.py Cookie: supersecret-token localhost:8787')
    result = classify_extractor_error(exc)
    assert result['category'] == 'extractor_error'
    assert 'supersecret-token' not in result['message']
    assert '/tmp/secret/server.py' not in result['message']
    assert 'localhost:8787' not in result['message']
