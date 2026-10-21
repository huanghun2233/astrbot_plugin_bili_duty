"""Thin async wrapper around the ``bili`` CLI (bili-cli 0.5.x).

Everything the plugin does to B站 goes through this module: the CLI is the
single place where credentials, rate limits and the audit log live, so the
plugin itself never touches cookies or the B站 HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

CLI_FALLBACKS = (
    Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "bili.exe",
    Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "bili",
    Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "bili-agent" / "Scripts" / "bili.exe",
)


class BiliError(RuntimeError):
    """Raised when the CLI answers with ok=false or cannot be started."""

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def resolve_cli(configured: str = "") -> str | None:
    """Find the bili executable: explicit config > PATH > known install spots."""
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bili")
    if found:
        return found
    for candidate in CLI_FALLBACKS:
        if candidate and candidate.is_file():
            return str(candidate)
    return None


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


async def run_bili(cli: str, *args: str, timeout: float = 180.0) -> dict:
    """Run one bili subcommand and return its JSON ``data`` payload."""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BiliError(f"找不到 bili 可执行文件：{cli}") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise BiliError(f"bili {' '.join(args)} 超时（{timeout:.0f}s）") from exc

    text = _decode(out).strip()
    if not text:
        raise BiliError(f"bili {' '.join(args)} 没有输出：{_decode(err)[:200]}")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BiliError(f"bili {' '.join(args)} 返回了非 JSON：{text[:200]}") from exc

    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise BiliError(
            error.get("message") or f"bili {' '.join(args)} 失败",
            code=error.get("code"),
            hint=error.get("hint"),
        )
    return envelope.get("data") or {}


async def version(cli: str) -> str:
    """Best-effort CLI version string for the status command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return _decode(out).strip() or "unknown"
    except Exception:
        return "unknown"
