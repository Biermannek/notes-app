from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
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
    conn.execute('''CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at DATETIME DEFAULT (datetime('now', 'localtime')),
        updated_at DATETIME)''')
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
    if sort_by not in ('created_at', 'updated_at'): sort_by = 'created_at'
    if order not in ('asc', 'desc'): order = 'desc'
    order_clause = f'COALESCE(updated_at, created_at) {order}' if sort_by == 'updated_at' else f'created_at {order}'
    conn = get_db()
    rows = conn.execute(f'SELECT id, text, created_at, updated_at FROM notes ORDER BY {order_clause}').fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/notes', methods=['POST'])
def add_note():
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    cursor = conn.execute('INSERT INTO notes (text) VALUES (?)', (data['text'].strip(),))
    conn.commit()
    row = conn.execute('SELECT id, text, created_at, updated_at FROM notes WHERE id = ?', (cursor.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201

@app.route('/api/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({'error': 'Text nesmí být prázdný'}), 400
    conn = get_db()
    result = conn.execute("UPDATE notes SET text = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (data['text'].strip(), note_id))
    conn.commit()
    if result.rowcount == 0:
        conn.close()
        return jsonify({'error': 'Poznámka nenalezena'}), 404
    row = conn.execute('SELECT id, text, created_at, updated_at FROM notes WHERE id = ?', (note_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route('/api/stats/daily', methods=['GET'])
def get_daily_stats():
    stat_type = request.args.get('type', 'created')
    days = request.args.get('days', None)
    date_from = request.args.get('from', None)
    date_to = request.args.get('to', None)
    today = datetime.now().date()
    if days:
        try: n = int(days)
        except ValueError: n = 7
        date_from = (today - timedelta(days=n - 1)).isoformat()
        date_to = today.isoformat()
    elif not date_from or not date_to:
        date_from = (today - timedelta(days=6)).isoformat()
        date_to = today.isoformat()
    col = 'created_at' if stat_type == 'created' else 'updated_at'
    conn = get_db()
    if stat_type == 'edited':
        rows = conn.execute(f'SELECT date({col}) as day, COUNT(*) as count FROM notes WHERE {col} IS NOT NULL AND date({col}) BETWEEN ? AND ? GROUP BY day ORDER BY day', (date_from, date_to)).fetchall()
    else:
        rows = conn.execute(f'SELECT date({col}) as day, COUNT(*) as count FROM notes WHERE date({col}) BETWEEN ? AND ? GROUP BY day ORDER BY day', (date_from, date_to)).fetchall()
    conn.close()
    data_map = {row['day']: row['count'] for row in rows}
    result = []
    current = datetime.fromisoformat(date_from).date()
    end = datetime.fromisoformat(date_to).date()
    while current <= end:
        day_str = current.isoformat()
        result.append({'day': day_str, 'count': data_map.get(day_str, 0)})
        current += timedelta(days=1)
    return jsonify({'type': stat_type, 'from': date_from, 'to': date_to, 'data': result})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
