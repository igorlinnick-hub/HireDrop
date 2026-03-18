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
            date_found TEXT NOT NULL,
            platform TEXT DEFAULT 'remoteok',
            description TEXT DEFAULT '',
            location TEXT DEFAULT '',
            job_type TEXT DEFAULT ''
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            date_applied TEXT NOT NULL,
            status TEXT DEFAULT 'applied',
            cover_letter TEXT DEFAULT '',
            FOREIGN KEY (job_id) REFERENCES jobs (id)
        )
    """)
    # Migrate existing tables if needed
    for col, default in [("platform", "'remoteok'"), ("description", "''"), ("location", "''"), ("job_type", "''")]:
        try:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    try:
        cursor.execute("ALTER TABLE applications ADD COLUMN cover_letter TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def save_job(title, company, link, status="new", platform="remoteok", description="", location="", job_type=""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO jobs (title, company, link, status, date_found, platform, description, location, job_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, company, link, status, datetime.now().isoformat(), platform, description, location, job_type),
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


def update_job_status(job_id, status):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()


def save_application(job_id, cover_letter=""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO applications (job_id, date_applied, cover_letter) VALUES (?, ?, ?)",
        (job_id, datetime.now().isoformat(), cover_letter),
    )
    conn.commit()
    conn.close()


def get_today_applications_by_platform():
    """Return {platform: count} of today's applications."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT j.platform, COUNT(*) as cnt
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        WHERE a.date_applied LIKE ?
        GROUP BY j.platform
    """, (today + "%",))
    result = {row["platform"]: row["cnt"] for row in cursor.fetchall()}
    conn.close()
    return result


def get_today_applications_detail(platform=None):
    """Return list of today's applications with job details."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor()
    if platform:
        cursor.execute("""
            SELECT j.id, j.title, j.company, j.link, j.platform, a.date_applied
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.date_applied LIKE ? AND j.platform = ?
            ORDER BY a.date_applied DESC
        """, (today + "%", platform))
    else:
        cursor.execute("""
            SELECT j.id, j.title, j.company, j.link, j.platform, a.date_applied
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.date_applied LIKE ?
            ORDER BY a.date_applied DESC
        """, (today + "%",))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows
