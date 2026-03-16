from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
from version import __version__
import sqlite3
import os

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
        updated_at DATETIME)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS note_tags (
        note_id INTEGER NOT NULL,
        tag_id  INTEGER NOT NULL,
        PRIMARY KEY (note_id, tag_id),
        FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE)''')
    try:
        conn.execute('ALTER TABLE notes ADD COLUMN updated_at DATETIME')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


# ── helpers ──────────────────────────────────────────────────────────────────

def save_tags_for_note(conn, note_id, tag_names):
    """Uloží tagy pro poznámku (přepíše stávající)."""
    conn.execute('DELETE FROM note_tags WHERE note_id = ?', (note_id,))
    for raw in tag_names:
        name = raw.strip().lower()
        if not name:
            continue
        conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (name,))
        tag_id = conn.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()['id']
        conn.execute('INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)', (note_id, tag_id))
    # Smaž tagy, které už nikde nejsou použity
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM note_tags)')


def get_tags_for_notes(conn, note_ids):
    """Vrátí slovník note_id → [tag_name, ...]"""
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


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', version=__version__)


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
    sort_by  = request.args.get('sort_by', 'created_at')
    order    = request.args.get('order', 'desc')
    tags_raw = request.args.get('tags', '')
    tag_mode = request.args.get('tag_mode', 'or')

    if sort_by not in ('created_at', 'updated_at'): sort_by = 'created_at'
    if order    not in ('asc', 'desc'):              order   = 'desc'
    if tag_mode not in ('or', 'and'):                tag_mode = 'or'

    order_clause = (f'COALESCE(n.updated_at, n.created_at) {order}'
                    if sort_by == 'updated_at' else f'n.created_at {order}')

    tag_list = [t.strip().lower() for t in tags_raw.split(',') if t.strip()]

    conn = get_db()
    if tag_list:
        ph = ','.join('?' * len(tag_list))
        if tag_mode == 'and':
            query = (
                f'SELECT DISTINCT n.id, n.text, n.created_at, n.updated_at FROM notes n '
                f'JOIN note_tags nt ON n.id = nt.note_id '
                f'JOIN tags t ON nt.tag_id = t.id '
                f'WHERE t.name IN ({ph}) '
                f'GROUP BY n.id HAVING COUNT(DISTINCT t.name) = {len(tag_list)} '
                f'ORDER BY {order_clause}'
            )
        else:
            query = (
                f'SELECT DISTINCT n.id, n.text, n.created_at, n.updated_at FROM notes n '
                f'JOIN note_tags nt ON n.id = nt.note_id '
                f'JOIN tags t ON nt.tag_id = t.id '
                f'WHERE t.name IN ({ph}) ORDER BY {order_clause}'
            )
        rows = conn.execute(query, tag_list).fetchall()
    else:
        rows = conn.execute(
            f'SELECT id, n.id, n.text, n.created_at, n.updated_at FROM notes n ORDER BY {order_clause}'
            .replace('SELECT id, n.id', 'SELECT n.id')
        ).fetchall()

    note_ids  = [r['id'] for r in rows]
    tags_map  = get_tags_for_notes(conn, note_ids)
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
    row  = conn.execute('SELECT id, text, created_at, updated_at FROM notes WHERE id = ?', (note_id,)).fetchone()
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
    row  = conn.execute('SELECT id, text, created_at, updated_at FROM notes WHERE id = ?', (note_id,)).fetchone()
    tags = get_tags_for_notes(conn, [note_id]).get(note_id, [])
    conn.close()
    result = dict(row); result['tags'] = tags
    return jsonify(result)


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
