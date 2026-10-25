from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import attach_session_cookie, clear_session_cookie
from app.config import get_settings
from app.db import get_session
from app.schemas.auth import (
    AuthMeResponse,
    AuthSessionResponse,
    LogInRequest,
    SignUpRequest,
)
from app.schemas.errors import ErrorResponse
from app.services.auth import (
    login_user,
    register_user,
    resolve_current_user,
    revoke_current_session,
    to_public_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Petición inválida"},
    401: {"model": ErrorResponse, "description": "No autenticado"},
    403: {"model": ErrorResponse, "description": "Cuenta suspendida o borrada"},
    409: {"model": ErrorResponse, "description": "Email ya registrado"},
}


@router.post(
    "/signup",
    response_model=AuthSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar un usuario",
    description=(
        "Crea un alumno (o admin si el email está en BOOTSTRAP_ADMIN_EMAILS). "
        "La contraseña se guarda con argon2. Devuelve PublicUser y la cookie "
        "HttpOnly `session` (o AUTH_COOKIE_NAME)."
    ),
    responses={400: ERROR_RESPONSES[400], 409: ERROR_RESPONSES[409]},
)
async def signup(
    body: SignUpRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthSessionResponse:
    settings = get_settings()
    user, token = await register_user(session, settings, body)
    attach_session_cookie(response, token, settings)
    return AuthSessionResponse(user=to_public_user(user))


@router.post(
    "/login",
    response_model=AuthSessionResponse,
    summary="Iniciar sesión",
    description=(
        "Valida email y contraseña. Credenciales inválidas → "
        "`invalid_credentials`. Cuenta suspendida o borrada → "
        "`account_suspended`. Devuelve PublicUser y la cookie de sesión."
    ),
    responses={
        400: ERROR_RESPONSES[400],
        401: ERROR_RESPONSES[401],
        403: ERROR_RESPONSES[403],
    },
)
async def login(
    body: LogInRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthSessionResponse:
    settings = get_settings()
    user, token = await login_user(session, settings, body)
    attach_session_cookie(response, token, settings)
    return AuthSessionResponse(user=to_public_user(user))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar sesión",
    description=(
        "Revoca la sesión persistida (jti) y borra la cookie. "
        "Idempotente: sin cookie o con token inválido también responde 204."
    ),
)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    settings = get_settings()
    await revoke_current_session(request, session, settings)
    clear_session_cookie(response, settings)


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="Usuario de la sesión actual",
    description=(
        "Requiere cookie `session` o `Authorization: Bearer`. "
        "Sin sesión, token caducado o manipulado → `unauthorized`. "
        "Usuario suspendido o borrado → `account_suspended`."
    ),
    responses={401: ERROR_RESPONSES[401], 403: ERROR_RESPONSES[403]},
)
async def me(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthMeResponse:
    settings = get_settings()
    user = await resolve_current_user(request, session, settings)
    return AuthMeResponse(
        user=to_public_user(user),
        system_role=user.system_role,
        is_admin=user.is_admin,
    )
