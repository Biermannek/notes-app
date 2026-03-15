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
            created_at DATETIME DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    conn.commit()
    conn.close()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/notes', methods=['GET'])
def get_notes():
    conn = get_db()
    rows = conn.execute(
        'SELECT id, text, created_at FROM notes ORDER BY created_at DESC'
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
        'SELECT id, text, created_at FROM notes WHERE id = ?', (cursor.lastrowid,)
    ).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
