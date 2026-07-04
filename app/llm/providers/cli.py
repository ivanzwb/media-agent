from __future__ import annotations

import json
import logging
import os
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
        # --dangerously-skip-permissions keeps the daemon non-interactive
        # (Multica uses this flag instead of --auto).
        # --model is appended dynamically by _run() when the caller provides
        # a model hint via opts["model"].
        args=["run", "--format", "json", "--dangerously-skip-permissions", "{prompt}"],
    ),
    "claude": _ToolDef(
        id="claude",
        label="Claude Code",
        exe="claude",
        # -p  = non-interactive pipe mode.
        # --permission-mode bypassPermissions auto-approves tool calls
        # in daemon execution (Multica uses this for autonomous runs).
        args=["-p", "--permission-mode", "bypassPermissions", "{prompt}"],
    ),
    "codex": _ToolDef(
        id="codex",
        label="Codex (OpenAI)",
        exe="codex",
        # Multica uses a complex app-server + JSON-RPC protocol for
        # persistent daemons; our simpler exec --prompt is sufficient
        # for one-shot reuse-oriented usage.
        args=["exec", "--prompt", "{prompt}"],
    ),
    "copilot": _ToolDef(
        id="copilot",
        label="GitHub Copilot",
        exe="copilot",
        # Standalone Copilot CLI (npm: @github/copilot-cli).
        # --allow-all bypasses tool/path/URL permission prompts;
        # --no-ask-user prevents interactive questions.
        # --output-format json gives structured NDJSON output
        # for parsing (Multica uses exactly these flags).
        args=["-p", "{prompt}", "--output-format", "json", "--allow-all", "--no-ask-user"],
    ),
    "gemini": _ToolDef(
        id="gemini",
        label="Gemini CLI",
        exe="gemini",
        # Google Gemini CLI with --yolo (auto-approve tools) and
        # -o stream-json (structured NDJSON output).
        # Based on Multica's gemini backend.
        args=["-p", "{prompt}", "--yolo", "-o", "stream-json"],
    ),
    "cursor-agent": _ToolDef(
        id="cursor-agent",
        label="Cursor Agent（Cursor 编辑器 headless CLI）",
        exe="cursor-agent",
        # cursor-agent 接受直接 prompt 参数（与 Multica 调用方式一致）
        args=["{prompt}"],
    ),
}


# ── model flag per tool ───────────────────────────────────────────────────

_MODEL_FLAGS: dict[str, str] = {
    # Multica consistently supports --model across claude, copilot, and opencode.
    # gemini uses -m (short flag) instead.
    "opencode": "--model",
    "claude": "--model",
    "copilot": "--model",
    "gemini": "-m",
}

# Default models per tool — used when neither the caller passes a model nor
# MEDIA_AGENT_LLM_MODEL is configured.  The opencode default model
_DEFAULT_MODELS: dict[str, str] = {
    "opencode": "opencode/big-pickle",
}


# ── detection ───────────────────────────────────────────────────────────────

def _env_override(tool_id: str) -> str | None:
    """Check for ``MEDIA_AGENT_CLI_{TOOL_ID}_PATH`` env var override.

    Mirrors Multica's ``MULTICA_*_PATH`` pattern (``exec.LookPath`` +
    env-var override), adapted to this project's ``MEDIA_AGENT_*``
    naming convention.
    """
    var = f"MEDIA_AGENT_CLI_{tool_id.upper().replace('-', '_')}_PATH"
    val = os.environ.get(var)
    if val and os.path.isfile(val):
        return os.path.realpath(val)
    return None


def detect(tool_id: str) -> str | None:
    """Return the full path to *tool_id* if installed, otherwise *None*.

    Resolution order (matching Multica's probe pattern):
    1. ``MEDIA_AGENT_CLI_{TOOL_ID}_PATH`` env var override.
    2. ``shutil.which()`` — PATH search; on Windows also tries ``.exe``
       and ``.cmd`` suffixes.
    """
    td = _TOOLS.get(tool_id)
    if td is None:
        return None
    # 1. env var override (like Multica's MULTICA_*_PATH)
    override = _env_override(tool_id)
    if override:
        return override
    # 2. PATH search
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


# ── Windows native binary resolution ─────────────────────────────────────────

def _resolve_windows_native(exe_path: str) -> str:
    """Resolve the native Windows executable behind an npm-installed .cmd shim.

    On Windows, ``opencode`` installed via ``npm install -g opencode-ai``
    resolves to a ``opencode.cmd`` batch shim.  Batch's ``%*`` argument
    forwarding does **not** preserve newlines, so multi-line prompts get
    silently truncated at the first line break.

    This function checks whether *exe_path* points to a ``.cmd`` shim and,
    if so, looks for the bundled native ``.exe`` inside the npm package
    directory tree (``opencode-windows-{x64,x64-baseline,arm64}``).

    Returns the native binary path when found, otherwise returns
    ``exe_path`` unchanged.
    """
    if sys.platform != "win32":
        return exe_path
    if not exe_path.lower().endswith(".cmd"):
        return exe_path

    prefix = os.path.dirname(exe_path)
    # Try platform packages in priority order — baseline x64 (no AVX2)
    # first, then regular x64, then arm64 as final fallback.
    #
    # Baseline is preferred because the regular x64 binary (Bun v1.3.14)
    # segfaults on some Windows 11 / CPU combinations on startup
    # (``opencode --help`` returns SIGSEGV), while the baseline variant
    # works correctly on the same hardware.
    for pkg in (
        "opencode-windows-x64-baseline",
        "opencode-windows-x64",
        "opencode-windows-arm64",
    ):
        candidate = os.path.join(
            prefix,
            "node_modules",
            "opencode-ai",
            "node_modules",
            pkg,
            "bin",
            "opencode.exe",
        )
        if os.path.isfile(candidate):
            return candidate

    return exe_path


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

    def __init__(self, tool_id: str, timeout: int = 300,
                 model: str = "") -> None:
        td = _TOOLS.get(tool_id)
        if td is None:
            raise ValueError(f"Unknown CLI tool: {tool_id}")
        self._td = td
        exe_path = detect(tool_id) or td.exe  # fallback to bare name
        # On Windows, resolve npm .cmd shim to the native .exe to avoid
        # batch newline truncation in multi-line prompts.  The baseline
        # x64 variant (no AVX2) is preferred because the regular x64
        # binary segfaults on some Win11/CPU combos.
        if tool_id == "opencode":
            exe_path = _resolve_windows_native(exe_path)
        self._exe_path = exe_path
        self._timeout = timeout
        self._default_model = model

    # -- public API -----------------------------------------------------------

    def chat(self, messages: list[Message], **opts) -> str:
        """Run the CLI tool with a prompt built from *messages*."""
        prompt = self._build_prompt(messages)
        timeout = opts.get("timeout", self._timeout)
        model = opts.get("model", "")
        return self._run(prompt, timeout=timeout, model=model)

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
        result = "\n\n".join(parts)
        # Sanitize: Bun (OpenCode's runtime) segfaults on null bytes in
        # the prompt string.  Strip them unconditionally for safety.
        return result.replace("\x00", "")

    def _run(self, prompt: str, timeout: int, model: str = "") -> str:
        """Execute the CLI tool and return its stdout.

        On failure raises ``RuntimeError``.
        """
        # Bun segfaults on null bytes in the prompt.  Strip them.
        prompt = prompt.replace("\x00", "")
        kwargs: dict = dict(
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        # If no explicit model was passed, fall back to the default model
        # configured at construction time (e.g. from MEDIA_AGENT_LLM_MODEL),
        # then to the built-in per-tool fallback (e.g. opencode needs a
        # working model because its default is broken on many systems).
        effective_model = (model or self._default_model
                           or _DEFAULT_MODELS.get(self._td.id, ""))

        try:
            # Build the argument list, replacing {prompt} placeholders
            # and injecting --model / -m when the tool supports it.
            raw_args = list(self._td.args)
            if effective_model and self._td.id in _MODEL_FLAGS:
                prompt_idx = raw_args.index("{prompt}")
                raw_args[prompt_idx:prompt_idx] = [_MODEL_FLAGS[self._td.id], effective_model]
            args = [self._exe_path] + [
                a.replace("{prompt}", prompt) for a in raw_args
            ]
            logger.debug("running: %s", args)
            proc = subprocess.run(args, **kwargs)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{self._td.label} timed out after {timeout}s")
        except ValueError as e:
            raise RuntimeError(
                f"{self._td.label} rejected: {e}. "
                "The prompt contains characters the OS cannot pass via command line."
            ) from None

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

            # OpenCode --format json: text in part.text
            part = event.get("part") or {}
            text = part.get("text") if isinstance(part, dict) else None
            if text and isinstance(text, str):
                contents.append(text)
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
