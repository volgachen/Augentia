import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        self.db_type: str = os.getenv("DB_TYPE", "mysql")
        self.mysql_host: str = os.getenv("MYSQL_HOST", "127.0.0.1")
        self.mysql_port: int = int(os.getenv("MYSQL_PORT", "3306"))
        self.mysql_user: str = os.getenv("MYSQL_USER", "root")
        self.mysql_password: str = os.getenv("MYSQL_PASSWORD", "")
        self.mysql_database: str = os.getenv("MYSQL_DATABASE", "augentia")
        self.sqlite_path: str = os.getenv("SQLITE_PATH", ".local/augentia.db")
        self.augentia_home: str = os.getenv(
            "AUGENTIA_HOME",
            os.path.join(os.path.expanduser("~"), ".augentia"),
        )
        self.worktree_root: str = os.getenv(
            "AUGENTIA_WORKTREE_ROOT",
            os.path.join(os.path.expanduser("~"), ".augentia", "worktrees"),
        )
        self.auth_enabled: bool = os.getenv("AUGENTIA_AUTH_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        self.auth_password_hash: str = os.getenv("AUGENTIA_AUTH_PASSWORD_HASH", "")
        self.session_secret: str = os.getenv("AUGENTIA_SESSION_SECRET", "")
        self.internal_token: str = os.getenv("AUGENTIA_INTERNAL_TOKEN", "")
        self.cookie_secure: bool = os.getenv("AUGENTIA_COOKIE_SECURE", "false").lower() in {
            "1", "true", "yes", "on",
        }
        self.session_ttl_seconds: int = int(os.getenv("AUGENTIA_SESSION_TTL_SECONDS", "43200"))
        self.allowed_origins: list[str] = [
            origin.strip().rstrip("/")
            for origin in os.getenv(
                "AUGENTIA_ALLOWED_ORIGINS",
                "http://127.0.0.1:12599,http://localhost:12599",
            ).split(",")
            if origin.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
