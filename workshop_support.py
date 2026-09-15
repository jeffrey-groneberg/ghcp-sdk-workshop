"""Supplied workshop plumbing: bounded tools and durable artifacts, not an agent loop."""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import math
import os
import platform
import re
import shutil
import struct
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from copilot import (
    MCPStdioServerConfig,
    PermissionRequest,
    PermissionRequestResult,
    PostToolUseFailureHookInput,
    PostToolUseFailureHookOutput,
    PostToolUseHookInput,
    PostToolUseHookOutput,
    PreToolUseHookInput,
    PreToolUseHookOutput,
    SessionHooks,
    Tool,
    define_tool,
)
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import (
    PermissionRequestCustomTool,
    PermissionRequestMcp,
    PermissionRequestRead,
    PermissionRequestUrl,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

ROOT = Path(__file__).resolve().parent
MCP_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_wait_for",
    "browser_take_screenshot",
]
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_TOOL_CALLS = 24
Observation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class WorkshopError(Exception):
    """An actionable workshop failure."""


class FinishRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["baseline", "unchanged", "changed"]
    summary: str = Field(min_length=1, max_length=2000, description="A short plain-text conclusion.")
    changes: list[Observation] = Field(
        default_factory=list,
        max_length=20,
        description="Concrete plain-text observations. Nonempty only for a changed page.",
    )


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    url: str
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    captured_at: AwareDatetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["baseline", "unchanged", "changed"]


def normalize_url(value: str) -> str:
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise WorkshopError("The URL must not contain whitespace or control characters.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise WorkshopError(f"Invalid URL: {error}") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise WorkshopError("Use an explicit http:// or https:// URL.")
    if parts.username is not None or parts.password is not None or port == 0:
        raise WorkshopError("Use a URL without embedded credentials and with a valid port.")
    return urlunsplit(parts._replace(path=parts.path or "/"))


def copilot_environment(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    environment = dict(environ)
    if environment.get("CODESPACES") == "true":
        # Empty overrides also work when the SDK merges its child environment.
        environment.update(GH_TOKEN="", GITHUB_TOKEN="")
    return environment


def image_digest(path: Path, limit: int = MAX_IMAGE_BYTES) -> str:
    if path.is_symlink() or not path.is_file():
        raise WorkshopError(f"Expected a regular screenshot file: {path.name}")
    if path.stat().st_size > limit:
        raise WorkshopError(f"{path.name} exceeds the {limit // 1024} KiB image limit. Use a shorter page.")
    content = path.read_bytes()
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise WorkshopError(f"{path.name} is not a PNG screenshot.")
    width, height = struct.unpack(">II", content[16:24])
    if not width or not height:
        raise WorkshopError(f"{path.name} has invalid image dimensions.")
    return hashlib.sha256(content).hexdigest()


def markdown_text(value: str) -> str:
    text = html.escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\`*_\[\]{}()#!|$])", r"\\\1", text)


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class RunContext:
    url: str
    key: str
    directory: Path
    monitor_directory: Path
    previous: Snapshot | None
    image_limit: int = MAX_IMAGE_BYTES
    navigated: bool = False
    captured_at: datetime | None = None
    inspected: dict[str, str] = field(default_factory=dict)
    staged: FinishRunParams | None = None
    fatal_error: str | None = None

    @property
    def after(self) -> Path:
        return self.directory / "after.png"

    @property
    def before(self) -> Path:
        return self.directory / "before.png"

    def context(self) -> dict[str, object]:
        return {
            "url": self.url,
            "previous_screenshot": str(self.before) if self.previous else None,
            "previous_capture": self.previous.captured_at.isoformat() if self.previous else None,
            "current_screenshot": str(self.after),
            "screenshot_arguments": {
                "filename": str(self.after), "fullPage": True, "type": "png", "scale": "css",
            },
            "output_tool": "finish_run",
        }

    def mcp_server(self) -> MCPStdioServerConfig:
        node = shutil.which("node")
        entrypoint = ROOT / "node_modules/@playwright/mcp/cli.js"
        if not node or not entrypoint.is_file():
            raise WorkshopError("Node/MCP is missing. Complete Codespaces setup or run npm ci.")
        return {
            "type": "local",
            "command": node,
            "args": [
                str(entrypoint),
                "--config", str(ROOT / "playwright-mcp.config.json"),
                "--output-dir", str(self.directory),
            ],
            "working_directory": str(self.directory),
            "tools": MCP_TOOLS.copy(),
            "timeout": 45000,
            "env": {"COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        }

    def validate_result(self, result: FinishRunParams) -> None:
        if self.fatal_error:
            raise WorkshopError(self.fatal_error)
        if not self.navigated or self.captured_at is None:
            raise WorkshopError("Navigate to the page and successfully capture it before finishing.")
        expected = [self.after, self.before] if self.previous else [self.after]
        for path in expected:
            if self.inspected.get(path.name) != image_digest(path, self.image_limit):
                raise WorkshopError(f"Successfully view the current {path.name} before finishing.")
        if (result.status == "baseline") != (self.previous is None):
            raise WorkshopError("Use baseline only for a first run; otherwise compare with before.png.")
        if (result.status == "changed") != bool(result.changes):
            raise WorkshopError("Provide observations for changed, and an empty changes list otherwise.")

    def finish_tool(self) -> Tool:
        @define_tool(
            name="finish_run",
            description=(
                "Stage the final outcome after inspecting the screenshot(s). "
                "Use baseline when no previous screenshot exists, otherwise unchanged or changed. "
                "The host writes Markdown with local screenshot links only for changed. "
                "After this succeeds, end your response without calling more tools."
            ),
            defer="never",
        )
        async def finish_run(params: FinishRunParams) -> dict[str, str]:
            if self.staged is not None:
                raise WorkshopError("This run is already finalized.")
            self.validate_result(params)
            self.staged = params
            return {"status": "staged", "outcome": params.status, "next": "End your response."}

        return finish_run

    def publish(self) -> tuple[FinishRunParams, Path | None]:
        if self.staged is None:
            raise WorkshopError("The agent did not finish_run. No report or baseline was committed.")
        result = self.staged
        self.validate_result(result)
        if self.captured_at is None:
            raise WorkshopError("Missing capture timestamp.")
        snapshot = Snapshot(
            url=self.url,
            key=self.key,
            run_id=self.directory.name,
            captured_at=self.captured_at,
            sha256=image_digest(self.after, self.image_limit),
            status=result.status,
        )
        report = None
        if result.status == "changed":
            if self.previous is None:
                raise WorkshopError("A change report requires a previous snapshot.")
            report = self.directory / "report.md"
            lines = [
                "# Webpage change report", "",
                f"**URL:** {markdown_text(self.url)}",
                f"**Before:** {self.previous.captured_at.isoformat()}",
                f"**After:** {self.captured_at.isoformat()}", "",
                "## Summary", "", markdown_text(result.summary), "",
                "## Observed changes", "",
                *[f"- {markdown_text(change)}" for change in result.changes], "",
                "## Screenshots", "",
                "### Before", "", "![Before](before.png)", "",
                "### After", "", "![After](after.png)", "",
                "*AI visual interpretation, not a pixel-exact comparison. Review the evidence.*", "",
            ]
            atomic_write(report, "\n".join(lines))
        atomic_write(self.directory / "outcome.json", result.model_dump_json(indent=2))
        atomic_write(self.directory / "snapshot.json", snapshot.model_dump_json(indent=2))
        atomic_write(self.monitor_directory / "latest.json", snapshot.model_dump_json(indent=2))
        return result, report


@contextmanager
def open_run(url: str, output: Path) -> Iterator[RunContext]:
    url = normalize_url(url)
    capture = json.loads((ROOT / "playwright-mcp.config.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    identity = {
        "url": url,
        "capture": capture,
        "browser_version": lock["packages"]["node_modules/playwright"]["version"],
        "platform": [sys.platform, platform.machine()],
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    monitor_directory = output.resolve() / key
    monitor_directory.mkdir(parents=True, exist_ok=True)
    with (monitor_directory / ".lock").open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkshopError("Another run is already checking this URL with this configuration.") from error
        latest = monitor_directory / "latest.json"
        previous = None
        old_image = None
        if latest.exists():
            try:
                previous = Snapshot.model_validate_json(latest.read_text(encoding="utf-8"))
            except ValidationError as error:
                raise WorkshopError(f"Invalid baseline in {latest}; it has not been reset.") from error
            if previous.url != url or previous.key != key:
                raise WorkshopError("The baseline belongs to a different URL or capture configuration.")
            old_image = monitor_directory / "runs" / previous.run_id / "after.png"
            if image_digest(old_image) != previous.sha256:
                raise WorkshopError("The previous screenshot was modified; the baseline is not trustworthy.")
        directory = monitor_directory / "runs" / uuid4().hex
        directory.mkdir(parents=True)
        run = RunContext(url, key, directory, monitor_directory, previous)
        if old_image is not None:
            shutil.copyfile(old_image, run.before)
        yield run


class ToolGuard:
    def __init__(self, run: RunContext, max_calls: int = MAX_TOOL_CALLS):
        self.run = run
        self.max_calls = max_calls
        self.calls = 0

    def resolve_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise WorkshopError("Provide a screenshot path.")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.run.directory / candidate
        if candidate.is_symlink():
            raise WorkshopError("Symlinks are not permitted.")
        return candidate.resolve()

    def check(self, name: str, args: object) -> None:
        if self.run.staged is not None:
            raise WorkshopError("The outcome is already staged. End your response.")
        if not isinstance(args, dict):
            raise WorkshopError("Tool arguments must be an object.")
        if name == "view":
            if set(args) - {"path", "view_range", "forceReadLargeFiles"}:
                raise WorkshopError("Use the documented view options: path, view_range, forceReadLargeFiles.")
            if "forceReadLargeFiles" in args and not isinstance(args["forceReadLargeFiles"], bool):
                raise WorkshopError("forceReadLargeFiles must be a boolean.")
            allowed = {self.run.after}
            if self.run.previous:
                allowed.add(self.run.before)
            if self.resolve_path(args.get("path")) not in allowed:
                raise WorkshopError("Only the assigned before.png and after.png may be viewed.")
            path = self.resolve_path(args.get("path"))
            if path == self.run.after and self.run.captured_at is None:
                raise WorkshopError("Capture after.png with Playwright before viewing it.")
            image_digest(path, self.run.image_limit)
        elif name == "playwright-browser_navigate":
            if set(args) != {"url"} or not isinstance(args["url"], str):
                raise WorkshopError("Navigation requires the assigned URL.")
            if normalize_url(args["url"]) != self.run.url:
                raise WorkshopError("Navigation is restricted to the assigned URL.")
        elif name == "playwright-browser_take_screenshot":
            if not self.run.navigated:
                raise WorkshopError("Navigate to the assigned page before taking a screenshot.")
            if (
                set(args) - {"filename", "fullPage", "type", "scale"}
                or self.resolve_path(args.get("filename")) != self.run.after
                or args.get("fullPage") is not True
                or args.get("type", "png") != "png"
                or args.get("scale") != "css"
            ):
                raise WorkshopError("Save only after.png with fullPage=true, type=png, and scale=css.")
        elif name == "playwright-browser_snapshot":
            if set(args) - {"target", "depth", "boxes"}:
                raise WorkshopError("Return the page snapshot inline; do not write another file.")
        elif name == "playwright-browser_wait_for":
            if not args or set(args) - {"text", "textGone", "time"}:
                raise WorkshopError("Wait for page text or a short duration.")
            delay = args.get("time", 0)
            if not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 0 <= delay <= 5:
                raise WorkshopError("Each explicit wait must be between zero and five seconds.")
        elif name == "finish_run":
            self.run.validate_result(FinishRunParams.model_validate(args))
        else:
            raise WorkshopError(f"Tool not permitted: {name}")

    def before(self, data: PreToolUseHookInput, _context: object) -> PreToolUseHookOutput:
        self.calls += 1
        name = data["toolName"]
        try:
            if self.calls > self.max_calls:
                self.run.fatal_error = f"The run exceeded its {self.max_calls}-tool limit."
                raise WorkshopError(self.run.fatal_error)
            self.check(name, data["toolArgs"])
        except (WorkshopError, ValidationError, OSError) as error:
            print(f"[blocked] {name}: {error}", file=sys.stderr, flush=True)
            return {"permissionDecision": "deny", "permissionDecisionReason": str(error)}
        if name == "playwright-browser_navigate":
            self.run.navigated = False
        if name in {"playwright-browser_navigate", "playwright-browser_take_screenshot"}:
            self.run.captured_at = None
            self.run.inspected.pop("after.png", None)
        label = f"{name} {Path(data['toolArgs']['path']).name}" if name == "view" else name
        print(f"[tool] {label}", flush=True)
        return {}

    def after(self, data: PostToolUseHookInput, _context: object) -> PostToolUseHookOutput:
        name = data["toolName"]
        try:
            if name == "playwright-browser_navigate":
                self.run.navigated = True
            elif name == "playwright-browser_take_screenshot":
                image_digest(self.run.after, self.run.image_limit)
                self.run.captured_at = datetime.now(UTC)
            elif name == "view":
                path = self.resolve_path(data["toolArgs"].get("path"))
                self.run.inspected[path.name] = image_digest(path, self.run.image_limit)
        except (WorkshopError, OSError) as error:
            print(f"[error] {name}: {error}", file=sys.stderr, flush=True)
            return {"additionalContext": f"Artifact validation failed: {error}. Do not finish yet."}
        return {}

    def failed(
        self, data: PostToolUseFailureHookInput, _context: object
    ) -> PostToolUseFailureHookOutput:
        print(f"[error] {data['toolName']}: {data['error']}", file=sys.stderr, flush=True)
        return {}

    def permission(self, request: PermissionRequest, _context: object) -> PermissionRequestResult:
        if getattr(request, "managed_approval_required", False):
            return PermissionDecisionReject(feedback="Organization policy requires interactive approval.")
        if getattr(request, "request_sandbox_bypass", False):
            return PermissionDecisionReject(feedback="Sandbox bypass is not permitted.")
        try:
            if isinstance(request, PermissionRequestRead):
                self.check("view", {"path": request.path})
            elif isinstance(request, PermissionRequestMcp) and request.server_name == "playwright":
                name = request.tool_name.removeprefix("playwright-")
                if name not in MCP_TOOLS:
                    raise WorkshopError("This MCP tool is not enabled for the workshop.")
                self.check(f"playwright-{name}", request.args)
            elif isinstance(request, PermissionRequestCustomTool):
                self.check(request.tool_name, request.args)
            elif isinstance(request, PermissionRequestUrl) and normalize_url(request.url) == self.run.url:
                pass
            else:
                raise WorkshopError("This permission is outside the workshop's tool scope.")
        except (WorkshopError, ValidationError, OSError) as error:
            print(f"[permission denied] {error}", file=sys.stderr, flush=True)
            return PermissionDecisionReject(feedback=str(error))
        return PermissionDecisionApproveOnce()

    def hooks(self) -> SessionHooks:
        return {
            "on_pre_tool_use": self.before,
            "on_post_tool_use": self.after,
            "on_post_tool_use_failure": self.failed,
        }
