"""Runtime settings stay coherent under atomic updates."""

from fastapi import FastAPI

from app.database.session import session_scope
from app.system import settings_store


async def test_upsert_refreshes_a_setting_already_loaded_in_the_session(app: FastAPI) -> None:
    async with session_scope() as session:
        await settings_store.set_setting(session, "test.atomic", {"version": 1})
        assert await settings_store.get_setting(session, "test.atomic") == {"version": 1}

        await settings_store.set_setting(session, "test.atomic", {"version": 2})
        assert await settings_store.get_setting(session, "test.atomic") == {"version": 2}
