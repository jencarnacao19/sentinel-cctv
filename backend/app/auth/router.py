"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import login_security, service
from app.auth.deps import (
    AdminPrincipal,
    CurrentPrincipal,
    CurrentUser,
    client_key,
)
from app.auth.ratelimit import login_limiter, token_limiter
from app.auth.schemas import (
    ApiTokenCreatedResponse,
    ApiTokenCreateRequest,
    ApiTokenResponse,
    ChangePasswordRequest,
    ClearLoginBlocksRequest,
    CreateAdminRequest,
    LoginBlockResponse,
    LoginRequest,
    LoginSecurityResponse,
    LoginSecurityUpdate,
    TokenResponse,
    UserResponse,
)
from app.auth.tokens import create_access_token
from app.config import Settings, get_settings
from app.database.base import utcnow
from app.database.session import get_session
from app.logging_config import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("API")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _set_session_cookie(
    response: Response, token: str, settings: Settings, max_age_seconds: int
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.get("/status", summary="Whether an administrator account exists yet")
async def auth_status(session: SessionDep) -> dict[str, bool]:
    return {"initialised": await service.has_any_user(session)}


@router.post(
    "/setup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create the first administrator (first run only)",
)
async def setup_admin(
    payload: CreateAdminRequest, session: SessionDep, request: Request
) -> UserResponse:
    limit = await login_limiter.check(f"setup:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )
    try:
        user = await service.create_admin(session, payload.username, payload.password)
    except service.AdminAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except service.WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.problems
        ) from exc
    await session.commit()
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse, summary="Sign in")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    identity = login_security.resolve_login_client(request, settings)
    policy = await login_security.get_policy(session)
    if policy.enabled and identity.is_public:
        if not identity.is_verified:
            logger.error(
                "Public sign-in proxy identity unavailable",
                extra={"client": identity.address, "host": identity.public_hostname},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Public sign-in protection could not verify the visitor address. "
                    "Use the local Sentinel address or check the trusted proxy configuration."
                ),
            )
        blocked = await login_security.get_active_block(session, identity.address)
        if blocked is not None:
            headers: dict[str, str] = {}
            if blocked.blocked_until is not None:
                retry_after = max(
                    1,
                    int((blocked.blocked_until - utcnow()).total_seconds()),
                )
                headers["Retry-After"] = str(retry_after)
            logger.warning("Blocked public sign-in refused", extra={"client": identity.address})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "This IP address is blocked from administrator sign-in. "
                    "Use the local Sentinel address to review or remove the block."
                ),
                headers=headers,
            )

    limiter_address = identity.address if identity.is_verified else client_key(request)
    limit = await login_limiter.check(f"login:{limiter_address}")
    if not limit.allowed:
        logger.warning("Login rate limit hit", extra={"client": limiter_address})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )

    try:
        user = await service.authenticate(session, payload.username, payload.password)
    except service.InvalidCredentialsError as exc:
        await session.rollback()
        blocked_now = False
        if policy.enabled and identity.is_public and identity.is_verified:
            state = await login_security.record_failure(
                session,
                address=identity.address,
                public_hostname=identity.public_hostname or "unknown",
                policy=policy,
            )
            blocked_now = login_security.block_is_active(state)
            await session.commit()
        logger.warning(
            "Failed sign-in",
            extra={
                "username": payload.username,
                "client": identity.address,
                "blocked": blocked_now,
            },
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    token, expires_at = create_access_token(user.id)
    if identity.is_public and identity.is_verified:
        await login_security.clear_pending_failure(session, identity.address)
    await session.commit()
    _set_session_cookie(response, token, settings, settings.access_token_ttl_minutes * 60)
    logger.info("Signed in", extra={"username": user.username})
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
async def logout(response: Response, settings: SettingsDep) -> Response:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserResponse, summary="Current user")
async def me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)


async def _login_security_response(
    session: AsyncSession, settings: Settings
) -> LoginSecurityResponse:
    policy = await login_security.get_policy(session)
    blocks = await login_security.list_active_blocks(session)
    await session.commit()
    return LoginSecurityResponse(
        enabled=policy.enabled,
        failure_threshold=policy.failure_threshold,
        block_duration_seconds=policy.block_duration_seconds,
        public_hostnames=sorted(
            {host.rstrip(".").lower() for host in settings.viewer_trusted_hosts}
        ),
        trusted_proxy_cidrs=list(settings.trusted_proxy_cidrs),
        proxy_ready=login_security.proxy_configuration_ready(settings),
        blocked_count=len(blocks),
    )


@router.get(
    "/login-security",
    response_model=LoginSecurityResponse,
    summary="Public administrator sign-in protection",
)
async def get_login_security(
    _: AdminPrincipal, session: SessionDep, settings: SettingsDep
) -> LoginSecurityResponse:
    return await _login_security_response(session, settings)


@router.patch(
    "/login-security",
    response_model=LoginSecurityResponse,
    summary="Configure public administrator sign-in protection",
)
async def update_login_security(
    payload: LoginSecurityUpdate,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> LoginSecurityResponse:
    if payload.enabled and not login_security.proxy_configuration_ready(settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Configure a public hostname and trusted proxy network before enabling IP blocking."
            ),
        )
    await login_security.set_policy(
        session,
        login_security.LoginSecurityPolicy(
            enabled=payload.enabled,
            failure_threshold=payload.failure_threshold,
            block_duration_seconds=payload.block_duration_seconds,
        ),
    )
    await session.commit()
    logger.info(
        "Public sign-in protection updated",
        extra={
            "enabled": payload.enabled,
            "failure_threshold": payload.failure_threshold,
            "permanent": payload.block_duration_seconds is None,
        },
    )
    return await _login_security_response(session, settings)


@router.get(
    "/login-blocks",
    response_model=list[LoginBlockResponse],
    summary="List blocked public login addresses",
)
async def list_login_blocks(_: AdminPrincipal, session: SessionDep) -> list[LoginBlockResponse]:
    rows = await login_security.list_active_blocks(session)
    await session.commit()
    return [LoginBlockResponse.model_validate(row) for row in rows]


@router.delete(
    "/login-blocks/{address}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unblock one public login address",
)
async def remove_login_block(address: str, _: AdminPrincipal, session: SessionDep) -> Response:
    removed = await login_security.unblock(session, address)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found.")
    await session.commit()
    logger.info("Public sign-in address unblocked", extra={"client": address})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/login-blocks/clear",
    summary="Clear all public login blocks and pending failures",
)
async def clear_login_blocks(
    payload: ClearLoginBlocksRequest, _: AdminPrincipal, session: SessionDep
) -> dict[str, int]:
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Confirmation is required.",
        )
    removed = await login_security.clear_all_states(session)
    await session.commit()
    logger.info("All public sign-in blocks cleared", extra={"removed": removed})
    return {"removed": removed}


@router.get("/whoami", summary="Identify the caller (user or API token)")
async def whoami(principal: CurrentPrincipal) -> dict[str, object]:
    return {
        "kind": principal.kind,
        "id": principal.id,
        "name": principal.name,
        "is_admin": principal.is_admin,
        "scope": principal.scope,
    }


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the signed-in user's password",
)
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, session: SessionDep
) -> Response:
    try:
        await service.change_password(session, user, payload.current_password, payload.new_password)
    except service.InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except service.WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.problems
        ) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- API tokens ---------------------------------------------------------


@router.get("/tokens", response_model=list[ApiTokenResponse], summary="List API tokens")
async def list_tokens(_: AdminPrincipal, session: SessionDep) -> list[ApiTokenResponse]:
    rows = await service.list_api_tokens(session)
    return [ApiTokenResponse.model_validate(row) for row in rows]


@router.post(
    "/tokens",
    response_model=ApiTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API token (plaintext returned once)",
)
async def create_token(
    payload: ApiTokenCreateRequest,
    principal: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> ApiTokenCreatedResponse:
    limit = await token_limiter.check(f"token:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many tokens created. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )
    token, plaintext = await service.create_api_token(
        session,
        name=payload.name,
        scope=payload.scope,
        user=principal.user,
        expires_in_days=payload.expires_in_days,
    )
    await session.commit()
    return ApiTokenCreatedResponse(
        **ApiTokenResponse.model_validate(token).model_dump(), token=plaintext
    )


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API token",
)
async def revoke_token(token_id: str, _: AdminPrincipal, session: SessionDep) -> Response:
    token = await service.revoke_api_token(session, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found.")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
