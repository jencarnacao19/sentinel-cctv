"""Personal themes are not another way to change someone else's account."""

import pytest
from pydantic import SecretStr

from app.auth.tokens import create_access_token
from app.database.models import User
from app.database.session import session_scope

PREFERENCES = {"mode": "dark", "palette": "forest", "hover_sound": False}


async def test_preferences_survive_reload_and_are_isolated(admin_client):
    first = (await admin_client.get("/api/v1/viewer/appearance")).json()
    result = await admin_client.put(
        "/api/v1/appearance/me",
        json={
            "profile_id": first["profile_id"],
            "preferences": PREFERENCES,
        },
    )
    assert result.status_code == 200
    assert (await admin_client.get("/api/v1/viewer/appearance")).json()[
        "preferences"
    ] == PREFERENCES
    async with session_scope() as session:
        other = User(username="other", password_hash="not-used", is_active=True, is_admin=False)
        session.add(other)
        await session.flush()
        other_id = other.id
        await session.commit()
    token, _ = create_access_token(other_id)
    headers = {"Authorization": "Bearer " + token}
    other_view = (await admin_client.get("/api/v1/viewer/appearance", headers=headers)).json()
    assert other_view["profile_id"] == other_id
    assert other_view["preferences"]["palette"] == "slate"
    stolen = await admin_client.put(
        "/api/v1/appearance/me",
        headers=headers,
        json={
            "profile_id": first["profile_id"],
            "preferences": PREFERENCES,
        },
    )
    assert stolen.status_code == 409
    denied = await admin_client.put(
        "/api/v1/appearance/sound-policy", headers=headers, json={"hover_sounds_allowed": False}
    )
    assert denied.status_code == 403


async def test_shared_viewer_cannot_save_account_preferences_or_policy(client, settings):
    settings.viewer_enabled = True
    settings.viewer_key = SecretStr("test-only-viewer-key")
    headers = {"X-Sentinel-Viewer-Key": "test-only-viewer-key"}
    response = await client.get("/api/v1/viewer/appearance", headers=headers)
    assert response.status_code == 200
    assert response.json()["profile_id"] is None
    assert response.json()["preferences"] is None
    assert (
        await client.put(
            "/api/v1/appearance/me",
            headers=headers,
            json={
                "profile_id": "another-user",
                "preferences": PREFERENCES,
            },
        )
    ).status_code == 401
    assert (
        await client.put(
            "/api/v1/appearance/sound-policy", headers=headers, json={"hover_sounds_allowed": False}
        )
    ).status_code == 401


async def test_cookie_blocked_mobile_poll_never_clears_preferences_or_viewer_keys(client, settings):
    settings.viewer_enabled = True
    settings.cookie_secure = True
    settings.viewer_key = SecretStr("test-mobile-viewer-key")
    for _ in range(3):
        client.cookies.clear()  # HA WebViews can reject the migration cookie.
        response = await client.get(
            "/api/v1/viewer/appearance",
            headers={"X-Sentinel-Viewer-Key": "test-mobile-viewer-key"},
        )
        assert response.status_code == 200
        assert response.headers["clear-site-data"] == '"cache"'
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["profile_id"] is None


async def test_global_policy_persists_without_overwriting_personal_mute(admin_client):
    original = (await admin_client.get("/api/v1/viewer/appearance")).json()
    await admin_client.put(
        "/api/v1/appearance/me",
        json={"profile_id": original["profile_id"], "preferences": PREFERENCES},
    )
    for allowed in (False, True):
        result = await admin_client.put(
            "/api/v1/appearance/sound-policy", json={"hover_sounds_allowed": allowed}
        )
        assert result.status_code == 200
        state = (await admin_client.get("/api/v1/viewer/appearance")).json()
        assert state["hover_sounds_allowed"] is allowed
        assert state["preferences"]["hover_sound"] is False


@pytest.mark.parametrize(
    "preferences",
    [
        {**PREFERENCES, "palette": "javascript:alert(1)"},
        {**PREFERENCES, "hover_sound": "true"},
        {**PREFERENCES, "is_admin": True},
    ],
)
async def test_preference_values_are_strict_and_bounded(admin_client, preferences):
    profile = (await admin_client.get("/api/v1/viewer/appearance")).json()["profile_id"]
    response = await admin_client.put(
        "/api/v1/appearance/me", json={"profile_id": profile, "preferences": preferences}
    )
    assert response.status_code == 422
