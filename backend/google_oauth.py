import json
import secrets
import time

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

import config
import db
import security

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# state -> (user_id, code_verifier, expires_at). In-memory: use Redis if you run several workers.
_pending: dict[str, tuple[int, str, float]] = {}


def _flow() -> Flow:
    return Flow.from_client_secrets_file(
        config.GOOGLE_CLIENT_SECRETS, scopes=SCOPES, redirect_uri=config.GOOGLE_REDIRECT_URI
    )


def start(user_id: int) -> str:
    now = time.time()
    for key in [k for k, v in _pending.items() if v[2] < now]:
        del _pending[key]
    flow = _flow()
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    _pending[state] = (user_id, flow.code_verifier, now + 600)
    return url


def finish(state: str, code: str) -> int:
    """The state value was issued to a signed-in user in start(), so it identifies who is connecting."""
    entry = _pending.pop(state, None)
    if not entry or entry[2] < time.time():
        raise ValueError("The Google sign-in expired. Try connecting again")
    user_id = entry[0]
    flow = _flow()
    flow.code_verifier = entry[1]
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise ValueError("Google did not return offline access. Remove Opportune from your Google account permissions and connect again")
    if not set(SCOPES) <= set(creds.scopes or []):
        raise ValueError("Both Calendar and Gmail access are needed. Connect again and leave both boxes ticked")
    db.save_google_token(user_id, security.encrypt(creds.to_json()))
    return user_id


def fresh_token_json(user_id: int) -> str | None:
    """Returns a token JSON whose access token is valid, refreshing and re-saving it if needed."""
    enc = db.get_google_token(user_id)
    if not enc:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(security.decrypt(enc)), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        db.save_google_token(user_id, security.encrypt(creds.to_json()))
    return creds.to_json()


def disconnect(user_id: int):
    enc = db.get_google_token(user_id)
    if enc:
        info = json.loads(security.decrypt(enc))
        token = info.get("refresh_token") or info.get("token")
        try:  # best effort: revoke at Google too
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": token}, timeout=5)
        except Exception:
            pass
    db.delete_google_token(user_id)
