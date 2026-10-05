"""Database helpers: connection, table creation, audit log, statistics, state."""
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Configuration comes from environment variables (no secrets in source code)
DB_PATH = os.environ.get("RECON_DB_PATH", os.path.join(BASE_DIR, "database", "reconciliation.db"))
DELAY_THRESHOLD_SECONDS = int(os.environ.get("DELAY_THRESHOLD_SECONDS", "300"))  # 5 minutes
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

STATUSES = ["OFFLINE", "PENDING", "SYNCING", "SYNCED", "RECONCILED",
            "DUPLICATE", "CONFLICT", "FAILED", "DELAYED"]


def now_str():
    return datetime.now().strftime(TS_FORMAT)


def get_db_connection():
    """Open a SQLite connection (rows behave like dicts)."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables automatically if they do not exist."""
    conn = get_db_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT NOT NULL UNIQUE,
            device_id TEXT NOT NULL,
            amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            created_at TEXT NOT NULL,
            received_at TEXT,
            status TEXT NOT NULL,
            sync_status TEXT NOT NULL DEFAULT 'NOT_SYNCED',
            transaction_type TEXT NOT NULL DEFAULT 'NORMAL',
            retry_count INTEGER NOT NULL DEFAULT 0,
            original_transaction_id TEXT,
            failure_reason TEXT,
            conflict_reason TEXT,
            server_amount REAL           -- amount held by the online server (for conflicts)
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT NOT NULL,
            action TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            message TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO system_state (key, value) VALUES ('connectivity', 'ONLINE');
    """)
    conn.commit()
    conn.close()


def get_connectivity(conn):
    row = conn.execute("SELECT value FROM system_state WHERE key = 'connectivity'").fetchone()
    return row["value"] if row else "ONLINE"


def set_connectivity(conn, value):
    conn.execute("UPDATE system_state SET value = ? WHERE key = 'connectivity'", (value,))


def log_audit(conn, transaction_id, action, old_status, new_status, message):
    """Write one audit event (caller commits)."""
    conn.execute(
        "INSERT INTO audit_logs (transaction_id, action, old_status, new_status, message, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (transaction_id, action, old_status, new_status, message, now_str()))


def get_statistics(conn):
    """All numbers come straight from SQLite."""
    counts = {r["status"]: r["n"] for r in
              conn.execute("SELECT status, COUNT(*) AS n FROM transactions GROUP BY status")}
    return {
        "total": sum(counts.values()),
        "offline": counts.get("OFFLINE", 0),
        "pending": counts.get("PENDING", 0) + counts.get("SYNCING", 0) + counts.get("SYNCED", 0),
        "reconciled": counts.get("RECONCILED", 0),
        "duplicates": counts.get("DUPLICATE", 0),
        "conflicts": counts.get("CONFLICT", 0),
        "failed": counts.get("FAILED", 0),
        "delayed": counts.get("DELAYED", 0),
    }


def reset_all(conn):
    """Wipe everything and go back to ONLINE."""
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM audit_logs")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('transactions', 'audit_logs')")
    set_connectivity(conn, "ONLINE")
