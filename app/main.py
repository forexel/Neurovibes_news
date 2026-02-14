import os
from fastapi import FastAPI

app = FastAPI(title="Neurovibes News API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def config() -> dict[str, str]:
    return {
        "app_env": os.getenv("APP_ENV", "unknown"),
        "text_model": os.getenv("LLM_TEXT_MODEL", "unset"),
        "image_model": os.getenv("LLM_IMAGE_MODEL", "unset"),
    }
