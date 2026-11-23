"""Loads .env first, so import this before anything that reads environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See .env.example")
    return value


JWT_SECRET = need("JWT_SECRET")
TOKEN_ENC_KEY = need("TOKEN_ENC_KEY")

GOOGLE_CLIENT_SECRETS = os.environ.get("GOOGLE_CLIENT_SECRETS", str(BASE / "credentials.json"))
_render = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")  # Render sets this itself, e.g. https://opportune2.onrender.com
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip() or (
    f"{_render}/api/google/callback" if _render else "http://localhost:8000/api/google/callback")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", FRONTEND_URL)  # comma-separated
MAX_RUNS_PER_DAY = int(os.environ.get("MAX_RUNS_PER_DAY", "5"))

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
CV_ROOT = DATA_DIR / "cvs"
CV_ROOT.mkdir(parents=True, exist_ok=True)
SHOT_ROOT = DATA_DIR / "screenshots"  # email screenshots, one folder per user
SHOT_ROOT.mkdir(parents=True, exist_ok=True)
