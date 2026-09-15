# Build an autonomous webpage watcher with the Copilot SDK

A **60-minute Python workshop**: give an agent one goal, let it browse with **official Playwright MCP**, inspect screenshots with the built-in **`view`** tool, and save a Markdown report when it finds meaningful changes.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/jeffrey-groneberg/ghcp-sdk-workshop)

This is an **agent loop**, not a Python script that captures a page and asks AI to summarize it. Copilot chooses the tool calls and makes the comparison decision. Your application supplies capabilities, access boundaries, and durable output.

**You will edit:** [`starter.py`](starter.py). **Working solution:** [`watch.py`](watch.py).
The supplied [`workshop_support.py`](workshop_support.py) handles storage and guards so the workshop can focus on the SDK.

## Before the workshop

You need access to this repository, permission to create a Codespace, and a GitHub account with **Copilot CLI and a compatible vision model enabled**. Codespaces and Copilot usage are subject to your account's allowances and organization policies.

1. Open the Codespaces button and let setup finish. The container installs Python 3.12, Node 22, the pinned SDK/CLI/MCP packages, Chromium and its Linux libraries. No local installation is needed.
2. In the Codespaces terminal, authenticate **your own account**:

   ```bash
   env -u GH_TOKEN -u GITHUB_TOKEN copilot login --device-code
   python watch.py --list-models
   ```

   Follow the device-code instructions in your browser. The second command lists models available to you with PNG and two-image support, without running the agent. The workshop defaults to `claude-haiku-4.5`; use `--model NAME` if needed.

Codespaces' repository token is not a substitute for Copilot access. The login command excludes generic GitHub tokens only for that process; the Python launcher does the same in Codespaces. It does **not** change Git credentials or remove environment variables globally. An explicitly configured `COPILOT_GITHUB_TOKEN` remains an intentional override.

Never share tokens or `~/.copilot`. In a headless container without an OS credential store, the CLI may store login credentials there. Setup never signs in for you or makes a model request.

> **Organizer:** publish the workshop files before sharing the button. It opens the repository's creation flow; select the workshop branch if it has not been merged into the default branch. Dependency downloads and authentication are pre-work, outside the lesson's hour.

<details>
<summary>Local fallback: macOS, Linux, or WSL</summary>

From this repository, with Python 3.11+ and Node 22+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm_config_registry=https://packagefeedproxy.microsoft.io/npm/ npm ci
npm run browser:install
python -m copilot download-runtime
export PATH="$PWD/node_modules/.bin:$PATH"
copilot login --device-code
python watch.py --list-models
```

On Linux/WSL, install browser system libraries with `npm run browser:install -- --with-deps` instead. The support code uses POSIX file locking, so native Windows users should use Codespaces or WSL.

</details>

## The hour

| Step | Focus | Time |
| --- | --- | --- |
| 1 | Give the agent a goal | 10 min |
| 2 | Expose tools, not a workflow | 10 min |
| 3 | Connect the SDK session | 15 min |
| 4 | Observe the complete loop | 10 min |
| 5 | Introduce text and image changes | 10 min |
| 6 | Review the boundaries and stop | 5 min |

### 1. Give the agent a goal

Open `starter.py` and replace its empty `TASK` with:

```python
TASK = (
    "Check this URL for meaningful changes since the previous run. Capture and compare "
    "screenshots, ignoring tiny rendering noise. Save a Markdown report with screenshots "
    "only if something changed."
)
```

**Why:** describe the outcome, not a sequence of browser commands. The runner supplies the URL, previous-run information, and assigned screenshot paths as context. The SDK runs the model/tool iterations; we do not write a `while` loop or a pixel-diff algorithm.

**Checkpoint:** `python -c "from starter import TASK; print(TASK)"` prints your goal without calling Copilot.

### 2. Expose tools, not a workflow

Replace `allowed_tools()` in `starter.py`:

```python
def allowed_tools() -> ToolSet:
    allowed = ToolSet().add_builtin("view").add_custom("finish_run")
    for name in MCP_TOOLS:
        allowed.add_mcp(f"playwright-{name}")
    return allowed
```

| Capability | Provider | Purpose |
| --- | --- | --- |
| `browser_navigate`, `browser_snapshot`, `browser_wait_for` | Playwright MCP | Open the page, inspect its structure, and wait if needed. |
| `browser_take_screenshot` | Playwright MCP | Save a full-page PNG. |
| `view` | Built-in Copilot tool | Give the model the pixels from the saved before/after images. |
| `finish_run` | Supplied application tool | Stage the agent's decision and observations for a safe local write. |

**Why:** an agent needs capabilities, not unrestricted shell access. The SDK distinguishes built-in, MCP, and application tools. A source-qualified name looks like `mcp:playwright-browser_take_screenshot`; inside the MCP server's own allowlist, its name is just `browser_take_screenshot`.

`browser_snapshot` returns an accessibility snapshot, **not an image**. The MCP configuration omits inline screenshot image responses deliberately: the agent uses `view` to inspect the saved PNGs, including the previous run's image.

**Checkpoint:**

```bash
python -c "from starter import allowed_tools; print(*allowed_tools().to_list(), sep='\n')"
```

You should see only the six intended capabilities, not wildcard or shell access.

### 3. Connect the SDK session

Replace `build_session()` in `starter.py`:

```python
async def build_session(
    client: CopilotClient, run: RunContext, guard: ToolGuard, model: str
) -> CopilotSession:
    return await client.create_session(
        model=model,
        available_tools=allowed_tools(),
        mcp_servers={"playwright": run.mcp_server()},
        tools=[run.finish_tool()],
        working_directory=str(run.directory),
        system_message={"mode": "append", "content": SYSTEM_RULES},
        on_permission_request=guard.permission,
        hooks=guard.hooks(),
    )
```

**What this connects:**

- `run.mcp_server()` configures the **official installed `@playwright/mcp` server over stdio**. It launches Node in the run directory with the checked-in browser configuration, not a custom Python browser wrapper.
- `run.finish_tool()` exposes a small `@define_tool` function with a Pydantic schema: `status`, `summary`, and `changes`. It never browses or compares; those decisions belong to the agent.
- The guard allows only the requested navigation target and assigned image paths. It rejects unrelated files/actions and refuses to finalize a comparison until both images have actually been read.

Read the short `execute()` function in `watch.py`. Its central operation is one:

```python
await session.send_and_wait(prompt, timeout=args.timeout)
```

**Why:** the SDK performs the intermediate tool calls and feeds their results back to the model. The runner manages authentication, resource cleanup, and publication, not the browsing sequence.

The client uses `mode="empty"` with explicitly selected capabilities, avoiding inherited skills, file hooks, and cross-session memory. Permission checks are narrow rather than `approve_all`.

### 4. Observe the complete loop

In one terminal, start the fixture:

```bash
npm run demo
```

Use the Codespaces **Ports** panel to preview port **8000**, leaving it private. The agent runs inside the container, so give it the **localhost URL**, not the external `app.github.dev` forwarding URL.

In a second terminal:

```bash
python starter.py http://127.0.0.1:8000/
```

Expect a trace resembling:

```text
[tool] playwright-browser_navigate
[tool] playwright-browser_take_screenshot
[tool] view after.png
[tool] finish_run
Outcome: baseline
Artifacts: .../.webwatch/<page-key>/runs/<run-id>
```

The order and any retries are model-chosen. A `[blocked]` message is a guard explaining an invalid request, not permission to disable the guard.

**First run:** the agent captures and inspects the page, then records a baseline. There is no change report because there is nothing to compare yet.

Run the same command again. The agent should inspect **both images**, decide `unchanged`, and create no Markdown report. Each successful run becomes the next baseline.

**Why:** this demonstrates the full loop, including tool observations and a conditional action. **Every run uses Copilot**, even baseline and unchanged runs.

To compare with the supplied solution, run `python watch.py http://127.0.0.1:8000/`. To start a separate exercise without deleting existing evidence, add `--output .webwatch/fresh-demo`.

### 5. Introduce text and image changes

Edit [`demo/index.html`](demo/index.html):

1. Change `$19 / month` to `$29 / month`, save, and run the same agent command.
2. Open the printed `report.md` path and use **Markdown: Open Preview**. Read the observations and check the before/after images.
3. Change the SVG circle's `fill="#2563eb"` to `fill="#16a34a"` without changing any text. Run again.

**Why:** a text-only comparison would miss the image change. Here, `view` supplies visual evidence and the agent judges what matters. An optional layout experiment is changing `.cards` from two columns to one.

Changed runs produce a portable folder containing:

```text
before.png
after.png
report.md
outcome.json
snapshot.json
```

Python adds the URL, capture timestamps, and relative `![After](after.png)` / `![Before](before.png)` links. The agent supplies the decision and plain-text observations; it does not choose arbitrary output paths.

An interrupted or incomplete run does not advance `latest.json`. Screenshot files can remain for diagnosis, but a model saying "done" is not sufficient to publish a result.

### 6. Review the boundaries and stop

**Who does what?** Copilot chooses actions and interprets images; MCP implements browser operations; the SDK runs the agent loop; application code validates capabilities and saves the result.

This is **semantic visual comparison**, not a deterministic visual-regression test. Small details may be missed, and dynamic content may cause false positives. The fixture has no ads, animation, external assets, or changing timestamps so the exercise is repeatable.

Use only this demo or authorized public, non-sensitive pages: screenshots and webpage content are sent to the model. The app limits runs to 24 tool attempts and an overall timeout, and rejects images exceeding the smaller of its 3 MiB cap and the model's declared limit. It does not bypass organization approvals, authenticate to target websites, schedule checks, or sandbox arbitrary hostile pages.

Preserve any reports you want to keep, then **stop your Codespace** from the Codespaces page. Closing the browser tab alone is not the same as stopping it.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Setup did not finish | Read the creation log and rerun `bash .devcontainer/post-create.sh`. Do not disable TLS verification to work around network errors. |
| npm cannot reach its registry | Use `npm_config_registry=https://packagefeedproxy.microsoft.io/npm/ npm ci`. The bootstrap already scopes this setting to its own processes. |
| CLI rejects an `auth` command | The pinned CLI uses `copilot login --device-code`, not `copilot auth login`. |
| Authentication/model access fails | Complete personal login, then `python watch.py --list-models`. Choose an enabled model with `--model NAME`; resolve organization restrictions with your organizer. |
| The screenshot shows GitHub sign-in | Use `http://127.0.0.1:8000/` inside the Codespace, not the external forwarded URL. |
| Chromium or Linux libraries are missing | Rerun bootstrap; locally use `npm run browser:install` (Linux/WSL: append `-- --with-deps`). |
| No `finish_run`, a blocked tool, or oversized image | Read the tool trace; check your allowlist and task, and use a smaller page if needed. Do not replace the guards with blanket approval. |
| Run times out | The default is 180 seconds. Check browser/model errors first; use `--timeout SECONDS` deliberately if necessary. The previous baseline is preserved. |

<details>
<summary>Maintainer checks</summary>

```bash
python -m unittest discover -s tests -v
npm run test:mcp
```

The Python tests cover tool configuration, permissions, and persistence without model calls. The MCP check starts the real installed server and verifies a full-page Chromium screenshot. Neither substitutes for observing the live agent's visual reasoning.

</details>

## References

The API examples were researched with **Context7** and checked against the pinned packages. The workshop uses Python SDK **1.0.13**, Copilot CLI **1.0.83**, and Playwright MCP **0.0.81**; update them together only after checking the full exercise.

- [Python SDK and custom tools](https://github.com/github/copilot-sdk/blob/v1.0.13/python/README.md), [built-in image support](https://github.com/github/copilot-sdk/blob/v1.0.13/python/README.md#image-support), and [MCP integration](https://github.com/github/copilot-sdk/blob/v1.0.13/docs/features/mcp.md)
- [Official Playwright MCP configuration and tools](https://github.com/microsoft/playwright-mcp/blob/v0.0.81/README.md)
- [Codespaces dev containers](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers) and [private port forwarding](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace)