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


def get_file_contents(f: PacnewFile) -> tuple[str | None, str | None]:
    """Return (current_text, new_text). None means unreadable."""
    cur_text = new_text = None
    try:
        cur_text = Path(f.current_path).read_text(errors="replace") if f.exists else ""
    except (OSError, PermissionError):
        pass
    try:
        new_text = Path(f.pacnew_path).read_text(errors="replace")
    except (OSError, PermissionError):
        pass
    return cur_text, new_text


def get_unified_diff(f: PacnewFile) -> str | None:
    """Return unified diff string for diff2html, '' if identical, None if unreadable."""
    cur_text, new_text = get_file_contents(f)
    if cur_text is None or new_text is None:
        return None  # permission error — distinct from identical files
    cur = cur_text.splitlines(keepends=True)
    new = new_text.splitlines(keepends=True)
    # Use plain paths — diff2html parses fromfile/tofile from --- / +++ headers;
    # spaces in the label confuse its filename detection.
    return "".join(difflib.unified_diff(cur, new,
                                        fromfile=f.current_path,
                                        tofile=f.pacnew_path))


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

_EXPLAIN_PROMPT = """\
You are reviewing a Linux config file update for a user upgrading their system.

File: {filename}
Diff (current config → new version from package update):
{diff}

Reply in plain text, no markdown, 3 short paragraphs:
1. What changed (1-2 sentences, be specific)
2. Risk: none / low / medium / high — one sentence why
3. Recommendation: one of — "Use new version" / "Keep current" / "Merge manually: [what to preserve]"
"""

_MERGE_PROMPT = """\
You are merging two Linux config files.

File: {filename}

CURRENT FILE (the user's version):
{current}

NEW VERSION (from package update):
{new}

Output in EXACTLY this format with no other text:

SUMMARY:
- [one bullet per meaningful change: what was kept from current, what was added/changed from new, what was removed]

MERGED:
[complete merged file, ready to use, no markdown fences, no extra comments]

Rules for the merge:
- Preserve the user's custom settings from CURRENT that are absent or different in NEW
- Include important changes, new options, and security improvements from NEW
- For mirror lists or auto-generated files: use NEW entirely
"""


def _build_explain_prompt(f: PacnewFile) -> str:
    diff = get_diff_text(f)
    if not diff:
        diff = "(files not readable or identical)"
    return _EXPLAIN_PROMPT.format(filename=f.current_path, diff=diff)


def _build_merge_prompt(f: PacnewFile) -> str | None:
    cur_text, new_text = get_file_contents(f)
    if cur_text is None or new_text is None:
        return None
    return _MERGE_PROMPT.format(filename=f.current_path, current=cur_text, new=new_text)


async def explain_claude(f: PacnewFile, api_key: str) -> str:
    """Call Claude API to explain the diff. Returns explanation text or error string."""
    prompt = _build_explain_prompt(f)
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
    prompt = _build_explain_prompt(f)
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


def parse_merge_response(response: str) -> tuple[list[str], str]:
    """Split AI response into (summary_bullets, file_content).

    Expects SUMMARY: / MERGED: delimiters. Falls back gracefully if absent.
    """
    if "MERGED:" in response:
        parts = response.split("MERGED:", 1)
        content = parts[1].strip()
        # Strip accidental markdown fences the model may add despite instructions
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
        content = content.strip()

        bullets: list[str] = []
        if "SUMMARY:" in parts[0]:
            for line in parts[0].split("SUMMARY:", 1)[-1].splitlines():
                line = line.strip().lstrip("-*• ").strip()
                if line:
                    bullets.append(line)
        return bullets, content

    # No delimiters — treat whole response as file content
    return [], response.strip()


def guess_hljs_lang(path: str) -> str:
    """Return a highlight.js language hint for the file path."""
    name = Path(path).name.lower()
    if name.endswith((".sh", ".bash", ".zsh")) or name in ("bashrc", "bash_profile", "zshrc", "profile"):
        return "bash"
    if "nginx" in path:
        return "nginx"
    if name.endswith(".xml"):
        return "xml"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".yaml") or name.endswith(".yml"):
        return "yaml"
    # Most /etc config files are INI-like (key = value, [sections], # comments)
    return "ini"


async def merge_claude(f: PacnewFile, api_key: str) -> tuple[str, str]:
    """Generate merged file content via Claude. Returns (content, error)."""
    prompt = _build_merge_prompt(f)
    if not prompt:
        return "", "Cannot read file content."
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2048,
                      "messages": [{"role": "user", "content": prompt}]},
            )
    except httpx.RequestError as e:
        return "", f"Error: {e}"
    if resp.status_code == 401:
        return "", "Invalid Claude API key."
    if resp.status_code != 200:
        return "", f"Claude API error: HTTP {resp.status_code}."
    try:
        return resp.json()["content"][0]["text"].strip(), ""
    except (KeyError, IndexError):
        return "", "Unexpected response from Claude."


async def merge_ollama(f: PacnewFile, base_url: str, model: str) -> tuple[str, str]:
    """Generate merged file content via Ollama. Returns (content, error)."""
    prompt = _build_merge_prompt(f)
    if not prompt:
        return "", "Cannot read file content."
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/chat",
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False},
            )
    except httpx.ConnectError:
        return "", f"Cannot connect to Ollama at {base_url}."
    except httpx.RequestError as e:
        return "", f"Error: {e}"
    if resp.status_code == 404:
        return "", f"Ollama model '{model}' not found."
    if resp.status_code != 200:
        return "", f"Ollama error: HTTP {resp.status_code}."
    try:
        return resp.json()["message"]["content"].strip(), ""
    except (KeyError, TypeError):
        return "", "Unexpected response from Ollama."


def write_merge_temp(f: PacnewFile, content: str) -> str:
    """Write merged content to a temp file. Returns the temp file path."""
    import hashlib
    h = hashlib.md5(f.pacnew_path.encode()).hexdigest()[:8]
    suffix = Path(f.current_path).suffix or ""
    tmp_path = f"/tmp/garuda-pilot-merge-{h}{suffix}"
    Path(tmp_path).write_text(content)
    return tmp_path


async def ollama_available(base_url: str) -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
