
import os
import logging

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
# SETTINGS
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

app = FastAPI(title="WonderPals AI Studio")

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

    return HTMLResponse(f"""
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
            .status {{
                color: {color};
                font-weight: bold;
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
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🌈 WonderPals AI Studio</h1>
            <h2>{title}</h2>
            <p class="status">{message}</p>
            <a href="/">Atpakaļ uz sākumu</a>
        </div>
    </body>
    </html>
    """)


# --------------------------------------------------
# GOOGLE OAUTH FLOW
# --------------------------------------------------

def create_oauth_flow(state=None, code_verifier=None):

    if not all([
        PROJECT_ID,
        CLIENT_ID,
        CLIENT_SECRET,
        REDIRECT_URI
    ]):
        raise RuntimeError("Trūkst Google OAuth konfigurācijas.")

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
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=(code_verifier is None)
    )

    flow.redirect_uri = REDIRECT_URI

    return flow


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home():

    return HTMLResponse("""
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
            <p>YouTube konta savienojuma iestatīšana.</p>
            <a href="/auth/login">Pieslēgt YouTube kontu</a>
        </div>
    </body>
    </html>
    """)


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

        # Saglabājam abus datus nākamajam solim.
        request.session["oauth_state"] = state
        request.session["oauth_code_verifier"] = flow.code_verifier

        return RedirectResponse(
            authorization_url,
            status_code=302
        )

    except Exception:
        logger.exception("Google autorizācijas sākšanas kļūda.")

        return page(
            "Autorizācija neizdevās",
            "Neizdevās sākt Google autorizāciju."
        )


# --------------------------------------------------
# GOOGLE OAUTH CALLBACK
# --------------------------------------------------

@app.get("/oauth2callback")
async def oauth_callback(request: Request):

    try:
        if request.query_params.get("error"):
            logger.error("Google autorizācija tika atteikta.")

            return page(
                "Autorizācija pārtraukta",
                "Google konta autorizācija netika pabeigta."
            )

        state = request.session.get("oauth_state")
        code_verifier = request.session.get("oauth_code_verifier")

        if not state or not code_verifier:
            return page(
                "Sesija beigusies",
                "Drošības dati nav pieejami. Sāc autorizāciju vēlreiz."
            )

        # Atjaunojam iepriekš saglabāto PKCE verifier.
        flow = create_oauth_flow(
            state=state,
            code_verifier=code_verifier
        )

        authorization_response = (
            f"{REDIRECT_URI}?{request.url.query}"
        )

        flow.fetch_token(
            authorization_response=authorization_response
        )

        credentials = flow.credentials

        if not credentials.refresh_token:
            logger.warning("Google neatgrieza refresh token.")

        # Saglabājam OAuth datus Secret Manager.
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

        logger.info("YouTube OAuth autorizācija veiksmīgi saglabāta.")

        return page(
            "Google konts veiksmīgi pieslēgts!",
            "YouTube autorizācija ir saglabāta.",
            success=True
        )

    except Exception:
        logger.exception("YouTube OAuth callback kļūda.")

        return page(
            "Autorizācija neizdevās",
            "Pārbaudi Cloud Run žurnālus un Google iestatījumus."
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
