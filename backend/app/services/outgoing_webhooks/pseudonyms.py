"""Pseudonyms for the identifiers Open Wearables writes into Svix.

svix-server keeps ``message.uid`` and ``message.channels`` on the message row, and on the
deployed version (v1.69.0) nothing expires or prunes that row: payload retention reaches only
``messagecontent``.  Both used to carry readable identifiers — the event id a user id, provider,
metric and ingestion window; the channel ``user.<user-id>`` on every message — so both are now
written as pseudonyms.

- **Event id**: an HMAC-SHA256 digest.  Deterministic, so the same logical event still collides
  on Svix's ``(app_id, uid)`` unique index and is rejected with a 409.  Never read back.
- **User channel**: ``user.`` + AES-SIV of the user id's 16 bytes, hex encoded.  Deterministic, so
  every message for a user carries the same channel and endpoint filtering still works; and
  reversible with the key, so an endpoint's user filter can still be reported by the API.

Both keys are derived, with distinct contexts, from ``settings.svix_pseudonym_secret``, which is
itself derived from — but never equal to — ``secret_key`` when unset.  Svix holds neither key, so
it can neither read a channel nor recompute an event id.

Rotating the secret changes every pseudonym: events already in Svix stop deduplicating against new
ones, and an endpoint filtered on a user stops receiving until its ``user_id`` is saved again.
"""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from pydantic import SecretStr

from app.config import settings

USER_CHANNEL_PREFIX = "user."

_EVENT_ID_CONTEXT = b"open-wearables/svix/event-id/v1"
_USER_CHANNEL_CONTEXT = b"open-wearables/svix/user-channel/v1"
# AES-SIV output for a 16-byte UUID: 16-byte synthetic IV + 16-byte ciphertext.
_USER_CHANNEL_TOKEN_HEX_LEN = 64


def _secret(secret: SecretStr | None) -> bytes:
    chosen = secret if secret is not None else settings.svix_pseudonym_secret
    assert chosen is not None  # derived from secret_key at startup
    return chosen.get_secret_value().encode()


def _event_id_key(secret: SecretStr | None) -> bytes:
    return hmac.new(_secret(secret), _EVENT_ID_CONTEXT, hashlib.sha256).digest()


def _user_channel_cipher(secret: SecretStr | None) -> AESSIV:
    # AES-SIV-512 takes a 64-byte key.
    return AESSIV(hmac.new(_secret(secret), _USER_CHANNEL_CONTEXT, hashlib.sha512).digest())


def hash_event_id(raw: str | None, *, secret: SecretStr | None = None) -> str | None:
    """Return a keyed digest of the event id, or None when the caller supplied none.

    Hex output satisfies the Svix eventId constraints (``^[a-zA-Z0-9\\-_.]+$``, max 256).
    """
    if raw is None:
        return None
    return hmac.new(_event_id_key(secret), raw.encode(), hashlib.sha256).hexdigest()


def user_channel(user_id: UUID, *, secret: SecretStr | None = None) -> str:
    """The Svix channel for a user: deterministic, opaque to Svix, reversible with the key.

    69 characters of ``[a-z0-9.]``, inside the Svix channel constraints
    (``^[a-zA-Z0-9\\-_.]+$``, max 128).
    """
    token = _user_channel_cipher(secret).encrypt(user_id.bytes, [_USER_CHANNEL_CONTEXT])
    return f"{USER_CHANNEL_PREFIX}{token.hex()}"


def user_channels(user_id: UUID | None) -> list[str] | None:
    """The channel list for a message or endpoint scoped to ``user_id``; None when unscoped."""
    if user_id is None:
        return None
    return [user_channel(user_id)]


def user_id_from_channel(channel: str, *, secret: SecretStr | None = None) -> UUID | None:
    """The user a channel names, or None when it names none under the current key.

    Accepts the pseudonymous form, and the legacy readable ``user.<uuid>`` form so an endpoint
    created before pseudonymisation still reports its filter.  A token under another key, or
    anything malformed, is None rather than a guess.
    """
    if not channel.startswith(USER_CHANNEL_PREFIX):
        return None
    token = channel[len(USER_CHANNEL_PREFIX) :]
    if len(token) == _USER_CHANNEL_TOKEN_HEX_LEN:
        try:
            plain = _user_channel_cipher(secret).decrypt(bytes.fromhex(token), [_USER_CHANNEL_CONTEXT])
        except (ValueError, InvalidTag):
            return None
        return UUID(bytes=plain)
    try:
        return UUID(token)
    except ValueError:
        return None


def pseudonymous_channels(channels: list[str] | None) -> list[str] | None:
    """Channels as Svix must receive them: every user channel in its pseudonymous form.

    Applied at the Svix boundary, not only by producers, because a Celery job enqueued by an
    earlier release still carries the readable ``user.<uuid>``.  Idempotent: an already
    pseudonymous channel decrypts to its user and re-encrypts to the same token; a channel naming
    no user passes through unchanged.
    """
    if channels is None:
        return None
    out: list[str] = []
    for channel in channels:
        user_id = user_id_from_channel(channel)
        out.append(user_channel(user_id) if user_id is not None else channel)
    return out


def readable_channels(channels: list[str] | None) -> list[str] | None:
    """Channels as the developer API reports them: user channels decoded to ``user.<uuid>``."""
    if channels is None:
        return None
    readable: list[str] = []
    for channel in channels:
        user_id = user_id_from_channel(channel)
        readable.append(f"{USER_CHANNEL_PREFIX}{user_id}" if user_id is not None else channel)
    return readable
