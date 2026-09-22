"""Worker entry point; session startup owns each call."""

import importlib
import json
import sys

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, JobProcess, cli

from abita_s2s.config import load_config
from abita_s2s.release import identity
from abita_s2s.runtime.session_startup import (
    SHUTDOWN_PROCESS_SECONDS,
    finish_voice_call,
    start_voice_call,
)

load_dotenv(".env.local", override=False)
server = AgentServer(shutdown_process_timeout=SHUTDOWN_PROCESS_SECONDS)


def prewarm(proc: JobProcess) -> None:
    # Load AnyIO's lazy socket types before a call can block on their import.
    importlib.import_module("anyio.abc._sockets")


server.setup_fnc = prewarm


@server.rtc_session(agent_name="abita-s2s", on_session_end=finish_voice_call)
async def entrypoint(ctx: JobContext) -> None:
    await start_voice_call(ctx, simulation=ctx.simulation_context())


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "start" and "--help" not in sys.argv:
        load_config()
    print(json.dumps({"event": "release_identity", **identity()}), flush=True)
    cli.run_app(server)


if __name__ == "__main__":
    main()
