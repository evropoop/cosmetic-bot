import database as db

USER_ID = 1962088357  

conn = db.get_connection()
cursor = conn.cursor()
cursor.execute("UPDATE users SET free_checks = 9999 WHERE user_id = ?", (USER_ID,))
conn.commit()
conn.close()

print("✅ омагад ты шо крейзи?")