from flask import Flask, request, jsonify, render_template, session, Response, send_file
from datetime import datetime, timedelta
from version import __version__
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import re
import json
import glob
import threading
import fcntl
import time

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'notes-app-secret-key-change-in-production')
DB_PATH        = os.environ.get('DB_PATH',    '/data/notes.db')
BACKUP_DIR         = os.environ.get('BACKUP_DIR', '')
BACKUP_STATE       = '/data/.last_backup'
BACKUP_CONFIG_PATH = '/data/backup_config.json'
_SCHEDULER_LOCK_FD = None   # kept alive so flock stays held
_BACKUP_THREAD_LOCK = threading.Lock()  # prevents concurrent backups

_DEFAULT_CONFIG = {
    'frequency':    'daily',   # hourly | daily | weekly | monthly
    'time':         '02:00',   # HH:MM — pro daily/weekly/monthly
    'minute':       0,         # 0–59 — pro hourly
    'weekday':      0,         # 0=Po … 6=Ne — pro weekly
    'day_of_month': 1,         # 1–28 — pro monthly
}


# ── Backup helpers ────────────────────────────────────────────────────────────

def load_backup_config():
    if os.path.exists(BACKUP_CONFIG_PATH):
        try:
            with open(BACKUP_CONFIG_PATH) as f:
                cfg = json.load(f)
                return {**_DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_backup_config(cfg):
    os.makedirs('/data', exist_ok=True)
    with open(BACKUP_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


def _db_counts():
    """Returns (user_count, note_count) from the live DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        uc = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        nc = conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
        conn.close()
        return uc, nc
    except Exception:
        return 0, 0


def do_db_backup(created_by='automatická'):
    """Copy current SQLite DB to BACKUP_DIR. Creates a .meta.json sidecar with statistics.
    Returns (True, filepath) on success, (False, error_message) on failure.
    Retains the 30 most recent backups.
    """
    if not _BACKUP_THREAD_LOCK.acquire(blocking=False):
        return False, 'Záloha již probíhá'
    try:
        return _do_db_backup_inner(created_by)
    finally:
        _BACKUP_THREAD_LOCK.release()


def _do_db_backup_inner(created_by):
    bd = BACKUP_DIR
    if not bd:
        return False, 'BACKUP_DIR není nastaven'
    try:
        os.makedirs(bd, exist_ok=True)
        ts  = datetime.now().strftime('%Y-%m-%d_%H-%M')
        dst = os.path.join(bd, f'notes-backup-{ts}.db')
        # Online backup (safe for concurrent reads)
        src_conn = sqlite3.connect(DB_PATH)
        dst_conn = sqlite3.connect(dst)
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        # Metadata sidecar
        uc, nc = _db_counts()
        meta = {
            'created_at':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'created_by':  created_by,
            'version':     __version__,
            'user_count':  uc,
            'note_count':  nc,
            'db_size_kb':  round(os.path.getsize(dst) / 1024, 1),
        }
        with open(dst.replace('.db', '.meta.json'), 'w') as f:
            json.dump(meta, f)
        # Retain only 30 most recent — remove both .db and .meta.json
        all_db = sorted(glob.glob(os.path.join(bd, 'notes-backup-*.db')))
        for old in all_db[:-30]:
            try:
                os.remove(old)
                meta_old = old.replace('.db', '.meta.json')
                if os.path.exists(meta_old):
                    os.remove(meta_old)
            except Exception:
                pass
        # Persist last-backup timestamp
        with open(BACKUP_STATE, 'w') as f:
            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        app.logger.info(f'Backup OK: {dst}')
        return True, dst
    except Exception as e:
        app.logger.error(f'Backup error: {e}')
        return False, str(e)


def restore_from_backup(filename):
    """Restore DB from a backup file in BACKUP_DIR.
    Returns (True, message) or (False, error_message).
    """
    bd = BACKUP_DIR
    if not bd:
        return False, 'BACKUP_DIR není nastaven'
    src_path = os.path.join(bd, filename)
    if not os.path.exists(src_path):
        return False, 'Soubor zálohy nenalezen'
    # Basic sanity check — must be a valid SQLite file
    if not filename.startswith('notes-backup-') or not filename.endswith('.db'):
        return False, 'Neplatný název souboru zálohy'
    try:
        src_conn = sqlite3.connect(src_path)
        dst_conn = sqlite3.connect(DB_PATH)
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        # Update last-backup state so scheduler doesn't immediately re-backup
        with open(BACKUP_STATE, 'w') as f:
            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        app.logger.info(f'Restore OK from: {src_path}')
        return True, f'Záloha {filename} úspěšně obnovena'
    except Exception as e:
        app.logger.error(f'Restore error: {e}')
        return False, str(e)


def _get_last_backup():
    """Returns last backup datetime string, or None."""
    if os.path.exists(BACKUP_STATE):
        with open(BACKUP_STATE) as f:
            return f.read().strip() or None
    return None


def should_backup_now(cfg):
    """Returns True if a backup is due right now according to the schedule.

    Strategy: "has the scheduled moment already passed since the last backup?"
    This is robust against missed wakeups and app restarts — if the scheduler
    wakes up a few seconds or even minutes after the target time it still fires.
    """
    now  = datetime.now()
    freq = cfg.get('frequency', 'daily')
    last_str = _get_last_backup()
    last = None
    if last_str:
        try:
            last = datetime.strptime(last_str, '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass

    if freq == 'hourly':
        target_min = int(cfg.get('minute', 0))
        # Build the target timestamp for the CURRENT hour
        due = now.replace(minute=target_min, second=0, microsecond=0)
        if now < due:
            return False   # target minute hasn't arrived yet this hour
        # Already backed up at or after this hour's due time?
        if last and last >= due and last.date() == now.date() and last.hour == now.hour:
            return False
        return True

    # Parse HH:MM for daily/weekly/monthly
    try:
        h, m = map(int, cfg.get('time', '02:00').split(':'))
    except Exception:
        h, m = 2, 0

    if freq == 'daily':
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < due:
            return False   # not yet today
        if last and last.date() == now.date() and last >= due:
            return False   # already ran today
        return True

    if freq == 'weekly':
        target_wd = int(cfg.get('weekday', 0))
        if now.weekday() != target_wd:
            return False
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < due:
            return False
        if last:
            iso_now  = now.isocalendar()
            iso_last = last.isocalendar()
            if iso_last[0] == iso_now[0] and iso_last[1] == iso_now[1]:
                return False   # already ran this week
        return True

    if freq == 'monthly':
        target_day = int(cfg.get('day_of_month', 1))
        if now.day != target_day:
            return False
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < due:
            return False
        if last and last.year == now.year and last.month == now.month:
            return False   # already ran this month
        return True

    return False


def _backup_loop():
    """Background thread: checks every ~50 s against the configured schedule."""
    while True:
        try:
            if BACKUP_DIR:
                cfg = load_backup_config()
                if should_backup_now(cfg):
                    do_db_backup()
        except Exception as e:
            app.logger.error(f'Backup scheduler: {e}')
        time.sleep(50)


def start_backup_scheduler():
    """Start backup background thread. File lock ensures only one Gunicorn worker runs it.
    _SCHEDULER_LOCK_FD is kept as a module-level global so the OS lock stays held
    for the lifetime of the process (prevents GC-triggered early release).
    """
    global _SCHEDULER_LOCK_FD
    lock_path = '/data/.scheduler.lock'
    try:
        os.makedirs('/data', exist_ok=True)
        fd = open(lock_path, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _SCHEDULER_LOCK_FD = fd   # keep alive — must NOT be garbage-collected
        threading.Thread(target=_backup_loop, daemon=True).start()
        app.logger.info('Backup scheduler started (this worker owns the lock)')
    except IOError:
        app.logger.info('Backup scheduler: lock held by another worker, skipping')
    except Exception as e:
        app.logger.error(f'Backup scheduler start failed: {e}')


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        full_name     TEXT NOT NULL,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at    DATETIME DEFAULT (datetime('now', 'localtime')))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        text        TEXT NOT NULL,
        created_at  DATETIME DEFAULT (datetime('now', 'localtime')),
        updated_at  DATETIME,
        is_pinned   INTEGER NOT NULL DEFAULT 0,
        is_archived INTEGER NOT NULL DEFAULT 0,
        is_public   INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tags (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS note_tags (
        note_id INTEGER NOT NULL,
        tag_id  INTEGER NOT NULL,
        PRIMARY KEY (note_id, tag_id),
        FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS note_shares (
        note_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (note_id, user_id),
        FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        user_id           INTEGER PRIMARY KEY,
        show_public_notes INTEGER NOT NULL DEFAULT 1,
        accept_shares     INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS login_failures (
        username     TEXT PRIMARY KEY,
        failed_count INTEGER NOT NULL DEFAULT 0,
        last_failed  DATETIME,
        locked_until DATETIME)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ip_login_attempts (
        ip           TEXT NOT NULL,
        attempted_at DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ip_attempts ON ip_login_attempts(ip, attempted_at)')
    # Migrace starších DB — tabulka notes
    for col, definition in [
        ('updated_at',  'DATETIME'),
        ('is_pinned',   'INTEGER NOT NULL DEFAULT 0'),
        ('is_archived', 'INTEGER NOT NULL DEFAULT 0'),
        ('user_id',     'INTEGER REFERENCES users(id)'),
        ('is_public',   'INTEGER NOT NULL DEFAULT 0'),
    ]:
        try:
            conn.execute(f'ALTER TABLE notes ADD COLUMN {col} {definition}')
        except sqlite3.OperationalError:
            pass
    # Migrace — role uživatelů
    try:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    except sqlite3.OperationalError:
        pass
    # Auto-promote teplanm na admin (idempotentní)
    conn.execute("UPDATE users SET role = 'admin' WHERE username = 'teplanm'")
    conn.commit()
    conn.close()


init_db()
start_backup_scheduler()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user_id():
    return session.get('user_id')


def is_admin():
    """Returns True if the current session belongs to an admin user."""
    uid = current_user_id()
    if not uid:
        return False
    conn = get_db()
    row = conn.execute('SELECT role FROM users WHERE id = ?', (uid,)).fetchone()
    conn.close()
    return bool(row and row['role'] == 'admin')


def user_filter(strict=False, show_public=True):
    """Returns (sql_condition, params) for filtering notes by current user.
    strict=True: only own notes (used for archive view).
    strict=False: own notes + (public if show_public) + notes shared with current user.
    """
    uid = current_user_id()
    if strict:
        if uid:
            return ' AND n.user_id = ?', [uid]
        return ' AND n.user_id IS NULL', []
    else:
        if uid:
            if show_public:
                return (
                    ' AND (n.user_id = ? OR n.is_public = 1 OR '
                    'EXISTS (SELECT 1 FROM note_shares ns WHERE ns.note_id = n.id AND ns.user_id = ?))',
                    [uid, uid]
                )
            else:
                return (
                    ' AND (n.user_id = ? OR '
                    'EXISTS (SELECT 1 FROM note_shares ns WHERE ns.note_id = n.id AND ns.user_id = ?))',
                    [uid, uid]
                )
        # Unauthenticated: no notes returned (viewing requires login)
        return ' AND 1 = 0', []


def check_note_access(conn, note_id):
    """Returns True if current session may modify this note."""
    row = conn.execute('SELECT user_id FROM notes WHERE id = ?', (note_id,)).fetchone()
    if not row:
        return False
    return row['user_id'] == current_user_id()


def get_user_settings(conn, uid):
    """Returns settings dict for user. Returns defaults if no row or no uid."""
    if not uid:
        return {'show_public_notes': 1, 'accept_shares': 1}
    row = conn.execute(
        'SELECT show_public_notes, accept_shares FROM user_settings WHERE user_id = ?', (uid,)
    ).fetchone()
    if row:
        return {'show_public_notes': row['show_public_notes'], 'accept_shares': row['accept_shares']}
    return {'show_public_notes': 1, 'accept_shares': 1}


# ── DB helpers ────────────────────────────────────────────────────────────────

def save_tags_for_note(conn, note_id, tag_names):
    conn.execute('DELETE FROM note_tags WHERE note_id = ?', (note_id,))
    for raw in tag_names:
        name = raw.strip().lower()
        if not name:
            continue
        conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (name,))
        tag_id = conn.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()['id']
        conn.execute('INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)', (note_id, tag_id))
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM note_tags)')


def get_tags_for_notes(conn, note_ids):
    if not note_ids:
        return {}
    ph = ','.join('?' * len(note_ids))
    rows = conn.execute(
        f'SELECT nt.note_id, t.name FROM note_tags nt JOIN tags t ON nt.tag_id = t.id '
        f'WHERE nt.note_id IN ({ph}) ORDER BY t.name',
        note_ids
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row['note_id'], []).append(row['name'])
    return result


def get_shares_for_notes(conn, note_ids):
    if not note_ids:
        return {}
    ph = ','.join('?' * len(note_ids))
    rows = conn.execute(
        f'SELECT ns.note_id, ns.user_id, u.username, u.full_name '
        f'FROM note_shares ns JOIN users u ON ns.user_id = u.id '
        f'WHERE ns.note_id IN ({ph}) ORDER BY u.full_name',
        note_ids
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row['note_id'], []).append({
            'user_id':   row['user_id'],
            'username':  row['username'],
            'full_name': row['full_name'],
        })
    return result


def note_select():
    return ('SELECT n.id, n.text, n.created_at, n.updated_at, n.is_pinned, n.is_archived, '
            'n.is_public, n.user_id, u.full_name AS owner_name '
            'FROM notes n LEFT JOIN users u ON n.user_id = u.id')


def parse_search(query):
    """Parsuje hledaný výraz: "přesná fráze" nebo jednotlivá slova (OR)."""
    terms = []
    for phrase in re.findall(r'"([^"]+)"', query):
        phrase = phrase.strip().lower()
        if phrase:
            terms.append(phrase)
    remaining = re.sub(r'"[^"]*"', '', query).strip()
    for word in remaining.split():
        word = word.strip().lower()
        if word:
            terms.append(word)
    return terms


# ── Login security helpers ────────────────────────────────────────────────────

def get_client_ip():
    """Get real client IP, respecting X-Forwarded-For from reverse proxy."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def record_ip_attempt(conn, ip):
    """Record a login attempt for this IP and clean up entries older than 1 minute."""
    conn.execute(
        "DELETE FROM ip_login_attempts WHERE attempted_at < datetime('now', 'localtime', '-1 minute')"
    )
    conn.execute("INSERT INTO ip_login_attempts (ip) VALUES (?)", (ip,))
    conn.commit()


def check_ip_rate_limit(conn, ip):
    """Returns True if this IP has made more than 10 attempts in the last minute."""
    count = conn.execute(
        "SELECT COUNT(*) FROM ip_login_attempts "
        "WHERE ip = ? AND attempted_at >= datetime('now', 'localtime', '-1 minute')",
        (ip,)
    ).fetchone()[0]
    return count > 10


def check_user_lockout(conn, username):
    """Returns (is_locked, locked_until_str) or (False, None).
    If lockout has expired, resets the counter so user gets fresh 3 attempts.
    """
    row = conn.execute(
        'SELECT locked_until FROM login_failures WHERE username = ?', (username,)
    ).fetchone()
    if not row or not row['locked_until']:
        return False, None
    still_locked = conn.execute(
        "SELECT locked_until > datetime('now', 'localtime') FROM login_failures WHERE username = ?",
        (username,)
    ).fetchone()[0]
    if still_locked:
        return True, row['locked_until']
    # Lockout expired – reset so user gets fresh 3 attempts
    conn.execute('DELETE FROM login_failures WHERE username = ?', (username,))
    conn.commit()
    return False, None


def record_failed_login(conn, username):
    """Increment failed login count. After 3rd failure, locks account for 5 minutes.
    Returns the new failed_count."""
    conn.execute(
        '''INSERT INTO login_failures (username, failed_count, last_failed, locked_until)
           VALUES (?, 1, datetime('now', 'localtime'), NULL)
           ON CONFLICT(username) DO UPDATE SET
               failed_count = login_failures.failed_count + 1,
               last_failed  = datetime('now', 'localtime'),
               locked_until = CASE WHEN login_failures.failed_count + 1 >= 3
                              THEN datetime('now', 'localtime', '+5 minutes')
                              ELSE NULL END''',
        (username,)
    )
    conn.commit()
    row = conn.execute('SELECT failed_count FROM login_failures WHERE username = ?', (username,)).fetchone()
    return row['failed_count'] if row else 1


def reset_login_failures(conn, username):
    """Clear failed login counter after successful authentication."""
    conn.execute('DELETE FROM login_failures WHERE username = ?', (username,))
    conn.commit()


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    uid = current_user_id()
    if not uid:
        return jsonify(None)
    conn = get_db()
    row = conn.execute(
        'SELECT id, username, full_name, email, role FROM users WHERE id = ?', (uid,)
    ).fetchone()
    conn.close()
    if not row:
        session.clear()
        return jsonify(None)
    return jsonify(dict(row))


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data     = request.get_json() or {}
    username = (data.get('username') or '').strip().lower()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'error': 'Vyplňte přihlašovací jméno a heslo'}), 400

    ip   = get_client_ip()
    conn = get_db()

    # 1) Per-user lockout check
    is_locked, locked_until = check_user_lockout(conn, username)
    if is_locked:
        conn.close()
        try:
            dt = datetime.strptime(locked_until, '%Y-%m-%d %H:%M:%S')
            time_str = dt.strftime('%H:%M:%S')
        except Exception:
            time_str = str(locked_until)
        return jsonify({'error': f'Přihlášení je zablokováno do {time_str}. Zkuste to prosím až po uvedeném čase.'}), 423

    # 2) IP rate limit: record attempt, then check (10 pokusů za minutu)
    record_ip_attempt(conn, ip)
    if check_ip_rate_limit(conn, ip):
        conn.close()
        return jsonify({'error': 'Příliš mnoho pokusů o přihlášení. Zkuste to za chvíli.'}), 429

    # 3) Verify credentials
    row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        new_count = record_failed_login(conn, username)
        conn.close()
        if new_count == 2:
            return jsonify({'error': 'Nesprávné přihlašovací jméno nebo heslo. Pozor: při dalším neúspěšném pokusu bude přihlášení zablokováno na 5 minut.'}), 401
        return jsonify({'error': 'Nesprávné přihlašovací jméno nebo heslo'}), 401

    # 4) Success – reset failure counter and create session
    reset_login_failures(conn, username)
    conn.close()
    session['user_id'] = row['id']
    return jsonify({
        'id': row['id'], 'username': row['username'],
        'full_name': row['full_name'], 'email': row['email']
    })


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    data      = request.get_json() or {}
    username  = (data.get('username')  or '').strip().lower()
    full_name = (data.get('full_name') or '').strip()
    email     = (data.get('email')     or '').strip().lower()
    password  = data.get('password')  or ''
    password2 = data.get('password2') or ''

    if not all([username, full_name, email, password, password2]):
        return jsonify({'error': 'Všechna pole jsou povinná'}), 400
    if not re.match(r'^[a-z0-9_]{3,30}$', username):
        return jsonify({'error': 'Uživatelské jméno: 3–30 znaků, pouze a-z, 0-9 a _'}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': 'Neplatný formát e-mailu'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Heslo musí mít alespoň 6 znaků'}), 400
    if password != password2:
        return jsonify({'error': 'Hesla se neshodují'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, full_name, email, password_hash) VALUES (?, ?, ?, ?)',
            (username, full_name, email, generate_password_hash(password))
        )
        conn.commit()
        row = conn.execute(
            'SELECT id, username, full_name, email FROM users WHERE username = ?', (username,)
        ).fetchone()
        session['user_id'] = row['id']
        conn.close()
        return jsonify(dict(row)), 201
    except sqlite3.IntegrityError as e:
        conn.close()
        msg = str(e)
        if 'username' in msg:
            return jsonify({'error': 'Toto uživatelské jméno je již obsazeno'}), 409
        if 'email' in msg:
            return jsonify({'error': 'Tento e-mail je již registrován'}), 409
        return jsonify({'error': 'Chyba při registraci'}), 409


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/change-password', methods=['POST'])
def auth_change_password():
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'Nejste přihlášeni'}), 401
    data    = request.get_json() or {}
    old_pw  = data.get('old_password',  '')
    new_pw  = data.get('new_password',  '')
    new_pw2 = data.get('new_password2', '')
    if not old_pw or not new_pw or not new_pw2:
        return jsonify({'error': 'Všechna pole jsou povinná'}), 400
    if new_pw != new_pw2:
        return jsonify({'error': 'Nová hesla se neshodují'}), 400
    if len(new_pw) < 6:
        return jsonify({'error': 'Nové heslo musí mít alespoň 6 znaků'}), 400
    conn = get_db()
    row  = conn.execute('SELECT password_hash FROM users WHERE id = ?', (uid,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], old_pw):
        conn.close()
        return jsonify({'error': 'Původní heslo je nesprávné'}), 400
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                 (generate_password_hash(new_pw), uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── App routes ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', version=__version__)


@app.route('/manifest.json')
def manifest():
    from flask import Response
    import json
    data = {
        "id": "/",
        "name": "Poznámky",
        "short_name": "Poznámky",
        "description": "Zápisky vždy po ruce",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f0f2f5",
        "theme_color": "#1a1a2e",
        "orientation": "any",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    return Response(json.dumps(data), mimetype='application/manifest+json')


@app.route('/service-worker.js')
def service_worker():
    sw = """
const CACHE = 'notes-v2';
const OFFLINE_HTML = '<html><body style="font-family:sans-serif;padding:2rem;background:#f0f2f5"><h2>Jsi offline 📵</h2><p>Připoj se k internetu a zkus to znovu.</p></body></html>';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(['/', '/manifest.json']))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(r => { const c = r.clone(); caches.open(CACHE).then(cache => cache.put(e.request, c)); return r; })
        .catch(() => caches.match(e.request).then(r => r ||
          new Response(OFFLINE_HTML, {headers:{'Content-Type':'text/html'}})))
    );
  }
});
"""
    from flask import Response
    return Response(sw, mimetype='application/javascript')


@app.route('/api/tags', methods=['GET'])
def get_tags():
    uid = current_user_id()
    if not uid:
        return jsonify([])
    q   = request.args.get('q', '').strip().lower()
    notes_cond   = ('(n.user_id = ? OR n.is_public = 1 OR '
                    'EXISTS (SELECT 1 FROM note_shares ns WHERE ns.note_id = n.id AND ns.user_id = ?))')
    notes_params = [uid, uid]

    conn = get_db()
    if q:
        rows = conn.execute(
            f'SELECT t.name, COUNT(DISTINCT nt.note_id) as cnt FROM tags t '
            f'INNER JOIN note_tags nt ON t.id = nt.tag_id '
            f'INNER JOIN notes n ON nt.note_id = n.id '
            f'WHERE t.name LIKE ? AND {notes_cond} '
            f'GROUP BY t.id ORDER BY cnt DESC, t.name',
            [f'%{q}%'] + notes_params
        ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT t.name, COUNT(DISTINCT nt.note_id) as cnt FROM tags t '
            f'INNER JOIN note_tags nt ON t.id = nt.tag_id '
            f'INNER JOIN notes n ON nt.note_id = n.id '
            f'WHERE {notes_cond} '
            f'GROUP BY t.id ORDER BY cnt DESC, t.name',
            notes_params
        ).fetchall()
    conn.close()
    return jsonify([{'name': r['name'], 'count': r['cnt']} for r in rows])


@app.route('/api/notes', methods=['GET'])
def get_notes():
    if not current_user_id():
        return jsonify({'error': 'Přihlášení vyžadováno'}), 401
    sort_by    = request.args.get('sort_by', 'created_at')
    order      = request.args.get('order', 'desc')
    tags_raw   = request.args.get('tags', '')
    tag_mode   = request.args.get('tag_mode', 'or')
    search_raw = request.args.get('search', '').strip()

    if sort_by not in ('created_at', 'updated_at'): sort_by = 'created_at'
    if order    not in ('asc', 'desc'):              order   = 'desc'
    if tag_mode not in ('or', 'and'):                tag_mode = 'or'

    secondary    = (f'COALESCE(n.updated_at, n.created_at) {order}'
                    if sort_by == 'updated_at' else f'n.created_at {order}')
    order_clause = f'n.is_pinned DESC, {secondary}'

    show_archived = request.args.get('archived', '0') == '1'
    arch_val      = 1 if show_archived else 0
    tag_list      = [t.strip().lower() for t in tags_raw.split(',') if t.strip()]
    search_terms  = parse_search(search_raw) if search_raw else []
    sel           = note_select()

    conn = get_db()
    uid  = current_user_id()
    settings = get_user_settings(conn, uid)
    user_cond, user_params = user_filter(strict=show_archived,
                                         show_public=bool(settings['show_public_notes']))
    base_cond   = f'{user_cond} AND n.is_archived = {arch_val}'
    base_params = user_params[:]
    if search_terms:
        conds       = ['LOWER(n.text) LIKE ?' for _ in search_terms]
        base_cond  += ' AND (' + ' OR '.join(conds) + ')'
        base_params += [f'%{t}%' for t in search_terms]

    if tag_list:
        ph = ','.join('?' * len(tag_list))
        if tag_mode == 'and':
            query = (f'{sel} JOIN note_tags nt ON n.id = nt.note_id '
                     f'JOIN tags t ON nt.tag_id = t.id '
                     f'WHERE t.name IN ({ph}){base_cond} '
                     f'GROUP BY n.id HAVING COUNT(DISTINCT t.name) = {len(tag_list)} '
                     f'ORDER BY {order_clause}')
        else:
            query = (f'{sel} JOIN note_tags nt ON n.id = nt.note_id '
                     f'JOIN tags t ON nt.tag_id = t.id '
                     f'WHERE t.name IN ({ph}){base_cond} '
                     f'GROUP BY n.id ORDER BY {order_clause}')
        rows = conn.execute(query, tag_list + base_params).fetchall()
    else:
        rows = conn.execute(
            f'{sel} WHERE 1=1{base_cond} ORDER BY {order_clause}', base_params
        ).fetchall()

    note_ids   = [r['id'] for r in rows]
    tags_map   = get_tags_for_notes(conn, note_ids)
    shares_map = get_shares_for_notes(conn, note_ids)
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d['tags']   = tags_map.get(d['id'], [])
        d['shares'] = shares_map.get(d['id'], [])
        result.append(d)
    return jsonify(result)


@app.route('/api/notes', methods=['POST'])
def add_note():
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    uid       = current_user_id()
    is_public = 1 if (uid and data.get('is_public')) else 0
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO notes (text, user_id, is_public) VALUES (?, ?, ?)',
        (data['text'].strip(), uid, is_public)
    )
    note_id = cursor.lastrowid
    save_tags_for_note(conn, note_id, data.get('tags', []))
    conn.commit()
    row    = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    tags   = get_tags_for_notes(conn, [note_id]).get(note_id, [])
    shares = get_shares_for_notes(conn, [note_id]).get(note_id, [])
    conn.close()
    result = dict(row); result['tags'] = tags; result['shares'] = shares
    return jsonify(result), 201


@app.route('/api/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    uid  = current_user_id()
    conn = get_db()
    row  = conn.execute('SELECT user_id FROM notes WHERE id = ?', (note_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    is_owner       = row['user_id'] == uid
    is_shared_user = (not is_owner and uid and
                      conn.execute('SELECT 1 FROM note_shares WHERE note_id = ? AND user_id = ?',
                                   (note_id, uid)).fetchone() is not None)
    if not is_owner and not is_shared_user:
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    res = conn.execute(
        "UPDATE notes SET text = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (data['text'].strip(), note_id)
    )
    if res.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    if is_owner:
        save_tags_for_note(conn, note_id, data.get('tags', []))
    conn.commit()
    row    = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    tags   = get_tags_for_notes(conn, [note_id]).get(note_id, [])
    shares = get_shares_for_notes(conn, [note_id]).get(note_id, [])
    conn.close()
    result = dict(row); result['tags'] = tags; result['shares'] = shares
    return jsonify(result)


@app.route('/api/notes/<int:note_id>/pin', methods=['PUT'])
def toggle_pin(note_id):
    conn = get_db()
    if not check_note_access(conn, note_id):
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    res = conn.execute(
        'UPDATE notes SET is_pinned = 1 - is_pinned WHERE id = ?', (note_id,)
    )
    if res.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    conn.commit()
    row = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route('/api/notes/<int:note_id>/archive', methods=['PUT'])
def toggle_archive(note_id):
    conn = get_db()
    if not check_note_access(conn, note_id):
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    res = conn.execute(
        'UPDATE notes SET is_archived = 1 - is_archived WHERE id = ?', (note_id,)
    )
    if res.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    conn.commit()
    row = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route('/api/users/search', methods=['GET'])
def search_users():
    """Search users by username or full_name (for share autocomplete)."""
    uid = current_user_id()
    if not uid:
        return jsonify([]), 401
    q = request.args.get('q', '').strip().lower()
    if len(q) < 1:
        return jsonify([])
    conn = get_db()
    rows = conn.execute(
        'SELECT username, full_name FROM users WHERE id != ? AND '
        '(username LIKE ? OR LOWER(full_name) LIKE ?) LIMIT 8',
        (uid, f'%{q}%', f'%{q}%')
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/notes/<int:note_id>/shares', methods=['GET'])
def get_note_shares(note_id):
    conn = get_db()
    if not check_note_access(conn, note_id):
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    rows = conn.execute(
        'SELECT u.user_id, u.username, u.full_name FROM note_shares ns '
        'JOIN users u ON ns.user_id = u.id WHERE ns.note_id = ? ORDER BY u.full_name',
        (note_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/notes/<int:note_id>/shares', methods=['PUT'])
def update_note_shares(note_id):
    conn = get_db()
    if not check_note_access(conn, note_id):
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    data      = request.get_json() or {}
    usernames = [u.strip().lower() for u in data.get('usernames', []) if u.strip()]
    # Validate all usernames exist and accept shares
    user_ids = []
    for username in usernames:
        row = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': f'Uživatel „{username}" neexistuje'}), 404
        target_uid = row['id']
        target_settings = get_user_settings(conn, target_uid)
        if not target_settings['accept_shares']:
            conn.close()
            return jsonify({'error': f'Uživatel „{username}" nepřijímá sdílení poznámek'}), 403
        user_ids.append(target_uid)
    # Replace shares (full set)
    conn.execute('DELETE FROM note_shares WHERE note_id = ?', (note_id,))
    for share_uid in user_ids:
        conn.execute('INSERT OR IGNORE INTO note_shares (note_id, user_id) VALUES (?, ?)',
                     (note_id, share_uid))
    conn.commit()
    rows = conn.execute(
        'SELECT ns.user_id, u.username, u.full_name FROM note_shares ns '
        'JOIN users u ON ns.user_id = u.id WHERE ns.note_id = ? ORDER BY u.full_name',
        (note_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/notes/<int:note_id>/visibility', methods=['PUT'])
def toggle_visibility(note_id):
    conn = get_db()
    if not check_note_access(conn, note_id):
        conn.close()
        return jsonify({'error': 'Přístup odepřen'}), 403
    res = conn.execute(
        'UPDATE notes SET is_public = 1 - is_public WHERE id = ?', (note_id,)
    )
    if res.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    conn.commit()
    row = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'Nejste přihlášeni'}), 401
    conn = get_db()
    s = get_user_settings(conn, uid)
    conn.close()
    return jsonify(s)


@app.route('/api/settings', methods=['PUT'])
def api_update_settings():
    uid = current_user_id()
    if not uid:
        return jsonify({'error': 'Nejste přihlášeni'}), 401
    data         = request.get_json() or {}
    show_public  = 1 if data.get('show_public_notes', True) else 0
    accept_share = 1 if data.get('accept_shares',     True) else 0
    conn = get_db()
    conn.execute(
        'INSERT INTO user_settings (user_id, show_public_notes, accept_shares) VALUES (?, ?, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET '
        'show_public_notes = excluded.show_public_notes, '
        'accept_shares     = excluded.accept_shares',
        (uid, show_public, accept_share)
    )
    conn.commit()
    conn.close()
    return jsonify({'show_public_notes': show_public, 'accept_shares': accept_share})


@app.route('/api/stats/daily', methods=['GET'])
def get_daily_stats():
    stat_type = request.args.get('type', 'created')
    days      = request.args.get('days', None)
    date_from = request.args.get('from', None)
    date_to   = request.args.get('to', None)
    today = datetime.now().date()
    if days:
        try: n = int(days)
        except ValueError: n = 7
        date_from = (today - timedelta(days=n - 1)).isoformat()
        date_to   = today.isoformat()
    elif not date_from or not date_to:
        date_from = (today - timedelta(days=6)).isoformat()
        date_to   = today.isoformat()

    col = 'created_at' if stat_type == 'created' else 'updated_at'
    uid = current_user_id()
    if uid:
        user_cond, user_params = 'AND user_id = ?', [uid]
    else:
        user_cond, user_params = 'AND user_id IS NULL', []

    conn = get_db()
    if stat_type == 'edited':
        rows = conn.execute(
            f'SELECT date({col}) as day, COUNT(*) as count FROM notes '
            f'WHERE {col} IS NOT NULL AND date({col}) BETWEEN ? AND ? {user_cond} '
            f'GROUP BY day ORDER BY day',
            [date_from, date_to] + user_params
        ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT date({col}) as day, COUNT(*) as count FROM notes '
            f'WHERE date({col}) BETWEEN ? AND ? {user_cond} '
            f'GROUP BY day ORDER BY day',
            [date_from, date_to] + user_params
        ).fetchall()
    conn.close()
    data_map = {r['day']: r['count'] for r in rows}
    result, cur = [], datetime.fromisoformat(date_from).date()
    end = datetime.fromisoformat(date_to).date()
    while cur <= end:
        s = cur.isoformat(); result.append({'day': s, 'count': data_map.get(s, 0)}); cur += timedelta(days=1)
    return jsonify({'type': stat_type, 'from': date_from, 'to': date_to, 'data': result})


@app.route('/api/stats/tags', methods=['GET'])
def get_tag_stats():
    filter_raw = request.args.get('tags', '')
    tag_filter = [t.strip().lower() for t in filter_raw.split(',') if t.strip()]
    uid = current_user_id()
    if uid:
        notes_cond, notes_params = 'n.user_id = ?', [uid]
    else:
        notes_cond, notes_params = 'n.user_id IS NULL', []

    conn = get_db()
    if tag_filter:
        ph   = ','.join('?' * len(tag_filter))
        rows = conn.execute(
            f'SELECT t.name, COUNT(DISTINCT nt.note_id) as note_count FROM tags t '
            f'INNER JOIN note_tags nt ON t.id = nt.tag_id '
            f'INNER JOIN notes n ON nt.note_id = n.id '
            f'WHERE t.name IN ({ph}) AND {notes_cond} '
            f'GROUP BY t.id ORDER BY note_count DESC, t.name',
            tag_filter + notes_params
        ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT t.name, COUNT(DISTINCT nt.note_id) as note_count FROM tags t '
            f'INNER JOIN note_tags nt ON t.id = nt.tag_id '
            f'INNER JOIN notes n ON nt.note_id = n.id '
            f'WHERE {notes_cond} '
            f'GROUP BY t.id ORDER BY note_count DESC, t.name',
            notes_params
        ).fetchall()
    conn.close()
    return jsonify([{'name': r['name'], 'count': r['note_count']} for r in rows])


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route('/api/admin/users', methods=['GET'])
def admin_get_users():
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id, u.username, u.full_name, u.email, u.role, u.created_at,
               COUNT(n.id) AS note_count
        FROM users u
        LEFT JOIN notes n ON n.user_id = u.id
        GROUP BY u.id
        ORDER BY u.created_at ASC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Backup routes ─────────────────────────────────────────────────────────────

@app.route('/api/backup/status', methods=['GET'])
def backup_status():
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    bd    = BACKUP_DIR
    files = []
    if bd and os.path.isdir(bd):
        raw = sorted(glob.glob(os.path.join(bd, 'notes-backup-*.db')), reverse=True)
        for fp in raw[:30]:
            name     = os.path.basename(fp)
            sz       = os.path.getsize(fp)
            meta     = {}
            meta_fp  = fp.replace('.db', '.meta.json')
            if os.path.exists(meta_fp):
                try:
                    with open(meta_fp) as mf:
                        meta = json.load(mf)
                except Exception:
                    pass
            files.append({
                'name':       name,
                'size_kb':    round(sz / 1024, 1),
                'user_count': meta.get('user_count'),
                'note_count': meta.get('note_count'),
                'created_at': meta.get('created_at'),
                'created_by': meta.get('created_by', '—'),
                'version':    meta.get('version'),
            })
    return jsonify({
        'enabled':      bool(bd),
        'backup_dir':   bd or None,
        'last_backup':  _get_last_backup(),
        'backup_count': len(files),
        'backups':      files,
        'config':       load_backup_config(),
    })


@app.route('/api/backup/config', methods=['GET'])
def backup_get_config():
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    return jsonify(load_backup_config())


@app.route('/api/backup/config', methods=['PUT'])
def backup_put_config():
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    data = request.get_json() or {}
    cfg  = load_backup_config()
    for key in ('frequency', 'time', 'minute', 'weekday', 'day_of_month'):
        if key in data:
            cfg[key] = data[key]
    save_backup_config(cfg)
    return jsonify(cfg)


@app.route('/api/backup/trigger', methods=['POST'])
def backup_trigger():
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    # Resolve full name of the currently logged-in admin user
    uid = current_user_id()
    created_by = 'manuální'
    if uid:
        conn = get_db()
        row = conn.execute('SELECT full_name FROM users WHERE id = ?', (uid,)).fetchone()
        conn.close()
        if row and row['full_name']:
            created_by = row['full_name']
    ok, msg = do_db_backup(created_by=created_by)
    if ok:
        return jsonify({'ok': True, 'file': os.path.basename(msg)})
    return jsonify({'ok': False, 'error': msg}), 500


@app.route('/api/backup/restore/<filename>', methods=['POST'])
def backup_restore(filename):
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    ok, msg = restore_from_backup(filename)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 500


@app.route('/api/backup/download-db', methods=['GET'])
def backup_download_db():
    """Download a point-in-time copy of the SQLite database."""
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    ts   = datetime.now().strftime('%Y-%m-%d_%H-%M')
    tmp  = f'/tmp/notes-export-{ts}.db'
    src  = sqlite3.connect(DB_PATH)
    dst  = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close()
    src.close()
    return send_file(
        tmp,
        as_attachment=True,
        download_name=f'notes-backup-{ts}.db',
        mimetype='application/octet-stream'
    )


@app.route('/api/backup/export-json', methods=['GET'])
def backup_export_json():
    """Export full data as JSON (notes, tags, users)."""
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    conn  = get_db()
    users = [dict(r) for r in conn.execute(
        'SELECT id, username, full_name, email, role, created_at FROM users'
    ).fetchall()]
    notes_raw = conn.execute(
        'SELECT id, user_id, text, created_at, updated_at, '
        'is_pinned, is_archived, is_public FROM notes'
    ).fetchall()
    note_ids  = [r['id'] for r in notes_raw]
    tags_map  = get_tags_for_notes(conn, note_ids) if note_ids else {}
    notes     = []
    for r in notes_raw:
        d = dict(r)
        d['tags'] = tags_map.get(r['id'], [])
        notes.append(d)
    conn.close()
    ts     = datetime.now().strftime('%Y-%m-%d_%H-%M')
    export = {
        'version':     __version__,
        'exported_at': datetime.now().isoformat(),
        'users':       users,
        'notes':       notes,
    }
    return Response(
        json.dumps(export, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="notes-export-{ts}.json"'}
    )


@app.route('/api/backup/import-json', methods=['POST'])
def backup_import_json():
    """Import notes (and missing users) from a JSON export. Non-destructive — only adds."""
    if not is_admin():
        return jsonify({'error': 'Přístup odepřen'}), 403
    data       = request.get_json() or {}
    users_data = data.get('users', [])
    notes_data = data.get('notes', [])
    conn = get_db()
    imported_users = 0
    imported_notes = 0
    try:
        # Ensure users exist (import missing ones, skip existing usernames)
        for u in users_data:
            if not u.get('username') or not u.get('password_hash'):
                continue
            try:
                conn.execute(
                    'INSERT INTO users (username, full_name, email, password_hash, role) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (u['username'], u.get('full_name', ''), u.get('email', ''),
                     u['password_hash'], u.get('role', 'user'))
                )
                imported_users += 1
            except sqlite3.IntegrityError:
                pass
        # Map old user IDs → current IDs via username
        old_id_to_username = {u['id']: u['username'] for u in users_data if 'id' in u}
        for n in notes_data:
            text = n.get('text', '')
            if not text:
                continue
            uid = None
            old_uid = n.get('user_id')
            if old_uid and old_uid in old_id_to_username:
                row = conn.execute(
                    'SELECT id FROM users WHERE username = ?',
                    (old_id_to_username[old_uid],)
                ).fetchone()
                if row:
                    uid = row['id']
            conn.execute(
                'INSERT INTO notes (user_id, text, created_at, is_pinned, is_archived, is_public) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (uid, text,
                 n.get('created_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 n.get('is_pinned', 0), n.get('is_archived', 0), n.get('is_public', 0))
            )
            note_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            if n.get('tags'):
                save_tags_for_note(conn, note_id, n['tags'])
            imported_notes += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(e)}), 500
    conn.close()
    return jsonify({'ok': True, 'imported_notes': imported_notes, 'imported_users': imported_users})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
