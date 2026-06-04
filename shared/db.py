"""Lightweight DB connection helper for the worker container.

Reuses the same config hierarchy as scheduling/db.py:
  env vars (SCHEDULING_DB_*) → db_config.json → hardcoded defaults.

No connection pool — used only for monthly CSV exports.
"""

import json
import logging
import os
from contextlib import contextmanager

import psycopg2

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "andromeda",
    "user": "postgres",
    "password": "postgres",
}

DB_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "db_config.json")


def load_config() -> dict:
    """Load DB config: env vars > db_config.json > defaults."""
    config = dict(_DEFAULT_CONFIG)

    config_path = os.environ.get("SCHEDULING_DB_CONFIG", DB_CONFIG_PATH)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            for k in ("host", "port", "dbname", "user", "password"):
                if k in file_cfg:
                    config[k] = file_cfg[k]
            logger.info("Loaded DB config from %s", config_path)
        except Exception as e:
            logger.warning("Failed to load DB config from %s: %s", config_path, e)

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


@contextmanager
def get_connection(statement_timeout_ms: int = 120_000):
    """Yield a psycopg2 connection with statement_timeout.

    Default timeout is 120 s (CSV exports can be large queries).
    The connection is closed automatically on exit.
    """
    cfg = load_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        yield conn
    finally:
        conn.close()
