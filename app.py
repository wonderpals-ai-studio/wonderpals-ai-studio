import os
import json
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("wonderpals")

app = FastAPI(title="WonderPals AI Studio")

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    logger.warning("SESSION_SECRET is not configured; using an unsafe fallback.")
    SESSION_SECRET = "replace-this-session-secret"

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
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

HF_TOKEN = os.getenv("HF_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "google/gemma-2-2b-it:cheapest")
HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


# ---------------------------------------------------------
# GOOGLE / YOUTUBE OAUTH
# ---------------------------------------------------------

def create_oauth_flow(state: Optional[str] = None) -> Flow:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID "
            "and GOOGLE_CLIENT_SECRET in Cloud Run."
        )

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
        scopes=YOUTUBE_SCOPES,
        state=state,
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = REDIRECT_URI
    return flow


def save_youtube_token(credentials) -> None:
    """Store OAuth credentials as a new Secret Manager secret version."""
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{PROJECT_ID}/secrets/{YOUTUBE_TOKEN_SECRET}"
    payload = credentials.to_json().encode("utf-8")
    client.add_secret_version(
        request={"parent": parent, "payload": {"data": payload}}
    )


@app.get("/auth/login")
async def auth_login(request: Request):
    try:
        flow = create_oauth_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        request.session["oauth_state"] = state
        return RedirectResponse(authorization_url, status_code=302)
    except Exception:
        logger.exception("Could not start Google OAuth")
        return HTMLResponse(
            "Google autorizācija nav konfigurēta. Pārbaudi Cloud Run OAuth iestatījumus.",
            status_code=500,
        )


@app.get("/oauth2callback")
async def oauth_callback(request: Request):
    # Read the state saved when /auth/login started. Compare it with the
    # state returned by Google before exchanging the one-time authorization code.
    expected_state = request.session.get("oauth_state")
    returned_state = request.query_params.get("state")
    code = request.query_params.get("code")
    oauth_error = request.query_params.get("error")

    if oauth_error:
        logger.warning("Google OAuth was not completed; provider returned an error.")
        return HTMLResponse(
            "Google autorizācija tika atcelta vai noraidīta. Atgriezies sākumlapā un mēģini vēlreiz.",
            status_code=400,
        )

    if not expected_state or not returned_state or expected_state != returned_state:
        logger.warning("Google OAuth state validation failed.")
        return HTMLResponse(
            "Autorizācijas sesija nav derīga vai ir beigusies. Atgriezies sākumlapā un mēģini vēlreiz.",
            status_code=400,
        )

    if not code:
        logger.warning("Google OAuth callback did not contain an authorization code.")
        return HTMLResponse(
            "Google neatgrieza autorizācijas kodu. Atgriezies sākumlapā un mēģini vēlreiz.",
            status_code=400,
        )

    try:
        # PKCE is intentionally disabled in create_oauth_flow(). Pass the
        # authorization code directly so OAuthlib does not need to parse the
        # complete callback URL or look for a code_verifier in that URL.
        flow = create_oauth_flow(state=expected_state)
        flow.fetch_token(code=code)

        # Save first; only mark the browser session connected after Secret
        # Manager confirms that a new secret version was created.
        save_youtube_token(flow.credentials)

        request.session.pop("oauth_state", None)
        request.session["youtube_connected"] = True
        return RedirectResponse("/", status_code=303)
    except Exception as exc:
        # Do not log the callback URL, authorization code, or credentials.
        logger.error(
            "Google OAuth callback/token storage failed (%s).",
            type(exc).__name__,
            exc_info=True,
        )
        return HTMLResponse(
            "Neizdevās saglabāt YouTube autorizāciju. Pārbaudi Cloud Run žurnālus.",
            status_code=500,
        )


# ---------------------------------------------------------
# HUGGING FACE STORY GENERATION
# ---------------------------------------------------------

class StoryRequest(BaseModel):
    topic: str = Field(default="A friendly adventure", min_length=3, max_length=160)
    age_group: str = Field(default="All children", max_length=60)
    language: str = Field(default="English", max_length=40)
    duration_minutes: int = Field(default=3, ge=1, le=10)


async def generate_story(data: StoryRequest) -> str:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is not configured in Cloud Run.")

    system_prompt = (
        "You are a kind, imaginative writer of original stories for children. "
        "Keep the story age-appropriate, gentle, positive, inclusive, and safe. "
        "Avoid frightening, violent, sexual, hateful, or dangerous content. "
        "Use simple language and finish with a warm, reassuring ending."
    )
    user_prompt = (
        f"Write an original children's story in {data.language}. "
        f"Audience: {data.age_group}. Topic: {data.topic}. "
        f"Target length: about {data.duration_minutes} minutes when read aloud. "
        "Give it a short title. Return only the title and story."
    )

    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": HF_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 1800,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(HF_CHAT_URL, headers=headers, json=payload)

    if response.status_code >= 400:
        # Log only status and a short provider response; never log the token.
        logger.error("Hugging Face request failed: HTTP %s: %s",
                     response.status_code, response.text[:1000])
        raise RuntimeError(
            f"Hugging Face API returned HTTP {response.status_code}. "
            "Check HF access, model availability, and provider credits."
        )

    result = response.json()
    try:
        story = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.error("Unexpected Hugging Face response: %s",
                     json.dumps(result)[:1000])
        raise RuntimeError("Hugging Face returned an unexpected response.")

    if not story:
        raise RuntimeError("Hugging Face returned an empty story.")
    return story


@app.post("/story/generate")
async def story_generate(data: StoryRequest):
    try:
        story = await generate_story(data)
        return JSONResponse({"ok": True, "story": story})
    except Exception as exc:
        logger.exception("Story generation failed")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=502,
        )


# ---------------------------------------------------------
# HEALTH CHECKS AND HOME PAGE
# ---------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "huggingface_configured": bool(HF_TOKEN),
        "youtube_oauth_configured": bool(CLIENT_ID and CLIENT_SECRET),
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    connected = bool(request.session.get("youtube_connected"))
    youtube_status = (
        "YouTube autorizēts šajā pārlūka sesijā"
        if connected else "YouTube vēl nav autorizēts"
    )

    html = """
<!doctype html>
<html lang="lv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WonderPals AI Studio</title>
  <style>
    body{font-family:Arial,sans-serif;background:#f4f6fb;color:#20243a;
         max-width:850px;margin:35px auto;padding:0 18px}
    .card{background:white;padding:24px;border-radius:16px;
          box-shadow:0 4px 18px #18204412;margin:18px 0}
    input,select,button{font:inherit;padding:12px;border-radius:9px;
          border:1px solid #ccd2e0;width:100%;box-sizing:border-box;margin:7px 0}
    button{background:#5556d9;color:white;border:0;cursor:pointer}
    a{color:#5556d9}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}
    .muted{color:#68708a}
  </style>
</head>
<body>
  <h1>🌈 WonderPals AI Studio</h1>
  <p class="muted">Bērniem draudzīgu stāstu veidošanas studija</p>
  <section class="card">
    <h2>📺 YouTube savienojums</h2>
    <p id="ytstatus">__YT_STATUS__</p>
    <a href="/auth/login">Savienot Google / YouTube kontu</a>
    <p class="muted">Autorizācija ļauj sagatavot piekļuvi YouTube augšupielādei.
    Šī versija pati video nepublicē.</p>
  </section>
  <section class="card">
    <h2>📖 Ģenerēt stāstu</h2>
    <label>Tēma</label>
    <input id="topic" value="A little star who learns to help others">
    <label>Mērķauditorija</label>
    <select id="age">
      <option>All children</option>
      <option>Preschool children</option>
      <option>Children aged 5-8</option>
      <option>Children aged 9-12</option>
    </select>
    <label>Valoda</label>
    <select id="language"><option>English</option><option>Latvian</option></select>
    <label>Garums (minūtēs)</label>
    <select id="duration">
      <option>2</option><option selected>3</option><option>5</option>
      <option>7</option><option>10</option>
    </select>
    <button id="generate" onclick="makeStory()">✨ Ģenerēt stāstu</button>
    <p id="message" class="muted"></p>
    <pre id="story"></pre>
  </section>
  <script>
    async function makeStory(){
      const button=document.getElementById('generate');
      const message=document.getElementById('message');
      const output=document.getElementById('story');
      button.disabled=true;
      message.textContent='✨ Hugging Face raksta stāstu...';
      output.textContent='';
      try{
        const response=await fetch('/story/generate',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            topic:document.getElementById('topic').value,
            age_group:document.getElementById('age').value,
            language:document.getElementById('language').value,
            duration_minutes:Number(document.getElementById('duration').value)
          })
        });
        const data=await response.json();
        if(!response.ok || !data.ok) throw new Error(data.error||'Neizdevās ģenerēt stāstu');
        output.textContent=data.story;
        message.textContent='Stāsts ir gatavs!';
      }catch(error){
        message.textContent='Kļūda: '+error.message;
      }finally{
        button.disabled=false;
      }
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(html.replace("__YT_STATUS__", youtube_status))
