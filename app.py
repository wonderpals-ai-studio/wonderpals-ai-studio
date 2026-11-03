import os
import json
import logging
import os
import tempfile
import subprocess
import wave
import math
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest


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

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
PIPER_ENABLED = os.getenv("PIPER_ENABLED", "true").lower() == "true"
PIPER_VOICES = {
    "narrator": "/voices/en_US-lessac-medium.onnx",
    "rabbit": "/voices/en_US-amy-medium.onnx",
    "turtle": "/voices/en_US-ryan-high.onnx",
}

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


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


def load_youtube_credentials() -> Credentials:
    """Load the saved OAuth credentials from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = (
        f"projects/{PROJECT_ID}/secrets/{YOUTUBE_TOKEN_SECRET}/versions/latest"
    )
    result = client.access_secret_version(request={"name": name})
    info = json.loads(result.payload.data.decode("utf-8"))
    return Credentials.from_authorized_user_info(info, scopes=YOUTUBE_SCOPES)


async def verify_youtube_connection() -> dict:
    """Refresh the saved token if needed and verify it against YouTube."""
    credentials = load_youtube_credentials()

    if not credentials.valid:
        if not credentials.refresh_token:
            raise RuntimeError("Saglabātajam tokenam nav refresh_token.")
        credentials.refresh(GoogleAuthRequest())
        # Keep the refreshed token in Secret Manager.
        save_youtube_token(credentials)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {credentials.token}"},
        )

    if response.status_code == 401:
        raise RuntimeError("YouTube noraidīja tokenu (401). Nepieciešama atkārtota autorizācija.")
    if response.status_code == 403:
        # YouTube returns a structured reason such as insufficientPermissions,
        # quotaExceeded, or accessNotConfigured. Keep the diagnostic bounded.
        try:
            error_info = response.json().get("error", {})
            details = error_info.get("errors", [])
            reason = details[0].get("reason", "unknown") if details else "unknown"
            message = error_info.get("message", "YouTube API denied the request.")
        except (ValueError, AttributeError, TypeError):
            reason = "unknown"
            message = "YouTube API denied the request."
        logger.error("YouTube API returned 403; reason=%s; message=%s", reason, message[:500])
        raise RuntimeError(f"YouTube API 403 ({reason}): {message[:300]}")
    response.raise_for_status()

    data = response.json()
    channels = data.get("items", [])
    if not channels:
        raise RuntimeError("Tokens darbojas, bet šim Google kontam nav atrasts YouTube kanāls.")

    channel = channels[0].get("snippet", {})
    return {
        "ok": True,
        "channel_title": channel.get("title", "YouTube kanāls"),
        "message": "YouTube savienojums darbojas.",
    }


@app.get("/youtube/status")
async def youtube_status(request: Request):
    # This endpoint verifies the saved token with the actual YouTube API.
    try:
        result = await verify_youtube_connection()
        request.session["youtube_connected"] = True
        return JSONResponse(result)
    except Exception as exc:
        request.session["youtube_connected"] = False
        logger.error(
            "YouTube connection verification failed (%s).",
            type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=200,
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
    # Validate the OAuth state stored when /auth/login was started.
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
        # PKCE is disabled in create_oauth_flow(); exchange the code directly.
        # This avoids parsing the full callback URL and looking for a verifier.
        flow = create_oauth_flow(state=expected_state)
        flow.fetch_token(code=code)

        # Mark the session connected only after Secret Manager saves the token.
        save_youtube_token(flow.credentials)
        request.session.pop("oauth_state", None)
        request.session["youtube_connected"] = True
        return RedirectResponse("/", status_code=303)
    except Exception as exc:
        # Never log the callback URL, authorization code, or credentials.
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
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured in Cloud Run.")

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
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_completion_tokens": 1800,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(GROQ_CHAT_URL, headers=headers, json=payload)

    if response.status_code >= 400:
        # Log provider response for diagnosis, but never log the API key.
        logger.error(
            "Groq request failed: HTTP %s: %s",
            response.status_code,
            response.text[:1000],
        )
        if response.status_code == 401:
            raise RuntimeError("Groq rejected the API key. Check the GROQ_API_KEY Secret in Cloud Run.")
        if response.status_code == 429:
            raise RuntimeError("Groq rate limit or quota reached. Wait and try again; check your Groq usage limits.")
        raise RuntimeError(f"Groq API returned HTTP {response.status_code}. Check Cloud Run logs for details.")

    result = response.json()
    try:
        story = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.error("Unexpected Groq response: %s", json.dumps(result)[:1000])
        raise RuntimeError("Groq returned an unexpected response.")

    if not story:
        raise RuntimeError("Groq returned an empty story.")
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



class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    voice: str = Field(default="narrator", pattern="^(narrator|rabbit|turtle)$")




async def make_piper_wav(text: str, voice: str, output_path: str) -> None:
    if not PIPER_ENABLED:
        raise RuntimeError("Piper balss ģenerēšana ir izslēgta.")
    voice_model = PIPER_VOICES[voice]
    process = await __import__("asyncio").create_subprocess_exec(
        "piper", "--model", voice_model, "--output_file", output_path,
        stdin=__import__("asyncio").subprocess.PIPE,
        stdout=__import__("asyncio").subprocess.PIPE,
        stderr=__import__("asyncio").subprocess.PIPE,
    )
    _, stderr = await process.communicate(text.encode("utf-8"))
    if process.returncode != 0 or not Path(output_path).exists() or Path(output_path).stat().st_size < 1000:
        raise RuntimeError(stderr.decode("utf-8", errors="replace")[-500:] or "Piper audio kļūda.")

@app.post("/speech/generate")
async def speech_generate(data: SpeechRequest):
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="wonderpals_", suffix=".wav", delete=False) as wav_file:
            output_path = wav_file.name
        await make_piper_wav(data.text, data.voice, output_path)
        return FileResponse(
            output_path, media_type="audio/wav", filename="wonderpals_story.wav",
            background=__import__("starlette.background", fromlist=["BackgroundTask"]).BackgroundTask(lambda: Path(output_path).unlink(missing_ok=True)),
        )
    except Exception as exc:
        if output_path:
            Path(output_path).unlink(missing_ok=True)
        logger.exception("Piper speech generation failed")
        return JSONResponse({"ok": False, "error": f"Neizdevās izveidot balsi: {str(exc)[:300]}"}, status_code=502)


@app.post("/video/create")
async def video_create(
    story: str = Form(...),
    voice: str = Form("narrator"),
    scenes: list[UploadFile] = File(...),
):
    """Create an MP4 from user-provided 3D stills, narration and licensed background music."""
    work = Path(tempfile.mkdtemp(prefix="wonderpals_video_"))
    try:
        if not story.strip() or len(story) > 12000:
            raise RuntimeError("Stāsts ir tukšs vai pārāk garš.")
        if voice not in PIPER_VOICES:
            raise RuntimeError("Izvēlētā balss nav derīga.")
        valid_scenes = []
        for idx, upload in enumerate(scenes[:12]):
            suffix = Path(upload.filename or "scene.jpg").suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            target = work / f"scene_{idx:02d}{suffix}"
            content = await upload.read()
            if len(content) > 12 * 1024 * 1024:
                continue
            target.write_bytes(content)
            if content:
                valid_scenes.append(target)
        if not valid_scenes:
            raise RuntimeError("Pievieno vismaz vienu 3D ainas attēlu (JPG, PNG vai WEBP).")

        voice_path = work / "narration.wav"
        await make_piper_wav(story, voice, str(voice_path))
        with wave.open(str(voice_path), "rb") as wf:
            duration = wf.getnframes() / float(wf.getframerate())
        duration = max(5.0, duration)
        per_scene = duration / len(valid_scenes)
        clip_paths = []
        for idx, image_path in enumerate(valid_scenes):
            clip = work / f"clip_{idx:02d}.mp4"
            frames = max(150, int(math.ceil(per_scene * 30)))
            vf = (
                "scale=1600:900:force_original_aspect_ratio=increase,"
                "crop=1600:900,setsar=1,"
                f"zoompan=z='min(zoom+0.0007,1.12)':d={frames}:s=1280x720:fps=30,"
                "format=yuv420p"
            )
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image_path), "-vf", vf,
                   "-t", f"{per_scene:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
                   "-pix_fmt", "yuv420p", str(clip)]
            proc = await __import__("asyncio").create_subprocess_exec(*cmd, stdout=__import__("asyncio").subprocess.PIPE, stderr=__import__("asyncio").subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError("FFmpeg nevarēja izveidot ainas video: " + err.decode(errors="replace")[-400:])
            clip_paths.append(clip)

        concat_file = work / "clips.txt"
        concat_file.write_text("\n".join("file '" + str(x) + "'" for x in clip_paths), encoding="utf-8")
        silent_video = work / "silent.mp4"
        proc = await __import__("asyncio").create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(silent_video),
            stdout=__import__("asyncio").subprocess.PIPE, stderr=__import__("asyncio").subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("Neizdevās apvienot ainas: " + err.decode(errors="replace")[-400:])

        music_path = Path("assets/music/background.mp3")
        final_path = work / "wonderpals_video.mp4"
        if music_path.exists():
            audio_inputs = ["-i", str(voice_path), "-stream_loop", "-1", "-i", str(music_path)]
            filter_complex = "[0:a]volume=1.0[voice];[1:a]volume=0.16[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            cmd = ["ffmpeg", "-y", "-i", str(silent_video), *audio_inputs, "-filter_complex", filter_complex,
                   "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-shortest", "-movflags", "+faststart", str(final_path)]
        else:
            cmd = ["ffmpeg", "-y", "-i", str(silent_video), "-i", str(voice_path), "-map", "0:v:0", "-map", "1:a:0",
                   "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(final_path)]
        proc = await __import__("asyncio").create_subprocess_exec(*cmd, stdout=__import__("asyncio").subprocess.PIPE, stderr=__import__("asyncio").subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not final_path.exists():
            raise RuntimeError("Neizdevās izveidot MP4: " + err.decode(errors="replace")[-500:])
        from starlette.background import BackgroundTask
        return FileResponse(str(final_path), media_type="video/mp4", filename="wonderpals_story.mp4", background=BackgroundTask(lambda: __import__("shutil").rmtree(work, ignore_errors=True)))
    except Exception as exc:
        logger.exception("Video creation failed")
        __import__("shutil").rmtree(work, ignore_errors=True)
        return JSONResponse({"ok": False, "error": str(exc)[:700]}, status_code=502)


# ---------------------------------------------------------
# HEALTH CHECKS AND HOME PAGE
# ---------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "groq_configured": bool(GROQ_API_KEY),
        "piper_enabled": PIPER_ENABLED,
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
    <button type="button" id="checkYoutube" onclick="checkYouTube()">Pārbaudīt YouTube savienojumu</button>
    <p id="ytcheckresult" class="muted">Vēl nav veikta tokena pārbaude.</p>
    <a href="/auth/login">Savienot Google / YouTube kontu</a>
    <p class="muted">Pārbaude sazinās ar YouTube API un pārbauda saglabāto tokenu.
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
    <hr>
    <h3>🔊 Pārvērst stāstu balsī</h3>
    <label>Izvēlies balsi</label>
    <select id="voice">
      <option value="narrator">Stāstnieks — mierīga balss</option>
      <option value="rabbit">Trusis — gaišāka balss</option>
      <option value="turtle">Bruņurupucis — zemāka balss</option>
    </select>
    <button id="speak" onclick="makeAudio()">🎙️ Izveidot audio</button>
    <p id="audioMessage" class="muted">Audio tiek veidots tikai pēc pogas nospiešanas.</p>
    <audio id="audioPlayer" controls style="width:100%;display:none"></audio>
    <hr>
    <h3>🎬 Izveidot 3D stāsta video</h3>
    <p class="muted">Pievieno 3D ainu attēlus (JPG, PNG vai WEBP). Video būs ar lēnu kameras kustību, stāstītāja balsi un, ja fails ir pieejams, fona mūziku.</p>
    <label>3D ainu attēli (1–12)</label>
    <input id="sceneImages" type="file" accept="image/jpeg,image/png,image/webp" multiple>
    <button id="makeVideo" onclick="makeVideo()">🎬 Izveidot MP4 video</button>
    <p id="videoMessage" class="muted">Vispirms ģenerē stāstu un pievieno tam atbilstošus 3D attēlus.</p>
    <video id="videoPlayer" controls style="width:100%;display:none"></video>
    <a id="videoDownload" style="display:none" download="wonderpals_story.mp4">Lejupielādēt MP4 video</a>
  </section>
  <script>
    async function checkYouTube(){
      const button=document.getElementById('checkYoutube');
      const result=document.getElementById('ytcheckresult');
      button.disabled=true;
      result.textContent='Pārbauda saglabāto tokenu YouTube...';
      try{
        const response=await fetch('/youtube/status');
        const data=await response.json();
        if(data.ok){
          result.textContent='✅ SAVIENOJUMS DARBOJAS — kanāls: '+data.channel_title;
          result.style.color='green';
          document.getElementById('ytstatus').textContent='YouTube savienojums pārbaudīts un darbojas';
        }else{
          result.textContent='❌ SAVIENOJUMS NEDARBOJAS — '+data.message;
          result.style.color='crimson';
          document.getElementById('ytstatus').textContent='YouTube savienojums nav pārbaudīts vai nedarbojas';
        }
      }catch(error){
        result.textContent='❌ Pārbaudes kļūda: '+error.message;
        result.style.color='crimson';
      }finally{
        button.disabled=false;
      }
    }

    async function makeAudio(){
      const story=document.getElementById('story').textContent.trim();
      const button=document.getElementById('speak');
      const message=document.getElementById('audioMessage');
      const player=document.getElementById('audioPlayer');
      if(!story){message.textContent='Vispirms ģenerē stāstu.';return;}
      button.disabled=true; message.textContent='🎙️ Veido audio. Pirmajā reizē balss modelis var lejupielādēties...';
      player.style.display='none';
      try{
        const response=await fetch('/speech/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:story,voice:document.getElementById('voice').value})});
        if(!response.ok){let err={};try{err=await response.json();}catch(e){}throw new Error(err.error||'Audio izveide neizdevās');}
        const blob=await response.blob();
        if(window.currentAudioUrl) URL.revokeObjectURL(window.currentAudioUrl);
        window.currentAudioUrl=URL.createObjectURL(blob);player.src=window.currentAudioUrl;player.style.display='block';
        message.textContent='Audio gatavs! Nospied atskaņošanas pogu.';
      }catch(error){message.textContent='Kļūda: '+error.message;}
      finally{button.disabled=false;}
    }

    async function makeVideo(){
      const story=document.getElementById('story').textContent.trim();
      const files=document.getElementById('sceneImages').files;
      const button=document.getElementById('makeVideo');
      const message=document.getElementById('videoMessage');
      if(!story){message.textContent='Vispirms ģenerē stāstu.';return;}
      if(!files.length){message.textContent='Pievieno vismaz vienu 3D ainas attēlu.';return;}
      if(files.length>12){message.textContent='Pievieno ne vairāk kā 12 attēlus.';return;}
      const form=new FormData();form.append('story',story);form.append('voice',document.getElementById('voice').value);
      for(const file of files)form.append('scenes',file);
      button.disabled=true;message.textContent='🎬 Veido video. Tas var aizņemt dažas minūtes...';
      try{
        const response=await fetch('/video/create',{method:'POST',body:form});
        if(!response.ok){let err={};try{err=await response.json();}catch(e){}throw new Error(err.error||'Video izveide neizdevās');}
        const blob=await response.blob();if(window.currentVideoUrl)URL.revokeObjectURL(window.currentVideoUrl);
        window.currentVideoUrl=URL.createObjectURL(blob);
        const player=document.getElementById('videoPlayer');player.src=window.currentVideoUrl;player.style.display='block';
        const link=document.getElementById('videoDownload');link.href=window.currentVideoUrl;link.style.display='inline-block';
        message.textContent='✅ Video gatavs! Pārbaudi to un lejupielādē MP4.';
      }catch(error){message.textContent='Kļūda: '+error.message;}finally{button.disabled=false;}
    }

    async function makeStory(){
      const button=document.getElementById('generate');
      const message=document.getElementById('message');
      const output=document.getElementById('story');
      button.disabled=true;
      message.textContent='✨ Groq raksta stāstu...';
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
