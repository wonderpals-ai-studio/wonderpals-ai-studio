from fastapi import FastAPI

app = FastAPI(title="WonderPals AI Studio")

@app.get("/")
def home():
    return {
        "project": "WonderPals AI Studio",
        "status": "running"
    }

@app.get("/health")
def health():
    return {"status": "healthy"}
