from __future__ import annotations

import json
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
        args=["run", "--auto", "--format", "json", "{prompt}"],
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
        args=["copilot", "-p", "{prompt}"],
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
    # All current tools use {prompt} arg substitution — kept as empty set
    # for future use if a stdin-based tool is added.
    _STDIN_TOOLS: ClassVar[set[str]] = set()

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
        kwargs: dict = dict(
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        if use_stdin:
            args = [self._exe_path] + self._td.args
            logger.debug("running (stdin): %s", args)
            try:
                proc = subprocess.run(args, input=prompt, **kwargs)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{self._td.label} timed out after {timeout}s")
        else:
            # Replace {prompt} in the argument list
            args = [self._exe_path] + [
                a.replace("{prompt}", prompt) for a in self._td.args
            ]
            logger.debug("running: %s", args)
            try:
                proc = subprocess.run(args, **kwargs)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{self._td.label} timed out after {timeout}s")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:500]
            stdout = (proc.stdout or "").strip()
            # Some tools (e.g. opencode --format json) write the real error
            # message to stdout as a JSON error event, not to stderr.
            if stdout.startswith("{"):
                try:
                    parsed = self._parse_json_events(stdout)
                except RuntimeError as json_err:
                    # JSON error events found — use that message
                    raise RuntimeError(
                        f"{self._td.label} exited with code {proc.returncode}: {json_err}"
                    ) from None
                if parsed and parsed != stdout:
                    raise RuntimeError(
                        f"{self._td.label} exited with code {proc.returncode}: {parsed}"
                    )
            raise RuntimeError(
                f"{self._td.label} exited with code {proc.returncode}"
                + (f": {stderr}" if stderr else "")
            )

        output = (proc.stdout or "").strip()
        if not output:
            raise RuntimeError(f"{self._td.label} returned empty output")

        # ── JSON event stream handler (e.g. opencode --format json) ─────────
        # Some tools output newline-delimited JSON events.  Extract the
        # assistant response from events that carry a ``content`` field.
        if output.startswith("{"):
            parsed = self._parse_json_events(output)
            if parsed is not None:
                return parsed

        # Some tools wrap output in markdown code fences — strip the outermost
        # ``` … ``` if present so JSON parsing works downstream.
        if output.startswith("```"):
            # Remove opening fence (may include language hint like ```json)
            output = output.split("\n", 1)[1] if "\n" in output else output[3:]
            if output.endswith("```"):
                output = output[:-3].rstrip()

        return output

    @staticmethod
    def _parse_json_events(output: str) -> str | None:
        """Parse newline-delimited JSON events and extract response content.

        Returns *None* when the output is not recognizable as structured
        JSON events, allowing the caller to fall back to plain-text handling.
        Raises ``RuntimeError`` if all events are errors.
        """
        contents: list[str] = []
        errors: list[str] = []
        parsed_any = False
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_any = True

            # Error events
            if event.get("type") == "error":
                err = event.get("error", {})
                msg = err.get("data", {}).get("message", "") or err.get("message", "")
                if msg:
                    errors.append(msg)
                continue

            # Assistant response content
            content = event.get("content")
            if content and isinstance(content, str):
                contents.append(content)
                continue

            # Some events carry a top-level message field
            msg = event.get("message")
            if msg and isinstance(msg, str):
                contents.append(msg)

        if not parsed_any:
            return None  # not JSON events — let caller handle as plain text

        if errors and not contents:
            raise RuntimeError("; ".join(errors))

        if contents:
            return "\n".join(contents)

        # JSON events were found but no content — still return the raw
        # output so the caller gets something to show.
        return output
