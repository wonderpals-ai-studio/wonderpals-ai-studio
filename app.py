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
# STĀSTU ĢENERĒŠANA (HUGGING FACE)
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
        "model": "google/gemma-2-2b-it:cheapest",
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


@app.post("/story/generate")
async def story_generate(data: StoryRequest):
    """Generate one story without blocking the web server's event loop."""
    try:
        story = await run_in_threadpool(
            generate_story,
            data.idea,
            data.age,
            data.length,
        )
        return JSONResponse({"success": True, "story": story})
    except Exception:
        # Keep credentials/provider details out of the public browser response.
        logger.exception("Hugging Face story generation failed.")
        return JSONResponse(
            {
                "success": False,
                "error": "Neizdevās izveidot stāstu. Pārbaudi Hugging Face piekļuvi un bezmaksas kvotu.",
            },
            status_code=500,
        )

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
