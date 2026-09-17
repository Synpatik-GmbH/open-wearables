from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.config import settings
from app.database import DbSession
from app.models import Developer, User
from app.repositories.developer_repository import DeveloperRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import SDKAuthContext

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
developer_repository = DeveloperRepository(Developer)
user_repository = UserRepository(User)


async def get_current_developer(
    db: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Developer:
    """Get current authenticated developer from JWT token.

    SDK-scoped tokens are rejected - they can only access /sdk/ endpoints.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])

        # Reject SDK-scoped tokens - they can ONLY access /sdk/ endpoints
        if payload.get("scope") == "sdk":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SDK tokens cannot access this endpoint",
                headers={"WWW-Authenticate": "Bearer"},
            )

        developer_id: str = payload.get("sub")
        if developer_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    developer = developer_repository.get(db, UUID(developer_id))
    if not developer:
        raise credentials_exception

    return developer


async def get_current_developer_optional(
    db: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> Developer | None:
    """Get current authenticated developer from JWT token, or None if not authenticated.

    SDK-scoped tokens return None - they are not developer tokens.
    """
    if not token:
        return None

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])

        # SDK tokens are not developer tokens - return None to allow fallback to API key
        if payload.get("scope") == "sdk":
            return None

        developer_id: str = payload.get("sub")
        if developer_id is None:
            return None

        # Validate that developer_id is a valid UUID
        try:
            developer_uuid = UUID(developer_id)
        except ValueError:
            return None

    except JWTError:
        return None

    return developer_repository.get(db, developer_uuid)


DeveloperDep = Annotated[Developer, Depends(get_current_developer)]
DeveloperOptionalDep = Annotated[Developer | None, Depends(get_current_developer_optional)]


def _sdk_auth_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide SDK token or API key",
    )


def _parse_sdk_subject(sub: object) -> UUID | None:
    if not isinstance(sub, str) or not sub:
        return None
    try:
        return UUID(sub)
    except ValueError:
        return None


async def get_sdk_auth(
    db: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    x_open_wearables_api_key: str | None = Header(None, alias="X-Open-Wearables-API-Key"),
) -> SDKAuthContext:
    """Authenticate SDK requests using either SDK user token or API key.

    Accepts:
    - SDK token (Bearer token with scope="sdk")
    - API key (X-Open-Wearables-API-Key header)

    Returns SDKAuthContext with auth_type and relevant identifiers.
    """
    # Import here to avoid circular imports
    from app.services.api_key_service import api_key_service

    # Try SDK user token first
    if token:
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        except JWTError:
            payload = None  # Fall through to API key check

        if payload is not None and payload.get("scope") == "sdk":
            # Fork delta (2.41.9.1): a decodable SDK token is a credential only while its
            # user exists. A deleted user's token is rejected here, never rescued by an API
            # key, and a lookup failure propagates. See FORK-DELTA.md.
            user_id = _parse_sdk_subject(payload.get("sub"))
            if user_id is None or user_repository.get(db, user_id) is None:
                raise _sdk_auth_required()
            return SDKAuthContext(
                auth_type="sdk_token",
                user_id=user_id,
                app_id=payload.get("app_id"),
            )

    # Fall back to API key (backwards compatibility)
    if x_open_wearables_api_key:
        api_key = api_key_service.validate_api_key(db, x_open_wearables_api_key)
        return SDKAuthContext(auth_type="api_key", api_key_id=api_key.id)

    raise _sdk_auth_required()


SDKAuthDep = Annotated[SDKAuthContext, Depends(get_sdk_auth)]
