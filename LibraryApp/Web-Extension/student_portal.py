from flask import Flask, session, jsonify, request, send_from_directory, send_file
import sqlite3
import os
import sys
import json
from datetime import datetime, timedelta
import socket
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
import time
import subprocess
from collections import defaultdict
import queue
try:
    from dotenv import load_dotenv
    # Try loading .env from multiple locations (repo root, parent, cwd)
    _env_loaded = load_dotenv()  # cwd
    if not _env_loaded:
        _env_loaded = load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))
    if not _env_loaded:
        _env_loaded = load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env'))
except ImportError:
    pass

# Setup path to import database.py from parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from database import PostgresConnectionWrapper
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    PostgresConnectionWrapper = None
    psycopg2 = None
    RealDictCursor = None
    POSTGRES_AVAILABLE = False

# Global connection pool for PostgreSQL (portal)
_portal_pg_pool = None
_portal_pool_lock = threading.Lock()
_portal_pool_failed = False  # Track if pool init already failed
_portal_cloud_fail_time = 0  # Timestamp of last cloud failure (retry after cooldown)
_PORTAL_CLOUD_RETRY_COOLDOWN = 60  # Retry cloud DB after 60 seconds
_library_schema_checked = False


def _ensure_sslmode_require(db_url: str) -> str:
    """Ensure DATABASE_URL contains sslmode=require."""
    try:
        parsed = urlparse(db_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if 'sslmode' not in query:
            query['sslmode'] = 'require'
            parsed = parsed._replace(query=urlencode(query))
            return urlunparse(parsed)
        return db_url
    except Exception:
        if 'sslmode' in (db_url or ''):
            return db_url
        sep = '&' if '?' in (db_url or '') else '?'
        return f"{db_url}{sep}sslmode=require"


def _extract_supabase_project_ref() -> str | None:
    """Extract Supabase project ref from SUPABASE_URL/VITE_SUPABASE_URL if available."""
    candidates = [os.getenv('SUPABASE_URL'), os.getenv('VITE_SUPABASE_URL')]
    for raw in candidates:
        if not raw:
            continue
        try:
            host = (urlparse(raw).hostname or '').strip().lower()
            if host.endswith('.supabase.co'):
                ref = host.split('.')[0]
                if ref:
                    return ref
        except Exception:
            continue
    return None


def _normalize_database_url(db_url: str) -> str:
    """Normalize DATABASE_URL; auto-fallback unresolved pooler host to direct DB host."""
    if not db_url:
        return db_url

    db_url = _ensure_sslmode_require(db_url)

    try:
        parsed = urlparse(db_url)
        host = (parsed.hostname or '').strip().lower()
        if not host:
            return db_url

        test_port = parsed.port or 5432
        try:
            socket.getaddrinfo(host, test_port)
            return db_url
        except Exception:
            pass

        if host.endswith('pooler.supabase.com'):
            ref = _extract_supabase_project_ref()
            if ref:
                direct_host = f"db.{ref}.supabase.co"
                userinfo = ''
                if parsed.username:
                    userinfo = parsed.username
                    if parsed.password is not None:
                        userinfo += f":{parsed.password}"
                    userinfo += '@'
                new_netloc = f"{userinfo}{direct_host}:5432"
                rewritten = urlunparse(parsed._replace(netloc=new_netloc))
                rewritten = _ensure_sslmode_require(rewritten)
                print(f"[Portal] Replaced unresolved Supabase pooler host '{host}' with '{direct_host}'")
                return rewritten
    except Exception:
        pass

    return db_url


# --- Configuration ---
# --- Configuration ---
if getattr(sys, 'frozen', False):
    # Running as PyInstaller executable
    # Use the directory of the executable for persistence
    BASE_DIR = os.path.join(os.path.dirname(sys.executable), 'Web-Extension')
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    # Running as script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads', 'study_materials')
REGISTRATION_PHOTO_FOLDER = os.path.join(BASE_DIR, 'uploads', 'registration_photos')
PROFILE_PHOTO_FOLDER = os.path.join(BASE_DIR, 'uploads', 'profile_photos')
os.makedirs(PROFILE_PHOTO_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'ppt', 'pptx', 'txt', 'jpg', 'jpeg', 'png', 'zip', 'rar'}
ALLOWED_PHOTO_EXTENSIONS = {'jpg', 'jpeg', 'png'}
MAX_PHOTO_BYTES = 50 * 1024  # 50KB


_fine_cache = {
    'path': None,
    'mtime': None,
    'value': 5,
    'last_checked': 0,
}


def _get_library_settings_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'library_settings.json')
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'library_settings.json')


def get_portal_fine_per_day():
    """Get fine/day dynamically from shared DB settings (preferred), then file/env fallback."""
    default_fine = 5
    now_ts = time.time()
    # Short cache window to avoid DB/file reads on every request
    if (now_ts - float(_fine_cache.get('last_checked', 0) or 0)) < 15:
        return _fine_cache.get('value', default_fine) or default_fine

    # 1) Preferred source: synced DB setting (available on Render)
    try:
        conn = get_library_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", ('fine_per_day',))
        row = cursor.fetchone()
        conn.close()
        if row:
            try:
                raw = row['value']
            except Exception:
                raw = row[0]
            _fine_cache['value'] = int(float(raw))
            _fine_cache['last_checked'] = now_ts
            return _fine_cache['value']
    except Exception:
        pass

    # 2) On server (Render), DO NOT use local file fallback (can be stale).
    if os.getenv('RENDER') or os.getenv('IS_SERVER'):
        env_fine = os.getenv('FINE_PER_DAY')
        if env_fine is not None:
            try:
                _fine_cache['value'] = int(float(env_fine))
            except Exception:
                _fine_cache['value'] = default_fine
        _fine_cache['last_checked'] = now_ts
        return _fine_cache.get('value', default_fine) or default_fine

    # 3) Local file fallback (desktop script/exe mode)
    settings_path = _get_library_settings_path()
    _fine_cache['path'] = settings_path

    try:
        mtime = os.path.getmtime(settings_path)
    except Exception:
        # 4) Environment fallback
        env_fine = os.getenv('FINE_PER_DAY')
        if env_fine is not None:
            try:
                _fine_cache['value'] = int(float(env_fine))
            except Exception:
                _fine_cache['value'] = default_fine
        _fine_cache['last_checked'] = now_ts
        return _fine_cache.get('value', default_fine) or default_fine

    # Reload only when file changes
    if _fine_cache.get('mtime') != mtime:
        try:
            with open(settings_path, 'r') as f:
                settings = json.load(f)
            _fine_cache['value'] = int(settings.get('fine_per_day', default_fine))
            _fine_cache['mtime'] = mtime
            print(f"[Portal] fine_per_day refreshed: {_fine_cache['value']}")
        except Exception as e:
            print(f"[Portal] Failed to refresh fine_per_day from settings: {e}")

    _fine_cache['last_checked'] = now_ts
    return _fine_cache.get('value', default_fine) or default_fine

# Create upload folder if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REGISTRATION_PHOTO_FOLDER, exist_ok=True)


# --- Bootstrapping for PyInstaller ---
if getattr(sys, 'frozen', False):
    import shutil
    
    # 1. Bootstrap portal.db
    portal_db_path = os.path.join(BASE_DIR, 'portal.db')
    if not os.path.exists(portal_db_path):
        try:
            bundled_portal = os.path.join(sys._MEIPASS, 'Web-Extension', 'portal.db')
            if os.path.exists(bundled_portal):
                shutil.copy2(bundled_portal, portal_db_path)
                print(f"[Bootstrap] Copied bundled portal.db to {portal_db_path}")
        except Exception as e:
            print(f"[Bootstrap] Error copying portal.db: {e}")

    # 2. Bootstrap library.db (Parent of Web-Extension)
    repo_root = os.path.dirname(BASE_DIR)
    library_db_path = os.path.join(repo_root, 'library.db')
    if not os.path.exists(library_db_path):
        try:
            bundled_library = os.path.join(sys._MEIPASS, 'library.db')
            if os.path.exists(bundled_library):
                shutil.copy2(bundled_library, library_db_path)
                print(f"[Bootstrap] Copied bundled library.db to {library_db_path}")
        except Exception as e:
            print(f"[Bootstrap] Error copying library.db: {e}")


def _is_postgres_connection(conn) -> bool:
    """Best-effort check to determine if this is a PostgresConnectionWrapper."""
    try:
        return PostgresConnectionWrapper is not None and isinstance(conn, PostgresConnectionWrapper)
    except Exception:
        return False


def _push_to_cloud(sql, params=None):
    """Fire-and-forget: replicate a write to Supabase in a background thread.
    Used by student_portal endpoints that modify library data on desktop (local SQLite)
    to keep Supabase in sync for the web portal on Render."""
    database_url = _normalize_database_url(os.getenv('DATABASE_URL'))
    if not database_url or not POSTGRES_AVAILABLE:
        return
    # Don't push if we're already on Render (writes go directly to Postgres)
    if os.getenv('RENDER') or os.getenv('IS_SERVER'):
        return
    def _do_push():
        try:
            pg_sql = sql.replace('?', '%s')
            pg_sql = pg_sql.replace('INSTR(', 'STRPOS(').replace('instr(', 'strpos(')
            conn = psycopg2.connect(database_url, connect_timeout=5)
            cur = conn.cursor()
            cur.execute(pg_sql, params)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Portal Cloud Push] Failed (sync will catch up): {e}")
    threading.Thread(target=_do_push, daemon=True).start()


def _requests_pk_column(conn) -> str:
    """Return the primary key column for the requests table ('req_id' or legacy 'id')."""
    try:
        cursor = conn.cursor()
        if _is_postgres_connection(conn):
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'requests'
            """)
            cols = {str(r[0]) for r in cursor.fetchall()}
        else:
            cursor.execute("PRAGMA table_info(requests)")
            cols = {str(r['name']) for r in cursor.fetchall()}
    except Exception:
        # Default to modern schema
        return 'req_id'

    if 'req_id' in cols:
        return 'req_id'
    if 'id' in cols:
        return 'id'
    # Last resort
    return 'req_id'


def _safe_str(v):
    return str(v).strip() if v is not None else ''


def _normalize_enrollment(enrollment: str) -> str:
    return _safe_str(enrollment).upper()


def _allowed_photo_filename(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS


def _read_file_size_bytes(file_storage) -> int:
    """Return size of an uploaded file without trusting Content-Length."""
    try:
        pos = file_storage.stream.tell()
        file_storage.stream.seek(0, os.SEEK_END)
        size = int(file_storage.stream.tell())
        file_storage.stream.seek(pos, os.SEEK_SET)
        return size
    except Exception:
        # Fallback: read into memory (photo is limited to 50KB anyway)
        try:
            data = file_storage.read()
            file_storage.stream.seek(0)
            return len(data)
        except Exception:
            return 0

# --- Secure Secret Key Management ---
def get_or_create_secret_key():
    """Get secret key from env variable, or generate and persist one locally"""
    # 1. Check environment variable first
    env_key = os.environ.get('FLASK_SECRET_KEY') or os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    
    # 2. Check for persisted key file
    key_file = os.path.join(BASE_DIR, '.secret_key')
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            return f.read().strip()
    
    # 3. Generate new key and persist it
    import secrets
    new_key = secrets.token_hex(32)
    try:
        with open(key_file, 'w') as f:
            f.write(new_key)
        print(f"[Security] Generated new secret key and saved to {key_file}")
    except Exception as e:
        print(f"[Security] Warning: Could not persist secret key: {e}")
    return new_key

# Serve React Build
app = Flask(__name__, static_folder='frontend/dist')
app.secret_key = get_or_create_secret_key()

# --- Startup diagnostics (visible in Render logs) ---
print(f"[STARTUP] DATABASE_URL set: {bool(os.getenv('DATABASE_URL'))}")
print(f"[STARTUP] POSTGRES_AVAILABLE (psycopg2 imported): {POSTGRES_AVAILABLE}")
print(f"[STARTUP] BASE_DIR: {BASE_DIR}")
print(f"[STARTUP] CWD: {os.getcwd()}")


# --- Build / Version info (helps diagnose "server not updated") ---
APP_START_TIME_UTC = datetime.utcnow().isoformat(timespec='seconds') + 'Z'


def _best_effort_git_commit() -> str | None:
    """Return git commit hash if available (works locally; may be unavailable in some deployments)."""
    try:
        # Avoid hanging in some environments
        out = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        v = out.decode('utf-8', errors='ignore').strip()
        return v or None
    except Exception:
        return None


APP_VERSION = (
    os.environ.get('APP_VERSION')
    or os.environ.get('GIT_COMMIT')
    or _best_effort_git_commit()
    or APP_START_TIME_UTC
)


@app.after_request
def _add_version_header(resp):
    # Always present so you can quickly check in browser DevTools (Network tab)
    resp.headers['X-App-Version'] = APP_VERSION
    return resp


@app.get('/api/version')
def api_version():
    """Debug endpoint to confirm what code/static build the server is running."""
    info = {
        'status': 'success',
        'app_version': APP_VERSION,
        'app_start_time_utc': APP_START_TIME_UTC,
        'pid': os.getpid(),
        'database_url_set': bool(os.getenv('DATABASE_URL')),
        'postgres_available': POSTGRES_AVAILABLE,
        'pool_initialized': _portal_pg_pool is not None,
        'pool_failed': _portal_pool_failed,
    }

    try:
        index_path = os.path.join(app.static_folder, 'index.html')
        st = os.stat(index_path)
        info['static_index'] = {
            'path': 'frontend/dist/index.html',
            'size_bytes': int(st.st_size),
            'mtime_utc': datetime.utcfromtimestamp(st.st_mtime).isoformat(timespec='seconds') + 'Z',
        }
    except Exception:
        info['static_index'] = None

    return jsonify(info)


# --- Rate Limiter (Custom Implementation - No External Dependencies) ---
class RateLimiter:
    """In-memory sliding window rate limiter"""
    def __init__(self):
        self.requests = defaultdict(list)  # {key: [timestamps]}
        self.lock = threading.Lock()
        
        # Rate limit configurations: {endpoint_pattern: (max_requests, window_seconds)}
        self.limits = {
            '/api/login': (5, 60),           # 5 attempts per minute
            '/api/public/forgot-password': (3, 300),  # 3 requests per 5 minutes
            '/api/change_password': (3, 60),  # 3 attempts per minute
            'default': (60, 60)               # 60 requests per minute (default)
        }
    
    def _get_client_key(self, endpoint):
        """Generate unique key for client + endpoint"""
        # Use IP address as identifier
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
        return f"{client_ip}:{endpoint}"
    
    def _cleanup_old_requests(self, key, window_seconds):
        """Remove requests outside the time window"""
        cutoff = time.time() - window_seconds
        self.requests[key] = [ts for ts in self.requests[key] if ts > cutoff]
    
    def is_rate_limited(self, endpoint):
        """Check if request should be rate limited"""
        # Get limit config for endpoint
        limit_config = self.limits.get(endpoint, self.limits['default'])
        max_requests, window_seconds = limit_config
        
        key = self._get_client_key(endpoint)
        current_time = time.time()
        
        with self.lock:
            self._cleanup_old_requests(key, window_seconds)
            
            if len(self.requests[key]) >= max_requests:
                return True, max_requests, window_seconds
            
            # Record this request
            self.requests[key].append(current_time)
            return False, max_requests, window_seconds
    
    def get_retry_after(self, endpoint):
        """Get seconds until rate limit resets"""
        limit_config = self.limits.get(endpoint, self.limits['default'])
        _, window_seconds = limit_config
        key = self._get_client_key(endpoint)
        
        if self.requests[key]:
            oldest = min(self.requests[key])
            return int(window_seconds - (time.time() - oldest)) + 1
        return window_seconds

# Initialize rate limiter
rate_limiter = RateLimiter()

def rate_limit(f):
    """Decorator to apply rate limiting to an endpoint"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        endpoint = request.path
        is_limited, max_req, window = rate_limiter.is_rate_limited(endpoint)
        
        if is_limited:
            retry_after = rate_limiter.get_retry_after(endpoint)
            response = jsonify({
                'status': 'error',
                'message': f'Too many requests. Please try again in {retry_after} seconds.',
                'retry_after': retry_after
            })
            response.status_code = 429
            response.headers['Retry-After'] = str(retry_after)
            return response
        
        return f(*args, **kwargs)
    return decorated_function


# --- CSRF Protection (Double-Submit Cookie Pattern) ---
def generate_csrf_token():
    """Generate a secure random CSRF token"""
    import secrets
    return secrets.token_hex(32)

# Endpoints excluded from CSRF protection (login flow needs cookie first)
CSRF_EXCLUDED_ENDPOINTS = [
    '/api/login',
    '/api/public/forgot-password',
    '/api/public/register',
    '/api/change_password',  # Part of first-time login flow
    '/api/request',  # Student requests (session-protected)
    '/api/settings',  # Settings update (session-protected)
    '/api/request-deletion',  # Deletion request (session-protected)
    '/api/admin/notices',  # Desktop app access
    '/api/admin/requests',  # Desktop app access
    '/api/admin/deletion',  # Desktop app access
]


@app.before_request
def enforce_admin_local_access():
    """Ensure admin endpoints are only accessible from the local machine (Desktop App)."""
    if request.path.startswith('/api/admin/'):
        # Only allow localhost IPs.
        # Explicitly ignoring X-Forwarded-For header to prevent IP spoofing.
        if request.remote_addr not in ['127.0.0.1', '::1']:
            return jsonify({
                'status': 'error',
                'message': 'Forbidden: Admin access restricted to local application.'
            }), 403


@app.before_request
def csrf_protect():
    """Validate CSRF token for state-changing requests"""
    # Skip for safe methods (GET, HEAD, OPTIONS)
    if request.method in ['GET', 'HEAD', 'OPTIONS']:
        return
    
    # Skip for excluded endpoints
    if request.path in CSRF_EXCLUDED_ENDPOINTS:
        return
    
    # Skip for admin endpoints (desktop app access only)
    if request.path.startswith('/api/admin/'):
        return
    
    # Skip for static files
    if request.path.startswith('/static') or request.path.startswith('/assets'):
        return
    
    # Get token from header and cookie
    header_token = request.headers.get('X-CSRF-Token')
    cookie_token = request.cookies.get('csrf_token')
    
    # Validate both exist and match
    if not header_token or not cookie_token or header_token != cookie_token:
        return jsonify({
            'status': 'error', 
            'message': 'CSRF token missing or invalid. Please refresh the page.'
        }), 403

@app.after_request
def set_csrf_cookie(response):
    """Set CSRF token cookie on every response if not present"""
    if 'csrf_token' not in request.cookies:
        token = generate_csrf_token()
        # httponly=False so JavaScript can read it
        # samesite='Lax' for balance of security and usability
        response.set_cookie(
            'csrf_token', 
            token, 
            httponly=False, 
            samesite='Lax',
            max_age=86400  # 24 hours
        )
    return response


# --- Observability: Logging Middleware (serialized via queue) ---
_log_queue = queue.Queue()

def _log_writer_loop():
    """Single background thread that drains the log queue and writes in batches."""
    while True:
        batch = []
        try:
            # Block until at least one item
            batch.append(_log_queue.get(timeout=5))
            # Drain remaining without blocking
            while not _log_queue.empty():
                try:
                    batch.append(_log_queue.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            continue
        
        if batch:
            try:
                conn = get_portal_db()
                cursor = conn.cursor()
                cursor.executemany(
                    "INSERT INTO access_logs (endpoint, method, status) VALUES (?, ?, ?)",
                    batch
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Logging batch failed: {e}")

_log_writer_thread = threading.Thread(target=_log_writer_loop, daemon=True)
_log_writer_thread.start()

@app.after_request
def log_request(response):
    """Log every request to the access_logs table (queued, non-blocking)"""
    if request.path.startswith('/static') or request.path.startswith('/assets'):
        return response
    
    try:
        _log_queue.put_nowait((request.path, request.method, response.status_code))
    except queue.Full:
        pass
        
    return response


@app.after_request
def disable_api_response_caching(response):
    """Force fresh API reads to avoid stale dashboard/session data from browser caches."""
    try:
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
    except Exception:
        pass
    return response

def cleanup_logs():
    """Delete logs older than 7 days"""
    try:
        conn = get_portal_db()
        cursor = conn.cursor()
        cutoff_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute("DELETE FROM access_logs WHERE timestamp < ?", (cutoff_date,))
        conn.commit()
        conn.close()
        print("System: Cleaned up old access logs.")
    except Exception as e:
        print(f"Log cleanup failed: {e}")


def _log_portal_exception(context: str, exc: Exception) -> str:
    """Log exceptions to a local file for debugging portal 500 errors.

    Returns a short error id that can be shared to find the log entry.
    """
    try:
        import traceback
        error_id = f"E{int(time.time())}"
        log_path = os.path.join(BASE_DIR, 'portal_errors.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{datetime.now().isoformat()} [{error_id}] {context}\n")
            f.write(traceback.format_exc())
            f.write("\n")
        return error_id
    except Exception:
        return "EUNKNOWN"


def _parse_date_any(value):
    """Parse a value into a `date`.

    Supports SQLite text dates and Postgres date/datetime objects.
    Returns `datetime.date` or None.
    """
    if value is None:
        return None

    # psycopg2 may return date/datetime objects
    try:
        from datetime import date as _date
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, _date):
            return value
    except Exception:
        pass

    s = str(value).strip()
    if not s:
        return None

    # Common formats we have seen across environments
    fmts = (
        '%Y-%m-%d',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S.%f%z',
    )

    # Try fast ISO parsing first
    try:
        # Handles YYYY-MM-DD and many ISO datetime strings
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.date()
    except Exception:
        pass

    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue

    return None


def _to_iso_date(value):
    d = _parse_date_any(value)
    return d.isoformat() if d else (str(value) if value is not None else None)


@app.route('/api/admin/observability', methods=['GET'])
def api_admin_observability():
    """Observability stats used by the Desktop Admin -> Observability tab.

    The desktop UI expects specific keys (total_24h, hourly_data, endpoint_data, etc.).
    """
    try:
        def _parse_ts(value):
            if value is None:
                return None
            if isinstance(value, datetime):
                return value
            s = str(value)
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    continue
            return None

        now = datetime.now()
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)

        conn = get_portal_db()
        cursor = conn.cursor()

        # Pull last 7 days and compute everything in Python to keep it backend-agnostic (SQLite/Postgres).
        cursor.execute(
            "SELECT endpoint, status, timestamp FROM access_logs WHERE timestamp >= ?",
            (cutoff_7d.strftime('%Y-%m-%d %H:%M:%S'),)
        )
        rows_7d = [dict(r) for r in cursor.fetchall()]
        conn.close()

        # Normalize
        normalized = []
        for r in rows_7d:
            ts = _parse_ts(r.get('timestamp'))
            try:
                status = int(r.get('status') or 0)
            except Exception:
                status = 0
            normalized.append({
                'endpoint': r.get('endpoint') or '',
                'status': status,
                'ts': ts,
            })

        rows_24h = [r for r in normalized if r['ts'] and r['ts'] >= cutoff_24h]

        total_24h = len(rows_24h)
        total_7d = len([r for r in normalized if r['ts']])

        success_24h = sum(1 for r in rows_24h if 200 <= r['status'] < 300)
        errors_24h = sum(1 for r in rows_24h if r['status'] >= 400)
        success_rate = (success_24h / total_24h * 100.0) if total_24h else 0.0

        # Hourly breakdown (00-23)
        hourly_data = {f"{h:02d}": 0 for h in range(24)}
        for r in rows_24h:
            h = r['ts'].hour
            hourly_data[f"{h:02d}"] = hourly_data.get(f"{h:02d}", 0) + 1

        peak_count = max(hourly_data.values()) if hourly_data else 0
        peak_hour_key = None
        if peak_count > 0:
            # choose earliest peak hour for stability
            for h in sorted(hourly_data.keys()):
                if hourly_data[h] == peak_count:
                    peak_hour_key = h
                    break
        peak_hour = f"{peak_hour_key}:00" if peak_hour_key is not None else 'N/A'

        # Endpoint counts (last 24h)
        ep_counts = {}
        for r in rows_24h:
            ep = r['endpoint']
            ep_counts[ep] = ep_counts.get(ep, 0) + 1
        endpoint_data = sorted(ep_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        # Status buckets (last 24h)
        status_data = {
            '2xx Success': 0,
            '3xx Redirect': 0,
            '4xx Client Error': 0,
            '5xx Server Error': 0,
        }
        for r in rows_24h:
            s = r['status']
            if 200 <= s < 300:
                status_data['2xx Success'] += 1
            elif 300 <= s < 400:
                status_data['3xx Redirect'] += 1
            elif 400 <= s < 500:
                status_data['4xx Client Error'] += 1
            elif s >= 500:
                status_data['5xx Server Error'] += 1

        # 7-day trend: YYYY-MM-DD -> count
        trend_counts = {}
        for r in normalized:
            if not r['ts']:
                continue
            day = r['ts'].strftime('%Y-%m-%d')
            trend_counts[day] = trend_counts.get(day, 0) + 1
        trend_data = sorted(trend_counts.items(), key=lambda x: x[0])

        return jsonify({
            'status': 'ok',
            'total_24h': total_24h,
            'total_7d': total_7d,
            'success_24h': success_24h,
            'success_rate': success_rate,
            'errors_24h': errors_24h,
            'peak_hour': peak_hour,
            'peak_count': peak_count,
            'hourly_data': hourly_data,
            'endpoint_data': endpoint_data,
            'status_data': status_data,
            'trend_data': trend_data,
        })
    except Exception as e:
        error_id = _log_portal_exception('api_admin_observability', e)
        return jsonify({'status': 'error', 'message': 'Observability failed', 'error_id': error_id}), 500

def get_db_connection(local_db_name):
    """Local-first on desktop, Postgres on Render (cloud deployment).
    Desktop: always SQLite for speed; SyncManager syncs to Supabase in background.
    Render: always Postgres since the server is co-located with Supabase."""
    global _portal_pg_pool, _portal_pool_failed
    
    database_url = _normalize_database_url(os.getenv('DATABASE_URL'))
    is_server_deploy = os.getenv('RENDER') or os.getenv('IS_SERVER')
    
    # On Render (cloud deployment), use Postgres directly — low latency, no local state needed
    if is_server_deploy and database_url and POSTGRES_AVAILABLE:
        # Initialize pool once
        if _portal_pg_pool is None and not _portal_pool_failed:
            with _portal_pool_lock:
                if _portal_pg_pool is None and not _portal_pool_failed:
                    # Disabled ThreadedConnectionPool to prevent SSL drops with Supabase pooler
                    _portal_pool_failed = True
        
        if _portal_pg_pool:
            try:
                conn = _portal_pg_pool.getconn()
                conn.autocommit = False
                return PostgresConnectionWrapper(conn, pool=_portal_pg_pool)
            except Exception:
                pass
        
        try:
            conn = psycopg2.connect(database_url, connect_timeout=10)
            return PostgresConnectionWrapper(conn)
        except Exception as e:
            msg = f"[ERROR] Portal: Cloud DB unreachable on server deployment ({e})."
            print(msg)
            # On server we must not silently serve stale packaged SQLite data.
            raise RuntimeError(msg)
    
    # Desktop / local: always use SQLite for instant response
    if local_db_name == 'library.db':
        db_path = os.path.join(os.path.dirname(BASE_DIR), 'library.db')
    else:
        db_path = os.path.join(BASE_DIR, 'portal.db')
        
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for concurrent read/write access (prevents "database is locked")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return conn

def _ensure_local_library_schema():
    """Create core local library tables if missing.
    Prevents login failures like 'no such table: students' after fresh DB clears."""
    global _library_schema_checked

    if _library_schema_checked:
        return

    # Only for desktop/local SQLite mode
    is_server_deploy = os.getenv('RENDER') or os.getenv('IS_SERVER')
    if is_server_deploy:
        _library_schema_checked = True
        return

    lib_path = os.path.join(os.path.dirname(BASE_DIR), 'library.db')
    conn = sqlite3.connect(lib_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enrollment_no TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                department TEXT,
                year TEXT,
                date_registered DATE DEFAULT CURRENT_DATE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                isbn TEXT,
                category TEXT,
                total_copies INTEGER DEFAULT 1,
                available_copies INTEGER DEFAULT 1,
                date_added DATE DEFAULT CURRENT_DATE,
                barcode TEXT,
                price REAL DEFAULT 0,
                cover_url TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS borrow_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enrollment_no TEXT NOT NULL,
                book_id TEXT NOT NULL,
                borrow_date DATE NOT NULL,
                due_date DATE NOT NULL,
                return_date DATE,
                status TEXT DEFAULT 'borrowed',
                fine INTEGER DEFAULT 0,
                academic_year TEXT,
                FOREIGN KEY (enrollment_no) REFERENCES students (enrollment_no) ON DELETE RESTRICT ON UPDATE CASCADE,
                FOREIGN KEY (book_id) REFERENCES books (book_id) ON DELETE RESTRICT ON UPDATE CASCADE
            )
        ''')
        conn.commit()
        _library_schema_checked = True
    except Exception as e:
        print(f"[Schema Init] local library schema check failed: {e}")
    finally:
        conn.close()

def get_library_db():
    """Read-Only Connection to Core Data"""
    _ensure_local_library_schema()
    # If generic DB is used, both library and portal data are in the same Postgres DB
    return get_db_connection('library.db')

def get_portal_db():
    """Read-Write Connection to Sandbox Data"""
    # If generic DB is used, both library and portal data are in the same Postgres DB
    return get_db_connection('portal.db')

def create_table_safe(cursor, table_name, pg_sql, sqlite_sql):
    """Helper to create tables with backend-specific syntax"""
    database_url = os.getenv('DATABASE_URL')
    
    # Check if the cursor is a Postgres cursor (it's wrapped in our PostgresCursorWrapper or native)
    is_postgres = False
    if hasattr(cursor, 'cursor') and 'psycopg2' in str(type(cursor.cursor)):
        is_postgres = True
        
    if database_url and POSTGRES_AVAILABLE and is_postgres:
        try:
            cursor.execute(pg_sql)
        except Exception as e:
            print(f"Table creation warning ({table_name}): {e}")
    else:
        cursor.execute(sqlite_sql)

def init_portal_db():
    """Initialize the Sandbox DB for Requests and Notes"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Requests Table
    create_table_safe(cursor, 'requests', '''
        CREATE TABLE IF NOT EXISTS requests (
            req_id SERIAL PRIMARY KEY,
            enrollment_no TEXT,
            request_type TEXT,
            details TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS requests (
            req_id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_no TEXT,
            request_type TEXT,      -- 'profile_update', 'renewal', 'extension', 'notification'
            details TEXT,           -- JSON payload or text description
            status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Auth Table
    create_table_safe(cursor, 'student_auth', '''
        CREATE TABLE IF NOT EXISTS student_auth (
            enrollment_no TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            is_first_login INTEGER DEFAULT 1,
            last_changed TIMESTAMP
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS student_auth (
            enrollment_no TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            is_first_login INTEGER DEFAULT 1, -- 1=True, 0=False
            last_changed DATETIME
        )
    ''')

    # Notices Table
    create_table_safe(cursor, 'notices', '''
        CREATE TABLE IF NOT EXISTS notices (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Deletion Requests
    create_table_safe(cursor, 'deletion_requests', '''
        CREATE TABLE IF NOT EXISTS deletion_requests (
            id SERIAL PRIMARY KEY,
            student_id TEXT NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(student_id) REFERENCES students(enrollment_no)
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS deletion_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'pending', -- pending, approved, rejected
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(student_id) REFERENCES students(enrollment_no)
        )
    ''')

    # User Settings
    create_table_safe(cursor, 'user_settings', '''
        CREATE TABLE IF NOT EXISTS user_settings (
            enrollment_no TEXT PRIMARY KEY,
            email TEXT,
            library_alerts INTEGER DEFAULT 0,
            loan_reminders INTEGER DEFAULT 1,
            theme TEXT DEFAULT 'light',
            language TEXT DEFAULT 'English',
            data_consent INTEGER DEFAULT 1
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS user_settings (
            enrollment_no TEXT PRIMARY KEY,
            email TEXT,
            library_alerts INTEGER DEFAULT 0,
            loan_reminders INTEGER DEFAULT 1,
            theme TEXT DEFAULT 'light',
            language TEXT DEFAULT 'English',
            data_consent INTEGER DEFAULT 1
        )
    ''')

    # Notifications
    create_table_safe(cursor, 'user_notifications', '''
        CREATE TABLE IF NOT EXISTS user_notifications (
            id SERIAL PRIMARY KEY,
            enrollment_no TEXT,
            type TEXT,
            title TEXT,
            message TEXT,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_no TEXT,
            type TEXT,              -- 'request_update', 'security', 'system', 'overdue'
            title TEXT,
            message TEXT,
            link TEXT,              -- Optional action link
            is_read INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Book Waitlist
    create_table_safe(cursor, 'book_waitlist', '''
        CREATE TABLE IF NOT EXISTS book_waitlist (
            id SERIAL PRIMARY KEY,
            enrollment_no TEXT NOT NULL,
            book_id TEXT NOT NULL,
            book_title TEXT,
            notified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(enrollment_no, book_id)
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS book_waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_no TEXT NOT NULL,
            book_id INTEGER NOT NULL,
            book_title TEXT,
            notified INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(enrollment_no, book_id)
        )
    ''')

    # Access Logs
    create_table_safe(cursor, 'access_logs', '''
        CREATE TABLE IF NOT EXISTS access_logs (
            id SERIAL PRIMARY KEY,
            endpoint TEXT,
            method TEXT,
            status INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT,
            method TEXT,
            status INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Study Materials
    create_table_safe(cursor, 'study_materials', '''
        CREATE TABLE IF NOT EXISTS study_materials (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            filename TEXT NOT NULL,
            original_filename TEXT,
            file_size INTEGER,
            branch TEXT DEFAULT 'Computer',
            year TEXT NOT NULL,
            category TEXT,
            uploaded_by TEXT DEFAULT 'Library Admin',
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS study_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            filename TEXT NOT NULL,
            original_filename TEXT,
            file_size INTEGER,
            branch TEXT DEFAULT 'Computer',
            year TEXT NOT NULL,  -- '1st', '2nd', '3rd', '4th', '5th', '6th' (Semester)
            category TEXT,  -- 'Notes', 'PYQ', 'Study Material', 'Other'
            uploaded_by TEXT DEFAULT 'Library Admin',
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        )
    ''')

    # Wishlist
    create_table_safe(cursor, 'book_wishlist', '''
        CREATE TABLE IF NOT EXISTS book_wishlist (
            id SERIAL PRIMARY KEY,
            book_id TEXT NOT NULL,
            enrollment_no TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, enrollment_no)
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS book_wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            enrollment_no TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, enrollment_no)
        )
    ''')

    # Ratings
    create_table_safe(cursor, 'book_ratings', '''
        CREATE TABLE IF NOT EXISTS book_ratings (
            id SERIAL PRIMARY KEY,
            book_id TEXT NOT NULL,
            enrollment_no TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, enrollment_no)
        )
    ''', '''
        CREATE TABLE IF NOT EXISTS book_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            enrollment_no TEXT NOT NULL,
            rating INTEGER NOT NULL, -- 1-5
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, enrollment_no)
        )
    ''')

    
    conn.commit()
    conn.close()

# Initialize on Import
init_portal_db()

# Run cleanup on startup (after all functions are defined)
threading.Thread(target=cleanup_logs, daemon=True).start()

# --- Helper Functions for Email ---

def send_email_bg(recipient, subject, body):
    """Background task to send email using shared settings"""
    if not recipient:
        return
        
    try:
        # Path to email_settings.json (One level up from Web-Extension)
        # student_portal.py is in LibraryApp/Web-Extension
        # email_settings.json is in LibraryApp/
        settings_path = os.path.join(os.path.dirname(BASE_DIR), 'email_settings.json')
        
        if not os.path.exists(settings_path):
            print(f"Email settings not found at {settings_path}")
            return

        with open(settings_path, 'r') as f:
            settings = json.load(f)

        if not settings.get('enabled'):
            return

        msg = MIMEMultipart('alternative')
        msg['From'] = settings['sender_email']
        msg['To'] = recipient
        msg['Subject'] = subject
        
        # Attach plain text version (fallback)
        msg.attach(MIMEText("Please enable HTML to view this email.", 'plain'))
        
        # Attach HTML version if body looks like HTML, otherwise plain
        if body.strip().startswith('<html') or body.strip().startswith('<!DOCTYPE html'):
             msg.attach(MIMEText(body, 'html'))
        else:
             msg.attach(MIMEText(body, 'plain'))

        # Standard SMTP (Gmail/Outlook)
        server = smtplib.SMTP(settings['smtp_server'], settings['smtp_port'])
        server.starttls()
        server.login(settings['sender_email'], settings['sender_password'])
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {recipient}")
        
    except Exception as e:
        print(f"Failed to send email: {e}")

def trigger_notification_email(enrollment_no, subject, body):
    """Fetches user email and triggers background send"""
    try:
        # 1. Check User Settings (Portal DB)
        conn_portal = get_portal_db()
        cursor_portal = conn_portal.cursor()
        cursor_portal.execute("SELECT email FROM user_settings WHERE enrollment_no = ?", (enrollment_no,))
        setting = cursor_portal.fetchone()
        conn_portal.close()
        
        email = setting['email'] if setting and setting['email'] else None
        
        # 2. If no custom email, check College Records (Library DB)
        if not email:
            conn_lib = get_library_db()
            cursor_lib = conn_lib.cursor()
            cursor_lib.execute("SELECT email FROM students WHERE enrollment_no = ?", (enrollment_no,))
            student = cursor_lib.fetchone()
            conn_lib.close()
            email = student['email'] if student else None
            
        if email:
            threading.Thread(target=send_email_bg, args=(email, subject, body)).start()
            
    except Exception as e:
        print(f"Error triggering email: {e}")

# --- Auth Endpoints ---

@app.route('/api/request-deletion', methods=['POST'])
def request_deletion():
    data = request.json
    password = data.get('password', '').strip()
    reason = data.get('reason', 'User requested deletion via Student Portal')
    
    # 1. Verify Session
    if 'student_id' not in session:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    student_id = session['student_id']
    
    conn = get_portal_db()
    c = conn.cursor()
    
    # 2. Verify Password (Re-authentication)
    # Check student_auth first
    c.execute("SELECT password FROM student_auth WHERE enrollment_no = ?", (student_id,))
    auth_record = c.fetchone()
    
    is_valid = False
    if auth_record:
        stored_pw = auth_record['password']
        # Try hash verification first
        try:
            if check_password_hash(stored_pw, password):
                is_valid = True
        except:
            # Fallback to plain text comparison
            if stored_pw == password:
                is_valid = True
    else:
        # No auth record - check against enrollment_no as default password
        if password == student_id:
            is_valid = True
            
    if not is_valid:
        conn.close()
        return jsonify({"status": "error", "message": "Incorrect password"}), 403
        
    # 3. Check for existing pending request
    c.execute("SELECT id FROM deletion_requests WHERE student_id = ? AND status = 'pending'", (student_id,))
    existing = c.fetchone()
    if existing:
        conn.close()
        return jsonify({"status": "error", "message": "A deletion request is already pending."}), 400
        
    # 4. Create Request
    try:
        c.execute("INSERT INTO deletion_requests (student_id, reason) VALUES (?, ?)", (student_id, reason))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Deletion request submitted for librarian approval."})
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/login', methods=['POST'])
@rate_limit
def api_login():
    try:
        data = request.json
        enrollment = data.get('enrollment_no', '').strip()
        password = data.get('password', '').strip()
        
        if not enrollment:
            return jsonify({'status': 'error', 'message': 'Enrollment number required'}), 400
        
        # 1. Check if student exists in MAIN DB (Read-Only)
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        try:
            cursor_lib.execute("SELECT * FROM students WHERE enrollment_no = ?", (enrollment,))
            student = cursor_lib.fetchone()
        except sqlite3.OperationalError as oe:
            # Self-heal on fresh/empty DB files where table may not yet exist
            if 'no such table' in str(oe).lower() and 'students' in str(oe).lower():
                conn_lib.close()
                _ensure_local_library_schema()
                conn_lib = get_library_db()
                cursor_lib = conn_lib.cursor()
                cursor_lib.execute("SELECT * FROM students WHERE enrollment_no = ?", (enrollment,))
                student = cursor_lib.fetchone()
            else:
                conn_lib.close()
                raise
        conn_lib.close()
        
        if not student:
            return jsonify({'status': 'error', 'message': 'Student not found'}), 401
        
        # 2. Check Auth Status in PORTAL DB (Shadow Auth)
        conn_portal = get_portal_db()
        cursor_p = conn_portal.cursor()
        cursor_p.execute("SELECT * FROM student_auth WHERE enrollment_no = ?", (enrollment,))
        auth_record = cursor_p.fetchone()
        
        require_change = False
        
        if not auth_record:
            # FIRST LOGIN ATTEMPT EVER for this user
            # Default behavior: Password MUST be enrollment number
            if password == enrollment:
                # Create auth record with HASHED password
                hashed_pw = generate_password_hash(enrollment)
                cursor_p.execute("INSERT INTO student_auth (enrollment_no, password, is_first_login) VALUES (?, ?, 1)", 
                                 (enrollment, hashed_pw))
                conn_portal.commit()
                _push_to_cloud(
                    "INSERT INTO student_auth (enrollment_no, password, is_first_login) VALUES (?, ?, 1) ON CONFLICT (enrollment_no) DO NOTHING",
                    (enrollment, hashed_pw)
                )
                require_change = True
            else:
                conn_portal.close()
                return jsonify({'status': 'error', 'message': 'Invalid password (First login? Use Enrollment No.)'}), 401
        else:
            # Existing auth record
            stored_pw = auth_record['password']
            
            # 1. Try verifying hash
            is_valid = False
            try:
                if check_password_hash(stored_pw, password):
                    is_valid = True
            except:
                # Not a hash (legacy plain text)
                if stored_pw == password:
                    is_valid = True
                    # MIGRATION: Upgrade to hash immediately
                    new_hash = generate_password_hash(password)
                    cursor_p.execute("UPDATE student_auth SET password = ? WHERE enrollment_no = ?", (new_hash, enrollment))
                    conn_portal.commit()
            
            if not is_valid:
                conn_portal.close()
                return jsonify({'status': 'error', 'message': 'Invalid password'}), 401
                
            if auth_record['is_first_login']:
                require_change = True

        # Login Success - Create Session
        session['student_id'] = enrollment
        session['logged_in'] = True
        
        conn_portal.close()
        
        # Return full user details (similar to /api/me) for Profile page consistency
        student_year = student['year'] if student['year'] else '1st'
        
        return jsonify({
            'status': 'success', 
            'enrollment_no': enrollment,
            'name': student['name'],
            'department': student['department'] if student['department'] else 'General',
            'year': student_year,
            'email': student['email'],
            'require_change': require_change
        })
    except Exception as e:
        error_id = _log_portal_exception('api_login', e)
        return jsonify({'status': 'error', 'message': f'Login failed: {str(e)}', 'error_id': error_id}), 500

@app.route('/api/public/forgot-password', methods=['POST'])
@rate_limit
def api_forgot_password():
    data = request.json
    enrollment = data.get('enrollment_no')
    
    if not enrollment:
        return jsonify({'status': 'error', 'message': 'Enrollment number required'}), 400
        
    try:
        # Verify Student Exists in Library DB
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        cursor_lib.execute("SELECT name, email FROM students WHERE enrollment_no = ?", (enrollment,))
        student = cursor_lib.fetchone()
        conn_lib.close()
        
        if not student:
            return jsonify({'status': 'error', 'message': 'Student not found'}), 404
            
        student_name = student['name'].split()[0] if student and student['name'] else "Student"
        
        # Create Request in Portal DB
        conn_portal = get_portal_db()
        cursor_portal = conn_portal.cursor()
        
        # Check for existing pending request to avoid spam
        # Use req_id for PostgreSQL compatibility (primary key column name)
        pk = _requests_pk_column(conn_portal)
        cursor_portal.execute(f"SELECT {pk} FROM requests WHERE enrollment_no = ? AND request_type = ? AND status = ?", 
                             (enrollment, 'password_reset', 'pending'))
        existing = cursor_portal.fetchone()
        if existing:
             conn_portal.close()
             return jsonify({'status': 'error', 'message': 'A reset request is already pending.'}), 400
             
        cursor_portal.execute("INSERT INTO requests (enrollment_no, request_type, details) VALUES (?, ?, ?)",
                       (enrollment, 'password_reset', 'Request to reset password to default.'))
        conn_portal.commit()
        conn_portal.close()
        
        # Send Receipt Email (non-blocking)
        try:
            email_body = generate_email_template(
                header_title="Password Reset Requested",
                user_name=student_name,
                main_text="We have received your request to reset your password.",
                details_dict={'Action': 'Account Password Reset', 'Status': 'Pending Librarian Approval'},
                theme='blue',
                footer_note="If you did not request this, please contact the library immediately."
            )
            trigger_notification_email(enrollment, "Password Reset Request", email_body)
        except Exception as email_error:
            print(f"Email notification failed (non-critical): {email_error}")
            # Continue - request was created successfully even if email fails
        
        return jsonify({'status': 'success', 'message': 'Password reset request submitted successfully'})
        
    except Exception as e:
        import traceback
        print(f"Forgot password error: {e}")
        print(traceback.format_exc())
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

@app.route('/api/change_password', methods=['POST'])
@rate_limit
def api_change_password():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
        
    data = request.json
    new_password = data.get('new_password', '').strip()
    enrollment = session.get('student_id')
    
    if not new_password or len(new_password) < 6:
        return jsonify({'status': 'error', 'message': 'Password must be at least 6 characters'}), 400
    
    # Get student name from library.db
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (enrollment,))
    student = cursor_lib.fetchone()
    conn_lib.close()
    student_name = student['name'] if student else enrollment
    
    # Update password in portal.db
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Hash the new password
    hashed_pw = generate_password_hash(new_password)
    
    cursor.execute("""
        UPDATE student_auth 
        SET password = ?, is_first_login = 0, last_changed = CURRENT_TIMESTAMP
        WHERE enrollment_no = ?
    """, (hashed_pw, enrollment))
    
    _push_to_cloud(
        "INSERT INTO student_auth (enrollment_no, password, is_first_login, last_changed) VALUES (?, ?, 0, CURRENT_TIMESTAMP) ON CONFLICT (enrollment_no) DO UPDATE SET password = EXCLUDED.password, is_first_login = 0, last_changed = CURRENT_TIMESTAMP",
        (enrollment, hashed_pw)
    )
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'status': 'success', 
        'message': 'Password updated successfully',
        'name': student_name,
        'enrollment_no': enrollment
    })


# =====================================================================
# PUBLIC STUDENT REGISTRATION (REQUEST FLOW)
# =====================================================================


@app.route('/api/public/register', methods=['POST'])
@rate_limit
def api_public_register_student():
    """Students can submit a registration request (pending librarian approval)."""
    try:
        # Expect multipart/form-data
        enrollment_no = _normalize_enrollment(request.form.get('enrollment_no', ''))
        name = _safe_str(request.form.get('name', ''))
        year = _safe_str(request.form.get('year', ''))
        department = _safe_str(request.form.get('department', ''))
        phone = _safe_str(request.form.get('phone', ''))
        email = _safe_str(request.form.get('email', ''))

        if not enrollment_no or not name or not year or not department or not phone or not email:
            return jsonify({'status': 'error', 'message': 'All fields are required (except photo).'}), 400

        if len(phone) < 8:
            return jsonify({'status': 'error', 'message': 'Invalid mobile number.'}), 400

        if '@' not in email or '.' not in email:
            return jsonify({'status': 'error', 'message': 'Invalid email address.'}), 400

        # 1) Ensure student does NOT already exist in library DB
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        cursor_lib.execute("SELECT enrollment_no FROM students WHERE enrollment_no = ?", (enrollment_no,))
        existing = cursor_lib.fetchone()
        conn_lib.close()
        if existing:
            return jsonify({'status': 'error', 'message': 'You are already registered in the library.'}), 409

        # 2) Prevent duplicate pending registration requests
        conn_portal = get_portal_db()
        cursor_p = conn_portal.cursor()
        cursor_p.execute(
            """
            SELECT 1 FROM requests
            WHERE enrollment_no = ? AND request_type = 'student_registration' AND status = 'pending'
            LIMIT 1
            """,
            (enrollment_no,)
        )
        pending = cursor_p.fetchone()
        if pending:
            conn_portal.close()
            return jsonify({'status': 'error', 'message': 'A registration request is already pending. Please wait for librarian approval.'}), 400

        # 3) Optional photo upload (<=50KB)
        photo_path = None
        if 'photo' in request.files and request.files['photo'] and request.files['photo'].filename:
            photo = request.files['photo']
            if not _allowed_photo_filename(photo.filename):
                conn_portal.close()
                return jsonify({'status': 'error', 'message': 'Photo must be JPG or PNG.'}), 400

            size = _read_file_size_bytes(photo)
            if size > MAX_PHOTO_BYTES:
                conn_portal.close()
                return jsonify({'status': 'error', 'message': 'Photo too large. Max size is 50KB.'}), 400

            original_filename = secure_filename(photo.filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{timestamp}_{enrollment_no}_{original_filename}"
            photo_path = os.path.join(REGISTRATION_PHOTO_FOLDER, unique_filename)
            photo.save(photo_path)

        details = {
            'name': name,
            'year': year,
            'department': department,
            'phone': phone,
            'email': email,
            'photo_filename': os.path.basename(photo_path) if photo_path else None
        }

        cursor_p.execute(
            "INSERT INTO requests (enrollment_no, request_type, details) VALUES (?, 'student_registration', ?)",
            (enrollment_no, json.dumps(details))
        )
        conn_portal.commit()
        conn_portal.close()

        return jsonify({
            'status': 'success',
            'message': 'Registration request submitted. Please wait for librarian approval.'
        })
    except Exception as e:
        error_id = _log_portal_exception('api_public_register_student', e)
        return jsonify({'status': 'error', 'message': f'Registration failed: {str(e)}', 'error_id': error_id}), 500


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    if 'student_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
        
    data = request.json or {}
    enrollment = session['student_id']

    conn = get_portal_db()
    cursor = conn.cursor()

    # BUG A5 FIX: Only update fields that are explicitly present in the request.
    # A partial payload (e.g. just {theme: 'dark'} from the dark mode toggle) was
    # previously overwriting ALL columns with falsy defaults, zeroing out the user's
    # email, notification prefs, and data consent on every toggle.
    #
    # Strategy: read current row first, merge with incoming fields, then upsert.
    cursor.execute("SELECT * FROM user_settings WHERE enrollment_no = ?", (enrollment,))
    existing = cursor.fetchone()

    # Build merged field set — start from existing values (or safe defaults)
    if existing:
        cur_email        = existing['email']
        cur_alerts       = existing['library_alerts']
        cur_reminders    = existing['loan_reminders']
        cur_theme        = existing['theme']
        cur_language     = existing['language']
        cur_consent      = existing['data_consent']
    else:
        cur_email        = None
        cur_alerts       = 1
        cur_reminders    = 1
        cur_theme        = 'light'
        cur_language     = 'English'
        cur_consent      = 1

    # Only override a field if the key is present in the incoming payload
    new_email     = data['email']        if 'email'         in data else cur_email
    new_alerts    = (1 if data['libraryAlerts'] else 0) if 'libraryAlerts' in data else cur_alerts
    new_reminders = (1 if data['loanReminders'] else 0) if 'loanReminders' in data else cur_reminders
    new_theme     = data['theme']        if 'theme'         in data else cur_theme
    new_language  = data['language']     if 'language'      in data else cur_language
    new_consent   = (1 if data['dataConsent'] else 0) if 'dataConsent' in data else cur_consent

    cursor.execute("""
        INSERT INTO user_settings (enrollment_no, email, library_alerts, loan_reminders, theme, language, data_consent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(enrollment_no) DO UPDATE SET
            email=excluded.email,
            library_alerts=excluded.library_alerts,
            loan_reminders=excluded.loan_reminders,
            theme=excluded.theme,
            language=excluded.language,
            data_consent=excluded.data_consent
    """, (enrollment, new_email, new_alerts, new_reminders, new_theme, new_language, new_consent))

    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Settings updated successfully'})

@app.route('/api/profile/photo', methods=['GET', 'POST', 'DELETE'])
def api_profile_photo():
    """Manage student profile photos."""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    enrollment = str(session['student_id']).strip()

    if request.method == 'GET':
        for ext in ['.jpg', '.jpeg', '.png']:
            path = os.path.join(PROFILE_PHOTO_FOLDER, f"{enrollment}{ext}")
            if os.path.exists(path):
                return send_file(path)
        return jsonify({'error': 'No photo found'}), 404

    if request.method == 'POST':
        if 'photo' not in request.files:
            return jsonify({'error': 'No photo provided'}), 400

        photo = request.files['photo']
        if photo.filename == '':
            return jsonify({'error': 'Empty filename'}), 400

        ext = os.path.splitext(photo.filename)[1].lower()
        if ext not in ['.jpg', '.jpeg', '.png']:
            return jsonify({'error': 'Invalid file type. Only JPG and PNG are allowed.'}), 400

        # Remove old photos
        for old_ext in ['.jpg', '.jpeg', '.png']:
            old_path = os.path.join(PROFILE_PHOTO_FOLDER, f"{enrollment}{old_ext}")
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError as e:
                    # BUG A2 FIX: Log instead of silently swallowing — a stale photo
                    # file left on disk means the old extension wins on next login.
                    print(f"[Profile Photo] Failed to remove old photo {old_path}: {e}")

        # Save new photo
        save_path = os.path.join(PROFILE_PHOTO_FOLDER, f"{enrollment}{ext}")
        photo.save(save_path)
        
        return jsonify({'status': 'success', 'message': 'Profile photo updated'})
        
    if request.method == 'DELETE':
        deleted = False
        for old_ext in ['.jpg', '.jpeg', '.png']:
            old_path = os.path.join(PROFILE_PHOTO_FOLDER, f"{enrollment}{old_ext}")
            if os.path.exists(old_path):
                try: 
                    os.remove(old_path)
                    deleted = True
                except OSError as e:
                    # BUG A2 FIX: Log removal failures on DELETE too
                    print(f"[Profile Photo] Failed to remove photo {old_path}: {e}")
        return jsonify({'status': 'success', 'message': 'Profile photo removed'}) if deleted else jsonify({'error': 'No photo found'}), 404

    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, message, created_at FROM notices WHERE active = 1 ORDER BY created_at DESC")
    notices = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'notices': notices})

@app.route('/api/loan-history')
def api_loan_history():
    """Get comprehensive loan history with all statuses"""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    enrollment = session['student_id']
    conn = get_library_db()
    cursor = conn.cursor()
    
    # Get ALL borrow records (borrowed, returned, overdue)
    cursor.execute("""
        SELECT b.title, b.author, b.category, b.cover_url, br.borrow_date, br.due_date, br.return_date, br.status, br.fine
        FROM borrow_records br
        JOIN books b ON br.book_id = b.book_id
        WHERE br.enrollment_no = ?
        ORDER BY br.borrow_date DESC
    """, (enrollment,))

    all_records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    # Categorize records
    currently_borrowed = []
    returned_on_time = []
    returned_late = []
    currently_overdue = []
    
    today = datetime.now()
    
    for record in all_records:
        # Determine actual status
        if record['status'] == 'borrowed':
            if record['due_date']:
                try:
                    due_dt = datetime.strptime(record['due_date'], '%Y-%m-%d')
                    if due_dt < today:
                        record['actual_status'] = 'Currently Overdue'
                        record['overdue_days'] = (today - due_dt).days
                        currently_overdue.append(record)
                    else:
                        record['actual_status'] = 'Currently Borrowed'
                        record['days_left'] = (due_dt - today).days
                        currently_borrowed.append(record)
                except:
                    record['actual_status'] = 'Currently Borrowed'
                    currently_borrowed.append(record)
        elif record['status'] == 'returned':
            if record['due_date'] and record['return_date']:
                try:
                    due_dt = datetime.strptime(record['due_date'], '%Y-%m-%d')
                    return_dt = datetime.strptime(record['return_date'], '%Y-%m-%d')
                    if return_dt > due_dt:
                        record['actual_status'] = 'Returned Late'
                        record['fine_paid'] = record.get('fine', 0) > 0
                        returned_late.append(record)
                    else:
                        record['actual_status'] = 'Returned On Time'
                        returned_on_time.append(record)
                except:
                    record['actual_status'] = 'Returned'
                    returned_on_time.append(record)
    
    return jsonify({
        'currently_borrowed': currently_borrowed,
        'currently_overdue': currently_overdue,
        'returned_on_time': returned_on_time,
        'returned_late': returned_late,
        'total_borrowed': len(all_records),
        'total_fines_paid': sum([r.get('fine', 0) for r in returned_late])
    })

# --- Notification System API ---

@app.route('/api/notifications', methods=['GET'])
def api_get_notifications():
    """Unified Notification Stream"""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    enrollment = session['student_id']
    notifications = []
    
    # 1. Fetch Persistent Notifications (History)
    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM user_notifications 
        WHERE enrollment_no = ? 
        ORDER BY created_at DESC 
        LIMIT 50
    """, (enrollment,))
    history_items = [dict(row) for row in cursor.fetchall()]
    
    # 2. Real-time Overdue Alerts (High Priority)
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    cursor_lib.execute("""
        SELECT b.title, br.due_date, br.book_id
        FROM borrow_records br
        JOIN books b ON br.book_id = b.book_id
        WHERE br.enrollment_no = ? AND br.status = 'borrowed'
    """, (enrollment,))
    borrows = cursor_lib.fetchall()
    conn_lib.close()
    
    today = datetime.now()
    active_alerts = []
    
    for row in borrows:
        if row['due_date']:
            try:
                due_dt = datetime.strptime(row['due_date'], '%Y-%m-%d')
                delta = (due_dt - today).days
                
                if delta < 0:
                    active_alerts.append({
                        'id': f"overdue_{row['book_id']}", # Virtual ID
                        'type': 'danger',
                        'title': 'Overdue Book',
                        'message': f"'{row['title']}' is overdue by {abs(delta)} days. Please return immediately.",
                        'is_read': 0, # Always unread/active until resolved
                        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'link': f"/books/{row['book_id']}"
                    })
                elif delta <= 2:
                    active_alerts.append({
                        'id': f"warning_{row['book_id']}",
                        'type': 'warning',
                        'title': 'Due Soon',
                        'message': f"'{row['title']}' is due in {delta} days.",
                        'is_read': 0,
                        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'link': f"/books/{row['book_id']}"
                    })
            except:
                pass

    # 3. Security Alert
    cursor.execute("SELECT is_first_login FROM student_auth WHERE enrollment_no = ?", (enrollment,))
    auth = cursor.fetchone()
    if auth and auth['is_first_login']:
        active_alerts.insert(0, {
            'id': 'security_alert',
            'type': 'danger',
            'title': 'Security Alert',
            'message': 'You are using a default password. Change it now to secure your account.',
            'is_read': 0,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'link': '/settings'
        })

    # 4. Broadcast Notices (System)
    cursor.execute("SELECT * FROM notices WHERE active = 1 ORDER BY created_at DESC LIMIT 10")
    notices = [dict(row) for row in cursor.fetchall()]
    broadcasts = []
    
    for note in notices:
        broadcasts.append({
            'id': f"notice_{note['id']}",
            'type': 'system',
            'title': note['title'],
            'message': note['message'],
            'is_read': 0, # Notices are technically always "unread" unless tracked separately, but for now we show them.
            'created_at': note['created_at'],
            'link': None
        })

    conn.close()
    
    # Combine: Security > Overdue > History > Broadcasts
    # Note: History includes past request updates. Broadcasts are general.
    # We'll merge them all and sort by date for the "All" tab.
    
    combined = active_alerts + history_items + broadcasts
    
    # Sort by created_at desc
    def get_date(item):
        try:
            return datetime.strptime(item['created_at'], '%Y-%m-%d %H:%M:%S')
        except:
             try:
                 # Backup format if milliseconds exist
                 return datetime.strptime(item['created_at'].split('.')[0], '%Y-%m-%d %H:%M:%S')
             except:
                 return datetime.min

    combined.sort(key=get_date, reverse=True)
    
    # Count Unread
    # For generated items (alerts/broadcasts), they count as unread if they aren't explicitly suppressed.
    # Logic: Database items have 'is_read'. Virtual items (Overdue/Security) depend on existence.
    # Broadcasts: We don't have per-user read state yet. We'll mark them as read for badge count to avoid permanent red dot,
    # OR we just count DB items + Active Alerts.
    
    unread_db = len([n for n in history_items if not n['is_read']])
    unread_alerts = len(active_alerts) # Alerts are always actionable/unread
    # We won't count Broadcasts in the badge to avoid annoyance, they appear in the list silently (or maybe separate logic later)
    
    return jsonify({
        'notifications': combined,
        'unread_count': unread_db + unread_alerts
    })

@app.route('/api/notifications/mark-read', methods=['POST'])
def api_mark_read():
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    notif_id = data.get('id')
    enrollment = session['student_id']
    
    conn = get_portal_db()
    cursor = conn.cursor()
    
    if notif_id == 'all':
        cursor.execute("UPDATE user_notifications SET is_read = 1 WHERE enrollment_no = ?", (enrollment,))
    elif str(notif_id).isdigit():
        # Only mark DB items (virtual alerts can't be marked read via API, they persist until resolved)
        cursor.execute("UPDATE user_notifications SET is_read = 1 WHERE id = ? AND enrollment_no = ?", (notif_id, enrollment))
        
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/notifications/<int:notif_id>', methods=['DELETE'])
def api_delete_notification(notif_id):
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
        
    enrollment = session['student_id']
    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_notifications WHERE id = ? AND enrollment_no = ?", (notif_id, enrollment))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/notices', methods=['GET', 'POST', 'DELETE'])
def api_admin_notices():
    """Admin management for notices"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    if request.method == 'GET':
        # List all notices (active and inactive)
        cursor.execute("SELECT * FROM notices ORDER BY created_at DESC")
        notices = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'notices': notices})
        
    elif request.method == 'POST':
        # Create new notice
        data = request.json
        title = data.get('title')
        message = data.get('message')
        
        if not title or not message:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Title and message required'}), 400
            
        cursor.execute("INSERT INTO notices (title, message) VALUES (?, ?)", (title, message))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Notice posted'})

@app.route('/api/admin/notices/<int:notice_id>', methods=['DELETE'])
def api_delete_notice(notice_id):
    """Deactivate a notice"""
    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE notices SET active = 0 WHERE id = ?", (notice_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'Notice deleted'})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'success'})

@app.route('/api/me')
def api_me():
    if 'student_id' not in session:
        return jsonify({'user': None})
    
    conn = get_library_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM students WHERE enrollment_no = ?", (session['student_id'],))
    student = cursor.fetchone()
    conn.close()
    
    if student:
        # Determine if Pass Out
        student_year = student['year'] if student['year'] else '1st'
        is_pass_out = False
        # Normalize year string for pass out detection
        if student_year.strip().lower() in ['pass out', 'passout', 'passed out', 'alumni', 'graduate']:
            student_year = 'Pass Out'
            is_pass_out = True
        
        # Fetch User Settings Override
        conn_portal = get_portal_db()
        cursor_p = conn_portal.cursor()
        cursor_p.execute("SELECT * FROM user_settings WHERE enrollment_no = ?", (session['student_id'],))
        settings = cursor_p.fetchone()
        conn_portal.close()
        
        # Default Email logic
        default_email = f"{student['name'].replace(' ', '.').lower()}@gpa.edu"
        user_email = settings['email'] if settings and settings['email'] else dict(student).get('email', default_email)
        
        return jsonify({'user': {
            'name': student['name'],
            'enrollment_no': student['enrollment_no'],
            'department': student['department'],
            'year': student_year,
            'email': user_email,
            'phone': student['phone'] or 'N/A',  # BUG A6 FIX: None → 'N/A'
            'settings': {
                'libraryAlerts': bool(settings['library_alerts']) if settings else False,
                'loanReminders': bool(settings['loan_reminders']) if settings else True,
                'theme': settings['theme'] if settings else 'light',
                'language': settings['language'] if settings else 'English',
                'dataConsent': bool(settings['data_consent']) if settings else True
            },
            'privileges': {
                 'max_books': 5,
                 'loan_duration': '7 Days',
                 'renewal_limit': '2 Renewals per book'
            },
            'account_info': {
                'password_last_changed': 'Recently'
            },
            'can_request': not is_pass_out
        }})
    return jsonify({'user': None})

@app.route('/api/user-policies')
def api_user_policies():
    """Fetch user specific policies and account info"""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
        
    return jsonify({
        'policies': {
            'max_books': 5,
            'loan_duration': '7 Days',
            'renewal_limit': '2 Renewals per book',
            'password_last_changed': 'Recently'
        }
    })

@app.route('/api/alerts')
def api_alerts():
    """Lightweight check for overdue items and security alerts"""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    enrollment = session['student_id']
    
    # 1. Check Security Alert (Highest Priority)
    conn_portal = get_portal_db()
    cursor_p = conn_portal.cursor()
    cursor_p.execute("SELECT is_first_login FROM student_auth WHERE enrollment_no = ?", (enrollment,))
    auth_record = cursor_p.fetchone()
    conn_portal.close()
    
    if auth_record and auth_record['is_first_login']:
        return jsonify({
            'has_alert': True,
            'type': 'security',
            'message': 'Action Required: Change Default Password',
            'action_link': '/settings', # Or prompt modal
            'count': 1
        })
    
    # 2. Check Overdue Items
    conn = get_library_db()
    cursor = conn.cursor()
    
    # Check for active borrows only - fast query
    cursor.execute("""
        SELECT b.title, br.due_date, COALESCE(br.fine, 0) as fine
        FROM borrow_records br
        JOIN books b ON br.book_id = b.book_id
        WHERE br.enrollment_no = ? AND br.status = 'borrowed'
    """, (enrollment,))
    
    borrows = cursor.fetchall()
    conn.close()
    
    today = datetime.now()
    fine_per_day = get_portal_fine_per_day()
    overdue_count = 0
    total_fine = 0
    overdue_titles = []
    
    for row in borrows:
        if row['due_date']:
            try:
                due_dt = datetime.strptime(row['due_date'], '%Y-%m-%d')
                delta = (due_dt - today).days
                if delta < 0:
                    overdue_count += 1
                    days_late = abs(delta)
                    stored_fine = int(row['fine'] or 0)
                    computed_fine = days_late * fine_per_day
                    total_fine += max(stored_fine, computed_fine)
                    overdue_titles.append(row['title'])
            except:
                pass
                
    return jsonify({
        'has_alert': overdue_count > 0,
        'type': 'overdue',
        'count': overdue_count,
        'fine_estimate': total_fine,
        'items': overdue_titles
    })

@app.route('/api/services')
def api_services():
    """Fetch available digital resources and services"""
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # In a real app, these would be in a 'resources' table
    resources = [
        {
            'id': 1,
            'title': "IEEE Xplore Access",
            'type': "Research Database",
            'description': "Full access to IEEE journals, conferences, and standards.",
            'link': "#",
            'icon': "Globe"
        },
        {
            'id': 2,
            'title': "ProQuest E-Books",
            'type': "E-Book Platform",
            'description': "Access to over 150,000 academic e-books.",
            'link': "#",
            'icon': "Book"
        },
        {
            'id': 3,
            'title': "JSTOR Archive",
            'type': "Journal Archive",
            'description': "Academic journal archive for humanities and sciences.",
            'link': "#",
            'icon': "Archive"
        }
    ]
    
    return jsonify({'resources': resources})

# --- Dashboard Data Aggregation ---

@app.route('/api/dashboard')
def api_dashboard():
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    enrollment = session['student_id']
    
    # 1. Fetch Core Data (Read-Only)
    conn = get_library_db()
    cursor = conn.cursor()
    
    # Active Loans
    cursor.execute("""
        SELECT b.title, b.author, b.cover_url, br.borrow_date, br.due_date, br.book_id, br.accession_no, COALESCE(br.fine, 0) as fine
        FROM borrow_records br
        JOIN books b ON br.book_id = b.book_id
        WHERE br.enrollment_no = ? AND br.status = 'borrowed'
        ORDER BY br.due_date ASC
    """, (enrollment,))
    raw_borrows = cursor.fetchall()
    
    # History
    cursor.execute("""
        SELECT b.title, b.author, b.category, br.borrow_date, br.return_date, br.status
        FROM borrow_records br
        JOIN books b ON br.book_id = b.book_id
        WHERE br.enrollment_no = ? AND br.status = 'returned'
        ORDER BY br.return_date DESC
        LIMIT 50
    """, (enrollment,))
    raw_history = cursor.fetchall()
    
    conn.close()
    
    # 2. Process Business Logic (Fines/Alerts)
    borrows = []
    notifications = []
    
    # High Priority Auth Alert
    conn_portal = get_portal_db()
    cursor_p = conn_portal.cursor()
    cursor_p.execute("SELECT is_first_login FROM student_auth WHERE enrollment_no = ?", (enrollment,))
    auth_record = cursor_p.fetchone()
    conn_portal.close()
    
    if auth_record and auth_record['is_first_login']:
         notifications.append({
            'type': 'danger',
            'title': 'Security Alert',
            'msg': "You are using the default password. Please change it immediately."
        })

    today = datetime.now().date()
    fine_per_day = get_portal_fine_per_day()
    
    for row in raw_borrows:
        item = dict(row)
        if item['due_date']:
            try:
                due_d = _parse_date_any(item['due_date'])
                if not due_d:
                    raise ValueError('Unparseable due_date')

                # Normalize outgoing date format for frontend
                item['due_date'] = due_d.isoformat()
                item['borrow_date'] = _to_iso_date(item.get('borrow_date'))

                delta = (due_d - today).days
                
                # Logic: Green (3+), Yellow (1-2), Red (<0)
                if delta < 0:
                    item['status'] = 'overdue'
                    overdue_days = abs(delta)
                    item['days_msg'] = f"Overdue by {overdue_days} days"
                    stored_fine = int(item.get('fine') or 0)
                    computed_fine = overdue_days * fine_per_day
                    item['fine'] = max(stored_fine, computed_fine)
                    notifications.append({
                        'type': 'danger',
                        'msg': f"'{item['title']}' is OVERDUE! Fine: ₹{item['fine']}"
                    })
                elif delta <= 2:
                    item['status'] = 'warning'
                    item['days_msg'] = f"Due in {delta} days"
                    notifications.append({
                        'type': 'warning',
                        'msg': f"'{item['title']}' is due soon ({delta} days)."
                    })
                else:
                    item['status'] = 'safe'
                    item['days_msg'] = f"{delta} days left"
            except:
                item['status'] = 'unknown'
                item['days_msg'] = '-'
        borrows.append(item)

    # 3. Fetch Sandbox Data (Requests Status) + Accurate Summary Counts
    conn_portal = get_portal_db()
    cursor_p = conn_portal.cursor()
    cursor_p.execute("SELECT * FROM requests WHERE enrollment_no = ? ORDER BY created_at DESC LIMIT 5", (enrollment,))
    requests = [dict(row) for row in cursor_p.fetchall()]

    # --- Accurate stat counts (no LIMIT cap) ---
    # Accurate returned count from library DB
    conn_lib2 = get_library_db()
    cursor_lib2 = conn_lib2.cursor()
    cursor_lib2.execute(
        "SELECT COUNT(*) as c FROM borrow_records WHERE enrollment_no = ? AND status = 'returned'",
        (enrollment,)
    )
    _row = cursor_lib2.fetchone()
    returned_count = (_row['c'] if _row and 'c' in _row.keys() else _row[0]) if _row else 0

    # Accurate borrowed count
    cursor_lib2.execute(
        "SELECT COUNT(*) as c FROM borrow_records WHERE enrollment_no = ? AND status = 'borrowed'",
        (enrollment,)
    )
    _row = cursor_lib2.fetchone()
    borrowed_count_db = (_row['c'] if _row and 'c' in _row.keys() else _row[0]) if _row else 0

    # Total cumulative fine (including already-paid)
    cursor_lib2.execute(
        "SELECT COALESCE(SUM(fine), 0) as total FROM borrow_records WHERE enrollment_no = ? AND fine > 0",
        (enrollment,)
    )
    _row = cursor_lib2.fetchone()
    total_fine_ever = int((_row['total'] if _row and 'total' in _row.keys() else _row[0]) if _row else 0)
    conn_lib2.close()

    # Pending requests count
    cursor_p.execute(
        "SELECT COUNT(*) as c FROM requests WHERE enrollment_no = ? AND status = 'pending'",
        (enrollment,)
    )
    _row = cursor_p.fetchone()
    pending_requests_count = (_row['c'] if _row and 'c' in _row.keys() else _row[0]) if _row else 0

    # Wishlist count
    try:
        cursor_p.execute(
            "SELECT COUNT(*) as c FROM book_wishlist WHERE enrollment_no = ?",
            (enrollment,)
        )
        _row = cursor_p.fetchone()
        wishlist_count = (_row['c'] if _row and 'c' in _row.keys() else _row[0]) if _row else 0
    except Exception:
        wishlist_count = 0

    conn_portal.close()

    # 4. Analytics & Gamification (Computed on Read-Only Data)
    stats = {
        'total_books': len(raw_history) + len(borrows),
        'total_fines': sum([x.get('fine', 0) for x in borrows if x.get('status') == 'overdue']),
        'fav_category': 'General',
        'categories': {}
    }
    
    # Category Dist
    cat_count = {}
    for book in raw_history:
        cat = book['category'] or 'Uncategorized'
        cat_count[cat] = cat_count.get(cat, 0) + 1
    
    stats['categories'] = cat_count
    if cat_count:
        stats['fav_category'] = max(cat_count, key=cat_count.get)
        
    # Badges Logic
    badges = []
    if stats['total_books'] >= 5:
        badges.append({'id': 'bookworm', 'label': 'Bookworm', 'icon': '🐛', 'color': 'bg-emerald-100 text-emerald-700'})
    if stats['total_books'] >= 10:
        badges.append({'id': 'scholar', 'label': 'Scholar', 'icon': '🎓', 'color': 'bg-indigo-100 text-indigo-700'})
    
    # Check for overdue history
    has_overdues = any(x['status'] == 'overdue' for x in raw_history) # raw_history needs status mapping
    if not has_overdues and stats['total_books'] > 2:
        badges.append({'id': 'clean_sheet', 'label': 'Clean Sheet', 'icon': '🛡️', 'color': 'bg-blue-100 text-blue-700'})

    # 4. Library Notices (Active Broadcasts)
    conn_portal = get_portal_db()
    cursor_p = conn_portal.cursor()
    cursor_p.execute("SELECT id, title, message as content, created_at as date FROM notices WHERE active = 1 ORDER BY created_at DESC")
    notices = [dict(row) for row in cursor_p.fetchall()]
    conn_portal.close()

    # Normalize dates for frontend JS Date parsing
    for n in notices:
        n['date'] = _to_iso_date(n.get('date'))

    history = [dict(row) for row in raw_history]
    for h in history:
        h['borrow_date'] = _to_iso_date(h.get('borrow_date'))
        h['return_date'] = _to_iso_date(h.get('return_date'))

    active_fine = sum(b.get('fine', 0) for b in borrows if b.get('status') == 'overdue')

    return jsonify({
        'borrows': borrows,
        'history': history,
        'notices': notices,
        'notifications': notifications,
        'recent_requests': requests,
        'analytics': {
            'stats': stats,
            'badges': badges
        },
        'summary': {
            'borrowed_count': borrowed_count_db,
            'returned_count': returned_count,
            'overdue_count': len([b for b in borrows if b.get('status') == 'overdue']),
            'pending_requests_count': pending_requests_count,
            'wishlist_count': wishlist_count,
            'active_fine': int(active_fine),
            'total_fine_ever': total_fine_ever,
        }
    })

# --- Write Endpoints (Sandbox Only) ---

@app.route('/api/books/<book_id>', methods=['GET'])
def get_book_details(book_id):
    """Fetch details for a specific book."""
    try:
        conn = get_library_db()
        cursor = conn.cursor()
        
        # Fetch book details
        cursor.execute("SELECT * FROM books WHERE book_id = ?", (str(book_id),))
        book = cursor.fetchone()
        
        if not book:
            conn.close()
            return jsonify({'error': 'Book not found'}), 404
            
        book_data = dict(book)
        
        # Calculate availability
        cursor.execute("SELECT COUNT(*) FROM borrow_records WHERE book_id = ? AND status = 'borrowed'", (str(book_id),))
        borrowed_count = cursor.fetchone()[0]
        book_data['available_copies'] = book_data['total_copies'] - borrowed_count
        
        # Fetch Ratings
        portal_conn = get_portal_db()
        portal_cursor = portal_conn.cursor()
        
        # Check if current user is on waitlist & Get their rating
        user_rating = 0
        
        if 'student_id' in session:
            portal_cursor.execute(
                "SELECT id FROM book_waitlist WHERE enrollment_no = ? AND book_id = ? AND notified = 0",
                (session['student_id'], str(book_id))
            )
            waitlist_entry = portal_cursor.fetchone()
            book_data['on_waitlist'] = waitlist_entry is not None

            # Wishlist check
            portal_cursor.execute(
                "SELECT id FROM book_wishlist WHERE enrollment_no = ? AND book_id = ?",
                (session['student_id'], str(book_id))
            )
            book_data['isWishlisted'] = portal_cursor.fetchone() is not None
            
            portal_cursor.execute(
                "SELECT rating FROM book_ratings WHERE enrollment_no = ? AND book_id = ?",
                (session['student_id'], str(book_id))
            )
            rating_entry = portal_cursor.fetchone()
            if rating_entry:
                user_rating = rating_entry['rating']
        else:
            book_data['on_waitlist'] = False
            
        # Get Average Rating
        portal_cursor.execute("SELECT AVG(rating), COUNT(rating) FROM book_ratings WHERE book_id = ?", (str(book_id),))
        rating_stats = portal_cursor.fetchone()
        
        book_data['rating_avg'] = round(rating_stats[0], 1) if rating_stats[0] else None
        book_data['rating_count'] = rating_stats[1] if rating_stats[1] else 0
        book_data['user_rating'] = user_rating
        
        portal_conn.close()
        conn.close()
        return jsonify(book_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/books/<book_id>/rate', methods=['POST'])
def rate_book(book_id):
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        enrollment = session['student_id']
        rating = int(request.json.get('rating', 0))
        if rating < 1 or rating > 5:
            return jsonify({'error': 'Invalid rating'}), 400
        
        conn = get_portal_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM book_ratings WHERE book_id = ? AND enrollment_no = ?", (str(book_id), enrollment))
        if cursor.fetchone():
            cursor.execute("UPDATE book_ratings SET rating = ? WHERE book_id = ? AND enrollment_no = ?", (rating, str(book_id), enrollment))
        else:
            cursor.execute("INSERT INTO book_ratings (book_id, enrollment_no, rating) VALUES (?, ?, ?)", (str(book_id), enrollment, rating))
        
        conn.commit()
        
        cursor.execute("SELECT AVG(rating), COUNT(rating) FROM book_ratings WHERE book_id = ?", (str(book_id),))
        stats = cursor.fetchone()
        conn.close()
        
        return jsonify({'status': 'success', 'new_avg': round(stats[0], 1) if stats[0] else 0, 'new_count': stats[1] if stats[1] else 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/books/<book_id>/wishlist', methods=['POST'])
def toggle_wishlist_api(book_id):
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        enrollment = session['student_id']
        conn = get_portal_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM book_wishlist WHERE book_id = ? AND enrollment_no = ?", (str(book_id), enrollment))
        if cursor.fetchone():
            cursor.execute("DELETE FROM book_wishlist WHERE book_id = ? AND enrollment_no = ?", (str(book_id), enrollment))
            status = 'removed'
        else:
            cursor.execute("INSERT INTO book_wishlist (book_id, enrollment_no) VALUES (?, ?)", (str(book_id), enrollment))
            status = 'added'
            
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'action': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/books/<book_id>/notify', methods=['POST'])
def add_to_waitlist(book_id):
    """Add student to waitlist for out-of-stock book."""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        enrollment_no = session['student_id']
        
        # Get book details
        library_conn = get_library_db()
        library_cursor = library_conn.cursor()
        library_cursor.execute("SELECT title, total_copies FROM books WHERE book_id = ?", (book_id,))
        book = library_cursor.fetchone()
        
        if not book:
            library_conn.close()
            return jsonify({'error': 'Book not found'}), 404
        
        book_title = book['title']
        
        # Check availability
        library_cursor.execute("SELECT COUNT(*) FROM borrow_records WHERE book_id = ? AND status = 'borrowed'", (book_id,))
        borrowed_count = library_cursor.fetchone()[0]
        available = book['total_copies'] - borrowed_count
        library_conn.close()
        
        if available > 0:
            return jsonify({'error': 'Book is currently available'}), 400
        
        # Add to waitlist
        portal_conn = get_portal_db()
        portal_cursor = portal_conn.cursor()
        
        try:
            portal_cursor.execute(
                "INSERT INTO book_waitlist (enrollment_no, book_id, book_title) VALUES (?, ?, ?)",
                (enrollment_no, book_id, book_title)
            )
            portal_conn.commit()
            portal_conn.close()
            
            return jsonify({
                'success': True,
                'message': 'You will be notified when this book becomes available'
            })
        except sqlite3.IntegrityError:
            portal_conn.close()
            return jsonify({'error': 'You are already on the waitlist for this book'}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/books/<book_id>/notify', methods=['DELETE'])
def remove_from_waitlist(book_id):
    """Remove student from waitlist."""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        enrollment_no = session['student_id']
        
        portal_conn = get_portal_db()
        portal_cursor = portal_conn.cursor()
        
        portal_cursor.execute(
            "DELETE FROM book_waitlist WHERE enrollment_no = ? AND book_id = ? AND notified = 0",
            (enrollment_no, book_id)
        )
        portal_conn.commit()
        
        if portal_cursor.rowcount == 0:
            portal_conn.close()
            return jsonify({'error': 'Not on waitlist for this book'}), 404
        
        portal_conn.close()
        return jsonify({
            'success': True,
            'message': 'Removed from waitlist'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def generate_email_template(header_title, user_name, main_text, details_dict=None, theme='blue', footer_note=None):
    """
    Generates a unified responsive HTML email.
    theme: 'blue' (Info/Receipt), 'green' (Success/Approved), 'orange' (Warning/Rejected)
    """
    colors = {
        'blue': {'bg': '#0F3460', 'box_bg': '#f0f4f8', 'box_border': '#d9e2ec', 'accent': '#0F3460'},
        'green': {'bg': '#28a745', 'box_bg': '#f0fdf4', 'box_border': '#bbf7d0', 'accent': '#15803d'},
        'orange': {'bg': '#dc3545', 'box_bg': '#fff5f5', 'box_border': '#feb2b2', 'accent': '#c53030'}
    }
    c = colors.get(theme, colors['blue'])
    
    # Build Details Table
    details_html = ""
    if details_dict:
        rows = ""
        for label, value in details_dict.items():
            rows += f"""
            <tr>
                <td style="padding: 8px 0; vertical-align: top; width: 35%; color: #666; font-weight: bold;">{label}:</td>
                <td style="padding: 8px 0; vertical-align: top; color: #333; font-weight: 500;">{value}</td>
            </tr>"""
        details_html = f"""
        <div style="background-color: {c['box_bg']}; border: 1px solid {c['box_border']}; border-radius: 8px; padding: 20px; margin: 25px 0;">
            <table style="width: 100%; border-collapse: collapse;">
                {rows}
            </table>
            {f'<p style="font-size: 13px; color: #666; font-style: italic; margin-top: 15px; border-top: 1px solid {c["box_border"]}; padding-top: 10px;">{footer_note}</p>' if footer_note else ''}
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: 'Helvetica', 'Arial', sans-serif; margin: 0; padding: 0; background-color: #f4f4f4; }}
        .container {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
        .header {{ background-color: {c['bg']}; color: #ffffff; padding: 30px 20px; text-align: center; }}
        .content {{ padding: 40px 30px; color: #333333; line-height: 1.6; }}
        .footer {{ text-align: center; font-size: 12px; color: #888888; padding: 20px; background-color: #f8f9fa; border-top: 1px solid #e1e1e1; }}
    </style>
</head>
<body>
    <div style="padding: 20px 0;">
        <div class="container">
            <div class="header">
                <h2 style="margin:0; font-size: 24px;">{header_title}</h2>
            </div>
            <div class="content">
                <p style="font-size: 16px;">Dear <strong>{user_name}</strong>,</p>
                <p style="font-size: 16px;">{main_text}</p>
                
                {details_html}
                
                <p style="margin-top: 30px;">Best regards,<br><strong>GPA Library Team</strong></p>
            </div>
            <div class="footer">
                &copy; {datetime.now().year} Government Polytechnic Awasari (Kh).<br>
                Automated System Message.
            </div>
        </div>
    </div>
</body>
</html>"""


@app.route('/api/request', methods=['POST'])
def api_submit_request():
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # Restrict Pass Out students from submitting requests
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    cursor_lib.execute("SELECT year FROM students WHERE enrollment_no = ?", (session['student_id'],))
    student = cursor_lib.fetchone()
    conn_lib.close()
    student_year = student['year'] if student and student['year'] else ''
    if student_year.strip().lower() in ['pass out', 'passout', 'passed out', 'alumni', 'graduate']:
        return jsonify({'error': 'Requests are not allowed for Pass Out students.'}), 403

    data = request.json
    req_type = data.get('type') # 'profile_update', 'renewal'
    details = data.get('details') # e.g., "Change email to x@y.com"

    if not req_type or not details:
        return jsonify({'error': 'Missing data'}), 400

    # Prevent duplicate pending renewal requests for the same book copy
    if req_type == 'renewal':
        try:
            parsed = json.loads(details) if isinstance(details, str) else details
            dup_accession = parsed.get('accession_no') if isinstance(parsed, dict) else None
        except:
            dup_accession = None
        if dup_accession:
            conn_dup = get_portal_db()
            cur_dup = conn_dup.cursor()
            cur_dup.execute(
                "SELECT details FROM requests WHERE enrollment_no = ? AND request_type = 'renewal' AND status = 'pending'",
                (session['student_id'],)
            )
            for row in cur_dup.fetchall():
                try:
                    existing_details = row['details']
                    if isinstance(existing_details, str):
                        existing_details = json.loads(existing_details)
                    if isinstance(existing_details, str):
                        existing_details = json.loads(existing_details)
                    if isinstance(existing_details, dict) and existing_details.get('accession_no') == dup_accession:
                        conn_dup.close()
                        return jsonify({'error': 'A renewal request for this book is already pending.'}), 409
                except:
                    continue
            conn_dup.close()

    # Prevent duplicate book requests and enforce borrow limits
    if req_type == 'book_request':
        try:
            parsed = json.loads(details) if isinstance(details, str) else details
            book_id = parsed.get('book_id') if isinstance(parsed, dict) else None
        except:
            book_id = None
            
        if book_id:
            # 1. Check for pending requests for the same book
            conn_dup = get_portal_db()
            cur_dup = conn_dup.cursor()
            cur_dup.execute(
                "SELECT details FROM requests WHERE enrollment_no = ? AND request_type = 'book_request' AND status = 'pending'",
                (session['student_id'],)
            )
            for row in cur_dup.fetchall():
                try:
                    existing_details = row['details']
                    if isinstance(existing_details, str):
                        existing_details = json.loads(existing_details)
                    if isinstance(existing_details, str):
                        existing_details = json.loads(existing_details)
                    if isinstance(existing_details, dict) and existing_details.get('book_id') == book_id:
                        conn_dup.close()
                        return jsonify({'error': 'You already have a pending request for this book.'}), 409
                except:
                    continue
            conn_dup.close()
            
            # 2. Check active loans and borrow limits
            conn_lib = get_library_db()
            cur_lib = conn_lib.cursor()
            
            # Check if already borrowed by this student
            cur_lib.execute("SELECT COUNT(*) as count FROM borrow_records WHERE enrollment_no = ? AND book_id = ? AND status = 'borrowed'", 
                            (session['student_id'], book_id))
            if cur_lib.fetchone()['count'] > 0:
                conn_lib.close()
                return jsonify({'error': 'You already have an active loan for this book.'}), 409
                
            # Check max borrow limit (configurable, assuming 5 as default limit for students)
            limit = int(os.getenv('MAX_BOOKS_PER_STUDENT', 5))
            cur_lib.execute("SELECT COUNT(*) as count FROM borrow_records WHERE enrollment_no = ? AND status = 'borrowed'", 
                            (session['student_id'],))
            if cur_lib.fetchone()['count'] >= limit:
                conn_lib.close()
                return jsonify({'error': f'Borrow limit reached. You can only borrow up to {limit} books.'}), 403
                
            conn_lib.close()


    try:
        conn = get_portal_db()
        cursor = conn.cursor()
        # Normalize details: if it's already a JSON string, store as-is; otherwise serialize
        if isinstance(details, str):
            # Validate it's valid JSON, else wrap it
            try:
                json.loads(details)
                details_to_store = details
            except (json.JSONDecodeError, TypeError):
                details_to_store = json.dumps(details)
        else:
            details_to_store = json.dumps(details)
        cursor.execute("INSERT INTO requests (enrollment_no, request_type, details) VALUES (?, ?, ?)",
                       (session['student_id'], req_type, details_to_store))
        conn.commit()
        conn.close()
        
        # Send Email Notification
        # Send Email Notification
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        
        # Fetch Name
        cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (session['student_id'],))
        student = cursor_lib.fetchone()
        student_name = student['name'].split()[0] if student and student['name'] else "Student"
        
        # Helper to parse book title from string details
        def get_title_from_details(details_obj):
            t = req_type
            if isinstance(details_obj, dict):
                 if 'title' in details_obj: return details_obj['title']
                 if 'book_id' in details_obj:
                     try:
                        cursor_lib.execute("SELECT title FROM books WHERE book_id = ?", (details_obj['book_id'],))
                        bd = cursor_lib.fetchone()
                        if bd: return bd['title']
                     except: pass
            elif isinstance(details_obj, str):
                if "Request for book: " in details_obj:
                    import re
                    match = re.search(r"Request for book: (.*?) \(ID:", details_obj)
                    if match: return match.group(1)
                    return details_obj.replace("Request for book: ", "")
            return t

        # Prepare Email Content based on Type
        email_subject = ""
        header_title = "Request Received"
        main_text = ""
        details_dict = {}
        theme = 'blue'
        
        current_date_str = datetime.now().strftime('%d %b %Y, %I:%M %p')

        if req_type == 'book_request':
            b_title = get_title_from_details(details)
            email_subject = f"Request Received: {b_title}"
            header_title = "Reservation Received"
            main_text = f"We have received your request to reserve <strong>{b_title}</strong>."
            details_dict = {
                'Book Title': b_title,
                'Request Date': current_date_str,
                'Status': 'Pending Approval'
            }
            
        elif req_type == 'renewal':
            b_title = get_title_from_details(details)
            email_subject = f"Renewal Request: {b_title}"
            header_title = "Renewal Request"
            main_text = f"We have received your request to renew <strong>{b_title}</strong>."
            details_dict = {
                'Book Title': b_title,
                'Request Date': current_date_str,
                'Status': 'Pending Approval'
            }
            
        elif req_type == 'profile_update':
            email_subject = "Profile Update Request"
            header_title = "Profile Update"
            main_text = "We have received your request to update your library profile."
            details_summary = json.dumps(details) if isinstance(details, dict) else str(details)
            details_dict = {
                'Requested Changes': details_summary,
                'Request Date': current_date_str
            }
            
        else:
            # Generic fallback
            email_subject = f"Request Received: {req_type}"
            main_text = f"We have received your {req_type} request."
            details_dict = {'Details': str(details)}

        conn_lib.close()

        email_body = generate_email_template(
            header_title=header_title,
            user_name=student_name,
            main_text=main_text,
            details_dict=details_dict,
            theme='blue',
            footer_note="You will be notified once the librarian reviews your request."
        )
        
        trigger_notification_email(session['student_id'], email_subject, email_body)
        
        return jsonify({'status': 'success', 'message': 'Request submitted to librarian'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/requests', methods=['GET'])
def api_get_requests():
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM requests WHERE enrollment_no = ? ORDER BY created_at DESC", (session['student_id'],))
    rows = cursor.fetchall()
    conn.close()

    # Bug 9 Fix: Normalize PK — always expose as `req_id` so the cancel button
    # works regardless of whether the DB schema uses `req_id` or legacy `id`.
    requests = []
    for row in rows:
        r = dict(row)
        if 'req_id' not in r or r['req_id'] is None:
            r['req_id'] = r.get('id')  # fall back to legacy column
        requests.append(r)

    return jsonify({'requests': requests})

@app.route('/api/request/<int:req_id>/cancel', methods=['POST'])
def api_cancel_request(req_id):
    if 'student_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Verify ownership and status
    pk = _requests_pk_column(conn)
    cursor.execute(f"SELECT status FROM requests WHERE {pk} = ? AND enrollment_no = ?", (req_id, session['student_id']))
    req = cursor.fetchone()
    
    if not req:
        conn.close()
        return jsonify({'error': 'Request not found'}), 404
        
    if req['status'] != 'pending':
        conn.close()
        return jsonify({'error': f'Cannot cancel request in {req["status"]} state'}), 400
        
    # Cancel request
    try:
        cursor.execute(f"UPDATE requests SET status = 'cancelled' WHERE {pk} = ? AND enrollment_no = ?", (req_id, session['student_id']))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Request cancelled successfully'})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/books')
def api_books():
    # Read-Only Catalogue with pagination
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    availability = request.args.get('availability', 'all').strip().lower()

    # Pagination params (safe defaults and bounds)
    try:
        page = int(request.args.get('page', 1))
    except Exception:
        page = 1
    try:
        per_page = int(request.args.get('per_page', 30))
    except Exception:
        per_page = 30

    page = max(1, page)
    per_page = max(10, min(per_page, 100))
    offset = (page - 1) * per_page

    conn = get_library_db()
    cursor = conn.cursor()

    where_parts = []
    params = []

    if query:
        like_q = f'%{query}%'
        if _is_postgres_connection(conn):
            # PostgreSQL: ILIKE is case-insensitive
            where_parts.append("(b.title ILIKE ? OR b.author ILIKE ? OR COALESCE(b.isbn,'') ILIKE ? OR b.book_id ILIKE ?)")
        else:
            # SQLite: explicit NOCASE collation for deterministic behavior
            where_parts.append("(b.title LIKE ? COLLATE NOCASE OR b.author LIKE ? COLLATE NOCASE OR COALESCE(b.isbn,'') LIKE ? COLLATE NOCASE OR b.book_id LIKE ? COLLATE NOCASE)")
        params.extend([like_q, like_q, like_q, like_q])

    if category and category != 'All':
        where_parts.append("b.category = ?")
        params.append(category)

    # Availability filter using stored available_copies for speed
    if availability == 'available':
        where_parts.append("COALESCE(b.available_copies, 0) > 0")
    elif availability == 'out_of_stock':
        where_parts.append("COALESCE(b.available_copies, 0) <= 0")

    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ''

    from_sql = "FROM books b"

    # Total count for pagination metadata
    cursor.execute(f"SELECT COUNT(*) {from_sql} {where_sql}", params)
    total = cursor.fetchone()[0]

    # Paged rows
    cursor.execute(
        f"""
        SELECT
            b.book_id,
            b.title,
            b.author,
            b.category,
            b.total_copies,
            COALESCE(b.available_copies, 0) AS available_copies,
            b.cover_url
        {from_sql}
        {where_sql}
        ORDER BY b.title
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset]
    )
    books = [dict(row) for row in cursor.fetchall()]

    # Get distinct categories for filter
    cursor.execute("SELECT DISTINCT category FROM books WHERE category IS NOT NULL AND TRIM(category) != '' ORDER BY category")
    categories = [row[0] for row in cursor.fetchall()]

    conn.close()

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    return jsonify({
        'books': books,
        'categories': categories,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages
        }
    })

# --- Admin/Librarian API Endpoints ---

@app.route('/api/admin/all-requests')
def api_admin_all_requests():
    """Fetch all pending requests for librarian management"""
    try:
        conn = get_portal_db()
        cursor = conn.cursor()

        # Fetch general requests (profile_update, renewal, book_reservation, student_registration, etc.)
        pk = _requests_pk_column(conn)
        cursor.execute(f"""
            SELECT {pk} as req_id, enrollment_no, request_type, details, status, created_at
            FROM requests
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
        general_requests = []
        for row in cursor.fetchall():
            req = dict(row)
            # Try to parse JSON details
            try:
                req['details'] = json.loads(req['details']) if req['details'] else {}
            except Exception:
                req['details'] = {'raw': req.get('details')}
            general_requests.append(req)

        # Fetch deletion requests
        cursor.execute("""
            SELECT id, student_id, reason, status, timestamp
            FROM deletion_requests
            WHERE status = 'pending'
            ORDER BY timestamp DESC
        """)
        deletion_requests = [dict(row) for row in cursor.fetchall()]

        conn.close()

        # Get student names from library DB
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()

        # Enrich general requests with student names and department
        for req in general_requests:
            enrollment_no = str(req.get('enrollment_no', '')).strip()
            cursor_lib.execute("SELECT name, department FROM students WHERE enrollment_no = ?", (enrollment_no,))
            student = cursor_lib.fetchone()
            if not student:
                # If this is a registration request, use the submitted name
                if req.get('request_type') == 'student_registration':
                    try:
                        d = req.get('details') or {}
                        req['student_name'] = d.get('name') or 'New Student'
                        req['department'] = d.get('department') or ''
                    except Exception:
                        req['student_name'] = 'New Student'
                        req['department'] = ''
                else:
                    req['student_name'] = 'Unknown'
                    req['department'] = ''
            else:
                try:
                    req['student_name'] = student['name']
                    req['department'] = student['department'] or ''
                except Exception:
                    # Last-resort fallback
                    req['student_name'] = dict(student).get('name', 'Unknown')
                    req['department'] = dict(student).get('department', '')

        # Enrich deletion requests with student names and department
        for req in deletion_requests:
            enrollment_no = str(req.get('student_id', '')).strip()
            cursor_lib.execute("SELECT name, department FROM students WHERE enrollment_no = ?", (enrollment_no,))
            student = cursor_lib.fetchone()
            if not student:
                req['student_name'] = 'Unknown'
                req['department'] = ''
            else:
                try:
                    req['student_name'] = student['name']
                    req['department'] = student['department'] or ''
                except Exception:
                    req['student_name'] = dict(student).get('name', 'Unknown')
                    req['department'] = dict(student).get('department', '')

        conn_lib.close()

        # Get rejected count from portal DB
        conn2 = get_portal_db()
        cursor2 = conn2.cursor()
        cursor2.execute("SELECT COUNT(*) as count FROM requests WHERE status = 'rejected'")
        rejected_count = cursor2.fetchone()['count']

        # Get deletion counts by status
        cursor2.execute("SELECT status, COUNT(*) as count FROM deletion_requests GROUP BY status")
        deletion_counts = {row['status']: row['count'] for row in cursor2.fetchall()}
        conn2.close()

        return jsonify({
            'requests': general_requests,
            'deletion_requests': deletion_requests,
            'rejected_count': rejected_count,
            'deletion_counts': deletion_counts,
            'counts': {
                'total': len(general_requests) + len(deletion_requests),
                'requests': len(general_requests),
                'deletions': len(deletion_requests)
            }
        })
    except Exception as e:
        error_id = _log_portal_exception('api_admin_all_requests', e)
        return jsonify({'status': 'error', 'message': 'Failed to load requests', 'error_id': error_id}), 500

@app.route('/api/admin/request-history')
def api_admin_request_history():
    """Fetch processed (approved/rejected) requests with search and filter"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Get filter params
    q = request.args.get('q', '').strip()
    days = request.args.get('days')
    
    pk = _requests_pk_column(conn)
    # Base query
    query = f"""
        SELECT {pk} as req_id, enrollment_no, request_type, details, status, created_at
        FROM requests
        WHERE status IN ('approved', 'rejected')
    """
    params = []
    
    # Date filter (backend-agnostic)
    if days and days.isdigit():
        cutoff = datetime.now() - timedelta(days=int(days))
        query += " AND created_at >= ?"
        params.append(cutoff.strftime('%Y-%m-%d %H:%M:%S'))
    
    query += " ORDER BY created_at DESC LIMIT 100"
    
    cursor.execute(query, params)
    processed_requests = []
    for row in cursor.fetchall():
        req = dict(row)
        try:
            req['details'] = json.loads(req['details']) if req['details'] else {}
        except:
            req['details'] = {'raw': req['details']}
        processed_requests.append(req)
    
    conn.close()
    
    # Get student names and filter by search query
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    
    filtered_requests = []
    
    for req in processed_requests:
        cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (req['enrollment_no'],))
        student = cursor_lib.fetchone()
        if student:
            student_name = student['name']
        else:
            # For registration requests, show the submitted name
            if req.get('request_type') == 'student_registration':
                try:
                    d = req.get('details') or {}
                    student_name = d.get('name') or 'New Student'
                except Exception:
                    student_name = 'New Student'
            else:
                student_name = 'Unknown'
        req['student_name'] = student_name
        
        # Apply search filter (if search query exists)
        if q:
            search_str = q.lower()
            if (search_str in req['enrollment_no'].lower() or 
                search_str in student_name.lower() or 
                search_str in req['request_type'].lower()):
                filtered_requests.append(req)
        else:
            filtered_requests.append(req)
    
    conn_lib.close()
    
    # Count by status (of filtered results)
    approved_count = len([r for r in filtered_requests if r['status'] == 'approved'])
    rejected_count = len([r for r in filtered_requests if r['status'] == 'rejected'])
    
    return jsonify({
        'history': filtered_requests,
        'counts': {
            'approved': approved_count,
            'rejected': rejected_count,
            'total': len(filtered_requests)
        }
    })

@app.route('/api/admin/deletion-history')
def api_admin_deletion_history():
    """Fetch processed deletion requests with search and filter"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Get filter params
    q = request.args.get('q', '').strip()
    days = request.args.get('days')
    
    # Base query
    query = """
        SELECT id, student_id, reason, status, timestamp
        FROM deletion_requests
        WHERE status IN ('approved', 'rejected')
    """
    params = []
    
    # Date filter (backend-agnostic)
    if days and days.isdigit():
        cutoff = datetime.now() - timedelta(days=int(days))
        query += " AND timestamp >= ?"
        params.append(cutoff.strftime('%Y-%m-%d %H:%M:%S'))
    
    query += " ORDER BY timestamp DESC LIMIT 100"
    
    cursor.execute(query, params)
    processed_deletions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    # Get student names and filter
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    
    filtered_deletions = []
    
    for req in processed_deletions:
        cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (req['student_id'],))
        student = cursor_lib.fetchone()
        student_name = student['name'] if student else 'Deleted Account'
        req['student_name'] = student_name
        
        # Apply search filter
        if q:
            search_str = q.lower()
            if (search_str in req['student_id'].lower() or 
                search_str in student_name.lower()):
                filtered_deletions.append(req)
        else:
            filtered_deletions.append(req)
    
    conn_lib.close()
    
    # Count by status (of filtered results)
    approved_count = len([r for r in filtered_deletions if r['status'] == 'approved'])
    rejected_count = len([r for r in filtered_deletions if r['status'] == 'rejected'])
    
    return jsonify({
        'history': filtered_deletions,
        'counts': {
            'approved': approved_count,
            'rejected': rejected_count,
            'total': len(filtered_deletions)
        }
    })

@app.route('/api/admin/requests/<int:req_id>/approve', methods=['GET', 'POST'])
def api_admin_approve_request(req_id):
    """Approve a general request"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    pk = _requests_pk_column(conn)
    # Get the request details
    cursor.execute(f"SELECT * FROM requests WHERE {pk} = ?", (req_id,))
    req = cursor.fetchone()
    
    if not req:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Request not found'}), 404
    
    # Handle special request types
    if req['request_type'] == 'student_registration':
        # Approving a registration inserts into the library DB
        try:
            details = json.loads(req['details']) if req['details'] else {}
        except Exception:
            details = {}

        enrollment_no = _normalize_enrollment(req['enrollment_no'])
        name = _safe_str(details.get('name'))
        year = _safe_str(details.get('year'))
        department = _safe_str(details.get('department'))
        phone = _safe_str(details.get('phone'))
        email = _safe_str(details.get('email'))

        if not enrollment_no or not name:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid registration details.'}), 400

        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        cursor_lib.execute("SELECT enrollment_no FROM students WHERE enrollment_no = ?", (enrollment_no,))
        exists = cursor_lib.fetchone()
        if exists:
            conn_lib.close()
            conn.close()
            return jsonify({'status': 'error', 'message': 'Student already exists in library.'}), 409

        # Insert into students
        cursor_lib.execute(
            """
            INSERT INTO students (enrollment_no, name, email, phone, department, year)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (enrollment_no, name, email, phone, department, year)
        )
        conn_lib.commit()
        conn_lib.close()

        # Ensure portal user settings has email so notifications can be delivered
        try:
            cursor.execute(
                """
                INSERT INTO user_settings (enrollment_no, email)
                VALUES (?, ?)
                ON CONFLICT(enrollment_no) DO UPDATE SET email=excluded.email
                """,
                (enrollment_no, email)
            )
        except Exception:
            pass

        # Update status to approved
        cursor.execute(f"UPDATE requests SET status = 'approved' WHERE {pk} = ?", (req_id,))

        # Notify student (stored notification for when they login)
        cursor.execute(
            """
            INSERT INTO user_notifications (enrollment_no, type, title, message, link, created_at)
            VALUES (?, 'request_update', 'Registration Approved', ?, '/login', ?)
            """,
            (enrollment_no, "Your library registration has been approved. You can now login.", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )

        # Email (best-effort)
        try:
            email_body = generate_email_template(
                header_title="Registration Approved",
                user_name=name.split()[0] if name else "Student",
                main_text="Your library registration request has been approved. You can now login to the portal.",
                details_dict={
                    'Enrollment No': enrollment_no,
                    'Status': 'Approved',
                    'Date': datetime.now().strftime('%d %b %Y')
                },
                theme='green',
                footer_note="Welcome to the library portal."
            )
            trigger_notification_email(enrollment_no, "✅ Registration Approved", email_body)
        except Exception:
            pass

        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Registration approved and student added to library.'})

    # For book requests, check availability and then create a real borrow_record (Bug 1 fix)
    if req['request_type'] == 'book_request':
        try:
            details = json.loads(req['details']) if req['details'] else {}
            book_id = details.get('book_id')
            if book_id:
                conn_lib = get_library_db()
                cursor_lib = conn_lib.cursor()
                cursor_lib.execute("SELECT available_copies FROM books WHERE book_id = ?", (book_id,))
                book_row = cursor_lib.fetchone()

                if book_row:
                    actual_available = book_row['available_copies']

                    if actual_available <= 0:
                        conn_lib.close()
                        conn.close()
                        return jsonify({'status': 'error', 'message': 'Cannot approve: No available copies left in the library.'}), 400

                    # --- Bug 1 Fix: Create borrow_record + decrement available_copies ---
                    borrow_date = datetime.now().strftime('%Y-%m-%d')
                    due_date = (datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d')
                    enrollment_no = _normalize_enrollment(req['enrollment_no'])
                    try:
                        cursor_lib.execute(
                            """
                            INSERT INTO borrow_records (enrollment_no, book_id, borrow_date, due_date, status)
                            VALUES (?, ?, ?, ?, 'borrowed')
                            """,
                            (enrollment_no, book_id, borrow_date, due_date)
                        )
                        cursor_lib.execute(
                            "UPDATE books SET available_copies = MAX(0, available_copies - 1) WHERE book_id = ?",
                            (book_id,)
                        )
                        conn_lib.commit()
                        # Push to cloud
                        _push_to_cloud(
                            "INSERT INTO borrow_records (enrollment_no, book_id, borrow_date, due_date, status) VALUES (?, ?, ?, ?, 'borrowed')",
                            (enrollment_no, book_id, borrow_date, due_date)
                        )
                        _push_to_cloud(
                            "UPDATE books SET available_copies = GREATEST(0, available_copies - 1) WHERE book_id = ?",
                            (book_id,)
                        )
                        print(f"[book_request approve] Created borrow_record for {enrollment_no} / {book_id}")
                    except Exception as br_err:
                        print(f"[book_request approve] Failed to create borrow_record: {br_err}")
                conn_lib.close()
        except Exception as e:
            print(f"[book_request approve] Outer error: {e}")

    # Update status to approved
    cursor.execute(f"UPDATE requests SET status = 'approved' WHERE {pk} = ?", (req_id,))
    
    # NOTIFICATION TRIGGER: Notify student
    # Parse details to get book name
    message = f"Your {req['request_type']} request has been approved."
    book_title = req['request_type']
    
    try:
        details = json.loads(req['details']) if req['details'] else {}
        
        # Try to find title in details first
        if 'title' in details:
            book_title = details['title']
            message = f"Your request for '{book_title}' has been approved."
        
        # If not, look update from library DB using book_id
        elif 'book_id' in details:
            try:
                conn_lib = get_library_db()
                cursor_lib = conn_lib.cursor()
                cursor_lib.execute("SELECT title FROM books WHERE book_id = ?", (details['book_id'],))
                book_data = cursor_lib.fetchone()
                conn_lib.close()
                
                if book_data:
                    book_title = book_data['title']
                    message = f"Your request for '{book_title}' has been approved."
            except:
                pass
        
        # Handle string details (e.g. "Request for book: Title (ID: X)")
        if isinstance(details, str):
            try:
                if "Request for book: " in details:
                    import re
                    match = re.search(r"Request for book: (.*?) \(ID:", details)
                    if match:
                        book_title = match.group(1)
                    else:
                        book_title = details.replace("Request for book: ", "")
                    message = f"Your request for '{book_title}' has been approved."
            except:
                pass
    except:
        pass

    cursor.execute("""
        INSERT INTO user_notifications (enrollment_no, type, title, message, link, created_at)
        VALUES (?, 'request_update', 'Request Approved', ?, '/requests', ?)
    """, (req['enrollment_no'], message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    
    # Email Trigger
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (req['enrollment_no'],))
    student_record = cursor_lib.fetchone()
    conn_lib.close()
    
    student_name = student_record['name'].split()[0] if student_record and student_record['name'] else "Student"
    
    # Email Construction
    email_subject = "Request Approved"
    header_title = "Request Approved"
    main_text = f"Your request has been approved."
    details_dict = {}
    footer_note = "Thank you for using the GPA Library System."

    if req['request_type'] == 'book_request':
        email_subject = f"✅ Ready for Pickup: {book_title}"
        header_title = "Request Approved"
        main_text = f"Great news! Your request to reserve <strong>{book_title}</strong> has been approved. It is ready for collection."
        deadline = (datetime.now() + timedelta(days=2)).strftime('%d %b %Y')
        details_dict = {
            'Location': 'Main Library Desk',
            'Bring': 'Student ID Card',
            'Deadline': deadline
        }
        footer_note = "If not collected by the deadline, the reservation will be cancelled."

    elif req['request_type'] == 'renewal':
        # Actually extend the due date in the library database
        try:
            details_parsed = json.loads(req['details']) if isinstance(req['details'], str) else req['details']
            renewal_book_id = details_parsed.get('book_id') if isinstance(details_parsed, dict) else None
            
            if renewal_book_id:
                conn_lib = get_library_db()
                cursor_lib = conn_lib.cursor()
                
                # Get accession_no if provided (targets specific copy)
                renewal_accession = details_parsed.get('accession_no') if isinstance(details_parsed, dict) else None
                
                # Get current due date and extend by loan period (default 7 days)
                if renewal_accession:
                    cursor_lib.execute(
                        "SELECT due_date FROM borrow_records WHERE accession_no = ? AND enrollment_no = ? AND return_date IS NULL ORDER BY borrow_date DESC LIMIT 1",
                        (renewal_accession, req['enrollment_no'])
                    )
                else:
                    cursor_lib.execute(
                        "SELECT due_date FROM borrow_records WHERE book_id = ? AND enrollment_no = ? AND return_date IS NULL ORDER BY borrow_date DESC LIMIT 1",
                        (renewal_book_id, req['enrollment_no'])
                    )
                borrow_record = cursor_lib.fetchone()
                
                if borrow_record and borrow_record['due_date']:
                    try:
                        current_due = datetime.strptime(borrow_record['due_date'], '%Y-%m-%d')
                    except:
                        current_due = datetime.now()
                    
                    # Extend from today or current due date, whichever is later
                    extend_from = max(current_due, datetime.now())
                    new_due_date = extend_from + timedelta(days=7)
                    
                    # Update only the specific copy if accession_no is known
                    if renewal_accession:
                        cursor_lib.execute(
                            "UPDATE borrow_records SET due_date = ? WHERE accession_no = ? AND enrollment_no = ? AND return_date IS NULL",
                            (new_due_date.strftime('%Y-%m-%d'), renewal_accession, req['enrollment_no'])
                        )
                        _push_to_cloud(
                            "UPDATE borrow_records SET due_date = ? WHERE accession_no = ? AND enrollment_no = ? AND return_date IS NULL",
                            (new_due_date.strftime('%Y-%m-%d'), renewal_accession, req['enrollment_no'])
                        )
                    else:
                        cursor_lib.execute(
                            "UPDATE borrow_records SET due_date = ? WHERE book_id = ? AND enrollment_no = ? AND return_date IS NULL",
                            (new_due_date.strftime('%Y-%m-%d'), renewal_book_id, req['enrollment_no'])
                        )
                        _push_to_cloud(
                            "UPDATE borrow_records SET due_date = ? WHERE book_id = ? AND enrollment_no = ? AND return_date IS NULL",
                            (new_due_date.strftime('%Y-%m-%d'), renewal_book_id, req['enrollment_no'])
                        )
                    conn_lib.commit()
                conn_lib.close()
        except Exception as e:
            print(f"Renewal due date extension error: {e}")
        
        email_subject = f"✅ Renewal Approved: {book_title}"
        header_title = "Renewal Approved"
        main_text = f"Your request to renew <strong>{book_title}</strong> was successful. The due date has been extended."
        # Bug 6 Fix: new_due_date is only set inside the borrow_record found block.
        # Use a safe local reference set during the block, falling back to +7 days.
        _renewal_new_due = locals().get('new_due_date')
        if _renewal_new_due is not None:
            new_due = _renewal_new_due.strftime('%d %b %Y')
        else:
            new_due = (datetime.now() + timedelta(days=7)).strftime('%d %b %Y')
        details_dict = {
            'Item': book_title,
            'New Due Date': new_due
        }
        footer_note = "Please return the book by the new date to avoid fines."

    elif req['request_type'] == 'profile_update':
        try:
            details_updated = json.loads(req['details']) if isinstance(req['details'], str) else req['details']
            if details_updated:
                conn_lib = get_library_db()
                cursor_lib = conn_lib.cursor()
                
                valid_keys = ['name', 'email', 'phone', 'department', 'year']
                set_clauses = []
                params = []
                for k in valid_keys:
                    if k in details_updated and details_updated[k]:
                        set_clauses.append(f"{k} = ?")
                        params.append(str(details_updated[k]))
                
                if set_clauses:
                    params.append(req['enrollment_no'])
                    query = f"UPDATE students SET {', '.join(set_clauses)} WHERE enrollment_no = ?"
                    cursor_lib.execute(query, params)
                    conn_lib.commit()
                    
                    try:
                        _push_to_cloud(query, params)
                    except:
                        pass
                conn_lib.close()
        except Exception as e:
            print(f"Profile update applying error: {e}")

        email_subject = "✅ Profile Updated"
        header_title = "Update Successful"
        main_text = "Your profile update request has been processed and applied to your account."
        details_dict = {
            'Status': 'Changes Applied',
            'Date': datetime.now().strftime('%d %b %Y')
        }

    elif req['request_type'] == 'password_reset':
        # Execute Reset Logic
        try:
             # Reset to Enrollment Number using EXISTING cursor (Fixes Timeout)
             default_hash = generate_password_hash(req['enrollment_no'])
             cursor.execute("UPDATE student_auth SET password = ?, is_first_login = 1, last_changed = CURRENT_TIMESTAMP WHERE enrollment_no = ?", (default_hash, req['enrollment_no']))
             _push_to_cloud(
                 "INSERT INTO student_auth (enrollment_no, password, is_first_login, last_changed) VALUES (?, ?, 1, CURRENT_TIMESTAMP) ON CONFLICT (enrollment_no) DO UPDATE SET password = EXCLUDED.password, is_first_login = 1, last_changed = CURRENT_TIMESTAMP",
                 (req['enrollment_no'], default_hash)
             )
        except Exception as e:
             return jsonify({'error': f"Failed to reset password: {str(e)}"}), 500

        email_subject = "✅ Password Reset Successful"
        header_title = "Password Reset"
        main_text = "Your password has been successfully reset by the librarian."
        details_dict = {
            'New Password': 'Your Enrollment Number',
            'Action Required': 'Login & Set New Password'
        }
        footer_note = "Please change your password immediately after logging in."

    email_body = generate_email_template(
        header_title=header_title,
        user_name=student_name,
        main_text=main_text,
        details_dict=details_dict,
        theme='green',
        footer_note=footer_note
    )

    trigger_notification_email(req['enrollment_no'], email_subject, email_body)
    
    conn.commit()
    conn.close()

    return jsonify({'status': 'success', 'message': 'Request approved'})

@app.route('/api/admin/requests/<int:req_id>/reject', methods=['GET', 'POST'])
def api_admin_reject_request(req_id):
    """Reject a general request"""
    conn = get_portal_db()
    cursor = conn.cursor()

    pk = _requests_pk_column(conn)

    # Bug 5 Fix: SELECT before UPDATE — if req_id is wrong, don't mark anything rejected
    cursor.execute(f"SELECT enrollment_no, request_type, details FROM requests WHERE {pk} = ?", (req_id,))
    req = cursor.fetchone()

    if not req:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Request not found'}), 404

    cursor.execute(f"UPDATE requests SET status = 'rejected' WHERE {pk} = ?", (req_id,))

    if req:
         # Parse details to get book name
         message = f"Your {req['request_type']} request was rejected."
         book_title = req['request_type']
         
         try:
            details = json.loads(req['details']) if req['details'] else {}
            
            if 'title' in details:
                book_title = details['title']
                message = f"Your request for '{book_title}' was rejected."
            elif 'book_id' in details:
                try:
                    conn_lib = get_library_db()
                    cursor_lib = conn_lib.cursor()
                    cursor_lib.execute("SELECT title FROM books WHERE book_id = ?", (details['book_id'],))
                    book_data = cursor_lib.fetchone()
                    conn_lib.close()
                    
                    if book_data:
                        book_title = book_data['title']
                        message = f"Your request for '{book_title}' was rejected."
                except:
                    pass
            
            # Handle string details
            if isinstance(details, str):
                try:
                    if "Request for book: " in details:
                        import re
                        match = re.search(r"Request for book: (.*?) \(ID:", details)
                        if match:
                            book_title = match.group(1)
                        else:
                            book_title = details.replace("Request for book: ", "")
                        message = f"Your request for '{book_title}' was rejected."
                except:
                    pass
         except:
             pass

         reject_link = '/requests'
         reject_title = 'Request Rejected'
         if req['request_type'] == 'student_registration':
             reject_link = '/login'
             reject_title = 'Registration Rejected'
             message = "Your library registration request was rejected. Please contact the librarian."

         cursor.execute("""
            INSERT INTO user_notifications (enrollment_no, type, title, message, link, created_at)
            VALUES (?, 'request_update', ?, ?, ?, ?)
        """, (req['enrollment_no'], reject_title, message, reject_link, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

         # Email Trigger
         conn_lib = get_library_db()
         cursor_lib = conn_lib.cursor()
         cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (req['enrollment_no'],))
         student_record = cursor_lib.fetchone()
         conn_lib.close()
         student_name = student_record['name'].split()[0] if student_record and student_record['name'] else "Student"

         email_subject = f"Request Declined: {book_title}"
         main_text = f"We regret to inform you that your request regarding <strong>{book_title}</strong> could not be fulfilled at this time."
         
         if req['request_type'] == 'profile_update':
             email_subject = "Profile Update Declined"
             main_text = "Your request to update profile details was not approved."
             
         email_body = generate_email_template(
            header_title="Request Declined",
            user_name=student_name,
            main_text=main_text,
            details_dict=None,
            theme='orange',
            footer_note="For more information, please visit the library desk."
         )
         
         trigger_notification_email(req['enrollment_no'], email_subject, email_body)

    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Request rejected'})

@app.route('/api/admin/deletion/<int:del_id>/approve', methods=['GET', 'POST'])
def api_admin_approve_deletion(del_id):
    """Approve account deletion request"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Get deletion request
    cursor.execute("SELECT student_id FROM deletion_requests WHERE id = ?", (del_id,))
    req = cursor.fetchone()
    
    if not req:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Deletion request not found'}), 404
    
    student_id = req['student_id']
    
    # Update status
    cursor.execute("UPDATE deletion_requests SET status = 'approved' WHERE id = ?", (del_id,))
    
    # Clean up auth record so student cannot log in
    cursor.execute("DELETE FROM student_auth WHERE enrollment_no = ?", (student_id,))
    
    # Clean up portal requests and notifications
    try:
        cursor.execute("DELETE FROM requests WHERE enrollment_no = ?", (student_id,))
    except Exception:
        pass
    try:
        cursor.execute("DELETE FROM user_notifications WHERE enrollment_no = ?", (student_id,))
    except Exception:
        pass
    try:
        cursor.execute("DELETE FROM user_settings WHERE enrollment_no = ?", (student_id,))
    except Exception:
        pass
    
    conn.commit()
    conn.close()
    
    # Delete student from the MAIN library database
    try:
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        
        # Return any borrowed books (mark as returned)
        cursor_lib.execute(
            "UPDATE borrow_records SET status = 'returned', return_date = ? WHERE enrollment_no = ? AND status = 'borrowed'",
            (datetime.now().strftime('%Y-%m-%d'), student_id)
        )
        
        # Update available copies for returned books
        cursor_lib.execute(
            "SELECT book_id FROM borrow_records WHERE enrollment_no = ? AND return_date = ?",
            (student_id, datetime.now().strftime('%Y-%m-%d'))
        )
        returned_books = cursor_lib.fetchall()
        for book_row in returned_books:
            cursor_lib.execute(
                "UPDATE books SET available_copies = available_copies + 1 WHERE book_id = ?",
                (book_row['book_id'],)
            )
        
        # Delete the student record
        cursor_lib.execute("DELETE FROM students WHERE enrollment_no = ?", (student_id,))
        conn_lib.commit()
        conn_lib.close()
        
        # Push changes to cloud
        today = datetime.now().strftime('%Y-%m-%d')
        _push_to_cloud(
            "UPDATE borrow_records SET status = 'returned', return_date = ? WHERE enrollment_no = ? AND status = 'borrowed'",
            (today, student_id)
        )
        _push_to_cloud("DELETE FROM students WHERE enrollment_no = ?", (student_id,))
    except Exception as e:
        print(f"[Deletion] Error removing student from library DB: {e}")
    
    return jsonify({
        'status': 'success', 
        'message': 'Deletion approved. Student removed from library system.',
        'student_id': student_id
    })

@app.route('/api/admin/deletion/<int:del_id>/reject', methods=['GET', 'POST'])
def api_admin_reject_deletion(del_id):
    """Reject account deletion request"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    cursor.execute("UPDATE deletion_requests SET status = 'rejected' WHERE id = ?", (del_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Deletion request rejected'})

@app.route('/api/admin/password-reset/<enrollment_no>', methods=['GET', 'POST'])
def api_admin_reset_password(enrollment_no):
    """Reset student password to enrollment number"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Check if auth record exists
    cursor.execute("SELECT * FROM student_auth WHERE enrollment_no = ?", (enrollment_no,))
    auth = cursor.fetchone()
    
    # Hash the enrollment number for reset
    hashed_pw = generate_password_hash(enrollment_no)
    
    if auth:
        # Reset to enrollment number and mark as first login
        cursor.execute("""
            UPDATE student_auth 
            SET password = ?, is_first_login = 1, last_changed = CURRENT_TIMESTAMP
            WHERE enrollment_no = ?
        """, (hashed_pw, enrollment_no))
    else:
        # Create new auth record with default password
        cursor.execute("""
            INSERT INTO student_auth (enrollment_no, password, is_first_login)
            VALUES (?, ?, 1)
        """, (enrollment_no, hashed_pw))
    
    _push_to_cloud(
        "INSERT INTO student_auth (enrollment_no, password, is_first_login) VALUES (?, ?, 1) ON CONFLICT (enrollment_no) DO UPDATE SET password = EXCLUDED.password, is_first_login = 1, last_changed = CURRENT_TIMESTAMP",
        (enrollment_no, hashed_pw)
    )
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'status': 'success', 
        'message': f'Password reset to enrollment number. Student will be prompted to change on next login.'
    })

@app.route('/api/admin/bulk-password-reset', methods=['POST'])
def api_admin_bulk_password_reset():
    """Reset passwords for all students in a year group or all students"""
    data = request.json
    year = data.get('year')  # '1st', '2nd', '3rd', or None for all
    
    try:
        # Get students from library.db
        conn_lib = get_library_db()
        cursor_lib = conn_lib.cursor()
        
        if year:
            cursor_lib.execute("SELECT enrollment_no FROM students WHERE year = ?", (year,))
        else:
            cursor_lib.execute("SELECT enrollment_no FROM students")
        
        students = cursor_lib.fetchall()
        conn_lib.close()
        
        if not students:
            return jsonify({'status': 'error', 'message': 'No students found'}), 404
        
        # Reset each student's password in portal.db
        conn_portal = get_portal_db()
        cursor_portal = conn_portal.cursor()
        
        reset_count = 0
        for student in students:
            enrollment_no = student['enrollment_no']
            hashed_pw = generate_password_hash(enrollment_no)
            
            # Check if auth record exists
            cursor_portal.execute("SELECT * FROM student_auth WHERE enrollment_no = ?", (enrollment_no,))
            auth = cursor_portal.fetchone()
            
            if auth:
                cursor_portal.execute("""
                    UPDATE student_auth 
                    SET password = ?, is_first_login = 1, last_changed = CURRENT_TIMESTAMP
                    WHERE enrollment_no = ?
                """, (hashed_pw, enrollment_no))
            else:
                cursor_portal.execute("""
                    INSERT INTO student_auth (enrollment_no, password, is_first_login)
                    VALUES (?, ?, 1)
                """, (enrollment_no, hashed_pw))
            
            _push_to_cloud(
                "INSERT INTO student_auth (enrollment_no, password, is_first_login) VALUES (?, ?, 1) ON CONFLICT (enrollment_no) DO UPDATE SET password = EXCLUDED.password, is_first_login = 1, last_changed = CURRENT_TIMESTAMP",
                (enrollment_no, hashed_pw)
            )
            reset_count += 1
        
        conn_portal.commit()
        conn_portal.close()
        
        year_label = f"{year} Year" if year else "All Years"
        return jsonify({
            'status': 'success',
            'message': f'Password reset for {reset_count} students in {year_label}',
            'count': reset_count
        })
        
    except Exception as e:
        print(f"Bulk reset error: {e}")
        return jsonify({'status': 'error', 'message': 'Bulk reset failed'}), 500

@app.route('/api/admin/auth-stats')

def api_admin_auth_stats():
    """Get auth statistics and recent password resets for dashboard"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Total registered students
    cursor.execute("SELECT COUNT(*) as count FROM student_auth")
    total_registered = cursor.fetchone()['count']
    
    # Students with changed passwords (not first login)
    cursor.execute("SELECT COUNT(*) as count FROM student_auth WHERE is_first_login = 0")
    active_users = cursor.fetchone()['count']
    
    # Students still on default password
    cursor.execute("SELECT COUNT(*) as count FROM student_auth WHERE is_first_login = 1")
    pending_change = cursor.fetchone()['count']
    
    # Recent password resets (by checking last_changed within last 7 days where is_first_login = 1)
    cursor.execute("""
        SELECT enrollment_no, last_changed 
        FROM student_auth 
        WHERE is_first_login = 1 AND last_changed IS NOT NULL
        ORDER BY last_changed DESC 
        LIMIT 10
    """)
    recent_resets = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Get student names
    conn_lib = get_library_db()
    cursor_lib = conn_lib.cursor()
    
    for reset in recent_resets:
        cursor_lib.execute("SELECT name FROM students WHERE enrollment_no = ?", (reset['enrollment_no'],))
        student = cursor_lib.fetchone()
        reset['student_name'] = student['name'] if student else 'Unknown'
    
    conn_lib.close()
    
    return jsonify({
        'stats': {
            'total_registered': total_registered,
            'active_users': active_users,
            'pending_change': pending_change
        },
        'recent_resets': recent_resets
    })

@app.route('/api/admin/stats')
def api_admin_stats():
    """Get portal statistics for dashboard"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    # Count requests by status
    cursor.execute("SELECT status, COUNT(*) as count FROM requests GROUP BY status")
    request_stats = {row['status']: row['count'] for row in cursor.fetchall()}
    
    # Count deletion requests by status
    cursor.execute("SELECT status, COUNT(*) as count FROM deletion_requests GROUP BY status")
    deletion_stats = {row['status']: row['count'] for row in cursor.fetchall()}
    
    # Count active auth records
    cursor.execute("SELECT COUNT(*) as count FROM student_auth")
    auth_count = cursor.fetchone()['count']
    
    # Count first-time login pending
    cursor.execute("SELECT COUNT(*) as count FROM student_auth WHERE is_first_login = 1")
    first_login_count = cursor.fetchone()['count']
    
    conn.close()
    
    return jsonify({
        'requests': request_stats,
        'deletions': deletion_stats,
        'portal_users': auth_count,
        'pending_password_change': first_login_count
    })

# =====================================================================
# STUDY MATERIALS API ENDPOINTS
# =====================================================================

@app.route('/api/study-materials', methods=['GET'])
def api_get_study_materials():
    """Get study materials (optionally filtered by year and branch)"""
    year_filter = request.args.get('year', None)
    branch_filter = request.args.get('branch', None)
    
    conn = get_portal_db()
    cursor = conn.cursor()
    
    query = "SELECT * FROM study_materials WHERE active = 1"
    params = []
    
    if year_filter and year_filter != 'All':
        query += " AND year = ?"
        params.append(year_filter)
    
    if branch_filter and branch_filter != 'All':
        query += " AND (branch = ? OR branch = 'All Branches' OR branch IS NULL OR branch = '')"
        params.append(branch_filter)
    
    query += " ORDER BY upload_date DESC"
    
    cursor.execute(query, params)
    materials = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({'materials': materials})

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/admin/study-materials', methods=['GET', 'POST'])
def api_admin_study_materials():
    """Admin: Manage study materials"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    if request.method == 'GET':
        # Get all materials (including inactive)
        cursor.execute("SELECT * FROM study_materials ORDER BY upload_date DESC")
        materials = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'materials': materials})
    
    elif request.method == 'POST':
        # Handle file upload
        if 'file' not in request.files:
            conn.close()
            return jsonify({'status': 'error', 'message': 'No file uploaded'}), 400
        
        file = request.files['file']
        if file.filename == '':
            conn.close()
            return jsonify({'status': 'error', 'message': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            conn.close()
            return jsonify({'status': 'error', 'message': 'File type not allowed'}), 400
        
        # Get form data
        title = request.form.get('title')
        description = request.form.get('description', '')
        year = request.form.get('year')
        category = request.form.get('category', 'Notes')
        branch = request.form.get('branch', 'Computer')
        
        if not title or not year:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Title and year required'}), 400
        
        # Save file with unique name
        original_filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_filename = f"{timestamp}_{original_filename}"
        file_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        
        try:
            file.save(file_path)
            file_size = os.path.getsize(file_path)
            
            cursor.execute("""
                INSERT INTO study_materials (title, description, filename, original_filename, file_size, branch, year, category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, description, unique_filename, original_filename, file_size, branch, year, category))
            
            conn.commit()
            conn.close()
            return jsonify({'status': 'success', 'message': 'File uploaded successfully'})
        except Exception as e:
            conn.close()
            if os.path.exists(file_path):
                os.remove(file_path)
            return jsonify({'status': 'error', 'message': f'Upload failed: {str(e)}'}), 500

@app.route('/api/study-materials/<int:material_id>/download')
def download_study_material(material_id):
    """Download a study material file"""
    conn = get_portal_db()
    cursor = conn.cursor()
    cursor.execute("SELECT filename, original_filename FROM study_materials WHERE id = ? AND active = 1", (material_id,))
    material = cursor.fetchone()
    conn.close()
    
    if not material:
        return jsonify({'error': 'File not found'}), 404
    
    file_path = os.path.join(UPLOAD_FOLDER, material['filename'])
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found on server'}), 404
    
    return send_file(file_path, as_attachment=True, download_name=material['original_filename'])

@app.route('/api/admin/study-materials/<int:material_id>', methods=['DELETE', 'PUT'])
def api_admin_manage_material(material_id):
    """Admin: Delete or update study material"""
    conn = get_portal_db()
    cursor = conn.cursor()
    
    if request.method == 'DELETE':
        # Get filename before deletion
        cursor.execute("SELECT filename FROM study_materials WHERE id = ?", (material_id,))
        material = cursor.fetchone()
        
        # Soft delete (set active = 0)
        cursor.execute("UPDATE study_materials SET active = 0 WHERE id = ?", (material_id,))
        conn.commit()
        
        # Optionally delete physical file (commented out to keep files)
        # if material:
        #     file_path = os.path.join(UPLOAD_FOLDER, material['filename'])
        #     if os.path.exists(file_path):
        #         os.remove(file_path)
        
        conn.close()
        return jsonify({'status': 'success', 'message': 'Material deleted'})
    
    elif request.method == 'PUT':
        # Update material
        data = request.json
        cursor.execute("""
            UPDATE study_materials 
            SET title = ?, description = ?, drive_link = ?, year = ?, category = ?, branch = ?
            WHERE id = ?
        """, (data['title'], data.get('description', ''), data.get('drive_link', ''), 
              data['year'], data.get('category', 'Notes'), data.get('branch', 'All Branches'), material_id))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Material updated'})

# --- SPA Serving ---
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        resp = send_from_directory(app.static_folder, path)
        # Prevent stale UI when using PWA/service worker.
        # Hashed assets can be cached, but entrypoints should always revalidate.
        if path in ('index.html', 'sw.js', 'manifest.webmanifest') or path.endswith('registerSW.js') or path.startswith('workbox-'):
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            resp.headers['Pragma'] = 'no-cache'
        return resp
    
    # If path is an API call that wasn't matched, return 404
    if path.startswith('api/'):
        return jsonify({'error': 'Not Found'}), 404
        
    # Otherwise, for SPA routing, return index.html
    resp = send_from_directory(app.static_folder, 'index.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)

#