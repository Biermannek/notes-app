#!/usr/bin/env python3
"""
Migrace anonymních poznámek na konkrétního uživatele.

Spusť uvnitř kontejneru:
  docker exec -it <container_name> python /app/migrate_anon_notes.py

Nebo přes Portainer: Container → Console → python /app/migrate_anon_notes.py
"""
import sqlite3
import os

DB_PATH        = os.environ.get('DB_PATH', '/data/notes.db')
TARGET_USERNAME = 'teplanm'


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Chyba: databáze nenalezena na cestě '{DB_PATH}'.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Najdi cílového uživatele
    user_row = conn.execute(
        'SELECT id, full_name FROM users WHERE username = ?', (TARGET_USERNAME,)
    ).fetchone()

    if not user_row:
        print(f"Chyba: uživatel '{TARGET_USERNAME}' neexistuje v databázi.")
        print("\nDostupní uživatelé:")
        for u in conn.execute('SELECT username, full_name FROM users ORDER BY username').fetchall():
            print(f"  - {u['username']} ({u['full_name']})")
        conn.close()
        return

    user_id = user_row['id']

    # Zjisti počet anonymních poznámek
    count = conn.execute('SELECT COUNT(*) FROM notes WHERE user_id IS NULL').fetchone()[0]
    print(f"Nalezeno {count} anonymních poznámek (bez přiřazeného uživatele).")

    if count == 0:
        print("Nic k migraci, vše je již přiřazeno.")
        conn.close()
        return

    print(f"Přiřazuji {count} poznámek uživateli '{TARGET_USERNAME}' (id={user_id}, jméno: {user_row['full_name']})...")
    conn.execute('UPDATE notes SET user_id = ? WHERE user_id IS NULL', (user_id,))
    conn.commit()

    # Ověření
    remaining = conn.execute('SELECT COUNT(*) FROM notes WHERE user_id IS NULL').fetchone()[0]
    conn.close()

    if remaining == 0:
        print(f"✅ Hotovo! {count} poznámek přiřazeno uživateli '{TARGET_USERNAME}'.")
        print("   Anonymní uživatelé nyní neuvidí žádné poznámky (kromě veřejných).")
    else:
        print(f"⚠️  Migrace proběhla částečně: zbývá {remaining} nepřiřazených poznámek.")


if __name__ == '__main__':
    migrate()
