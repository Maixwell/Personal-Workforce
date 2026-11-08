"""Pure database configuration shared by production and test settings."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DATABASE_PATH = PROJECT_ROOT / "data" / "max_smart.sqlite3"
SQLITE_BUSY_TIMEOUT_SECONDS = 5
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1_000
QUEUED_RESERVATION_US = 60_000_000
IDEMPOTENCY_EXPIRATION_US = 2_592_000_000_000
EXECUTION_DEADLINE_US = 600_000_000
FIXED_LEASE_US = 720_000_000
RECOVERY_CLOCK_SKEW_MARGIN_US = FIXED_LEASE_US - EXECUTION_DEADLINE_US


def database_configuration(database_path):
    """Return the common SQLite configuration without opening a connection."""
    return {
        "ENGINE": "max_smart_db.backend",
        "NAME": str(Path(database_path)),
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            "transaction_mode": "IMMEDIATE",
            "init_command": (
                "PRAGMA synchronous=FULL;"
                f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"
            ),
        },
    }
