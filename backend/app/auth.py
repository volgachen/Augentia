import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Request, WebSocket

from app.config import Settings, get_settings

COOKIE_NAME = "augentia_session"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def validate_auth_config(settings: Settings) -> None:
    if not settings.auth_enabled:
        return
    missing = [
        name
        for name, value in (
            ("AUGENTIA_AUTH_PASSWORD_HASH", settings.auth_password_hash),
            ("AUGENTIA_SESSION_SECRET", settings.session_secret),
            ("AUGENTIA_INTERNAL_TOKEN", settings.internal_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"authentication enabled but missing: {', '.join(missing)}")
    if len(settings.session_secret) < 32:
        raise RuntimeError("AUGENTIA_SESSION_SECRET must be at least 32 characters")
    if len(settings.internal_token) < 32:
        raise RuntimeError("AUGENTIA_INTERNAL_TOKEN must be at least 32 characters")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_encode(salt)}${_encode(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_decode(expected)),
        )
        return hmac.compare_digest(actual, _decode(expected))
    except (ValueError, TypeError):
        return False


def create_session_cookie(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    payload = {
        "sub": "owner",
        "exp": int(time.time()) + settings.session_ttl_seconds,
        "nonce": secrets.token_urlsafe(12),
    }
    body = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body, settings.session_secret)
    return f"{body}.{signature}"


def verify_session_cookie(value: str | None, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not value:
        return False
    try:
        body, signature = value.split(".", 1)
        if not hmac.compare_digest(signature, _sign(body, settings.session_secret)):
            return False
        payload: dict[str, Any] = json.loads(_decode(body))
        return payload.get("sub") == "owner" and int(payload.get("exp", 0)) >= int(time.time())
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def is_request_authenticated(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.auth_enabled:
        return True
    if verify_session_cookie(request.cookies.get(COOKIE_NAME), settings):
        return True
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    return (
        scheme.lower() == "bearer"
        and bool(token)
        and hmac.compare_digest(token, settings.internal_token)
    )


def is_websocket_authenticated(ws: WebSocket, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.auth_enabled:
        return True
    if not verify_session_cookie(ws.cookies.get(COOKIE_NAME), settings):
        return False
    origin = ws.headers.get("origin")
    return bool(origin) and origin.rstrip("/") in settings.allowed_origins


def internal_auth_headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.auth_enabled:
        return {}
    return {"Authorization": f"Bearer {settings.internal_token}"}


def _sign(body: str, secret: str) -> str:
    return _encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
