"""FORK (Calibra 2.71.3.3, design D-15): the daily check of Garmin's refresh tokens."""

from celery import shared_task

from app.database import SessionLocal
from app.services.providers.garmin.token_keepalive import keep_tokens_alive


@shared_task
def keep_garmin_tokens_alive() -> dict[str, int]:
    """Refresh every Garmin connection whose refresh token has 30 days or less left."""
    with SessionLocal() as db:
        return keep_tokens_alive(db)
