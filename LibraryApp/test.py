import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), 'Web-Extension', 'portal.db')
if os.path.exists(db_path):
    c = sqlite3.connect(db_path)
    print(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
else:
    print("Test passed: db doesn't exist.")
