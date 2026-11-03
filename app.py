import os
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager

app = FastAPI(title="WonderPals AI Studio")

logging.basicConfig(level=logging.INFO)

# YouTube augšupielādes atļauja
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload"
]

# Sesijas atslēga būs jāiestata Cloud Run vidē
SESSION_SECRET = os.getenv("SESSION_SECRET", "temporary-not-secure")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax"
)


def get_oauth_settings():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")

    if not all([client_id, client_secret, redirect_uri]):
        raise ValueError("Google OAuth iestatījumi nav pilnībā konfigurēti.")

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }

    return client_config, redirect_uri


def create_flow(state=None):
    client_config, redirect_uri = get_oauth_settings()

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state
    )
    flow.redirect_uri = redirect_uri
    return flow


@app.get("/")
def home():
    return {
        "project": "WonderPals AI Studio",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/auth/login")
def auth_login(request: Request):
    if SESSION_SECRET == "temporary-not-secure":
        return HTMLResponse(
            "Serverim vēl nav iestatīta droša SESSION_SECRET vērtība.",
            status_code=503
        )

    try:
        flow = create_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true"
        )

        request.session["oauth_state"] = state

        from fastapi.responses import RedirectResponse
        return RedirectResponse(authorization_url)

    except Exception:
        logging.exception("OAuth sākšanas kļūda")
        return HTMLResponse(
            "Neizdevās sākt Google autorizāciju. Pārbaudi servera iestatījumus.",
            status_code=500
        )


@app.get("/oauth2callback")
def oauth_callback(request: Request):
    if request.query_params.get("error"):
        return HTMLResponse(
            "Google autorizācija tika atcelta vai noraidīta."
        )

    saved_state = request.session.pop("oauth_state", None)
    returned_state = request.query_params.get("state")

    if not saved_state or saved_state != returned_state:
        return HTMLResponse(
            "Autorizācijas pārbaude neizdevās. Lūdzu, mēģini vēlreiz.",
            status_code=400
        )

    try:
        flow = create_flow(state=saved_state)
        flow.fetch_token(authorization_response=str(request.url))

        credentials = flow.credentials

        if not credentials.refresh_token:
            return HTMLResponse(
                "Google neatgrieza atjaunošanas tokenu. "
                "Atcel lietotnes piekļuvi savā Google kontā un mēģini autorizēties vēlreiz."
            )

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        secret_name = os.getenv(
            "YOUTUBE_TOKEN_SECRET",
            "youtube-oauth-token"
        )

        if not project_id:
            raise ValueError("Trūkst GOOGLE_CLOUD_PROJECT iestatījuma.")

        client = secretmanager.SecretManagerServiceClient()

        parent = f"projects/{project_id}/secrets/{secret_name}"

        client.add_secret_version(
            request={
                "parent": parent,
                "payload": {
                    "data": credentials.to_json().encode("UTF-8")
                }
            }
        )

        return HTMLResponse(
            "<h2>Google konts veiksmīgi pieslēgts!</h2>"
            "<p>YouTube autorizācija ir saglabāta drošajā Google Secret Manager.</p>"
            "<p>Vari aizvērt šo lapu.</p>"
        )

    except Exception:
        logging.exception("OAuth callback kļūda")
        return HTMLResponse(
            "Autorizācija neizdevās. Pārbaudi Cloud Run iestatījumus un Secret Manager atļaujas.",
            status_code=500
        )
