import os
from fastapi import FastAPI
from openai import OpenAI

app = FastAPI(title="Neurovibes News API", version="0.1.0")


def get_llm_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def config() -> dict[str, str]:
    _ = get_llm_client()
    return {
        "app_env": os.getenv("APP_ENV", "unknown"),
        "text_model": os.getenv("LLM_TEXT_MODEL", "unset"),
        "image_model": os.getenv("LLM_IMAGE_MODEL", "unset"),
        "llm_base_url": os.getenv("OPENROUTER_BASE_URL", "unset"),
        "llm_api_key_set": "true" if bool(os.getenv("OPENROUTER_API_KEY")) else "false",
    }
