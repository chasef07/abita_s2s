"""Worker entry point; session startup owns each call."""

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, cli

from abita_s2s.runtime.session_startup import start_voice_call

server = AgentServer()


@server.rtc_session(agent_name="abita-s2s")
async def entrypoint(ctx: JobContext) -> None:
    await start_voice_call(ctx)


def main() -> None:
    load_dotenv(".env.local", override=False)
    cli.run_app(server)


if __name__ == "__main__":
    main()
