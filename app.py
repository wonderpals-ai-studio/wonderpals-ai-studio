
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
from google import genai


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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-flash-latest"

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
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )

        request.session["oauth_state"] = state
        request.session["oauth_code_verifier"] = flow.code_verifier

        return RedirectResponse(authorization_url)

    except Exception:
        logger.exception("YouTube login could not be started.")
        return HTMLResponse(
            "Neizdevās sākt Google pieslēgšanos. Pārbaudi konfigurāciju.",
            status_code=500,
        )


@app.get("/oauth2callback")
async def oauth_callback(request: Request):
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(
            "Google autorizācija tika atcelta vai neizdevās.",
            status_code=400,
        )

    state = request.session.get("oauth_state")
    code_verifier = request.session.get("oauth_code_verifier")

    if not state or not code_verifier:
        return HTMLResponse(
            "Autorizācijas sesija beigusies. Lūdzu, mēģini vēlreiz.",
            status_code=400,
        )

    try:
        flow = create_oauth_flow(
            state=state,
            code_verifier=code_verifier,
        )

        flow.fetch_token(code=request.query_params.get("code"))

        save_youtube_token(flow.credentials)

        request.session["youtube_connected"] = True
        request.session.pop("oauth_state", None)
        request.session.pop("oauth_code_verifier", None)

        return RedirectResponse("/")

    except Exception:
        logger.exception("YouTube OAuth callback failed.")
        return HTMLResponse(
            "Neizdevās saglabāt YouTube autorizāciju. Pārbaudi Cloud Run žurnālus.",
            status_code=500,
        )


# -----------------------------
# GEMINI STĀSTU ĢENERĒŠANA
# -----------------------------

class StoryRequest(BaseModel):
    idea: str = Field(min_length=3, max_length=500)
    age: str = Field(pattern=r"^(3-5|6-8|9-12)$")
    length: str = Field(pattern=r"^(short|medium|long)$")


def generate_story(idea: str, age: str, length: str) -> str:
    import os
    import json
    import urllib.request
    import urllib.error

    hf_token = os.getenv("HF_TOKEN")

    if not hf_token:
        raise RuntimeError("Hugging Face token is not configured.")

    length_instructions = {
        "short": "About 250-350 words.",
        "medium": "About 500-700 words.",
        "long": "About 900-1200 words.",
    }

    max_tokens = {
        "short": 650,
        "medium": 1100,
        "long": 1800,
    }

    prompt = f"""
You are the children's story writer for WonderPals AI Studio.

Write a completely original, delightful story in English.

STORY IDEA:
{idea}

TARGET AGE:
{age} years old.

LENGTH:
{length_instructions[length]}

IMPORTANT RULES:
- Make the story suitable for the specified age group.
- Use original characters and original settings.
- Keep the story positive, kind, imaginative and entertaining.
- No graphic violence, frightening scenes, dangerous instructions,
  inappropriate content or adult themes.
- Do not use copyrighted characters or imitate existing franchises.
- Include a clear beginning, middle and satisfying ending.
- Include a gentle positive lesson without sounding preachy.
- Use simple language appropriate for the target age.
- Do not ask children for personal information.
- Output a title followed by the complete narration.
- Do not include production notes or explanations outside the story.
"""

    payload = {
        "model": "google/gemma-2-2b-it:fastest",
        "messages": [
            {
                "role": "system",
                "content": "You write safe, original children's stories."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": max_tokens[length],
        "temperature": 0.8
    }

    request = urllib.request.Request(
        "https://router.huggingface.co/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {hf_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))

        story = result["choices"][0]["message"]["content"]

        if not story or not story.strip():
            raise RuntimeError("Hugging Face returned an empty story.")

        return story.strip()

    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Hugging Face API error {error.code}: {error_body[:1000]}"
        ) from None

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not connect to Hugging Face: {error.reason}"
        ) from None

# -----------------------------
# GALVENĀ LAPA
# -----------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    connected = request.session.get("youtube_connected", False)

    youtube_status = (
        "✅ YouTube konts veiksmīgi pieslēgts!"
        if connected
        else "YouTube konts vēl nav pieslēgts."
    )

    html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WonderPals AI Studio</title>
<style>
body {
    margin: 0;
    padding: 30px 15px;
    background: #f1f4ff;
    color: #252747;
    font-family: Arial, sans-serif;
}
.container {
    max-width: 850px;
    margin: auto;
}
header, .panel {
    background: white;
    padding: 25px;
    border-radius: 18px;
    margin-bottom: 20px;
    box-shadow: 0 5px 20px rgba(0,0,0,.06);
}
h1 { color: #6555d9; }
h2 { margin-top: 0; }
label {
    display: block;
    margin: 16px 0 7px;
    font-weight: bold;
}
input, select {
    box-sizing: border-box;
    width: 100%;
    padding: 13px;
    border: 1px solid #ccd0e0;
    border-radius: 9px;
    font-size: 16px;
}
button {
    border: 0;
    border-radius: 9px;
    padding: 13px 20px;
    margin-top: 18px;
    font-size: 16px;
    font-weight: bold;
    cursor: pointer;
    background: #6555d9;
    color: white;
}
button:hover { opacity: .9; }
#result {
    white-space: pre-wrap;
    line-height: 1.7;
    overflow-wrap: anywhere;
}
#message { margin-top: 15px; }
.small { color: #777; font-size: 13px; }
</style>
</head>
<body>
<div class="container">

<header>
    <h1>🌈 WonderPals AI Studio</h1>
    <p>Your magical AI-powered children's story studio.</p>
    <p id="youtubeStatus">YOUTUBE_STATUS</p>
    <a href="/auth/login">
        <button type="button">Connect YouTube account</button>
    </a>
</header>

<section class="panel">
    <h2>✨ AI Story Generator</h2>
    <p>Create an original children's story in English.</p>

    <label for="idea">What should the story be about?</label>
    <input id="idea" maxlength="500"
        placeholder="Example: A little dragon who learns to share">

    <label for="age">Target age</label>
    <select id="age">
        <option value="3-5">3–5 years</option>
        <option value="6-8" selected>6–8 years</option>
        <option value="9-12">9–12 years</option>
    </select>

    <label for="length">Story length</label>
    <select id="length">
        <option value="short">Short</option>
        <option value="medium" selected>Medium</option>
        <option value="long">Long</option>
    </select>

    <button id="generateButton" type="button" onclick="generateStory()">
        ✨ Generate Story
    </button>

    <p id="message"></p>
</section>

<section class="panel">
    <h2>📖 Generated Story</h2>
    <div id="result">Your story will appear here...</div>
</section>

<p class="small">
    WonderPals AI Studio — Story generation stage.
</p>

</div>

<script>
async function generateStory() {
    const idea = document.getElementById("idea").value.trim();
    const age = document.getElementById("age").value;
    const length = document.getElementById("length").value;
    const button = document.getElementById("generateButton");
    const message = document.getElementById("message");
    const result = document.getElementById("result");

    if (idea.length < 3) {
        message.textContent = "Please enter a story idea.";
        return;
    }

    button.disabled = true;
    button.textContent = "Creating your story...";
    message.textContent = "✨ Gemini is writing...";
    result.textContent = "";

    try {
        const response = await fetch("/story/generate", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({idea, age, length})
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Story generation failed.");
        }

        result.textContent = data.story;
        message.textContent = "✅ Your story is ready!";

    } catch (error) {
        message.textContent = "❌ " + error.message;
    } finally {
        button.disabled = false;
        button.textContent = "✨ Generate Story";
    }
}
</script>
</body>
</html>
"""

    html = html.replace("YOUTUBE_STATUS", youtube_status)
    return HTMLResponse(html)


# -----------------------------
# HEALTH CHECK
# -----------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "WonderPals AI Studio",
        "gemini_configured": bool(GEMINI_API_KEY),
    }
