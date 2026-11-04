
import os
import json
import math
import wave
import shutil
import logging
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, FileResponse
)
from starlette.middleware.sessions import SessionMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from google_auth_oauthlib.flow import Flow
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest


# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("wonderpals")

app = FastAPI(title="WonderPals AI Studio")

SESSION_SECRET = os.getenv("SESSION_SECRET", "replace-this-secret")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=True,
)

PROJECT_ID = os.getenv(
    "GOOGLE_CLOUD_PROJECT",
    "wonderpals-ai-studio-513606"
)

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://wonderpals-ai-studio-138820800509.europe-north1.run.app/oauth2callback"
)

YOUTUBE_TOKEN_SECRET = os.getenv(
    "YOUTUBE_TOKEN_SECRET",
    "youtube-oauth-token"
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

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


# =========================================================
# YOUTUBE OAUTH
# =========================================================

def create_oauth_flow(state: Optional[str] = None):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Google OAuth nav konfigurēts.")

    config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    flow = Flow.from_client_config(
        config,
        scopes=YOUTUBE_SCOPES,
        state=state,
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = REDIRECT_URI
    return flow


def save_youtube_token(credentials):
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{PROJECT_ID}/secrets/{YOUTUBE_TOKEN_SECRET}"

    client.add_secret_version(
        request={
            "parent": parent,
            "payload": {
                "data": credentials.to_json().encode("utf-8")
            },
        }
    )


def load_youtube_credentials():
    client = secretmanager.SecretManagerServiceClient()
    name = (
        f"projects/{PROJECT_ID}/secrets/"
        f"{YOUTUBE_TOKEN_SECRET}/versions/latest"
    )

    result = client.access_secret_version(request={"name": name})
    info = json.loads(result.payload.data.decode("utf-8"))

    return Credentials.from_authorized_user_info(
        info,
        scopes=YOUTUBE_SCOPES,
    )


async def verify_youtube_connection():
    credentials = load_youtube_credentials()

    if not credentials.valid:
        if not credentials.refresh_token:
            raise RuntimeError("Nav refresh_token. Autorizē YouTube vēlreiz.")

        credentials.refresh(GoogleAuthRequest())
        save_youtube_token(credentials)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={
                "Authorization": f"Bearer {credentials.token}"
            },
        )

    if response.status_code == 401:
        raise RuntimeError("YouTube autorizācija beigusies.")

    if response.status_code == 403:
        raise RuntimeError(
            "YouTube API atteica piekļuvi. Pārbaudi API un OAuth tiesības."
        )

    response.raise_for_status()

    channels = response.json().get("items", [])

    if not channels:
        raise RuntimeError("YouTube kanāls nav atrasts.")

    return {
        "ok": True,
        "channel_title": channels[0]["snippet"].get("title", "YouTube"),
        "message": "YouTube savienojums darbojas.",
    }


@app.get("/youtube/status")
async def youtube_status(request: Request):
    try:
        result = await verify_youtube_connection()
        request.session["youtube_connected"] = True
        return JSONResponse(result)

    except Exception as exc:
        request.session["youtube_connected"] = False
        logger.exception("YouTube pārbaudes kļūda")
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=200,
        )


@app.get("/auth/login")
async def auth_login(request: Request):
    try:
        flow = create_oauth_flow()

        url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )

        request.session["oauth_state"] = state
        return RedirectResponse(url)

    except Exception:
        logger.exception("OAuth kļūda")
        return HTMLResponse(
            "Google autorizācija nav konfigurēta.",
            status_code=500,
        )


@app.get("/oauth2callback")
async def oauth_callback(request: Request):
    expected = request.session.get("oauth_state")
    returned = request.query_params.get("state")
    code = request.query_params.get("code")

    if request.query_params.get("error"):
        return HTMLResponse("Google autorizācija tika atcelta.", status_code=400)

    if not expected or expected != returned or not code:
        return HTMLResponse(
            "Autorizācijas sesija nav derīga. Mēģini vēlreiz.",
            status_code=400,
        )

    try:
        flow = create_oauth_flow(state=expected)
        flow.fetch_token(code=code)

        save_youtube_token(flow.credentials)

        request.session.pop("oauth_state", None)
        request.session["youtube_connected"] = True

        return RedirectResponse("/", status_code=303)

    except Exception:
        logger.exception("OAuth callback kļūda")
        return HTMLResponse(
            "Neizdevās saglabāt YouTube autorizāciju.",
            status_code=500,
        )


# =========================================================
# STORY GENERATION
# =========================================================

class StoryRequest(BaseModel):
    topic: str = Field(
        default="A friendly adventure",
        min_length=3,
        max_length=160,
    )
    age_group: str = Field(default="All children", max_length=60)
    language: str = Field(default="English", max_length=40)
    duration_minutes: int = Field(default=3, ge=1, le=10)


async def generate_story(data: StoryRequest):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY nav konfigurēts.")

    system_prompt = (
        "You are a kind children's story writer. "
        "Write original, gentle, positive, inclusive and age-appropriate "
        "stories. Avoid frightening, violent, sexual, hateful or dangerous "
        "content. Finish with a warm and reassuring ending."
    )

    user_prompt = (
        f"Write an original children's story in {data.language}. "
        f"Audience: {data.age_group}. Topic: {data.topic}. "
        f"Length: approximately {data.duration_minutes} minutes when read aloud. "
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

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            GROQ_URL,
            headers=headers,
            json=payload,
        )

    if response.status_code == 401:
        raise RuntimeError("Groq API atslēga nav derīga.")

    if response.status_code == 429:
        raise RuntimeError("Groq limits sasniegts. Mēģini vēlāk.")

    response.raise_for_status()

    result = response.json()
    story = result["choices"][0]["message"]["content"].strip()

    if not story:
        raise RuntimeError("Groq atgrieza tukšu stāstu.")

    return story


@app.post("/story/generate")
async def story_generate(data: StoryRequest):
    try:
        story = await generate_story(data)
        return {"ok": True, "story": story}

    except Exception as exc:
        logger.exception("Stāsta ģenerēšanas kļūda")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=502,
        )


# =========================================================
# PIPER VOICE
# =========================================================

class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    voice: str = Field(
        default="narrator",
        pattern="^(narrator|rabbit|turtle)$",
    )


async def make_piper_wav(text: str, voice: str, output_path: str):
    if not PIPER_ENABLED:
        raise RuntimeError("Piper balss ir izslēgta.")

    if voice not in PIPER_VOICES:
        raise RuntimeError("Nezināma balss.")

    model = PIPER_VOICES[voice]

    process = await asyncio.create_subprocess_exec(
        "piper",
        "--model",
        model,
        "--output_file",
        output_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate(text.encode("utf-8"))

    if (
        process.returncode != 0
        or not Path(output_path).exists()
        or Path(output_path).stat().st_size < 1000
    ):
        raise RuntimeError(
            stderr.decode("utf-8", errors="replace")[-500:]
            or "Piper audio kļūda."
        )


@app.post("/speech/generate")
async def speech_generate(data: SpeechRequest):
    output = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        ) as f:
            output = f.name

        await make_piper_wav(data.text, data.voice, output)

        return FileResponse(
            output,
            media_type="audio/wav",
            filename="wonderpals_story.wav",
            background=BackgroundTask(
                lambda: Path(output).unlink(missing_ok=True)
            ),
        )

    except Exception as exc:
        if output:
            Path(output).unlink(missing_ok=True)

        logger.exception("Audio kļūda")

        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=502,
        )


# =========================================================
# VIDEO CREATION
# =========================================================

@app.post("/video/create")
async def video_create(
    story: str = Form(...),
    voice: str = Form("narrator"),
    scenes: list[UploadFile] = File(...),
):
    work = Path(tempfile.mkdtemp(prefix="wonderpals_video_"))

    try:
        if not story.strip() or len(story) > 12000:
            raise RuntimeError("Stāsts ir tukšs vai pārāk garš.")

        if voice not in PIPER_VOICES:
            raise RuntimeError("Izvēlētā balss nav derīga.")

        # Save uploaded images
        images = []

        for index, upload in enumerate(scenes[:12]):
            suffix = Path(upload.filename or "").suffix.lower()

            if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
                continue

            content = await upload.read()

            if not content or len(content) > 12 * 1024 * 1024:
                continue

            image_path = work / f"scene_{index:02d}{suffix}"
            image_path.write_bytes(content)
            images.append(image_path)

        if not images:
            raise RuntimeError(
                "Pievieno vismaz vienu JPG, PNG vai WEBP attēlu."
            )

        # Generate narration
        voice_path = work / "narration.wav"
        await make_piper_wav(story, voice, str(voice_path))

        with wave.open(str(voice_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()

        duration = max(5.0, duration)
        scene_duration = duration / len(images)

        clips = []

        # Create animated clips from still images
        for index, image_path in enumerate(images):
            clip = work / f"clip_{index:02d}.mp4"

            frames = max(150, math.ceil(scene_duration * 30))

            video_filter = (
                "scale=1600:900:force_original_aspect_ratio=increase,"
                "crop=1600:900,setsar=1,"
                f"zoompan=z='min(zoom+0.0007,1.12)':"
                f"d={frames}:s=1280x720:fps=30,"
                "format=yuv420p"
            )

            command = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", str(image_path),
                "-vf", video_filter,
                "-t", f"{scene_duration:.3f}",
                "-an",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                str(clip),
            ]

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            _, error = await process.communicate()

            if process.returncode != 0:
                raise RuntimeError(
                    "Neizdevās izveidot ainas video: "
                    + error.decode(errors="replace")[-500:]
                )

            clips.append(clip)

        # Join scene clips
        concat_file = work / "clips.txt"

        concat_file.write_text(
            "\n".join(
                "file '" + str(clip) + "'"
                for clip in clips
            ),
            encoding="utf-8",
        )

        silent_video = work / "silent.mp4"

        command = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(silent_video),
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, error = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(
                "Neizdevās apvienot ainas: "
                + error.decode(errors="replace")[-500:]
            )

        # Add narration and optional background music.
        # FFmpeg input 0 = video, input 1 = narration,
        # input 2 = music (only if the music file is valid).
        music_path = Path("assets/music/background.mp3")
        final_path = work / "wonderpals_video.mp4"

        audio_inputs = ["-i", str(voice_path)]
        audio_filter = "[1:a]volume=1.0[aout]"

        if music_path.exists():
            try:
                music_test = subprocess.run(
                    [
                        "ffmpeg", "-v", "error",
                        "-i", str(music_path),
                        "-t", "2",
                        "-f", "null", "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

                if music_test.returncode == 0:
                    audio_inputs += [
                        "-stream_loop", "-1",
                        "-i", str(music_path),
                    ]

                    audio_filter = (
                        "[1:a]volume=1.0[voice];"
                        "[2:a]volume=0.16[music];"
                        "[voice][music]"
                        "amix=inputs=2:duration=first:"
                        "dropout_transition=2[aout]"
                    )
                else:
                    logger.warning(
                        "Mūzikas fails nav derīgs; video būs bez mūzikas."
                    )

            except Exception:
                logger.exception(
                    "Neizdevās pārbaudīt mūzikas failu; turpinām bez mūzikas."
                )

        command = [
            "ffmpeg", "-y",
            "-i", str(silent_video),
            *audio_inputs,
            "-filter_complex", audio_filter,
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(final_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, error = await process.communicate()

        if process.returncode != 0 or not final_path.exists():
            raise RuntimeError(
                "MP4 izveides kļūda: "
                + error.decode(errors="replace")[-700:]
            )

        return FileResponse(
            str(final_path),
            media_type="video/mp4",
            filename="wonderpals_story.mp4",
            background=BackgroundTask(
                lambda: shutil.rmtree(work, ignore_errors=True)
            ),
        )

    except Exception as exc:
        logger.exception("Video izveides kļūda")
        shutil.rmtree(work, ignore_errors=True)

        return JSONResponse(
            {"ok": False, "error": str(exc)[:700]},
            status_code=502,
        )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "groq_configured": bool(GROQ_API_KEY),
        "piper_enabled": PIPER_ENABLED,
        "youtube_oauth_configured": bool(CLIENT_ID and CLIENT_SECRET),
    }


# =========================================================
# HOME PAGE
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    connected = bool(request.session.get("youtube_connected"))

    yt_status = (
        "YouTube autorizēts šajā pārlūka sesijā"
        if connected
        else "YouTube vēl nav autorizēts"
    )

    html = """
<!doctype html>
<html lang="lv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WonderPals AI Studio</title>

<style>
body {
    font-family: Arial, sans-serif;
    background: #f4f6fb;
    color: #20243a;
    max-width: 850px;
    margin: 35px auto;
    padding: 0 18px;
}
.card {
    background: white;
    padding: 24px;
    border-radius: 16px;
    box-shadow: 0 4px 18px #18204412;
    margin: 18px 0;
}
input, select, button {
    font: inherit;
    padding: 12px;
    border-radius: 9px;
    border: 1px solid #ccd2e0;
    width: 100%;
    box-sizing: border-box;
    margin: 7px 0;
}
button {
    background: #5556d9;
    color: white;
    border: 0;
    cursor: pointer;
}
button:disabled {
    opacity: .6;
    cursor: wait;
}
a { color: #5556d9; }
pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.5;
}
.muted { color: #68708a; }
video { width: 100%; }
</style>
</head>

<body>

<h1>🌈 WonderPals AI Studio</h1>
<p class="muted">Bērniem draudzīgu stāstu veidošanas studija</p>

<section class="card">
<h2>📺 YouTube savienojums</h2>
<p id="ytstatus">__YT_STATUS__</p>

<button id="checkYoutube" onclick="checkYouTube()">
Pārbaudīt YouTube savienojumu
</button>

<p id="ytresult" class="muted"></p>

<a href="/auth/login">Savienot Google / YouTube kontu</a>

<p class="muted">
Šī versija pārbauda YouTube savienojumu, bet vēl pati video nepublicē.
</p>
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
<select id="language">
<option>English</option>
<option>Latvian</option>
</select>

<label>Garums</label>
<select id="duration">
<option>2</option>
<option selected>3</option>
<option>5</option>
<option>7</option>
<option>10</option>
</select>

<button id="generate" onclick="makeStory()">
✨ Ģenerēt stāstu
</button>

<p id="storyMessage" class="muted"></p>
<pre id="story"></pre>

<hr>

<h3>🔊 Stāsta balss</h3>

<select id="voice">
<option value="narrator">Stāstnieks — mierīga balss</option>
<option value="rabbit">Trusis — gaišāka balss</option>
<option value="turtle">Bruņurupucis — zemāka balss</option>
</select>

<button id="audioButton" onclick="makeAudio()">
🎙️ Izveidot audio
</button>

<p id="audioMessage" class="muted"></p>
<audio id="audioPlayer" controls style="display:none;width:100%"></audio>

<hr>

<h3>🎬 Izveidot 3D stāsta video</h3>

<p class="muted">
Pievieno līdz 12 JPG, PNG vai WEBP attēliem.
Tie tiks pārvērsti video ainās ar lēnu kameras kustību,
stāstītāja balsi un derīgu fona mūziku, ja tā ir pieejama.
</p>

<label>3D ainu attēli</label>
<input
    id="sceneImages"
    type="file"
    accept="image/jpeg,image/png,image/webp"
    multiple
>

<button id="videoButton" onclick="makeVideo()">
🎬 Izveidot MP4 video
</button>

<p id="videoMessage" class="muted"></p>

<video id="videoPlayer" controls style="display:none"></video>

<p>
<a id="videoDownload" style="display:none" download="wonderpals_story.mp4">
Lejupielādēt MP4 video
</a>
</p>

</section>

<script>

async function checkYouTube() {
    const button = document.getElementById("checkYoutube");
    const result = document.getElementById("ytresult");

    button.disabled = true;
    result.textContent = "Pārbauda YouTube...";

    try {
        const response = await fetch("/youtube/status");
        const data = await response.json();

        if (data.ok) {
            result.textContent =
                "✅ SAVIENOJUMS DARBOJAS — " + data.channel_title;
            result.style.color = "green";
            document.getElementById("ytstatus").textContent =
                "YouTube savienojums darbojas";
        } else {
            result.textContent = "❌ " + data.message;
            result.style.color = "crimson";
        }
    } catch (error) {
        result.textContent = "Pārbaudes kļūda: " + error.message;
    } finally {
        button.disabled = false;
    }
}


async function makeStory() {
    const button = document.getElementById("generate");
    const message = document.getElementById("storyMessage");
    const output = document.getElementById("story");

    button.disabled = true;
    message.textContent = "✨ Groq raksta stāstu...";
    output.textContent = "";

    try {
        const response = await fetch("/story/generate", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                topic: document.getElementById("topic").value,
                age_group: document.getElementById("age").value,
                language: document.getElementById("language").value,
                duration_minutes:
                    Number(document.getElementById("duration").value)
            })
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(data.error || "Stāsta kļūda");
        }

        output.textContent = data.story;
        message.textContent = "✅ Stāsts ir gatavs!";

    } catch (error) {
        message.textContent = "Kļūda: " + error.message;
    } finally {
        button.disabled = false;
    }
}


async function makeAudio() {
    const story = document.getElementById("story").textContent.trim();
    const button = document.getElementById("audioButton");
    const message = document.getElementById("audioMessage");
    const player = document.getElementById("audioPlayer");

    if (!story) {
        message.textContent = "Vispirms ģenerē stāstu.";
        return;
    }

    button.disabled = true;
    message.textContent = "🎙️ Veido audio...";
    player.style.display = "none";

    try {
        const response = await fetch("/speech/generate", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                text: story,
                voice: document.getElementById("voice").value
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || "Audio kļūda");
        }

        const blob = await response.blob();

        if (window.audioUrl) {
            URL.revokeObjectURL(window.audioUrl);
        }

        window.audioUrl = URL.createObjectURL(blob);
        player.src = window.audioUrl;
        player.style.display = "block";

        message.textContent = "✅ Audio gatavs!";

    } catch (error) {
        message.textContent = "Kļūda: " + error.message;
    } finally {
        button.disabled = false;
    }
}


async function makeVideo() {
    const story = document.getElementById("story").textContent.trim();
    const files = document.getElementById("sceneImages").files;
    const button = document.getElementById("videoButton");
    const message = document.getElementById("videoMessage");

    if (!story) {
        message.textContent = "Vispirms ģenerē stāstu.";
        return;
    }

    if (!files.length) {
        message.textContent = "Pievieno vismaz vienu attēlu.";
        return;
    }

    if (files.length > 12) {
        message.textContent = "Atļauti ne vairāk kā 12 attēli.";
        return;
    }

    const form = new FormData();
    form.append("story", story);
    form.append("voice", document.getElementById("voice").value);

    for (const file of files) {
        form.append("scenes", file);
    }

    button.disabled = true;
    message.textContent = "🎬 Veido video. Lūdzu, pagaidi...";

    try {
        const response = await fetch("/video/create", {
            method: "POST",
            body: form
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || "Video kļūda");
        }

        const blob = await response.blob();

        if (window.videoUrl) {
            URL.revokeObjectURL(window.videoUrl);
        }

        window.videoUrl = URL.createObjectURL(blob);

        const player = document.getElementById("videoPlayer");
        player.src = window.videoUrl;
        player.style.display = "block";

        const link = document.getElementById("videoDownload");
        link.href = window.videoUrl;
        link.style.display = "inline-block";

        message.textContent = "✅ Video gatavs! Vari to noskatīties un lejupielādēt.";

    } catch (error) {
        message.textContent = "Kļūda: " + error.message;
    } finally {
        button.disabled = false;
    }
}

</script>
</body>
</html>
"""

    return HTMLResponse(html.replace("__YT_STATUS__", yt_status))
