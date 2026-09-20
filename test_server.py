import pytest
from server import app, is_safe_ip, valid_instagram_url

@pytest.fixture
def client():
    app.config['TESTING'] = True
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
    # Send 25 requests
    for i in range(25):
        rv = client.get('/api/health', environ_base={'REMOTE_ADDR': '127.0.0.5'})
        if i >= 20: # 21st request and beyond should be 429
            assert rv.status_code == 429
        else:
            assert rv.status_code == 200
