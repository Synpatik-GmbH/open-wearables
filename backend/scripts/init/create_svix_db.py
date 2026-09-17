#!/usr/bin/env python3
"""Ensure the 'svix' database exists, creating it if necessary.

Runs before migrations so that svix-server can connect on first deploy.
Uses autocommit because CREATE DATABASE cannot run inside a transaction.

The existence check comes first, on purpose: a deployment whose DB_USER is a
least-privilege role (no CREATEDB) must boot when the database was provisioned
for it out of band. PostgreSQL checks the CREATEDB privilege before it checks
for a duplicate name, so an unconditional CREATE DATABASE fails such a role
with InsufficientPrivilege even though the database exists. Only a role that
can create databases ever reaches the CREATE, and only when the name is absent.
"""

import psycopg
import psycopg.errors

from app.config import settings


def create_svix_db() -> None:
    dsn = (
        f"host={settings.db_host} "
        f"port={settings.db_port} "
        f"dbname={settings.db_name} "
        f"user={settings.db_user} "
        f"password={settings.db_password.get_secret_value()}"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = 'svix'").fetchone()
        if row is not None:
            print("Svix database already exists, skipping.")
            return
        try:
            conn.execute("CREATE DATABASE svix")
            print("✓ Created 'svix' database.")
        except psycopg.errors.DuplicateDatabase:
            print("Svix database already exists, skipping.")


if __name__ == "__main__":
    create_svix_db()
