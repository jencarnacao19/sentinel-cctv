"""The console recovery commands.

These matter precisely when the web interface cannot be reached, so they run
against a real migrated database rather than a mocked service layer: the
failure this guards against is a command that works in principle but not
against an actual install.

The ``app`` fixture is used purely for its side effects -- it applies the
migrations and binds the engine to a temporary data directory, which is the
state these commands assume they are being run against.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app import cli
from app.auth.service import create_admin, get_user_by_username
from app.database.session import session_scope
from app.security.passwords import verify_password

ORIGINAL_PASSWORD = "original-horse-battery"
NEW_PASSWORD = "reset-horse-battery"


@pytest.fixture
def answers_prompts_with(monkeypatch: pytest.MonkeyPatch):
    """Answer the getpass prompts with a scripted sequence."""

    def _install(*values: str) -> None:
        remaining = iter(values)
        monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": next(remaining))

    return _install


async def seed_admin(username: str = "operator") -> None:
    async with session_scope() as session:
        await create_admin(session, username, ORIGINAL_PASSWORD)


async def password_of(username: str) -> str:
    async with session_scope() as session:
        user = await get_user_by_username(session, username)
        assert user is not None
        return user.password_hash


class TestResetPassword:
    async def test_it_sets_a_new_password_without_the_old_one(
        self, app: FastAPI, answers_prompts_with
    ) -> None:
        del app
        await seed_admin()
        answers_prompts_with(NEW_PASSWORD, NEW_PASSWORD)

        assert await cli.run(["reset-password", "operator"]) == 0

        # The whole point of the command: the previous password is irrelevant,
        # because nobody knows it.
        assert verify_password(await password_of("operator"), NEW_PASSWORD)

    async def test_an_unknown_account_is_an_error_not_a_silent_success(
        self, app: FastAPI, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        await seed_admin()

        assert await cli.run(["reset-password", "nobody"]) == 1

        # An operator locked out at 3am needs to be told what to try next.
        assert "list-users" in capsys.readouterr().err

    async def test_mismatched_confirmation_leaves_the_password_alone(
        self, app: FastAPI, answers_prompts_with
    ) -> None:
        del app
        await seed_admin()
        answers_prompts_with("first-horse-battery", "second-horse-battery")

        assert await cli.run(["reset-password", "operator"]) == 1

        assert verify_password(await password_of("operator"), ORIGINAL_PASSWORD)

    async def test_a_weak_password_is_refused_with_the_reasons(
        self, app: FastAPI, answers_prompts_with, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        await seed_admin()
        answers_prompts_with("short", "short")

        assert await cli.run(["reset-password", "operator"]) == 1
        assert "not strong enough" in capsys.readouterr().err

        assert verify_password(await password_of("operator"), ORIGINAL_PASSWORD)


class TestCreateAdmin:
    async def test_it_creates_the_first_account(self, app: FastAPI, answers_prompts_with) -> None:
        del app
        answers_prompts_with(NEW_PASSWORD, NEW_PASSWORD)

        assert await cli.run(["create-admin", "operator"]) == 0

        async with session_scope() as session:
            user = await get_user_by_username(session, "operator")
            assert user is not None
            assert user.is_admin
            assert verify_password(user.password_hash, NEW_PASSWORD)

    async def test_it_refuses_a_second_account_and_points_at_the_alternative(
        self, app: FastAPI, answers_prompts_with, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        await seed_admin()
        answers_prompts_with(NEW_PASSWORD, NEW_PASSWORD)

        assert await cli.run(["create-admin", "intruder"]) == 1
        assert "reset-password" in capsys.readouterr().err


class TestListUsers:
    async def test_an_empty_install_says_so_rather_than_printing_nothing(
        self, app: FastAPI, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        assert await cli.run(["list-users"]) == 0
        assert "No accounts exist yet" in capsys.readouterr().out

    async def test_it_marks_which_account_is_an_administrator(
        self, app: FastAPI, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        await seed_admin()

        assert await cli.run(["list-users"]) == 0
        assert "operator (admin)" in capsys.readouterr().out


class TestStatus:
    async def test_it_reports_the_schema_revision_without_starting_the_server(
        self, app: FastAPI, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del app
        assert await cli.run(["status"]) == 0

        out = capsys.readouterr().out
        assert "Sentinel CCTV" in out
        # A missing revision would mean migrations never ran, which is exactly
        # what this command exists to tell an operator.
        assert "Schema revision" in out
        assert "not initialised" not in out


class TestArgumentParsing:
    def test_no_command_is_a_usage_error_rather_than_a_traceback(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main([])
        assert excinfo.value.code == 2
