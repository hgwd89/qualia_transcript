import sqlite3

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """Enable SQLite FK enforcement for every SQLAlchemy connection.

    SQLite parses FOREIGN KEY clauses by default but does not enforce them unless
    PRAGMA foreign_keys=ON is set per connection. Keep this at the engine-event
    layer so Flask requests, workers, migrations, tests, and CLI paths all share
    the same integrity contract.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


db = SQLAlchemy()
