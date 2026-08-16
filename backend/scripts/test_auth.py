import os

import _common  # noqa: F401

os.environ["AUGENTIA_AUTH_ENABLED"] = "true"
os.environ["AUGENTIA_SESSION_SECRET"] = "session-secret-0123456789-0123456789"
os.environ["AUGENTIA_INTERNAL_TOKEN"] = "internal-token-0123456789-0123456789"
os.environ["AUGENTIA_ALLOWED_ORIGINS"] = "http://testserver"

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import COOKIE_NAME, hash_password

os.environ["AUGENTIA_AUTH_PASSWORD_HASH"] = hash_password("test-password")

from app.config import get_settings

get_settings.cache_clear()

from app.main import app


def main() -> None:
    with TestClient(app) as client:
        unauthenticated = client.get("/api/v1/agents")
        assert unauthenticated.status_code == 401, unauthenticated.text
        try:
            with client.websocket_connect(
                "/api/v1/sessions/missing/stream",
                headers={"origin": "http://testserver"},
            ):
                raise AssertionError("unauthenticated websocket was accepted")
        except WebSocketDisconnect as error:
            assert error.code == 1008

        wrong = client.post("/api/v1/auth/login", json={"password": "wrong"})
        assert wrong.status_code == 401, wrong.text

        login = client.post("/api/v1/auth/login", json={"password": "test-password"})
        assert login.status_code == 200, login.text
        cookie = login.cookies.get(COOKIE_NAME)
        assert cookie

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["authenticated"] is True

        authenticated = client.get("/api/v1/agents")
        assert authenticated.status_code == 200, authenticated.text

        with client.websocket_connect(
            "/api/v1/sessions/missing/stream",
            headers={"origin": "http://testserver"},
        ) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "error"
        try:
            with client.websocket_connect(
                "/api/v1/sessions/missing/stream",
                headers={"origin": "http://malicious.example"},
            ):
                raise AssertionError("websocket with invalid origin was accepted")
        except WebSocketDisconnect as error:
            assert error.code == 1008

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 204, logout.text
        after_logout = client.get("/api/v1/agents")
        assert after_logout.status_code == 401, after_logout.text

        internal = client.get(
            "/api/v1/agents",
            headers={"Authorization": f"Bearer {os.environ['AUGENTIA_INTERNAL_TOKEN']}"},
        )
        assert internal.status_code == 200, internal.text

    print("auth integration checks passed")


if __name__ == "__main__":
    main()
