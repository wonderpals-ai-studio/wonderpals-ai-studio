
import os
import json
import logging
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager


# -----------------------------
# KONFIGURĀCIJA
# -----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "change-this-secret-before-production"),
    same_site="lax",
    https_only=True,
)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "wonderpals-ai-studio-513606")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://wonderpals-ai-studio-138820800509.europe-north1.run.app/oauth2callback",
)

YOUTUBE_TOKEN_SECRET = os.getenv("YOUTUBE_TOKEN_SECRET", "youtube-oauth-token")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


# -----------------------------
# GOOGLE YOUTUBE OAUTH
# -----------------------------

def create_oauth_flow(state=None, code_verifier=None):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Google OAuth credentials are not configured.")

    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=(code_verifier is None),
    )

    flow.redirect_uri = REDIRECT_URI
    return flow


def save_youtube_token(credentials):
    client = secretmanager.SecretManagerServiceClient()

    parent = f"projects/{PROJECT_ID}/secrets/{YOUTUBE_TOKEN_SECRET}"

    payload = credentials.to_json().encode("UTF-8")

    client.add_secret_version(
        request={
            "parent": parent,
            "payload": {"data": payload},
        }
    )


@app.get("/auth/login")
async def auth_login(request: Request):
    try:
        flow = create_oauth_flow()

        authorization_url, state = flow.authorization_url(
