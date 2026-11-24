import argparse
import ast
import asyncio
import io
import json
import re
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from copilot import ModelPolicyState, ToolInvocation
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import PermissionRequestMcp, PermissionRequestRead
from pydantic import ValidationError

import watch
from workshop_support import (
    MCP_TOOLS,
    ROOT,
    FinishRunParams,
    ToolGuard,
    WorkshopError,
    atomic_write,
    github_token,
    image_digest,
    normalize_url,
    open_run,
)

URL = "http://127.0.0.1:8000/"


def png(color=(20, 40, 60)):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(color) * 2) * 2))
        + chunk(b"IEND", b"")
    )


def event(name, args):
    return {"toolName": name, "toolArgs": args, "toolResult": {}}


def capture_and_inspect(run, color=(20, 40, 60)):
    guard = ToolGuard(run)
    navigate = event("playwright-browser_navigate", {"url": run.url})
    assert guard.before(navigate, {}) == {}
    guard.after(navigate, {})
    capture = event(
        "playwright-browser_take_screenshot",
        {"filename": "after.png", "fullPage": True, "type": "png", "scale": "css"},
    )
    assert guard.before(capture, {}) == {}
    run.after.write_bytes(png(color))
    assert guard.after(capture, {}) == {}
    for path in ([run.before, run.after] if run.previous else [run.after]):
        view = event("view", {"path": str(path)})
        assert guard.before(view, {}) == {}
        assert guard.after(view, {}) == {}
    return guard


async def finish(run, status, changes=None, summary="Observed the page."):
    tool = run.finish_tool()
    return await tool.handler(
        ToolInvocation(
            tool_name="finish_run",
            arguments={"status": status, "summary": summary, "changes": changes or []},
        )
    )


def vision_model(state=ModelPolicyState.ENABLED, images=5, media=("image/png",), vision=True):
    return SimpleNamespace(
        id="test",
        policy=None if state is None else SimpleNamespace(state=state),
        capabilities=SimpleNamespace(
            supports=SimpleNamespace(vision=vision),
            limits=SimpleNamespace(vision=SimpleNamespace(
                max_prompt_images=images,
                max_prompt_image_size=3 * 1024 * 1024,
                supported_media_types=list(media),
            )),
        ),
    )


def fake_client():
    client = AsyncMock()
    client.__aenter__.return_value = client
    return client


def fake_session(session_id="session-1", credits=(0.0, 2.5)):
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.session_id = session_id
    session.rpc.usage.get_metrics.side_effect = [
        SimpleNamespace(total_nano_aiu=value * 1e9, ai_credits_status=SimpleNamespace(value="complete"))
        for value in credits
    ]
    return session


def run_args(output, **overrides):
    values = dict(url=URL, output=output, model="test", timeout=10, list_models=False, max_credits=30.0, remember=False)
    return argparse.Namespace(**{**values, **overrides})


def execute(args, factory, client=None):
    with patch("watch.new_client", return_value=client or fake_client()), patch(
        "watch.available_models", new=AsyncMock(return_value=[vision_model()])
    ):
        asyncio.run(watch.execute(args, factory, watch.TASK))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.output = Path(self.temporary) / "output"
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def baseline(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            asyncio.run(finish(run, "baseline"))
            outcome, report = run.publish()
            self.assertEqual(outcome.status, "baseline")
            self.assertIsNone(report)
            return run.monitor_directory / "latest.json"

    def test_baseline_then_unchanged_advance_without_reports(self):
        latest = self.baseline()
        first = json.loads(latest.read_text())
        with open_run(URL, self.output) as run:
            self.assertEqual(run.previous.run_id, first["run_id"])
            capture_and_inspect(run)
            asyncio.run(finish(run, "unchanged"))
            _, report = run.publish()
            self.assertIsNone(report)
            self.assertNotEqual(json.loads(latest.read_text())["run_id"], first["run_id"])
            self.assertFalse(list(self.output.rglob("report.md")))

    def test_changed_report_has_portable_images_and_escaped_observations(self):
        self.baseline()
        with open_run(URL, self.output) as run:
            capture_and_inspect(run, (90, 50, 10))
            asyncio.run(finish(
                run, "changed",
                ["The price increased.", "![untrusted](https://invalid.example/image) <script>x</script>"],
                "A visible update.",
            ))
            _, report = run.publish()
            body = report.read_text()
            self.assertIn("![Before](before.png)", body)
            self.assertIn("![After](after.png)", body)
            self.assertIn(URL, body)
            self.assertIn("A visible update.", body)
            self.assertNotIn("<script>", body)
            self.assertNotIn("![untrusted]", body)
            for filename in ("before.png", "after.png"):
                self.assertTrue((report.parent / filename).is_file())

    def test_output_tool_is_invoked_and_cannot_finalize_twice(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            first = asyncio.run(finish(run, "baseline"))
            second = asyncio.run(finish(run, "baseline"))
            self.assertEqual(first.result_type, "success")
            self.assertNotEqual(second.result_type, "success")
            self.assertEqual(run.staged.status, "baseline")

    def test_missing_finalization_never_creates_latest(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            with self.assertRaisesRegex(WorkshopError, "did not finish_run"):
                run.publish()
            self.assertFalse((run.monitor_directory / "latest.json").exists())

    def test_cannot_finish_without_capture_or_successful_image_read(self):
        with open_run(URL, self.output) as run:
            result = FinishRunParams(status="baseline", summary="First run.")
            with self.assertRaises(WorkshopError):
                run.validate_result(result)
            capture_and_inspect(run)
            run.inspected.clear()
            with self.assertRaisesRegex(WorkshopError, "view"):
                run.validate_result(result)

    def test_comparison_requires_both_images(self):
        self.baseline()
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            run.inspected.pop("before.png")
            with self.assertRaisesRegex(WorkshopError, "before.png"):
                run.validate_result(FinishRunParams(status="unchanged", summary="Same page."))

    def test_changed_requires_observations_and_cannot_be_first_run(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            with self.assertRaisesRegex(WorkshopError, "baseline"):
                run.validate_result(FinishRunParams(status="changed", summary="Changed.", changes=["New title."]))
        self.baseline()
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            with self.assertRaisesRegex(WorkshopError, "observations"):
                run.validate_result(FinishRunParams(status="changed", summary="Changed."))

    def test_invalid_output_arguments(self):
        for data in (
            {"status": "failed", "summary": "Failure."},
            {"status": "baseline", "summary": "  "},
            {"status": "changed", "summary": "Change.", "changes": ["  "]},
            {"status": "baseline", "summary": "First.", "path": "../../outside.md"},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                FinishRunParams.model_validate(data)

    def test_screenshot_changed_after_inspection_is_rejected(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            run.after.write_bytes(png((255, 255, 255)))
            with self.assertRaisesRegex(WorkshopError, "view"):
                run.validate_result(FinishRunParams(status="baseline", summary="First."))

    def test_failed_recapture_invalidates_old_image(self):
        self.baseline()
        with open_run(URL, self.output) as run:
            guard = capture_and_inspect(run)
            data = event(
                "playwright-browser_take_screenshot",
                {"filename": "after.png", "fullPage": True, "scale": "css"},
            )
            guard.before(data, {})
            guard.failed({**data, "error": "Browser closed"}, {})
            with self.assertRaisesRegex(WorkshopError, "capture"):
                run.validate_result(FinishRunParams(status="unchanged", summary="Same."))

    def test_write_failure_preserves_previous_baseline(self):
        latest = self.baseline()
        old = latest.read_bytes()
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            asyncio.run(finish(run, "changed", ["New title."]))
            with patch("workshop_support.atomic_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    run.publish()
        self.assertEqual(latest.read_bytes(), old)

    def test_atomic_write_keeps_old_file_on_replace_failure(self):
        path = Path(self.temporary) / "state.json"
        path.write_text("old")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_baseline_is_not_silently_reset(self):
        latest = self.baseline()
        latest.write_text('{"version": 42}')
        with self.assertRaisesRegex(WorkshopError, "Invalid baseline"):
            with open_run(URL, self.output):
                self.fail("A corrupt baseline must not start a new run.")
        self.assertEqual(latest.read_text(), '{"version": 42}')

    def test_modified_baseline_image_is_rejected(self):
        latest = self.baseline()
        state = json.loads(latest.read_text())
        (latest.parent / "runs" / state["run_id"] / "after.png").write_bytes(png((255, 0, 0)))
        with self.assertRaisesRegex(WorkshopError, "modified"):
            with open_run(URL, self.output):
                self.fail("Changed baseline image was accepted.")

    def test_other_urls_have_separate_state(self):
        self.baseline()
        with open_run(URL + "another", self.output) as run:
            self.assertIsNone(run.previous)
            self.assertFalse(run.before.exists())

    def test_concurrent_same_page_run_is_rejected(self):
        with open_run(URL, self.output):
            with self.assertRaisesRegex(WorkshopError, "Another run"):
                with open_run(URL, self.output):
                    self.fail("Concurrent run acquired the lock.")

    def test_guard_blocks_unrelated_actions_and_paths(self):
        with open_run(URL, self.output) as run:
            guard = capture_and_inspect(run)
            cases = [
                ("view", {"path": "../secret.png"}),
                ("view", {"path": str(run.directory)}),
                ("view", "after.png"),
                ("playwright-browser_navigate", {"url": "https://invalid.example/"}),
                ("playwright-browser_snapshot", {"filename": "../snapshot.md"}),
                ("playwright-browser_take_screenshot", {"filename": "../after.png", "fullPage": True, "scale": "css"}),
                ("playwright-browser_take_screenshot", {"filename": "after.png", "scale": "css"}),
                ("playwright-browser_wait_for", {"time": 1000}),
                ("playwright-browser_wait_for", {"time": float("nan")}),
                ("playwright-browser_click", {"target": "button"}),
                ("bash", {"command": "anything"}),
            ]
            for name, args in cases:
                with self.subTest(name=name, args=args):
                    self.assertEqual(guard.before(event(name, args), {})["permissionDecision"], "deny")

    def test_symlink_and_oversized_images_are_rejected(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            outside = Path(self.temporary) / "outside.png"
            outside.write_bytes(png())
            run.after.unlink()
            run.after.symlink_to(outside)
            with self.assertRaisesRegex(WorkshopError, "Symlinks"):
                ToolGuard(run).check("view", {"path": str(run.after)})
            with self.assertRaisesRegex(WorkshopError, "exceeds"):
                image_digest(outside, limit=24)

    def test_native_view_large_file_flag_does_not_bypass_image_limits(self):
        with open_run(URL, self.output) as run:
            guard = capture_and_inspect(run)
            args = {"path": str(run.after), "forceReadLargeFiles": True}
            guard.check("view", args)
            with self.assertRaisesRegex(WorkshopError, "boolean"):
                guard.check("view", {**args, "forceReadLargeFiles": "yes"})
            run.image_limit = 24
            with self.assertRaisesRegex(WorkshopError, "exceeds"):
                guard.check("view", args)

    def test_permissions_accept_actual_runtime_mcp_name_and_deny_other_reads(self):
        with open_run(URL, self.output) as run:
            guard = capture_and_inspect(run)
            for name in ("browser_navigate", "playwright-browser_navigate"):
                request = PermissionRequestMcp(
                    read_only=False, server_name="playwright", tool_name=name,
                    tool_title="Navigate", args={"url": URL},
                )
                self.assertIsInstance(guard.permission(request, {}), PermissionDecisionApproveOnce)
            request = PermissionRequestRead(intention="read", path=str(run.after))
            self.assertIsInstance(guard.permission(request, {}), PermissionDecisionApproveOnce)
            request.managed_approval_required = True
            self.assertIsInstance(guard.permission(request, {}), PermissionDecisionReject)
            request = PermissionRequestRead(intention="read", path=str(run.directory / "secret"))
            self.assertIsInstance(guard.permission(request, {}), PermissionDecisionReject)

    def test_tool_budget_prevents_publication(self):
        with open_run(URL, self.output) as run:
            capture_and_inspect(run)
            guard = ToolGuard(run, max_calls=1)
            data = event("view", {"path": str(run.after)})
            self.assertEqual(guard.before(data, {}), {})
            self.assertEqual(guard.before(data, {})["permissionDecision"], "deny")
            with self.assertRaisesRegex(WorkshopError, "tool limit"):
                run.validate_result(FinishRunParams(status="baseline", summary="First."))

    def test_sign_in_uses_an_explicit_token_or_the_gh_login_never_ambient_tokens(self):
        ambient = {"PATH": "/usr/bin", "CODESPACES": "true", "GITHUB_TOKEN": "repo", "GH_TOKEN": "repo2"}
        self.assertEqual(github_token({**ambient, "COPILOT_GITHUB_TOKEN": " explicit "}), "explicit")
        signed_in = SimpleNamespace(returncode=0, stdout="personal\n")
        with patch("workshop_support.shutil.which", return_value="/usr/bin/gh"), patch(
            "workshop_support.subprocess.run", return_value=signed_in
        ) as gh:
            self.assertEqual(github_token(ambient), "personal")
        self.assertNotIn("GITHUB_TOKEN", gh.call_args.kwargs["env"])
        self.assertNotIn("GH_TOKEN", gh.call_args.kwargs["env"])
        self.assertEqual(ambient["GITHUB_TOKEN"], "repo")
        for path, result in ((None, signed_in), ("/usr/bin/gh", SimpleNamespace(returncode=1, stdout=""))):
            with self.subTest(path=path), patch("workshop_support.shutil.which", return_value=path), patch(
                "workshop_support.subprocess.run", return_value=result
            ), self.assertRaisesRegex(WorkshopError, "gh auth login"):
                github_token(ambient)

    def test_model_filter_requires_enabled_png_vision_with_two_images(self):
        self.assertTrue(watch.comparison_model(vision_model()))
        self.assertTrue(watch.comparison_model(vision_model(state=None)))
        for model in (
            vision_model(state=ModelPolicyState.DISABLED),
            vision_model(images=1),
            vision_model(media=("image/jpeg",)),
            vision_model(vision=False),
        ):
            self.assertFalse(watch.comparison_model(model))

    def test_invalid_urls_fail_early(self):
        for url in ("file:///tmp/page", "https://name:password@example.com", "http://host:bad", "http://ho st/", "\nhttp://host"):
            with self.subTest(url=url), self.assertRaises(WorkshopError):
                normalize_url(url)
        self.assertEqual(normalize_url("https://example.com"), "https://example.com/")

    def test_session_connects_only_selected_capabilities(self):
        with open_run(URL, self.output) as run:
            guard = ToolGuard(run)
            client = SimpleNamespace(create_session=AsyncMock())
            asyncio.run(watch.build_session(client, run, guard, "model"))
            options = client.create_session.call_args.kwargs
            self.assertEqual(
                set(options["tools"]["available_tools"].to_list()),
                {"builtin:view", "custom:finish_run", *[f"mcp:playwright-{name}" for name in MCP_TOOLS]},
            )
            self.assertEqual(options["tools"]["definitions"][0].name, "finish_run")
            self.assertTrue(options["tools"]["definitions"][0].is_terminal)
            server = options["mcp"]["servers"]["playwright"]
            self.assertEqual(server["tools"], MCP_TOOLS)
            self.assertEqual(server["working_directory"], str(run.directory))
            self.assertTrue(Path(server["args"][0]).is_file())
            self.assertEqual(options["permissions"]["on_request"], guard.permission)
            self.assertEqual(options["hooks"]["handlers"].keys(), guard.hooks().keys())
            self.assertEqual(options["execution"]["limits"]["max_ai_credits"], run.max_ai_credits)

    def test_remember_resumes_the_previous_session_by_id(self):
        with open_run(URL, self.output) as run:
            run.session_id = "session-1"
            capture_and_inspect(run)
            asyncio.run(finish(run, "baseline"))
            run.publish()
        with open_run(URL, self.output) as run:
            self.assertIsNone(run.resume_session_id)
        with open_run(URL, self.output, remember=True) as run:
            self.assertEqual(run.resume_session_id, "session-1")
            client = SimpleNamespace(create_session=AsyncMock(), resume_session=AsyncMock())
            asyncio.run(watch.build_session(client, run, ToolGuard(run), "model"))
            client.create_session.assert_not_called()
            self.assertEqual(client.resume_session.call_args.args, ("session-1",))
            self.assertIn("tools", client.resume_session.call_args.kwargs)

    def test_remember_requires_a_session_factory_that_resumes(self):
        with open_run(URL, self.output) as run:
            run.session_id = "session-1"
            capture_and_inspect(run)
            asyncio.run(finish(run, "baseline"))
            run.publish()
        session = fake_session(session_id="new-session")

        async def factory(*_args):
            return session

        with self.assertRaisesRegex(WorkshopError, "resume"):
            execute(run_args(self.output, remember=True), factory)
        session.send_and_wait.assert_not_called()

    def test_credits_report_separates_this_run_from_a_resumed_session(self):
        self.assertEqual(watch.credits_line(None, (3.114, "complete"), 30), "AI credits: 3.11 this run (cap 30)")
        self.assertEqual(
            watch.credits_line((4.1, "complete"), (6.08, "partial"), 30),
            "AI credits: at least 1.98 this run, 6.08 in this session (cap 30)",
        )
        self.assertEqual(watch.credits_line(None, None, 30), "AI credits: not reported (cap 30)")

    def test_readme_exercises_complete_the_supplied_starter(self):
        namespace = {"__name__": "completed_starter"}
        exec(compile((ROOT / "starter.py").read_text(), "starter.py", "exec"), namespace)
        core = (ROOT / "README.md").read_text().split("\n## Stretch exercises")[0]
        exercises = 0
        for block in re.findall(r"```python\n(.*?)```", core, re.DOTALL):
            first = ast.parse(block).body[0]
            if isinstance(first, (ast.Assign, ast.FunctionDef, ast.AsyncFunctionDef)):
                exec(compile(block, "README exercise", "exec"), namespace)
                exercises += 1
        self.assertEqual(exercises, 3)
        self.assertTrue(namespace["TASK"].strip())
        self.assertEqual(len(namespace["allowed_tools"]()), 6)
        with open_run(URL, self.output) as run:
            client = SimpleNamespace(create_session=AsyncMock())
            asyncio.run(namespace["build_session"](client, run, ToolGuard(run), "model"))
            options = client.create_session.call_args.kwargs
            self.assertEqual(options["tools"]["definitions"][0].name, "finish_run")
            self.assertEqual(options["execution"]["limits"]["max_ai_credits"], run.max_ai_credits)

    def test_turn_and_client_cleanup_failures_preserve_state(self):
        latest = self.baseline()
        old = latest.read_bytes()
        for failure in ("turn", "cleanup"):
            with self.subTest(failure=failure):
                client = fake_client()
                if failure == "cleanup":
                    client.__aexit__.side_effect = WorkshopError("cleanup failed")

                async def factory(_client, run, _guard, _model):
                    session = fake_session()

                    async def send(*_args, **_kwargs):
                        capture_and_inspect(run)
                        await finish(run, "unchanged")
                        if failure == "turn":
                            raise WorkshopError("turn failed")

                    session.send_and_wait.side_effect = send
                    return session

                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(WorkshopError):
                    execute(run_args(self.output), factory, client)
                self.assertIn("AI credits: 2.50 this run (cap 30)", output.getvalue())
                self.assertEqual(latest.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
