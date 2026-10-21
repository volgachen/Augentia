"""Shared helpers for standalone test scripts."""
import os
import sys
from pathlib import Path

# Make `app.*` importable when running `python scripts/foo.py` from backend/.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Load backend/.env so scripts driving adapters in-process see the same
# OPENAI_* and AUGENTIA_* vars the server uses.
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_DIR / ".env")
except ImportError:
    pass

# Apply the same logging config as the server so AUGENTIA_DEBUG /
# AUGENTIA_LOG_LEVEL control the in-process adapter scripts too.
# (No-op for scripts that only hit a running server — their logs come
#  from that other process.)
from app.logging_config import setup_logging  # noqa: E402
setup_logging()

BASE_URL = os.getenv("AUGENTIA_BASE_URL", "http://localhost:12598")
WS_BASE_URL = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")


def use_isolated_state(prefix: str = "augentia-test-") -> Path:
    """Point the in-process app at throwaway state before importing app.main.

    backend/.env usually names the developer's real database; hermetic scripts
    that drive the app in-process (TestClient / ASGITransport) must never write
    into it. Redirects the DB (temp SQLite), AUGENTIA_HOME (session configs,
    attachments) and the worktree root into a fresh temp dir.
    """
    import tempfile

    root = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["DB_TYPE"] = "sqlite"
    os.environ["SQLITE_PATH"] = str(root / "test.db")
    os.environ["AUGENTIA_HOME"] = str(root / "home")
    os.environ["AUGENTIA_WORKTREE_ROOT"] = str(root / "worktrees")
    from app.config import get_settings
    get_settings.cache_clear()
    return root


class Colors:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    DIM = "\033[2m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"{Colors.OK}[PASS]{Colors.END} {msg}")


def fail(msg: str) -> None:
    print(f"{Colors.FAIL}[FAIL]{Colors.END} {msg}")


def info(msg: str) -> None:
    print(f"{Colors.DIM}  {msg}{Colors.END}")


def section(msg: str) -> None:
    print(f"\n=== {msg} ===")
