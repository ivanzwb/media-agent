from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
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
        # --model is appended dynamically by _run() when the caller provides
        # a model hint via opts["model"].
        args=["run", "--format", "json", "{prompt}"],
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


# ── transient failure retry ─────────────────────────────────────────────────
#
# A CLI agent fronts a remote model service, so a run can die before it ever
# reaches the model — the local server returns "Unexpected server error" when
# it cannot fetch its model catalog, upstream answers 5xx, the connection is
# reset, and so on.  Those failures clear on their own, so retry them the way
# OpenAIProvider does instead of losing the whole run.
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0     # seconds — exponential backoff base

_TRANSIENT_MARKERS = (
    "unexpected server error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "overloaded",
    "rate limit",
    "too many requests",
    "unable to connect",
    "cannot connect",
    "connection reset",
    "connection closed",
    "connection refused",
    "socket connection was closed",
    "fetch failed",
    "network error",
    "stream error",
    "econnreset",
    "econnrefused",
    "etimedout",
    "enotfound",
    "eai_again",
)

# HTTP status codes worth another attempt, matched as standalone numbers so
# they do not fire on token counts or ids that merely contain the digits.
_TRANSIENT_STATUS_RE = re.compile(r"(?<!\d)(429|5[0-9]{2})(?!\d)")


class _TransientCLIError(RuntimeError):
    """A CLI failure that is worth retrying.

    Subclasses ``RuntimeError`` so callers that already handle CLI failures
    keep working when the retries are exhausted.
    """


def _is_transient(message: str) -> bool:
    text = (message or "").lower()
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return True
    return bool(_TRANSIENT_STATUS_RE.search(text))


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

    def __init__(self, tool_id: str, timeout: int = 500,
                 model: str = "", cwd: str | Path | None = None) -> None:
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
        self._cwd = str(cwd) if cwd else None
        self._proc: subprocess.Popen | None = None  # running subprocess handle
        self._cancelled = False

    # -- public API -----------------------------------------------------------

    def chat(self, messages: list[Message], **opts) -> str:
        """Run the CLI tool with a prompt built from *messages*."""
        prompt = self._build_prompt(messages)
        timeout = opts.get("timeout", self._timeout)
        model = opts.get("model", "")
        return self._run(
            prompt, timeout=timeout, model=model,
            allow_empty_output=bool(opts.get("allow_empty_output", False)))

    @property
    def tool_id(self) -> str:
        return self._td.id

    @property
    def tool_label(self) -> str:
        return self._td.label

    def cancel(self) -> bool:
        """Kill the running subprocess. Returns True if a process was killed."""
        # Set first: a cancel that lands between two attempts finds no live
        # process, and must still stop the retry loop from starting another.
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.kill()
            proc.wait(timeout=5)
            logger.info("CLI agent %s process %d killed", self._td.id, proc.pid)
            return True
        except Exception as e:
            logger.warning("Failed to kill CLI agent %s: %s", self._td.id, e)
            return False

    @property
    def is_running(self) -> bool:
        """True if a subprocess is currently executing."""
        return self._proc is not None and self._proc.poll() is None

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

    def _run(self, prompt: str, timeout: int, model: str = "",
             allow_empty_output: bool = False) -> str:
        """Execute the CLI tool and return its stdout, retrying hiccups.

        Only failures that look like a transient upstream problem are retried.
        A timeout has already consumed the caller's whole budget and a cancel
        must stay cancelled, so neither gets another attempt.

        On failure raises ``RuntimeError``.
        """
        attempt = 1
        while True:
            try:
                return self._run_once(prompt, timeout=timeout, model=model,
                                      allow_empty_output=allow_empty_output)
            except _TransientCLIError as exc:
                if attempt >= _MAX_ATTEMPTS or self._cancelled:
                    raise
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d/%d failed (%s), retrying in %.1fs…",
                    self._td.label, attempt, _MAX_ATTEMPTS, exc, delay)
                time.sleep(delay)
                if self._cancelled:
                    raise
                attempt += 1

    def _failure_detail(self, stdout: str | None, stderr: str | None) -> str:
        """Best available explanation for a non-zero exit."""
        stdout_text = (stdout or "").strip()
        # Some tools (e.g. opencode --format json) write the real error
        # message to stdout as a JSON error event, not to stderr.
        if stdout_text.startswith("{"):
            try:
                parsed = self._parse_json_events(stdout_text)
            except RuntimeError as json_err:
                return str(json_err)   # JSON error events carry the message
            if parsed and parsed != stdout_text:
                return parsed
        return (stderr or "").strip()[:500]

    def _run_once(self, prompt: str, timeout: int, model: str = "",
                  allow_empty_output: bool = False) -> str:
        """Execute the CLI tool once and return its stdout.

        Raises ``_TransientCLIError`` when the failure is worth another
        attempt, plain ``RuntimeError`` otherwise.
        """
        # Bun segfaults on null bytes in the prompt.  Strip them.
        prompt = prompt.replace("\x00", "")

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
            # Use a *local* handle for this call.  The provider instance is
            # shared across threads (auto-discover fans out parallel chat()
            # calls), so relying on self._proc for communicate()/kill() would
            # race — one thread could wait on / kill another thread's process
            # and deadlock.  self._proc is only mirrored for best-effort
            # cancel()/is_running observability.
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=self._cwd,
            )
            self._proc = proc
            returncode = -1
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise RuntimeError(f"{self._td.label} timed out after {timeout}s")
            finally:
                if self._proc is proc:
                    self._proc = None
        except ValueError as e:
            raise RuntimeError(
                f"{self._td.label} rejected: {e}. "
                "The prompt contains characters the OS cannot pass via command line."
            ) from None

        if returncode is not None and returncode != 0:
            detail = self._failure_detail(stdout, stderr)
            message = (f"{self._td.label} exited with code {returncode}"
                       + (f": {detail}" if detail else ""))
            if _is_transient(detail):
                raise _TransientCLIError(message) from None
            raise RuntimeError(message) from None

        output = (stdout or "").strip()
        if not output:
            if allow_empty_output:
                return ""
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
