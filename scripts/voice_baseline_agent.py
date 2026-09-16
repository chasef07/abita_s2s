"""Audio evaluation only: a transfer invocation ends the simulated conversation."""

from livekit.agents import RunContext, get_job_context
from livekit.agents.simulation import SimulationMode

from abita_s2s.call_control import CallControl, result


def require_audio_simulation():
    simulation = get_job_context().simulation_context()
    if simulation is None or simulation.simulation_mode != SimulationMode.SIMULATION_MODE_AUDIO:
        raise RuntimeError('Mock call control is restricted to native audio simulations')


def close_simulated_call(ctx):
    job = get_job_context()

    def on_close(event):
        # Match LiveKit EndCallTool cleanup: end the caller's room as well as
        # the agent session, so the simulator cannot wait for more speech.
        job.add_shutdown_callback(job.delete_room)
        job.shutdown(reason="simulated_call_complete")

    ctx.session.once('close', on_close)
    ctx.session.shutdown(drain=True)


async def simulated_transfer(self, ctx: RunContext) -> str:
    require_audio_simulation()
    ctx.disallow_interruptions()
    await ctx.wait_for_playout()
    speech = ctx.session.generate_reply(
        instructions="Say only: One moment while I transfer you to the office. Use the caller's language.",
        tool_choice='none', allow_interruptions=False,
    )
    await speech.wait_for_playout()
    self.status = 'accepted'
    self.attempts += 1
    # Drain preserves the current invocation in the session report; closing the
    # audio session prevents another caller turn or a failed-transfer recovery.
    close_simulated_call(ctx)
    return result('accepted', 'Simulated transfer accepted. This test call is complete; no real phone number was dialed.')


async def simulated_end(self, ctx: RunContext) -> str:
    require_audio_simulation()
    ctx.disallow_interruptions()
    self.ending = True
    close_simulated_call(ctx)
    return result('ended', 'Simulated call ended.')


# Own the worker entrypoint so spawned job processes install the overrides too.
from livekit.agents import AgentServer, JobContext, cli  # noqa: E402
from abita_s2s.runtime.session_startup import (  # noqa: E402
    SHUTDOWN_PROCESS_SECONDS, finish_voice_call, start_voice_call,
)

server = AgentServer(shutdown_process_timeout=SHUTDOWN_PROCESS_SECONDS)


@server.rtc_session(agent_name="abita-s2s", on_session_end=finish_voice_call)
async def entrypoint(ctx: JobContext):
    require_audio_simulation()
    CallControl._perform_transfer = simulated_transfer
    CallControl._end_call = simulated_end
    await start_voice_call(ctx, simulation=ctx.simulation_context())


if __name__ == '__main__':
    cli.run_app(server)
