"""Complete the three exercises in README.md. watch.py is the working reference."""

from copilot import CopilotClient, CopilotSession, ToolSet

from watch import SYSTEM_RULES, main
from workshop_support import MCP_TOOLS, RunContext, ToolGuard

TASK = ""  # Exercise 1: write the goal, not a script of browser operations.


def allowed_tools() -> ToolSet:
    # Exercise 2: opt into view, finish_run, and the selected MCP tools.
    raise NotImplementedError("Complete allowed_tools() using README step 2.")


async def build_session(
    client: CopilotClient, run: RunContext, guard: ToolGuard, model: str
) -> CopilotSession:
    # Exercise 3: connect the capabilities to a Copilot SDK session.
    raise NotImplementedError("Complete build_session() using README step 3.")


if __name__ == "__main__":
    main(session_factory=build_session, task=TASK)
