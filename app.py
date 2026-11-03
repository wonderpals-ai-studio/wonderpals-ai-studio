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
