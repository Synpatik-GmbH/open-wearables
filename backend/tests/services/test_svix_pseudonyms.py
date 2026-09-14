"""Pseudonyms for what Open Wearables writes into Svix.

Svix keeps ``message.uid`` and ``message.channels`` on the message row with no expiry on the
deployed server version, so both are written as pseudonyms: the event id as a keyed digest, the
user channel as a deterministic encryption of the user id. Deterministic so Svix can still
deduplicate and filter; keyed so Svix alone can neither read nor recompute them.
"""

import re
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.services.outgoing_webhooks import pseudonyms

# The Svix v1.69.0 OpenAPI constraints on a channel and on an event id.
SVIX_CHANNEL = re.compile(r"^[a-zA-Z0-9\-_.]+$")
SVIX_EVENT_ID = re.compile(r"^[a-zA-Z0-9\-_.]+$")


def _identifier_forms(user_id: UUID) -> list[str]:
    return [str(user_id), user_id.hex, str(user_id).upper(), user_id.hex.upper()]


class TestUserChannel:
    def test_is_deterministic_per_user(self) -> None:
        user_id = uuid4()
        assert pseudonyms.user_channel(user_id) == pseudonyms.user_channel(user_id)

    def test_differs_between_users(self) -> None:
        assert pseudonyms.user_channel(uuid4()) != pseudonyms.user_channel(uuid4())

    def test_carries_no_form_of_the_user_id(self) -> None:
        user_id = uuid4()
        channel = pseudonyms.user_channel(user_id)
        for form in _identifier_forms(user_id):
            assert form not in channel

    def test_satisfies_svix_channel_constraints(self) -> None:
        channel = pseudonyms.user_channel(uuid4())
        assert SVIX_CHANNEL.fullmatch(channel)
        assert len(channel) <= 128

    def test_round_trips_to_the_user(self) -> None:
        user_id = uuid4()
        assert pseudonyms.user_id_from_channel(pseudonyms.user_channel(user_id)) == user_id

    def test_legacy_readable_channel_still_reads_back(self) -> None:
        """Endpoints created before pseudonymisation keep reporting their filter."""
        user_id = uuid4()
        assert pseudonyms.user_id_from_channel(f"user.{user_id}") == user_id

    def test_a_token_under_another_key_is_not_read_as_a_user(self) -> None:
        user_id = uuid4()
        foreign = pseudonyms.user_channel(user_id, secret=SecretStr("some-other-secret"))
        assert pseudonyms.user_id_from_channel(foreign) is None

    @pytest.mark.parametrize("channel", ["project_123", "user.", "user.not-a-uuid", "user." + "ab" * 32])
    def test_anything_else_is_not_a_user(self, channel: str) -> None:
        assert pseudonyms.user_id_from_channel(channel) is None

    def test_readable_channels_decodes_only_user_channels(self) -> None:
        user_id = uuid4()
        assert pseudonyms.readable_channels([pseudonyms.user_channel(user_id), "project_123"]) == [
            f"user.{user_id}",
            "project_123",
        ]
        assert pseudonyms.readable_channels(None) is None


class TestEventId:
    RAW = (
        "timeseries.11de240a-4e95-4eed-ae00-983f34dcbd3b.apple.heart_rate"
        ".2026-09-12T12_37_09_00_00.2026-09-12T12_57_13_00_00.series.heart_rate.created"
    )

    def test_is_deterministic(self) -> None:
        assert pseudonyms.hash_event_id(self.RAW) == pseudonyms.hash_event_id(self.RAW)

    def test_none_passes_through(self) -> None:
        assert pseudonyms.hash_event_id(None) is None

    def test_satisfies_svix_event_id_constraints(self) -> None:
        digest = pseudonyms.hash_event_id(self.RAW)
        assert digest is not None
        assert SVIX_EVENT_ID.fullmatch(digest)
        assert len(digest) <= 256


class TestKeySeparation:
    """The pseudonym secret must not be the JWT signing key: event-id digests are returned to API
    callers, so under that key they would be HMAC outputs of the key that signs every access token.
    (The event-id and channel keys are derived under distinct contexts and hash functions; that
    separation is by construction and not pinned by a test here.)"""

    def test_default_secret_is_derived_and_is_not_secret_key(self) -> None:
        s = Settings(secret_key="the-jwt-signing-key")
        assert s.svix_pseudonym_secret is not None
        assert s.svix_pseudonym_secret.get_secret_value() != "the-jwt-signing-key"

    def test_an_explicit_secret_is_honoured(self) -> None:
        s = Settings(secret_key="k", svix_pseudonym_secret=SecretStr("explicit"))
        assert s.svix_pseudonym_secret is not None
        assert s.svix_pseudonym_secret.get_secret_value() == "explicit"

    def test_event_id_digest_is_not_an_hmac_under_secret_key(self) -> None:
        import hashlib
        import hmac

        raw = "workout.created.x"
        under_jwt_key = hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
        assert pseudonyms.hash_event_id(raw) != under_jwt_key
