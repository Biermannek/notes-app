from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
from version import __version__
import sqlite3
import os
import re

app = Flask(__name__)
DB_PATH = os.environ.get('DB_PATH', '/data/notes.db')


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at DATETIME DEFAULT (datetime('now', 'localtime')),
        updated_at DATETIME,
        is_pinned INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS note_tags (
        note_id INTEGER NOT NULL,
        tag_id  INTEGER NOT NULL,
        PRIMARY KEY (note_id, tag_id),
        FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE)''')
    # Migrace starších DB
    for col, definition in [
        ('updated_at', 'DATETIME'),
        ('is_pinned',  'INTEGER NOT NULL DEFAULT 0'),
    ]:
        try:
            conn.execute(f'ALTER TABLE notes ADD COLUMN {col} {definition}')
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()


# ── helpers ──────────────────────────────────────────────────────────────────

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


def note_select():
    return 'SELECT n.id, n.text, n.created_at, n.updated_at, n.is_pinned FROM notes n'


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


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', version=__version__)


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Poznámky",
        "short_name": "Poznámky",
        "description": "Zápisky vždy po ruce",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f0f2f5",
        "theme_color": "#1a1a2e",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    })


@app.route('/service-worker.js')
def service_worker():
    sw = """
const CACHE = 'notes-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() =>
      new Response('<h2 style="font-family:sans-serif;padding:2rem">Jsi offline 📵</h2>',
        {headers:{'Content-Type':'text/html'}})
    ));
  }
});
"""
    from flask import Response
    return Response(sw, mimetype='application/javascript')


@app.route('/api/tags', methods=['GET'])
def get_tags():
    q = request.args.get('q', '').strip().lower()
    conn = get_db()
    if q:
        rows = conn.execute(
            'SELECT t.name, COUNT(nt.note_id) as cnt FROM tags t '
            'LEFT JOIN note_tags nt ON t.id = nt.tag_id '
            'WHERE t.name LIKE ? GROUP BY t.id ORDER BY cnt DESC, t.name',
            (f'%{q}%',)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT t.name, COUNT(nt.note_id) as cnt FROM tags t '
            'LEFT JOIN note_tags nt ON t.id = nt.tag_id '
            'GROUP BY t.id ORDER BY cnt DESC, t.name'
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

    # Připnuté vždy první, pak řazení dle zvoleného kritéria
    secondary = (f'COALESCE(n.updated_at, n.created_at) {order}'
                 if sort_by == 'updated_at' else f'n.created_at {order}')
    order_clause = f'n.is_pinned DESC, {secondary}'

    tag_list     = [t.strip().lower() for t in tags_raw.split(',') if t.strip()]
    search_terms = parse_search(search_raw) if search_raw else []
    sel          = note_select()

    # Podmínka pro full-text hledání (OR mezi všemi výrazy)
    search_cond   = ''
    search_params = []
    if search_terms:
        conds         = ['LOWER(n.text) LIKE ?' for _ in search_terms]
        search_cond   = ' AND (' + ' OR '.join(conds) + ')'
        search_params = [f'%{t}%' for t in search_terms]

    conn = get_db()
    if tag_list:
        ph = ','.join('?' * len(tag_list))
        if tag_mode == 'and':
            query = (f'{sel} JOIN note_tags nt ON n.id = nt.note_id '
                     f'JOIN tags t ON nt.tag_id = t.id '
                     f'WHERE t.name IN ({ph}){search_cond} '
                     f'GROUP BY n.id HAVING COUNT(DISTINCT t.name) = {len(tag_list)} '
                     f'ORDER BY {order_clause}')
            params = tag_list + search_params
        else:
            query = (f'{sel} JOIN note_tags nt ON n.id = nt.note_id '
                     f'JOIN tags t ON nt.tag_id = t.id '
                     f'WHERE t.name IN ({ph}){search_cond} '
                     f'GROUP BY n.id ORDER BY {order_clause}')
            params = tag_list + search_params
        rows = conn.execute(query, params).fetchall()
    else:
        if search_cond:
            rows = conn.execute(
                f'{sel} WHERE 1=1{search_cond} ORDER BY {order_clause}', search_params
            ).fetchall()
        else:
            rows = conn.execute(f'{sel} ORDER BY {order_clause}').fetchall()

    note_ids = [r['id'] for r in rows]
    tags_map = get_tags_for_notes(conn, note_ids)
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d['tags'] = tags_map.get(d['id'], [])
        result.append(d)
    return jsonify(result)


@app.route('/api/notes', methods=['POST'])
def add_note():
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    cursor = conn.execute('INSERT INTO notes (text) VALUES (?)', (data['text'].strip(),))
    note_id = cursor.lastrowid
    save_tags_for_note(conn, note_id, data.get('tags', []))
    conn.commit()
    row  = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    tags = get_tags_for_notes(conn, [note_id]).get(note_id, [])
    conn.close()
    result = dict(row); result['tags'] = tags
    return jsonify(result), 201


@app.route('/api/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    res = conn.execute(
        "UPDATE notes SET text = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (data['text'].strip(), note_id)
    )
    if res.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    save_tags_for_note(conn, note_id, data.get('tags', []))
    conn.commit()
    row  = conn.execute(f'{note_select()} WHERE n.id = ?', (note_id,)).fetchone()
    tags = get_tags_for_notes(conn, [note_id]).get(note_id, [])
    conn.close()
    result = dict(row); result['tags'] = tags
    return jsonify(result)


@app.route('/api/notes/<int:note_id>/pin', methods=['PUT'])
def toggle_pin(note_id):
    conn = get_db()
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
    conn = get_db()
    if stat_type == 'edited':
        rows = conn.execute(
            f'SELECT date({col}) as day, COUNT(*) as count FROM notes '
            f'WHERE {col} IS NOT NULL AND date({col}) BETWEEN ? AND ? GROUP BY day ORDER BY day',
            (date_from, date_to)
        ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT date({col}) as day, COUNT(*) as count FROM notes '
            f'WHERE date({col}) BETWEEN ? AND ? GROUP BY day ORDER BY day',
            (date_from, date_to)
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
    conn = get_db()
    if tag_filter:
        ph   = ','.join('?' * len(tag_filter))
        rows = conn.execute(
            f'SELECT t.name, COUNT(DISTINCT nt.note_id) as note_count FROM tags t '
            f'LEFT JOIN note_tags nt ON t.id = nt.tag_id '
            f'WHERE t.name IN ({ph}) GROUP BY t.id ORDER BY note_count DESC, t.name',
            tag_filter
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT t.name, COUNT(DISTINCT nt.note_id) as note_count FROM tags t '
            'LEFT JOIN note_tags nt ON t.id = nt.tag_id '
            'GROUP BY t.id ORDER BY note_count DESC, t.name'
        ).fetchall()
    conn.close()
    return jsonify([{'name': r['name'], 'count': r['note_count']} for r in rows])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
