from __future__ import annotations

from sqlalchemy import event


def install_sqlite_pragmas(engine) -> None:
    """Apply SQLite settings to every DB-API connection."""
    if getattr(engine, "_aetherswap_sqlite_pragmas_installed", False):
        return
    setattr(engine, "_aetherswap_sqlite_pragmas_installed", True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
        finally:
            cursor.close()
