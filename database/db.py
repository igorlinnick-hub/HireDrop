import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "jobflow.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            link TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            date_found TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            date_applied TEXT NOT NULL,
            status TEXT DEFAULT 'applied',
            FOREIGN KEY (job_id) REFERENCES jobs (id)
        )
    """)
    conn.commit()
    conn.close()


def save_job(title, company, link, status="new"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO jobs (title, company, link, status, date_found) VALUES (?, ?, ?, ?, ?)",
        (title, company, link, status, datetime.now().isoformat()),
    )
    conn.commit()
    job_id = cursor.lastrowid
    conn.close()
    return job_id


def get_all_jobs():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs ORDER BY date_found DESC")
    jobs = cursor.fetchall()
    conn.close()
    return jobs


def job_exists(link):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM jobs WHERE link = ?", (link,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists
