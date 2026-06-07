import sqlite3
from passlib.hash import bcrypt

def test():
    conn = sqlite3.connect('books.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    username = "admin"
    password = "password"
    password_hash = bcrypt.hash(password)
    
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, password_hash, True)
        )
        conn.commit()
        
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        admin = cursor.fetchone()
        print("Admin ID:", admin["id"])
        
        cursor.execute("UPDATE books SET user_id = ? WHERE user_id IS NULL", (admin["id"],))
        conn.commit()
        print("Updated books:", cursor.rowcount)
    except Exception as e:
        print("ERROR:", e)

test()
