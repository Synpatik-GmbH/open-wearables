"""Who may obtain an SDK token (fork, Notion 2.71.4).

Nothing binds an application to a user, so a token is minted for any user id the caller
names. This deployment has one application, whose job is to mint for every user, so the
fork narrows the ways in instead of adding a binding:

- app credentials are the only way to mint; a developer login and the invitation-code
  routes are off unless a setting turns them on;
- minting stops while more than one application exists, so a second application cannot
  reach the first one's users before a binding is built;
- a token is minted only for a user that exists.
"""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.config import settings
from app.models import RefreshToken
from app.services.refresh_token_service import refresh_token_service
from app.services.user_invitation_code_service import user_invitation_code_service
from tests.factories import ApplicationFactory, DeveloperFactory, UserFactory
from tests.utils import developer_auth_headers

SECRET = "test_app_secret"


def _live_sdk_tokens(db: Session) -> int:
    return db.query(RefreshToken).filter(RefreshToken.user_id.isnot(None)).count()


def _mint(client: TestClient, prefix: str, user_id: object, app_id: str) -> object:
    return client.post(f"{prefix}/users/{user_id}/token", json={"app_id": app_id, "app_secret": SECRET})


class TestDeveloperLoginCannotMint:
    def test_refused_by_default(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        developer = DeveloperFactory()
        user = UserFactory()

        response = client.post(f"{api_v1_prefix}/users/{user.id}/token", headers=developer_auth_headers(developer.id))

        assert response.status_code == 403
        assert _live_sdk_tokens(db) == 0

    def test_allowed_when_the_setting_is_on(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "sdk_token_developer_mint_enabled", True)
        developer = DeveloperFactory()
        user = UserFactory()

        response = client.post(f"{api_v1_prefix}/users/{user.id}/token", headers=developer_auth_headers(developer.id))

        assert response.status_code == 200

    def test_a_token_it_minted_earlier_no_longer_refreshes(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Minted through the real route while it was on, so the test follows whatever
        # app_id that route writes.
        monkeypatch.setattr(settings, "sdk_token_developer_mint_enabled", True)
        developer = DeveloperFactory()
        user = UserFactory()
        minted = client.post(f"{api_v1_prefix}/users/{user.id}/token", headers=developer_auth_headers(developer.id))
        refresh_token = minted.json()["refresh_token"]

        monkeypatch.setattr(settings, "sdk_token_developer_mint_enabled", False)
        response = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 401


class TestInvitationCodesAreOff:
    def test_generate_is_not_found_by_default(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        developer = DeveloperFactory()
        user = UserFactory()

        response = client.post(
            f"{api_v1_prefix}/users/{user.id}/invitation-code", headers=developer_auth_headers(developer.id)
        )

        assert response.status_code == 404

    def test_a_valid_code_is_not_redeemed_by_default(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        developer = DeveloperFactory()
        user = UserFactory()
        code = user_invitation_code_service.generate(db, user.id, developer.id).code

        response = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": code})

        assert response.status_code == 404
        assert _live_sdk_tokens(db) == 0

    def test_a_token_a_code_minted_earlier_no_longer_refreshes(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "user_invitation_codes_enabled", True)
        developer = DeveloperFactory()
        user = UserFactory()
        code = user_invitation_code_service.generate(db, user.id, developer.id).code
        redeemed = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": code})
        assert redeemed.status_code == 200
        refresh_token = redeemed.json()["refresh_token"]

        monkeypatch.setattr(settings, "user_invitation_codes_enabled", False)
        response = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 401


class TestAppCredentialsStillWork:
    def test_mint_and_refresh(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        application = ApplicationFactory(app_secret=SECRET)
        user = UserFactory()

        minted = _mint(client, api_v1_prefix, user.id, application.app_id)
        assert minted.status_code == 200
        refreshed = client.post(
            f"{api_v1_prefix}/token/refresh", json={"refresh_token": minted.json()["refresh_token"]}
        )

        assert refreshed.status_code == 200


class TestMintingStopsWithASecondApplication:
    @pytest.mark.parametrize("caller", ["first", "second"])
    def test_neither_application_can_mint(
        self,
        client: TestClient,
        db: Session,
        api_v1_prefix: str,
        capfd: pytest.CaptureFixture[str],
        caller: str,
    ) -> None:
        apps = {"first": ApplicationFactory(app_secret=SECRET), "second": ApplicationFactory(app_secret=SECRET)}
        user = UserFactory()

        response = _mint(client, api_v1_prefix, user.id, apps[caller].app_id)

        assert response.status_code == 409
        assert _live_sdk_tokens(db) == 0
        # The alert reads this line (calibra-ow-deploy). It names no user.
        logged = "\n".join(line for line in capfd.readouterr().out.splitlines() if '"http_request"' not in line)
        assert '"action": "sdk_token_refused_application_count"' in logged
        assert '"application_count": 2' in logged
        assert str(user.id) not in logged

    def test_wrong_credentials_are_still_401_and_say_nothing_about_the_count(
        self, client: TestClient, db: Session, api_v1_prefix: str
    ) -> None:
        ApplicationFactory(app_secret=SECRET)
        other = ApplicationFactory(app_secret=SECRET)
        user = UserFactory()

        response = client.post(
            f"{api_v1_prefix}/users/{user.id}/token", json={"app_id": other.app_id, "app_secret": "wrong"}
        )

        assert response.status_code == 401

    def test_a_redeemed_code_cannot_mint_either(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "user_invitation_codes_enabled", True)
        ApplicationFactory(app_secret=SECRET)
        ApplicationFactory(app_secret=SECRET)
        developer = DeveloperFactory()
        user = UserFactory()
        code = user_invitation_code_service.generate(db, user.id, developer.id).code

        response = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": code})

        assert response.status_code == 409
        assert _live_sdk_tokens(db) == 0

    def test_an_unknown_code_learns_nothing_and_raises_no_alarm(
        self,
        client: TestClient,
        db: Session,
        api_v1_prefix: str,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        # Redeem is public. A stranger must not be able to read the application count
        # from the answer, nor fire the alert at will.
        monkeypatch.setattr(settings, "user_invitation_codes_enabled", True)
        ApplicationFactory(app_secret=SECRET)
        ApplicationFactory(app_secret=SECRET)

        response = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": "ZZZZZZZZ"})

        assert response.status_code == 404
        assert "sdk_token_refused_application_count" not in capfd.readouterr().out

    def test_a_refused_code_is_not_used_up(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "user_invitation_codes_enabled", True)
        ApplicationFactory(app_secret=SECRET)
        extra = ApplicationFactory(app_secret=SECRET)
        developer = DeveloperFactory()
        user = UserFactory()
        code = user_invitation_code_service.generate(db, user.id, developer.id).code
        refused = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": code})
        assert refused.status_code == 409

        db.delete(extra)
        db.commit()
        response = client.post(f"{api_v1_prefix}/invitation-code/redeem", json={"code": code})

        assert response.status_code == 200

    def test_a_token_minted_before_keeps_refreshing(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        # Devices already provisioned are not cut off; only new tokens stop.
        application = ApplicationFactory(app_secret=SECRET)
        user = UserFactory()
        refresh_token = refresh_token_service.mint_sdk_refresh_token(db, user.id, application.app_id)
        ApplicationFactory(app_secret=SECRET)

        response = client.post(f"{api_v1_prefix}/token/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 200


class TestUnknownUser:
    def test_no_token_for_a_user_that_does_not_exist(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        application = ApplicationFactory(app_secret=SECRET)

        response = _mint(client, api_v1_prefix, uuid4(), application.app_id)

        assert response.status_code == 404
        assert _live_sdk_tokens(db) == 0


class TestDashboardIsTold:
    def test_config_reports_invitation_codes_off_by_default(
        self, client: TestClient, db: Session, api_v1_prefix: str
    ) -> None:
        developer = DeveloperFactory()

        response = client.get(f"{api_v1_prefix}/config", headers=developer_auth_headers(developer.id))

        assert response.json()["user_invitation_codes_enabled"] is False

    def test_config_reports_invitation_codes_on(
        self, client: TestClient, db: Session, api_v1_prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "user_invitation_codes_enabled", True)
        developer = DeveloperFactory()

        response = client.get(f"{api_v1_prefix}/config", headers=developer_auth_headers(developer.id))

        assert response.json()["user_invitation_codes_enabled"] is True
