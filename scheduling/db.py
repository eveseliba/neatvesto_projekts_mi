"""Database connection pool with retry logic, timeouts, and health checking."""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import psycopg2
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "bkus",
    "user": "postgres",
    "password": "postgres",
    "minconn": 2,
    "maxconn": 10,
    "statement_timeout_ms": 5000,
    "search_months": 4,
    "max_slots_per_query": 5000,
    "tables": {
        "slots": "saule.vizisu_laiki",
        "visits_history": [
            "saule.vizites",
        ],
    },
}

DB_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "db_config.json")


def load_config() -> dict:
    """Load DB config: env vars > db_config.json > defaults."""
    config = dict(_DEFAULT_CONFIG)

    # Try JSON config file
    config_path = os.environ.get("SCHEDULING_DB_CONFIG", DB_CONFIG_PATH)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            # Merge — file config overrides defaults
            for k, v in file_cfg.items():
                config[k] = v
            logger.info("Loaded scheduling DB config from %s", config_path)
        except Exception as e:
            logger.warning("Failed to load DB config from %s: %s", config_path, e)

    # Env vars override everything
    env_map = {
        "SCHEDULING_DB_HOST": ("host", str),
        "SCHEDULING_DB_PORT": ("port", int),
        "SCHEDULING_DB_NAME": ("dbname", str),
        "SCHEDULING_DB_USER": ("user", str),
        "SCHEDULING_DB_PASSWORD": ("password", str),
    }
    for env_key, (cfg_key, cast) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None and val != "":
            config[cfg_key] = cast(val)

    return config


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class PoolExhaustedError(Exception):
    """Raised when no DB connection is available within the timeout."""


# ---------------------------------------------------------------------------
# Connection pool with semaphore-based timeout
# ---------------------------------------------------------------------------

class PoolWithTimeout:
    """ThreadedConnectionPool wrapper with blocking timeout on exhaustion.

    Validates connections before handing them out and transparently replaces
    stale/closed connections so callers never see
    ``InterfaceError: connection already closed``.
    """

    _MAX_REPLACE_ATTEMPTS = 2  # how many times to retry getconn on stale hit

    def __init__(self, minconn: int, maxconn: int, timeout: float = 5.0, **dsn_kwargs):
        self._pool = ThreadedConnectionPool(minconn, maxconn, **dsn_kwargs)
        self._semaphore = threading.Semaphore(maxconn)
        self._timeout = timeout

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _is_alive(conn) -> bool:
        """Return True if *conn* is usable (not closed, server responds)."""
        if conn.closed:
            return False
        try:
            # Lightweight server-side check
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            # Avoid leaving the session in idle-in-transaction state
            conn.rollback()
            return True
        except Exception:
            return False

    # -- public API ---------------------------------------------------------

    @contextmanager
    def connection(self):
        if not self._semaphore.acquire(timeout=self._timeout):
            raise PoolExhaustedError(
                "Database connection pool exhausted, try again shortly"
            )
        conn = None
        try:
            for _attempt in range(self._MAX_REPLACE_ATTEMPTS):
                conn = self._pool.getconn()
                if self._is_alive(conn):
                    break
                # Connection is stale — discard it so the pool creates a fresh one
                logger.warning("Discarding stale DB connection (attempt %d)", _attempt + 1)
                try:
                    self._pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
            else:
                # All attempts returned stale connections
                raise PoolExhaustedError(
                    "Unable to obtain a live database connection after "
                    f"{self._MAX_REPLACE_ATTEMPTS} attempts"
                )
            yield conn
        finally:
            if conn is not None:
                try:
                    # Return broken connections as closed so the pool replaces them
                    if conn.closed:
                        self._pool.putconn(conn, close=True)
                    else:
                        self._pool.putconn(conn)
                except Exception:
                    pass
            self._semaphore.release()

    def closeall(self):
        self._pool.closeall()

    @property
    def pool(self):
        return self._pool


# ---------------------------------------------------------------------------
# Global pool + scheduling availability flag
# ---------------------------------------------------------------------------

_pool: PoolWithTimeout | None = None
_pool_lock = threading.Lock()
_scheduling_available = False
_config: dict = {}


def get_config() -> dict:
    return _config


def is_scheduling_available() -> bool:
    return _scheduling_available


def _create_pool(config: dict) -> PoolWithTimeout:
    """Create a new connection pool from config."""
    dsn_kwargs = {
        "host": config["host"],
        "port": config["port"],
        "dbname": config["dbname"],
        "user": config["user"],
        "password": config["password"],
    }
    return PoolWithTimeout(
        minconn=config.get("minconn", 2),
        maxconn=config.get("maxconn", 10),
        timeout=5.0,
        **dsn_kwargs,
    )


def init_pool() -> bool:
    """Initialize the global connection pool. Returns True on success."""
    global _pool, _scheduling_available, _config

    _config = load_config()

    with _pool_lock:
        try:
            _pool = _create_pool(_config)
            # Test the connection
            with _pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            _scheduling_available = True
            logger.info(
                "Scheduling DB pool initialized (host=%s, port=%s, db=%s)",
                _config["host"], _config["port"], _config["dbname"],
            )
            return True
        except Exception as e:
            logger.warning("Failed to initialize scheduling DB pool: %s", e)
            _pool = None
            _scheduling_available = False
            return False


def close_pool():
    """Close the global connection pool."""
    global _pool, _scheduling_available

    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
            _scheduling_available = False


def get_pool_status() -> dict:
    """Return pool health info for the /health endpoint."""
    with _pool_lock:
        if _pool is None or not _scheduling_available:
            return {"scheduling_db": "disconnected"}
        try:
            with _pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return {
                "scheduling_db": "connected",
                "pool_size": _config.get("maxconn", 10),
            }
        except Exception as e:
            return {"scheduling_db": "disconnected", "error": str(e)}


# ---------------------------------------------------------------------------
# Query execution with retry + statement timeout
# ---------------------------------------------------------------------------

_RETRY_MAX = 3
_RETRY_BASE_DELAY = 0.5


@contextmanager
def get_connection():
    """Get a pooled connection with statement_timeout applied.

    Retries transient OperationalErrors during connection setup (up to 3 times
    with exponential backoff).  Once the connection is yielded, errors propagate
    normally without retry.
    """
    if _pool is None or not _scheduling_available:
        raise PoolExhaustedError("Scheduling database is not available")

    timeout_ms = _config.get("statement_timeout_ms", 5000)

    with _pool.connection() as conn:
        last_error = None
        for attempt in range(_RETRY_MAX):
            try:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = %s", (timeout_ms,))
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_error = e
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "DB connection setup error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, _RETRY_MAX, delay, e,
                )
                time.sleep(delay)
            except (psycopg2.ProgrammingError, psycopg2.DataError):
                raise
        else:
            raise last_error

        yield conn


# ---------------------------------------------------------------------------
# Background health check thread
# ---------------------------------------------------------------------------

def _health_check_loop(interval: float = 30.0):
    """Periodically attempt to reconnect if the pool is down."""
    global _pool, _scheduling_available

    while True:
        time.sleep(interval)
        with _pool_lock:
            if _scheduling_available and _pool is not None:
                # Pool is up — just verify connectivity
                try:
                    with _pool.connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT 1")
                except Exception as e:
                    logger.warning("Scheduling DB health check failed: %s", e)
                    _scheduling_available = False
            elif not _scheduling_available:
                # Pool is down — try to recreate it
                logger.info("Attempting to reconnect scheduling DB...")
                try:
                    if _pool is not None:
                        try:
                            _pool.closeall()
                        except Exception:
                            pass
                    _pool = _create_pool(_config)
                    with _pool.connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT 1")
                    _scheduling_available = True
                    logger.info("Scheduling DB reconnected successfully")
                except Exception as e:
                    logger.debug("Scheduling DB reconnect failed: %s", e)
                    _pool = None


def start_health_check_thread(interval: float = 30.0):
    """Start the background health check daemon thread."""
    t = threading.Thread(
        target=_health_check_loop,
        args=(interval,),
        daemon=True,
        name="scheduling-db-health",
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# Helper: parse schema-qualified table names safely
# ---------------------------------------------------------------------------

def qualify_table(table_name: str) -> sql.Composed:
    """Convert 'schema.table' string to safely quoted SQL identifier.

    E.g., 'saule.vizisu_laiki' → sql.SQL('"saule"."vizisu_laiki"')
    """
    parts = table_name.split(".", 1)
    if len(parts) == 2:
        return sql.SQL("{}.{}").format(
            sql.Identifier(parts[0]),
            sql.Identifier(parts[1]),
        )
    return sql.Identifier(parts[0])
