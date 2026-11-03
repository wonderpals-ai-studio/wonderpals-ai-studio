
import os
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wonderpals")


# --------------------------------------------------
# GOOGLE CLOUD SETTINGS
# --------------------------------------------------

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SESSION_SECRET = os.getenv("SESSION_SECRET")

TOKEN_SECRET = os.getenv(
    "YOUTUBE_TOKEN_SECRET",
    "youtube-oauth-token"
)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload"
]


# --------------------------------------------------
# APPLICATION
# --------------------------------------------------

if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET nav konfigurēts.")

app = FastAPI(
    title="WonderPals AI Studio",
    version="1.0.0"
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=True,
    max_age=3600
)


# --------------------------------------------------
# HTML PAGE
# --------------------------------------------------

def page(title, message, success=False):
    color = "#16a34a" if success else "#dc2626"

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html lang="lv">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{title}</title>
            <style>
                body {{
                    background: #101827;
                    color: white;
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px 20px;
                }}
                .card {{
                    max-width: 550px;
                    margin: auto;
                    padding: 30px;
                    background: #1e293b;
                    border-radius: 16px;
                }}
                a {{
                    display: inline-block;
                    margin-top: 20px;
                    padding: 14px 22px;
                    background: #2563eb;
                    color: white;
                    text-decoration: none;
                    border-radius: 8px;
                }}
                .status {{
                    color: {color};
                    font-weight: bold;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>WonderPals AI Studio</h1>
                <h2>{title}</h2>
                <p class="status">{message}</p>
                <a href="/">Atpakaļ uz sākumu</a>
            </div>
        </body>
        </html>
        """
    )


# --------------------------------------------------
# GOOGLE OAUTH FLOW
# --------------------------------------------------

def create_oauth_flow(state=None):

    if not CLIENT_ID:
        raise RuntimeError("Trūkst GOOGLE_CLIENT_ID.")

    if not CLIENT_SECRET:
        raise RuntimeError("Trūkst GOOGLE_CLIENT_SECRET.")

    if not REDIRECT_URI:
        raise RuntimeError("Trūkst GOOGLE_REDIRECT_URI.")

    if not PROJECT_ID:
        raise RuntimeError("Trūkst GOOGLE_CLOUD_PROJECT.")

    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state
    )

    flow.redirect_uri = REDIRECT_URI

    return flow


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home():

    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html lang="lv">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>WonderPals AI Studio</title>
            <style>
                body {
                    background: #101827;
                    color: white;
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px 20px;
                }
                .card {
                    max-width: 600px;
                    margin: auto;
                    padding: 35px;
                    background: #1e293b;
                    border-radius: 16px;
                }
                a {
                    display: inline-block;
                    margin-top: 20px;
                    padding: 15px 25px;
                    background: #2563eb;
                    color: white;
                    text-decoration: none;
                    border-radius: 8px;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>🌈 WonderPals AI Studio</h1>
                <p>Automatizēta bērnu YouTube kanāla platforma.</p>
                <p>Pašlaik konfigurējam YouTube savienojumu.</p>
                <a href="/auth/login">Pieslēgt YouTube kontu</a>
            </div>
        </body>
        </html>
        """
    )


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "project": "WonderPals AI Studio"
    }


# --------------------------------------------------
# START GOOGLE AUTHORIZATION
# --------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):

    try:
        flow = create_oauth_flow()

        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )

        request.session["oauth_state"] = state

        return RedirectResponse(
            authorization_url,
            status_code=302
        )

    except Exception:
        logger.exception("Google autorizācijas sākšanas kļūda.")

        return page(
            "Autorizācija neizdevās",
            "Pārbaudi Google Cloud iestatījumus un slepenos datus."
        )


# --------------------------------------------------
# GOOGLE OAUTH CALLBACK
# --------------------------------------------------

@app.get("/oauth2callback")
async def oauth_callback(request: Request):

    try:
        oauth_error = request.query_params.get("error")

        if oauth_error:
            logger.error("Google OAuth atgrieza kļūdu: %s", oauth_error)

            return page(
                "Autorizācija tika pārtraukta",
                "Google konta autorizācija netika pabeigta."
            )

        state = request.session.get("oauth_state")

        if not state:
            return page(
                "Sesija beigusies",
                "Lūdzu, mēģini pieslēgt kontu vēlreiz."
            )

        flow = create_oauth_flow(state=state)

        # Izmantojam konfigurēto HTTPS adresi,
        # nevis Cloud Run iekšējo HTTP adresi.
        authorization_response = (
            f"{REDIRECT_URI}?{request.url.query}"
        )

        flow.fetch_token(
            authorization_response=authorization_response
        )

        credentials = flow.credentials

        if not credentials.refresh_token:
            logger.warning(
                "Google neatgrieza refresh token."
            )

        # Saglabājam OAuth datus Google Secret Manager.
        client = secretmanager.SecretManagerServiceClient()

        parent = (
            f"projects/{PROJECT_ID}/secrets/{TOKEN_SECRET}"
        )

        payload = credentials.to_json().encode("UTF-8")

        client.add_secret_version(
            request={
                "parent": parent,
                "payload": {
                    "data": payload
                }
            }
        )

        request.session.clear()

        logger.info(
            "YouTube OAuth autorizācija veiksmīgi saglabāta."
        )

        return page(
            "Google konts veiksmīgi pieslēgts!",
            "YouTube autorizācija ir saglabāta Google Secret Manager.",
            success=True
        )

    except Exception:
        logger.exception("YouTube OAuth callback kļūda.")

        return page(
            "Autorizācija neizdevās",
            "Pārbaudi Cloud Run žurnālus, Secret Manager un OAuth iestatījumus."
        )


# --------------------------------------------------
# ERROR HANDLER
# --------------------------------------------------

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):

    logger.exception("Neapstrādāta lietotnes kļūda.")

    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": "Radās servera kļūda."
        }
    )
