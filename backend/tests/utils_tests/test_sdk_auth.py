"""Tests for SDK authentication utilities."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.services import user_service
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.auth import get_current_developer, get_sdk_auth
from tests.factories import ApiKeyFactory, DeveloperFactory, UserFactory

SDK_AUTH_REQUIRED = "Authentication required: provide SDK token or API key"


def _sdk_token_without_sub() -> str:
    claims = {
        "scope": "sdk",
        "app_id": "app_123",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm)


class TestGetSDKAuth:
    """Tests for SDK authentication dependency."""

    @pytest.mark.asyncio
    async def test_sdk_token_returns_context(self, db: Session) -> None:
        """Valid SDK token should return SDKAuthContext."""
        user_id = "123e4567-e89b-12d3-a456-426614174000"
        UserFactory(id=UUID(user_id))
        token = create_sdk_user_token("app_123", user_id)

        result = await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=None)

        assert result.auth_type == "sdk_token"
        assert str(result.user_id) == user_id
        assert result.app_id == "app_123"

    @pytest.mark.asyncio
    async def test_api_key_returns_context(self, db: Session) -> None:
        """Valid API key should return SDKAuthContext."""
        api_key = ApiKeyFactory()

        result = await get_sdk_auth(db=db, token=None, x_open_wearables_api_key=api_key.id)

        assert result.auth_type == "api_key"
        assert result.api_key_id == api_key.id

    @pytest.mark.asyncio
    async def test_no_auth_raises_401(self, db: Session) -> None:
        """Missing auth should raise 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=None, x_open_wearables_api_key=None)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key_raises_401(self, db: Session) -> None:
        """Invalid API key should raise 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=None, x_open_wearables_api_key="invalid_key")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_sdk_token_preferred_over_api_key(self, db: Session) -> None:
        """SDK token should be used even if API key is also provided."""
        api_key = ApiKeyFactory()
        user_id = "123e4567-e89b-12d3-a456-426614174001"
        UserFactory(id=UUID(user_id))
        token = create_sdk_user_token("app_123", user_id)

        result = await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=api_key.id)

        assert result.auth_type == "sdk_token"
        assert str(result.user_id) == user_id


class TestSDKTokenBlockedFromDeveloperEndpoints:
    """Tests for blocking SDK tokens from non-SDK endpoints."""

    @pytest.mark.asyncio
    async def test_sdk_token_rejected_by_get_current_developer(self, db: Session) -> None:
        """SDK tokens should be rejected by get_current_developer."""
        # Create a real developer for the DB (but token is SDK-scoped)
        DeveloperFactory()
        user_id = "123e4567-e89b-12d3-a456-426614174001"
        sdk_token = create_sdk_user_token("app_123", user_id)

        with pytest.raises(HTTPException) as exc_info:
            await get_current_developer(db=db, token=sdk_token)

        assert exc_info.value.status_code == 401
        assert "SDK tokens cannot access this endpoint" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_developer_token_accepted_by_get_current_developer(self, db: Session) -> None:
        """Developer tokens should still work with get_current_developer."""
        from app.utils.security import create_access_token

        developer = DeveloperFactory()
        dev_token = create_access_token(subject=str(developer.id))

        result = await get_current_developer(db=db, token=dev_token)

        assert result.id == developer.id

    @pytest.mark.asyncio
    async def test_no_token_raises_401(self, db: Session) -> None:
        """No token should raise 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_developer(db=db, token=None)

        assert exc_info.value.status_code == 401


class TestGetSDKAuthSubjectMustExist:
    """D-01: a decodable SDK token is a credential only while its user exists."""

    @pytest.mark.asyncio
    async def test_deleted_user_token_raises_401(self, db: Session) -> None:
        user = UserFactory()
        token = create_sdk_user_token("app_123", str(user.id))
        user_service.delete(db, user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=None)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == SDK_AUTH_REQUIRED
        assert exc_info.value.headers is None

    @pytest.mark.asyncio
    async def test_never_existing_user_token_raises_401(self, db: Session) -> None:
        token = create_sdk_user_token("app_123", str(uuid4()))

        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=None)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_token_without_sub_raises_401(self, db: Session) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=_sdk_token_without_sub(), x_open_wearables_api_key=None)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_token_with_non_uuid_sub_raises_401(self, db: Session) -> None:
        token = create_sdk_user_token("app_123", "not-a-uuid")

        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=None)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_deleted_user_token_is_not_rescued_by_api_key(self, db: Session) -> None:
        api_key = ApiKeyFactory()
        user = UserFactory()
        token = create_sdk_user_token("app_123", str(user.id))
        user_service.delete(db, user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=api_key.id)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_undecodable_token_still_falls_through_to_api_key(self, db: Session) -> None:
        api_key = ApiKeyFactory()

        result = await get_sdk_auth(db=db, token="not-a-jwt", x_open_wearables_api_key=api_key.id)

        assert result.auth_type == "api_key"

    @pytest.mark.asyncio
    async def test_non_sdk_jwt_still_falls_through_to_api_key(self, db: Session) -> None:
        from app.utils.security import create_access_token

        api_key = ApiKeyFactory()
        developer_token = create_access_token(subject=str(DeveloperFactory().id))

        result = await get_sdk_auth(db=db, token=developer_token, x_open_wearables_api_key=api_key.id)

        assert result.auth_type == "api_key"

    @pytest.mark.asyncio
    async def test_user_lookup_failure_propagates(self, db: Session) -> None:
        token = create_sdk_user_token("app_123", str(uuid4()))

        with (
            patch("app.utils.auth.user_repository.get", side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError),
        ):
            await get_sdk_auth(db=db, token=token, x_open_wearables_api_key=None)
