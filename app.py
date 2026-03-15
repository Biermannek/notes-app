from flask import Flask, request, jsonify, render_template
import sqlite3
import os

app = Flask(__name__)

DB_PATH = os.environ.get('DB_PATH', '/data/notes.db')


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at DATETIME DEFAULT (datetime('now', 'localtime')),
            updated_at DATETIME
        )
    ''')
    try:
        conn.execute('ALTER TABLE notes ADD COLUMN updated_at DATETIME')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/notes', methods=['GET'])
def get_notes():
    sort_by = request.args.get('sort_by', 'created_at')
    order = request.args.get('order', 'desc')
    if sort_by not in ('created_at', 'updated_at'):
        sort_by = 'created_at'
    if order not in ('asc', 'desc'):
        order = 'desc'
    if sort_by == 'updated_at':
        order_clause = f'COALESCE(updated_at, created_at) {order}'
    else:
        order_clause = f'created_at {order}'
    conn = get_db()
    rows = conn.execute(
        f'SELECT id, text, created_at, updated_at FROM notes ORDER BY {order_clause}'
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route('/api/notes', methods=['POST'])
def add_note():
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO notes (text) VALUES (?)', (data['text'].strip(),)
    )
    conn.commit()
    row = conn.execute(
        'SELECT id, text, created_at, updated_at FROM notes WHERE id = ?',
        (cursor.lastrowid,)
    ).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.route('/api/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    result = conn.execute(
        '''UPDATE notes SET text = ?, updated_at = datetime('now', 'localtime') WHERE id = ?''',
        (data['text'].strip(), note_id)
    )
    conn.commit()
    if result.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    row = conn.execute(
        'SELECT id, text, created_at, updated_at FROM notes WHERE id = ?',
        (note_id,)
    ).fetchone()
    conn.close()
    return jsonify(dict(row))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
