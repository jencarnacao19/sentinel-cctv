"""Console entry point, for the things a browser cannot fix.

A local-first product has no password-reset email to fall back on: if the only
administrator forgets their password, there is no second channel and no support
account to ask. Physical (or SSH) access to the box has to be the recovery path,
which is what this exists for. It deliberately stays small -- everything here is
either recovery from a lockout or answering "what state is this install in?"
without starting the server.

Every command opens the database directly rather than calling the HTTP API, so
it still works when the service will not start.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy import select

from app import __version__
from app.auth.service import create_admin, get_user_by_username
from app.config import get_settings
from app.database.bootstrap import current_revision, upgrade_to_head_async
from app.database.models import User
from app.database.session import session_scope
from app.security.passwords import hash_password, validate_password_strength


def _prompt_new_password() -> str | None:
    """Read a password twice, without echoing it.

    Taking the password from a prompt rather than an argument is deliberate:
    an argument would land in the shell history and in the process list, where
    any other user on the box could read it.
    """
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        return None
    problems = validate_password_strength(first)
    if problems:
        print("That password is not strong enough:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return None
    return first


async def _reset_password(username: str) -> int:
    async with session_scope() as session:
        user = await get_user_by_username(session, username)
        if user is None:
            print(
                f"No account named '{username}'. Run 'sentinel list-users' to see "
                "which accounts exist.",
                file=sys.stderr,
            )
            return 1

        password = _prompt_new_password()
        if password is None:
            return 1

        # The current password is not required here, unlike the API's change
        # flow: the entire point of this command is that nobody knows it. Access
        # to the database file is the authorisation.
        user.password_hash = hash_password(password)
        await session.flush()

    print(f"Password updated for '{username}'.")
    return 0


async def _create_admin(username: str) -> int:
    password = _prompt_new_password()
    if password is None:
        return 1

    async with session_scope() as session:
        try:
            await create_admin(session, username, password)
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
            print(str(exc), file=sys.stderr)
            print(
                "An administrator already exists; use 'sentinel reset-password' instead.",
                file=sys.stderr,
            )
            return 1

    print(f"Administrator '{username}' created.")
    return 0


async def _list_users() -> int:
    async with session_scope() as session:
        result = await session.execute(select(User).order_by(User.username))
        users = list(result.scalars())

    if not users:
        print("No accounts exist yet. Open the web interface to create the first one.")
        return 0

    for user in users:
        flags = []
        if user.is_admin:
            flags.append("admin")
        if not user.is_active:
            flags.append("disabled")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"{user.username}{suffix}")
    return 0


async def _show_status() -> int:
    settings = get_settings()
    revision = current_revision(settings)
    print(f"Sentinel CCTV {__version__}")
    print(f"Data directory:  {settings.data_dir}")
    print(f"Recordings:      {settings.recordings_dir}")
    print(f"Schema revision: {revision or 'not initialised'}")
    return 0


async def _migrate() -> int:
    """Apply migrations without starting the server.

    The server does this at start-up anyway. Having it separately is what makes
    it possible to apply a schema change and see the error, if there is one, on
    a terminal instead of buried in a container's start-up logs.
    """
    settings = get_settings()
    before = current_revision(settings)
    await upgrade_to_head_async(settings)
    after = current_revision(settings)
    if before == after:
        print(f"Already at the latest revision ({after}).")
    else:
        print(f"Migrated {before or 'empty database'} -> {after}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Administrative commands for a Sentinel CCTV install.",
    )
    parser.add_argument("--version", action="version", version=f"Sentinel CCTV {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    reset = subcommands.add_parser(
        "reset-password",
        help="Set a new password for an account, without knowing the old one.",
    )
    reset.add_argument("username")

    create = subcommands.add_parser(
        "create-admin",
        help="Create the first administrator account.",
    )
    create.add_argument("username")

    subcommands.add_parser("list-users", help="List the accounts on this install.")
    subcommands.add_parser("status", help="Show version, paths and schema revision.")
    subcommands.add_parser("migrate", help="Apply pending database migrations and exit.")

    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch, without owning the event loop.

    Kept separate from :func:`main` so the commands can be exercised from
    inside an existing loop -- the test suite runs in one, and ``asyncio.run``
    refuses to nest.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    match args.command:
        case "reset-password":
            return await _reset_password(args.username)
        case "create-admin":
            return await _create_admin(args.username)
        case "list-users":
            return await _list_users()
        case "status":
            return await _show_status()
        case "migrate":
            return await _migrate()
        case _:  # pragma: no cover - argparse rejects anything else first
            parser.error(f"Unknown command: {args.command}")
            return 2


def main(argv: Sequence[str] | None = None) -> int:
    """The console entry point declared in ``pyproject.toml``."""
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
