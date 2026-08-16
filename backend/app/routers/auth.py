import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import (
    COOKIE_NAME,
    create_session_cookie,
    is_request_authenticated,
    verify_password,
)
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
_LOGIN_DELAY_SECONDS = 0.5


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@router.get("/me")
async def auth_status(request: Request):
    settings = get_settings()
    return {
        "authenticated": is_request_authenticated(request, settings),
        "auth_enabled": settings.auth_enabled,
    }


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    settings = get_settings()
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False}
    if not verify_password(body.password, settings.auth_password_hash):
        await asyncio.sleep(_LOGIN_DELAY_SECONDS)
        raise HTTPException(status_code=401, detail="invalid credentials")
    response.set_cookie(
        COOKIE_NAME,
        create_session_cookie(settings),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "auth_enabled": True}


@router.post("/logout", status_code=204)
async def logout(response: Response):
    settings = get_settings()
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
