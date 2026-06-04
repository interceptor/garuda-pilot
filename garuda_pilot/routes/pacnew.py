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

    diff = pn.get_unified_diff(f)  # None=unreadable, ""=identical, str=diff
    return templates.TemplateResponse(request, "pacnew_diff.html", {
        "request": request,
        "f": f,
        "diff": diff,
        "diff_unreadable": diff is None,
        "diff_identical": diff == "",
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
            return HTMLResponse('<p style="color:var(--warning);">Claude API key not configured — go to <a href="/settings">Settings</a>.</p>')
        explanation = await pn.explain_claude(f, config.claude_api_key)
    elif provider == "ollama":
        explanation = await pn.explain_ollama(f, config.ollama_url, config.ollama_model)
    else:
        return HTMLResponse('<span style="color:var(--warning);">Unknown provider.</span>', status_code=400)

    label = "Claude" if provider == "claude" else f"Ollama ({config.ollama_model})"
    return HTMLResponse(
        f'<div class="ai-explanation">'
        f'<div class="ai-label">{label} — analysis</div>'
        f'<p>{explanation}</p>'
        f'</div>'
    )


@router.post("/htmx/pacnew-merge")
async def pacnew_merge(request: Request, path: str = "", provider: str = "claude"):
    """HTMX: generate a merged file using AI, show preview + apply command."""
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
            return HTMLResponse('<p style="color:var(--warning);">Claude API key not configured — go to <a href="/settings">Settings</a>.</p>')
        content, err = await pn.merge_claude(f, config.claude_api_key)
    elif provider == "ollama":
        content, err = await pn.merge_ollama(f, config.ollama_url, config.ollama_model)
    else:
        return HTMLResponse('<span style="color:var(--warning);">Unknown provider.</span>', status_code=400)

    if err:
        return HTMLResponse(f'<p style="color:var(--warning);">Error: {err}</p>')

    tmp_path = pn.write_merge_temp(f, content)
    label = "Claude" if provider == "claude" else f"Ollama ({config.ollama_model})"
    apply_cmd = f"sudo cp {tmp_path} {f.current_path} && sudo rm {f.pacnew_path}"
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f'<div class="ai-explanation">'
        f'<div class="ai-label">{label} — proposed merge (review before applying)</div>'
        f'<div style="position:relative;">'
        f'<pre class="merge-preview">{escaped}</pre>'
        f'<button class="btn btn-sm btn-secondary" style="position:absolute;top:6px;right:6px;"'
        f' onclick="copyMergeContent(this)">Copy</button>'
        f'</div>'
        f'<div class="cmd-group" style="margin-top:10px;">'
        f'<span class="cmd-label">Apply:</span>'
        f'<code>{apply_cmd}</code>'
        f'<span class="upgrade-copy-link" onclick="copyText(\'{apply_cmd}\', this)">copy</span>'
        f'</div>'
        f'<p style="color:var(--text-muted);font-size:0.82em;margin-top:6px;">'
        f'Written to <code>{tmp_path}</code></p>'
        f'</div>'
    )
