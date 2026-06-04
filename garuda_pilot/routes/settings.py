"""Settings route — manage AI provider config from the web UI."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..analysis.pacnew import ollama_available
from ..config import Config

router = APIRouter()


async def _ollama_models(base_url: str) -> list[str]:
    """Return list of locally available Ollama model names."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []


@router.get("/settings")
async def settings_page(request: Request):
    templates = request.app.state.templates
    config: Config = request.app.state.config
    ollama_ok = await ollama_available(config.ollama_url)
    models = await _ollama_models(config.ollama_url) if ollama_ok else []
    return templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "active_page": "settings",
        "config": config,
        "ollama_ok": ollama_ok,
        "ollama_models": models,
    })


@router.post("/htmx/settings-save")
async def settings_save(
    request: Request,
    claude_api_key: str = Form(default=""),
    ollama_url: str = Form(default="http://localhost:11434"),
    ollama_model: str = Form(default="llama3.2"),
):
    config: Config = request.app.state.config
    config.claude_api_key = claude_api_key.strip()
    config.ollama_url = ollama_url.strip() or "http://localhost:11434"
    config.ollama_model = ollama_model.strip() or "llama3.2"
    config.save()
    return HTMLResponse('<span style="color: var(--green);">Settings saved.</span>')


@router.post("/htmx/settings-test-ollama")
async def settings_test_ollama(request: Request):
    config: Config = request.app.state.config
    ok = await ollama_available(config.ollama_url)
    if ok:
        models = await _ollama_models(config.ollama_url)
        model_list = ", ".join(models[:5]) or "none pulled yet"
        return HTMLResponse(
            f'<span style="color: var(--green);">Connected — models: {model_list}</span>'
        )
    return HTMLResponse(
        f'<span style="color: var(--warning);">Cannot reach {config.ollama_url} — is Ollama running?</span>'
    )


@router.post("/htmx/settings-test-claude")
async def settings_test_claude(request: Request):
    config: Config = request.app.state.config
    if not config.claude_api_key:
        return HTMLResponse('<span style="color: var(--warning);">No API key set.</span>')
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": config.claude_api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 10,
                      "messages": [{"role": "user", "content": "Hi"}]},
            )
    except Exception as e:
        return HTMLResponse(f'<span style="color: var(--warning);">Error: {e}</span>')
    if resp.status_code == 200:
        return HTMLResponse('<span style="color: var(--green);">API key valid.</span>')
    if resp.status_code == 401:
        return HTMLResponse('<span style="color: var(--warning);">Invalid API key.</span>')
    return HTMLResponse(f'<span style="color: var(--warning);">HTTP {resp.status_code}</span>')
