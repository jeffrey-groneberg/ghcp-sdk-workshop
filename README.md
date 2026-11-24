# Build an autonomous webpage watcher with the Microsoft Copilot SDK

A **60-minute Python workshop** on **Microsoft Copilot SDK 2.0** (formerly GitHub Copilot SDK): give an agent one goal, let it browse with **official Playwright MCP**, inspect screenshots with the built-in **`view`** tool, and save a Markdown report when it finds meaningful changes.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/jeffrey-groneberg/ghcp-sdk-workshop)

This is an **agent loop**, not a Python script that captures a page and asks AI to summarize it. The agent chooses the tool calls and makes the comparison decision. Your application supplies capabilities, access boundaries, a cost cap, and durable output.

**You will edit:** [`starter.py`](starter.py). **Working solution:** [`watch.py`](watch.py).
The supplied [`workshop_support.py`](workshop_support.py) handles storage and guards so the workshop can focus on the SDK.

## Join the workshop

Scan the QR code to open [jeffrey-groneberg/ghcp-sdk-workshop](https://github.com/jeffrey-groneberg/ghcp-sdk-workshop) on your own device.

<a href="assets/repository-qr.png">
  <img src="assets/repository-qr.png" alt="QR code for the jeffrey-groneberg/ghcp-sdk-workshop repository" width="450">
</a>

[Open the full-size QR code for projection](assets/repository-qr.png).

## What SDK 2.0 changes in this lab

| SDK 2.0 | Where you meet it |
| --- | --- |
| The runtime runs **inside your Python process** by default. No GitHub Copilot CLI installation is needed. | Setup, step 3, stretch B |
| `mode="empty"` must be chosen explicitly, needs its own storage directory, and refuses a session without a tool allow-list. | Steps 2 and 3 |
| Session options are **grouped**: `tools`, `mcp`, `permissions`, `hooks`, `execution`. | Step 3 |
| A **terminal tool** (`is_terminal=True`) ends the agent's turn as soon as `finish_run` succeeds. | Step 3 |
| An **AI-credit cap** per session (`execution.limits`, experimental) and a credits report (`session.rpc.usage`, experimental). | Steps 3 and 4 |
| **Durable sessions** resumed by ID across separate runs. | Stretch A |

## Before the workshop

You need access to this repository, permission to create a Codespace, and a GitHub account with **GitHub Copilot and a compatible vision model enabled**. Codespaces and Copilot usage are subject to your account's allowances and organization policies.

1. Open the Codespaces button and let setup finish. The container installs Python 3.12, Node 22, the GitHub CLI, the pinned SDK and Playwright MCP packages, the SDK runtime, Chromium and its Linux libraries. No local installation is needed.
2. In the Codespaces terminal, check that the watcher reaches GitHub Copilot as **you**:

   ```bash
   python watch.py --list-models
   ```

   No login step is needed: a Codespace's `GITHUB_TOKEN` belongs to its owner, so the SDK signs in with your own Copilot access. The command prints the account it used, for example `Signed in as <you> (env)`, and lists the models available to you with PNG and two-image support, without running the agent. The workshop defaults to `claude-haiku-4.5`; use `--model NAME` if needed.

The SDK picks its identity in this order: `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, a stored GitHub Copilot CLI login, then the GitHub CLI login. If the Codespace token is refused, for example by an organization policy, sign in with the GitHub CLI and run the watcher without the Codespace token:

```bash
env -u GITHUB_TOKEN -u GH_TOKEN gh auth login --web --git-protocol https
env -u GITHUB_TOKEN -u GH_TOKEN python watch.py --list-models
```

Copy the one-time code and approve it in the browser. If asked to authenticate Git, answer **No**: Codespaces already manages Git. Keep the `env -u` prefix on every watcher command; it removes the variables only for that command and does **not** change Git credentials or your environment. Alternatively, set `COPILOT_GITHUB_TOKEN`, for example as a [Codespaces secret](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces), to a fine-grained personal access token owned by your personal account with the **Copilot Requests** permission.

Never share tokens, `~/.config/gh`, or `.webwatch/copilot-home`. In a container without an OS credential store, the GitHub CLI keeps its login in `~/.config/gh`. Setup never signs in for you or makes a model request.

> **Organizer:** publish the workshop files before sharing the button. It opens the repository's creation flow; select the workshop branch if it has not been merged into the default branch. Dependency downloads and authentication are pre-work, outside the lesson's hour.

<details>
<summary>Local fallback: macOS, Linux, or WSL</summary>

From this repository, with Python 3.11+, Node 22+, and the [GitHub CLI](https://cli.github.com/):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci
npm run browser:install
python -m copilot download-runtime --in-process
gh auth login
python watch.py --list-models
```

Skip `gh auth login` if `gh auth status` already shows your account. If `GH_TOKEN` or `GITHUB_TOKEN` is set in your shell, the SDK uses it first; check the `Signed in as` line. On Linux/WSL, install browser system libraries with `npm run browser:install -- --with-deps` instead. The support code uses POSIX file locking, so native Windows users should use Codespaces or WSL.

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

**Checkpoint:** `python -c "from starter import TASK; print(TASK)"` prints your goal without a model call.

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
| `view` | Built-in runtime tool | Give the model the pixels from the saved before/after images. |
| `finish_run` | Supplied application tool | Stage the agent's decision and observations for a safe local write. |

**Why:** an agent needs capabilities, not unrestricted shell access. The SDK distinguishes built-in, MCP, and application tools. A source-qualified name looks like `mcp:playwright-browser_take_screenshot`; inside the MCP server's own allowlist, its name is just `browser_take_screenshot`. In SDK 2.0, a client in `mode="empty"` refuses to create a session without such an allow-list, so no ambient shell, file-editing, or web tools slip in.

`browser_snapshot` returns an accessibility snapshot, **not an image**. The MCP configuration omits inline screenshot image responses deliberately: the agent uses `view` to inspect the saved PNGs, including the previous run's image. It also turns off tools that a visited page could register through the experimental WebMCP browser API.

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
        working_directory=str(run.directory),
        system_message={"mode": "append", "content": SYSTEM_RULES},
        tools={"available_tools": allowed_tools(), "definitions": [run.finish_tool()]},
        mcp={"servers": {"playwright": run.mcp_server()}},
        permissions={"on_request": guard.permission},
        hooks={"handlers": guard.hooks()},
        execution={"limits": {"max_ai_credits": run.max_ai_credits}},
    )
```

**What this connects:** SDK 2.0 groups session options by concern.

- `tools`: the allow-list from step 2, plus the application's own tool. `run.finish_tool()` is a small `@define_tool` function with a Pydantic schema: `status`, `summary`, and `changes`. It is a **terminal tool** (`is_terminal=True`): a successful call ends the agent's turn, while a failed call returns the error so the model can correct itself. It never browses or compares; those decisions belong to the agent.
- `mcp`: `run.mcp_server()` configures the **official installed `@playwright/mcp` server over stdio**. It launches Node in the run directory with the checked-in browser configuration, not a custom Python browser wrapper.
- `permissions` and `hooks`: the guard allows only the requested navigation target and assigned image paths. It rejects unrelated files/actions and refuses to finalize a comparison until both images have actually been read. Permission checks are narrow rather than `approve_all`.
- `execution`: an AI-credit cap for the session, marked **Experimental** in SDK 2.0.2 ("Limits applied to this session's current accounting window"). The SDK documents it as a soft cap: usage is checked after each model call, and once 10% or less remains, the runtime makes its final model call without tools. The watcher then cannot call `finish_run` and publishes nothing. The cap has a floor: "The value must be at least 30 and greater than the AI Credits the session has already used; a lower value is rejected." The default `--max-credits 30` is that minimum.

Read the short `execute()` function in `watch.py`. Its central operation is one:

```python
await session.send_and_wait(prompt, timeout=args.timeout)
```

**Why:** the SDK performs the intermediate tool calls and feeds their results back to the model. The runner checks the sign-in and manages resource cleanup, cost reporting, and publication, not the browsing sequence.

The client is created once per run:

```python
CopilotClient(
    mode="empty",
    runtime=RuntimeConnection.for_default(base_directory=str(copilot_home(output))),
)
```

SDK 2.0 has no default mode. `mode="empty"` enables only what each session opts into, avoiding inherited skills, file hooks, and cross-session memory, and it requires its own storage: session journals go to `.webwatch/copilot-home`, not your personal `~/.copilot`. `RuntimeConnection.for_default` loads the runtime into the watcher's own Python process, the 2.0 default. Without a `github` option, the runtime signs in through the credential order described in *Before the workshop*; a service acting for many users would pass `github={"token": ...}` instead.

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
AI credits: 3.11 this run (cap 30)
Session journal: .../.webwatch/copilot-home/session-state/<session-id>/events.jsonl
Outcome: baseline
Artifacts: .../.webwatch/<page-key>/runs/<run-id>
```

The order, any retries, and the cost are model-chosen. A `[blocked]` message is a guard explaining an invalid request, for example a screenshot requested in the same step as the navigation, or a `view` of a folder; the model reads the reason and corrects itself. It is not permission to disable the guard.

The credits line comes from the session's usage metrics, `session.rpc.usage.get_metrics()`, an API group that the 2.0.2 source marks experimental. In our test runs with `claude-haiku-4.5`, one run cost about 3 AI credits. The session journal is the runtime's own record: open it to see the prompt, each tool call, and the usage records behind the trace.

**First run:** the agent captures and inspects the page, then records a baseline. There is no change report because there is nothing to compare yet.

Run the same command again. The agent should inspect **both images**, decide `unchanged`, and create no Markdown report. Each successful run becomes the next baseline.

**Why:** this demonstrates the full loop, including tool observations and a conditional action. **Every run makes model requests**, even baseline and unchanged runs; the credits line shows what each run cost.

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
page-<time>.yml       Playwright MCP's page snapshot
console-<time>.log    Playwright MCP's console log
```

Python adds the URL, capture timestamps, and relative `![After](after.png)` / `![Before](before.png)` links. The agent supplies the decision and plain-text observations; it does not choose arbitrary output paths. `snapshot.json` also records which session produced the result.

An interrupted or incomplete run does not advance `latest.json`. Screenshot files can remain for diagnosis, but a model saying "done" is not sufficient to publish a result.

### 6. Review the boundaries and stop

**Who does what?** The model chooses actions and interprets images; MCP implements browser operations; the SDK's runtime runs the agent loop inside your Python process and signs in through its credential order; application code validates capabilities, caps the cost, and saves the result.

This is **semantic visual comparison**, not a deterministic visual-regression test. Small details may be missed, and dynamic content may cause false positives. The fixture has no ads, animation, external assets, or changing timestamps so the exercise is repeatable.

Use only this demo or authorized public, non-sensitive pages: screenshots and webpage content are sent to the model. The app limits runs to 24 tool attempts, an AI-credit cap, and an overall timeout, and rejects images exceeding the smaller of its 3 MiB cap and the model's declared limit. It does not bypass organization approvals, authenticate to target websites, schedule checks, or sandbox arbitrary hostile pages. In-process hosting is not a sandbox either: it provides no crash isolation, so a runtime failure ends the watcher process (stretch B shows the alternative).

Preserve any reports you want to keep, then **stop your Codespace** from the Codespaces page. Closing the browser tab alone is not the same as stopping it.

## Stretch exercises

Optional, for when you finish early. The solution `watch.py` already supports both flags used here.

### A. Remember earlier runs: durable sessions resumed by ID

So far, every run started a new session. Sessions are durable: the runtime journals each one under `.webwatch/copilot-home/session-state/<session-id>/`, and `snapshot.json` records which session produced the baseline. With `--remember`, the next run continues that session instead of starting a new one:

```bash
python watch.py http://127.0.0.1:8000/ --remember
```

1. Run it twice, with a page change in between, and compare the credits lines: they now show this run and the session total. The resumed session carries every earlier turn, so the agent can refer to what it saw before, and the total grows with each run. The cap counts that total, so a long-remembered session eventually needs a fresh start (see *Troubleshooting*).
2. Open the session journal: the same file now holds several runs, and each resumed run begins with a `session.resume` event.
3. Read `build_session()` in `watch.py`. When the runner sets `run.resume_session_id`, the solution calls `client.resume_session(run.resume_session_id, ...)` instead of `client.create_session(...)`, with the same options. Tools, the MCP server, handlers, and the credit limit are supplied again on every resume because they belong to this run.
4. Optional: teach your `starter.py` the same. Add `from functools import partial`, then choose the method before the call and use `await open_session(...)` with your existing keyword arguments:

   ```python
   open_session = client.create_session
   if run.resume_session_id:
       open_session = partial(client.resume_session, run.resume_session_id)
   ```

   Without this change, `python starter.py <url> --remember` stops before sending the prompt and explains why.

To start fresh, run once without `--remember`; later `--remember` runs continue that newer session.

### B. Move the runtime out of your process

```bash
COPILOT_SDK_DEFAULT_CONNECTION=stdio python watch.py http://127.0.0.1:8000/
```

SDK 2.0 loads the runtime into the watcher's own Python process. `RuntimeConnection.for_default` honours `COPILOT_SDK_DEFAULT_CONNECTION`, so this run starts the runtime as a child process instead, as SDK 1.x did; in code, `RuntimeConnection.for_spawned_stdio(...)` selects it explicitly. While a run is active, list the processes in a second terminal and compare the parent-PID (`PPID`) column in both modes:

```bash
ps -eo pid,ppid,command | grep -E "[w]atch\.py|[c]opilot-runtime"
```

With `stdio`, a `copilot-runtime` process has the watcher as its parent. In process, the watcher has no such child: the runtime runs inside it. Other `copilot-runtime` processes, for example from editor extensions, have other parents. In-process hosting needs no extra process but shares the watcher's fate; a child process isolates runtime failures and can get its own environment.

### C. Compare models by cost and judgement

```bash
python watch.py --list-models
python watch.py http://127.0.0.1:8000/ --model <another-listed-model> --output .webwatch/compare
```

Record a baseline, change the page, and run again with the other model. Its own `--output` folder keeps the baselines apart. Compare the credits lines and the observations in `report.md`: the task, tools, and guards are identical, only the model differs.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Setup did not finish | Read the creation log and rerun `bash .devcontainer/post-create.sh`. Do not disable TLS verification to work around network errors. |
| `Not signed in to GitHub Copilot`, or `Signed in as` shows the wrong account | Use the GitHub CLI fallback from *Before the workshop* with the `env -u GITHUB_TOKEN -u GH_TOKEN` prefix, or set `COPILOT_GITHUB_TOKEN`. Then run `python watch.py --list-models` again. |
| `gh auth login` says the `GITHUB_TOKEN` value is being used | Prefix the command with `env -u GITHUB_TOKEN -u GH_TOKEN`, as shown above. |
| Model access fails | Choose an enabled model from `python watch.py --list-models` with `--model NAME`; resolve organization restrictions with your organizer. |
| The screenshot shows GitHub sign-in | Use `http://127.0.0.1:8000/` inside the Codespace, not the external forwarded URL. |
| Chromium or Linux libraries are missing | Rerun bootstrap; locally use `npm run browser:install` (Linux/WSL: append `-- --with-deps`). |
| No `finish_run`, a blocked tool, or oversized image | Read the tool trace; check your allowlist and task, and use a smaller page if needed. Do not replace the guards with blanket approval. |
| `Minimum session limit is 30 AI credits` | Use `--max-credits 30` or more. |
| A remembered session stops without `finish_run`, or resuming it is rejected | The cap counts every run of the session, and the runtime rejects a cap that is not greater than the credits the session has already used. Run once without `--remember` to start a new session, or raise `--max-credits` deliberately. |
| The Python process ends abruptly during a run | Rerun with `COPILOT_SDK_DEFAULT_CONNECTION=stdio` to host the runtime in a child process (stretch B). |
| Run times out | The default is 180 seconds. Check browser/model errors first; use `--timeout SECONDS` deliberately if necessary. The previous baseline is preserved. |

<details>
<summary>Maintainer checks</summary>

```bash
python -m unittest discover -s tests -v
npm run test:mcp
```

The Python tests cover sign-in, tool configuration, permissions, session resume, and persistence without model calls. The MCP check starts the real installed server and verifies a full-page Chromium screenshot. Neither substitutes for observing the live agent's visual reasoning. The dev container includes an SSH server, so organizers can also run these checks in a Codespace from their own terminal with `gh codespace ssh`.

</details>

## References

The API usage was checked against the Microsoft Copilot SDK **2.0.2** source and documentation. The workshop pins `microsoft-copilot-sdk` **2.0.2** (runtime 1.0.115) and Playwright MCP **0.0.86**; update them together only after checking the full exercise.

- [Python SDK](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/python/README.md), including [image support](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/python/README.md#image-support), [permission handling](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/python/README.md#permission-handling), and [session hooks](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/python/README.md#session-hooks)
- [SDK v2 breaking changes and migration](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/v2-breaking-changes.md)
- [MCP servers](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/features/mcp.md), [session limits](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/features/session-limits.md), [session resume and persistence](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/features/session-persistence.md), [in-process runtime](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/setup/in-process-runtime.md), and [authentication](https://github.com/microsoft/copilot-sdk/blob/v2.0.2/docs/auth/authenticate.md)
- [Authenticating GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli): supported token types and the **Copilot Requests** permission
- [Official Playwright MCP configuration and tools](https://github.com/microsoft/playwright-mcp/blob/v0.0.86/README.md)
- [Codespaces dev containers](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers) and [private port forwarding](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace)