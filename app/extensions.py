from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, 'connect')
def enable_sqlite_foreign_keys(connection, connection_record):
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.close()


db = SQLAlchemy()
csrf = CSRFProtect()
