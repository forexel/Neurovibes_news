from __future__ import annotations

import json
import subprocess
from typing import IO, Any

import httpx


_FALLBACK_ERROR_MARKERS = (
    "handshake operation timed out",
    "handshake timed out",
    "connecttimeout",
    "readtimeout",
    "ssl:",
    "tls",
    "unexpected eof",
    "connection reset by peer",
    "remote protocol error",
    "network is unreachable",
    "timed out",
)


def mask_telegram_error(text: str, token: str | None = None, *, limit: int = 2000) -> str:
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    out = out.replace("api.telegram.org/bot", "api.telegram.org/bot***")
    return out[:limit]


def _should_fallback(error_text: str) -> bool:
    low = str(error_text or "").strip().lower()
    return any(marker in low for marker in _FALLBACK_ERROR_MARKERS)


def _parse_response_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {"ok": False, "error": (raw or "telegram_non_json_response")[:1000]}
    if isinstance(data, dict):
        return data
    return {"ok": False, "error": str(data)[:1000]}


def _httpx_post(
    url: str,
    *,
    json_payload: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True, http2=False, trust_env=True) as client:
        resp = client.post(url, json=json_payload, data=data, files=files)
    return _parse_response_json(resp.text)


def _curl_post(
    url: str,
    *,
    family: str,
    json_payload: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: float = 30.0,
    token: str | None = None,
) -> dict[str, Any]:
    args = [
        "curl",
        "-sS",
        "--http1.1",
        f"-{family}",
        "--connect-timeout",
        str(max(5, int(timeout // 2) or 5)),
        "--max-time",
        str(max(5, int(timeout))),
        "-X",
        "POST",
        url,
    ]

    stdin_data: str | None = None
    if json_payload is not None:
        args.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
        stdin_data = json.dumps(json_payload, ensure_ascii=False)
    else:
        for key, value in (data or {}).items():
            args.extend(["--form-string", f"{key}={value}"])
        for key, value in (files or {}).items():
            file_obj: IO[Any] | Any = value
            file_path = getattr(file_obj, "name", "")
            if not file_path:
                raise RuntimeError(f"telegram_file_fallback_requires_path:{key}")
            args.extend(["-F", f"{key}=@{file_path}"])

    completed = subprocess.run(
        args,
        input=stdin_data,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = mask_telegram_error(completed.stderr or completed.stdout or "curl_failed", token=token)
        return {"ok": False, "error": stderr[:1000]}
    return _parse_response_json(completed.stdout)


def telegram_api_post(
    url: str,
    *,
    json_payload: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: float = 30.0,
    token: str | None = None,
) -> dict[str, Any]:
    try:
        payload = _httpx_post(url, json_payload=json_payload, data=data, files=files, timeout=timeout)
    except Exception as exc:
        payload = {"ok": False, "error": mask_telegram_error(str(exc), token=token)}

    if payload.get("ok"):
        return payload

    error_text = mask_telegram_error(str(payload.get("error") or payload), token=token)
    if not _should_fallback(error_text):
        payload["error"] = error_text
        return payload

    for family in ("6", "4"):
        try:
            fallback_payload = _curl_post(
                url,
                family=family,
                json_payload=json_payload,
                data=data,
                files=files,
                timeout=timeout,
                token=token,
            )
        except Exception as exc:
            fallback_payload = {"ok": False, "error": mask_telegram_error(str(exc), token=token)}
        if fallback_payload.get("ok"):
            return fallback_payload
        error_text = mask_telegram_error(str(fallback_payload.get("error") or fallback_payload), token=token)

    return {"ok": False, "error": error_text[:1200]}
