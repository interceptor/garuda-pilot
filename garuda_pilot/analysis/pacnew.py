"""Pacnew file detection, diffing, classification, and AI-assisted review."""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PacnewFile:
    pacnew_path: str
    current_path: str
    exists: bool        # current file exists
    readable: bool      # both files readable by current user
    guidance: str       # "safe" / "review" / "careful"
    guidance_text: str


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

def _classify(path: str) -> tuple[str, str]:
    """Return (guidance_level, guidance_text) based on file path."""
    p = path.lower()
    name = Path(path).name.lower()

    if "mirrorlist" in name:
        return "safe", "Mirror lists are safe to replace — they rarely contain customizations."
    if p.endswith("/etc/pacman.conf"):
        return "review", "Check for custom repos or options before replacing."
    if "/etc/pam.d/" in p:
        return "careful", "PAM auth config — a bad merge can lock you out of the system."
    if "/etc/ssh/" in p:
        return "careful", "SSH config — review carefully to preserve access settings."
    if "/etc/sudoers" in p:
        return "careful", "Sudoers — errors here break sudo access."
    if "/etc/fstab" in p:
        return "careful", "Filesystem table — errors here can prevent booting."
    if "/etc/hosts" in p:
        return "careful", "Check for custom host entries before replacing."
    if "/etc/locale" in p or "/etc/locale.gen" in p:
        return "safe", "Locale config — generally safe to use the new version."
    if "/etc/profile" in p or "/etc/bash" in p or "/etc/zsh" in p or "/etc/fish" in p:
        return "review", "Shell config — may contain custom environment settings."
    if "/etc/default/" in p:
        return "review", "System defaults — review before replacing."
    if "/etc/systemd/" in p:
        return "review", "Systemd config — compare before replacing."
    if "/etc/NetworkManager/" in p or "/etc/network" in p:
        return "review", "Network config — check for custom connections or settings."

    return "review", "Review the diff before deciding."


# ---------------------------------------------------------------------------
# Finding and diffing
# ---------------------------------------------------------------------------

async def find_pacnew_files() -> list[PacnewFile]:
    """Find all .pacnew files under /etc."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", "/etc", "-name", "*.pacnew", "-type", "f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []

    files = []
    order = {"careful": 0, "review": 1, "safe": 2}

    for line in stdout.decode(errors="replace").splitlines():
        pacnew = line.strip()
        if not pacnew or not pacnew.endswith(".pacnew"):
            continue
        current = pacnew[:-len(".pacnew")]
        cur_path = Path(current)
        pac_path = Path(pacnew)

        exists = cur_path.exists()
        readable = False
        try:
            if exists:
                cur_path.read_bytes()
            pac_path.read_bytes()
            readable = True
        except (OSError, PermissionError):
            pass

        guidance, text = _classify(current)
        files.append(PacnewFile(
            pacnew_path=pacnew,
            current_path=current,
            exists=exists,
            readable=readable,
            guidance=guidance,
            guidance_text=text,
        ))

    return sorted(files, key=lambda f: (order.get(f.guidance, 1), f.current_path))


def get_unified_diff(f: PacnewFile) -> str:
    """Return unified diff string for rendering by diff2html."""
    try:
        cur = Path(f.current_path).read_text(errors="replace").splitlines(keepends=True) if f.exists else []
        new = Path(f.pacnew_path).read_text(errors="replace").splitlines(keepends=True)
    except (OSError, PermissionError):
        return ""
    return "".join(difflib.unified_diff(cur, new, fromfile=f.current_path, tofile=f.pacnew_path))


def get_diff_text(f: PacnewFile, max_lines: int = 150) -> str:
    """Return unified diff as plain text, truncated for AI consumption."""
    try:
        current_lines = (
            Path(f.current_path).read_text(errors="replace").splitlines(keepends=True)
            if f.exists else []
        )
        new_lines = Path(f.pacnew_path).read_text(errors="replace").splitlines(keepends=True)
    except (OSError, PermissionError):
        return ""

    lines = list(difflib.unified_diff(
        current_lines, new_lines,
        fromfile="current",
        tofile="new (pacnew)",
        lineterm="",
    ))
    truncated = lines[:max_lines]
    result = "".join(truncated)
    if len(lines) > max_lines:
        result += f"\n... ({len(lines) - max_lines} more lines truncated)"
    return result


# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are reviewing a Linux config file update for a user upgrading their system.

File: {filename}
Diff (current config → new version from package update):
{diff}

Reply in plain text, no markdown, 3 short paragraphs:
1. What changed (1-2 sentences, be specific)
2. Risk: none / low / medium / high — one sentence why
3. Recommendation: one of — "Use new version" / "Keep current" / "Merge manually: [what to preserve]"
"""


def _build_prompt(f: PacnewFile) -> str:
    diff = get_diff_text(f)
    if not diff:
        diff = "(files not readable or identical)"
    return _PROMPT_TEMPLATE.format(filename=f.current_path, diff=diff)


async def explain_claude(f: PacnewFile, api_key: str) -> str:
    """Call Claude API to explain the diff. Returns explanation text or error string."""
    prompt = _build_prompt(f)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
    except httpx.RequestError as e:
        return f"Error contacting Claude API: {e}"

    if resp.status_code == 401:
        return "Claude API error: invalid API key."
    if resp.status_code == 429:
        return "Claude API error: rate limit reached."
    if resp.status_code != 200:
        return f"Claude API error: HTTP {resp.status_code}."

    try:
        return resp.json()["content"][0]["text"].strip()
    except (KeyError, IndexError):
        return "Claude API returned an unexpected response."


async def explain_ollama(f: PacnewFile, base_url: str, model: str) -> str:
    """Call Ollama to explain the diff. Returns explanation text or error string."""
    prompt = _build_prompt(f)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
            )
    except httpx.ConnectError:
        return f"Cannot connect to Ollama at {base_url}. Is it running?"
    except httpx.RequestError as e:
        return f"Error contacting Ollama: {e}"

    if resp.status_code == 404:
        return f"Ollama model '{model}' not found. Run: ollama pull {model}"
    if resp.status_code != 200:
        return f"Ollama error: HTTP {resp.status_code}."

    try:
        return resp.json()["message"]["content"].strip()
    except (KeyError, TypeError):
        return "Ollama returned an unexpected response."


async def ollama_available(base_url: str) -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
