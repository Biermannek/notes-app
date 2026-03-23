from flask import Flask, request, jsonify, render_template, session
from datetime import datetime, timedelta
from version import __version__
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import re

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'notes-app-secret-key-change-in-production')
DB_PATH = os.environ.get('DB_PATH', '/data/notes.db')


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
    # Migrace starších DB
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
    conn.commit()
    conn.close()


init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user_id():
    return session.get('user_id')


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
        return ' AND (n.user_id IS NULL OR n.is_public = 1)', []


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
        'SELECT id, username, full_name, email FROM users WHERE id = ?', (uid,)
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
    q   = request.args.get('q', '').strip().lower()
    uid = current_user_id()
    if uid:
        notes_cond  = ('(n.user_id = ? OR n.is_public = 1 OR '
                       'EXISTS (SELECT 1 FROM note_shares ns WHERE ns.note_id = n.id AND ns.user_id = ?))')
        notes_params = [uid, uid]
    else:
        notes_cond, notes_params = '(n.user_id IS NULL OR n.is_public = 1)', []

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
