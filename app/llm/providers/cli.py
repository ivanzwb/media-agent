from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import ClassVar

from app.llm.base import Message

logger = logging.getLogger(__name__)

# ── tool definitions ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ToolDef:
    """Definition of a supported CLI AI tool."""
    id: str                # short identifier used in config
    label: str             # display name
    exe: str               # binary name (e.g. "opencode")
    # args template: {prompt} is replaced with the escaped prompt text.
    args: list[str]


_TOOLS: dict[str, _ToolDef] = {
    "opencode": _ToolDef(
        id="opencode",
        label="OpenCode",
        exe="opencode",
        args=["-p", "{prompt}"],
    ),
    "claude": _ToolDef(
        id="claude",
        label="Claude Code",
        exe="claude",
        args=["-p", "{prompt}"],
    ),
    "codex": _ToolDef(
        id="codex",
        label="Codex (OpenAI)",
        exe="codex",
        args=["exec", "--prompt", "{prompt}"],
    ),
    "copilot": _ToolDef(
        id="copilot",
        label="GitHub Copilot",
        exe="gh",
        args=["copilot", "suggest", "{prompt}"],
    ),
    "zcode": _ToolDef(
        id="zcode",
        label="ZCode",
        exe="zcode",
        args=["-p", "{prompt}"],
    ),
}


# ── detection ───────────────────────────────────────────────────────────────

def detect(tool_id: str) -> str | None:
    """Return the full path to *tool_id* if installed, otherwise *None*.

    Uses ``shutil.which()`` and, on Windows, also tries known suffixes (``.exe``,
    ``.cmd``).
    """
    td = _TOOLS.get(tool_id)
    if td is None:
        return None
    path = shutil.which(td.exe)
    if path is None and sys.platform == "win32":
        for suffix in (".exe", ".cmd"):
            path = shutil.which(td.exe + suffix)
            if path is not None:
                break
    return path


def detect_all() -> str | None:
    """Return the first available tool id (or *None*)."""
    for tid in _TOOLS:
        if detect(tid) is not None:
            return tid
    return None


# ── provider ─────────────────────────────────────────────────────────────────

class CLIProvider:
    """LLM provider that delegates to a locally-installed AI CLI tool.

    Implements the ``LLMProvider`` protocol's ``chat()`` method by launching
    a subprocess and piping the prompt to stdin.  Output is captured from
    stdout — stderr is forwarded to the Python logger.

    Supported tools (keyed by ``tool_id``):

    * ``opencode``
    * ``codex``
    * ``copilot``

    Parameters:
        tool_id:  one of the supported tool ids.
        timeout:  seconds before the subprocess is killed (default 120).
    """

    # CLI tools that expect the prompt on stdin rather than as an argument.
    _STDIN_TOOLS: ClassVar[set[str]] = {"codex", "copilot"}

    def __init__(self, tool_id: str, timeout: int = 120) -> None:
        td = _TOOLS.get(tool_id)
        if td is None:
            raise ValueError(f"Unknown CLI tool: {tool_id}")
        self._td = td
        self._exe_path = detect(tool_id) or td.exe  # fallback to bare name
        self._timeout = timeout

    # -- public API -----------------------------------------------------------

    def chat(self, messages: list[Message], **opts) -> str:
        """Run the CLI tool with a prompt built from *messages*."""
        prompt = self._build_prompt(messages)
        timeout = opts.get("timeout", self._timeout)
        return self._run(prompt, timeout=timeout)

    @property
    def tool_id(self) -> str:
        return self._td.id

    @property
    def tool_label(self) -> str:
        return self._td.label

    # -- helpers --------------------------------------------------------------

    def _build_prompt(self, messages: list[Message]) -> str:
        """Concatenate system + user messages into a single prompt string.

        The system message (if any) is prepended inside a ``[SYSTEM]`` block
        so the CLI tool understands the role distinction.
        """
        parts: list[str] = []
        for m in messages:
            if m.role == "system":
                parts.append(f"[SYSTEM]\n{m.content}")
            else:
                parts.append(m.content)
        return "\n\n".join(parts)

    def _run(self, prompt: str, timeout: int) -> str:
        """Execute the CLI tool and return its stdout.

        On failure raises ``RuntimeError``.
        """
        use_stdin = self._td.id in self._STDIN_TOOLS

        if use_stdin:
            args = [self._exe_path] + self._td.args
            logger.debug("running (stdin): %s", args)
            try:
                proc = subprocess.run(
                    args,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{self._td.label} timed out after {timeout}s")
        else:
            # Replace {prompt} in the argument list
            args = [self._exe_path] + [
                a.replace("{prompt}", prompt) for a in self._td.args
            ]
            logger.debug("running: %s", args)
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{self._td.label} timed out after {timeout}s")

        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:500]
            raise RuntimeError(
                f"{self._td.label} exited with code {proc.returncode}"
                + (f": {stderr}" if stderr else "")
            )

        output = proc.stdout.strip()
        if not output:
            raise RuntimeError(f"{self._td.label} returned empty output")

        # Some tools wrap output in markdown code fences — strip the outermost
        # ``` … ``` if present so JSON parsing works downstream.
        if output.startswith("```"):
            # Remove opening fence (may include language hint like ```json)
            output = output.split("\n", 1)[1] if "\n" in output else output[3:]
            if output.endswith("```"):
                output = output[:-3].rstrip()

        return output
