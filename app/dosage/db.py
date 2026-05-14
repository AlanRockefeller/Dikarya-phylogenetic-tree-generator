import sqlite3
from pathlib import Path

from flask import current_app


def get_database_path():
    configured = current_app.config.get("DOSAGE_DB_PATH")
    if configured:
        return Path(configured)
    return Path(current_app.instance_path) / "dosage_calculator.sqlite"


def get_csv_directory():
    configured = current_app.config.get("DOSAGE_CSV_DIR")
    if configured:
        return Path(configured)
    return Path(current_app.config["BASE_DIR"]) / "dosage-calculator"


def connect(path=None):
    db_path = Path(path) if path else get_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection

