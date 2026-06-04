"""Pacnew review routes."""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..analysis import pacnew as pn

router = APIRouter()


def _safe_path(raw: str) -> str | None:
    """Validate that the path is a .pacnew file under /etc."""
    path = unquote(raw)
    if path.endswith(".pacnew") and path.startswith("/etc/"):
        return path
    return None


@router.get("/pacnew")
async def pacnew_page(request: Request):
    templates = request.app.state.templates
    config = request.app.state.config

    files = await pn.find_pacnew_files()
    ollama_ok = await pn.ollama_available(config.ollama_url) if config.ollama_url else False

    return templates.TemplateResponse(request, "pacnew.html", {
        "request": request,
        "active_page": "pacnew",
        "files": files,
        "has_claude": bool(config.claude_api_key),
        "has_ollama": ollama_ok,
        "ollama_model": config.ollama_model,
    })


@router.post("/htmx/pacnew-diff")
async def pacnew_diff(request: Request, path: str = ""):
    """HTMX: return the diff for a single pacnew file."""
    templates = request.app.state.templates
    safe = _safe_path(path)
    if not safe:
        return HTMLResponse('<span style="color:var(--warning);">Invalid path.</span>', status_code=400)

    files = await pn.find_pacnew_files()
    f = next((x for x in files if x.pacnew_path == safe), None)
    if not f:
        return HTMLResponse('<span style="color:var(--warning);">File not found.</span>', status_code=404)

    diff_lines = pn.get_diff(f)
    return templates.TemplateResponse(request, "pacnew_diff.html", {
        "request": request,
        "f": f,
        "diff_lines": diff_lines,
    })


@router.post("/htmx/pacnew-explain")
async def pacnew_explain(request: Request, path: str = "", provider: str = "claude"):
    """HTMX: return AI explanation for a pacnew diff."""
    config = request.app.state.config
    safe = _safe_path(path)
    if not safe:
        return HTMLResponse('<span style="color:var(--warning);">Invalid path.</span>', status_code=400)

    files = await pn.find_pacnew_files()
    f = next((x for x in files if x.pacnew_path == safe), None)
    if not f:
        return HTMLResponse('<span style="color:var(--warning);">File not found.</span>', status_code=404)

    if provider == "claude":
        if not config.claude_api_key:
            return HTMLResponse(
                '<p style="color:var(--warning);">Claude API key not configured. '
                'Add <code>claude_api_key = "sk-ant-..."</code> to '
                '<code>~/.config/garuda-pilot/config.toml</code>.</p>'
            )
        explanation = await pn.explain_claude(f, config.claude_api_key)
    elif provider == "ollama":
        explanation = await pn.explain_ollama(f, config.ollama_url, config.ollama_model)
    else:
        return HTMLResponse('<span style="color:var(--warning);">Unknown provider.</span>', status_code=400)

    label = "Claude" if provider == "claude" else f"Ollama ({config.ollama_model})"
    return HTMLResponse(
        f'<div class="ai-explanation">'
        f'<div class="ai-label">{label}</div>'
        f'<p>{explanation}</p>'
        f'</div>'
    )
