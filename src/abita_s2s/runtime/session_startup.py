"""Compose one voice session per LiveKit job."""

from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import load_config
from abita_s2s.model_config import create_model


async def start_voice_call(ctx: JobContext) -> None:
    session = AgentSession(
        llm=create_model(load_config()),
        vad=None,
        turn_handling={"turn_detection": "realtime_llm"},
    )
    # Session.start connects the job and registers session shutdown with LiveKit.
    await session.start(
        agent=AbitaAgent(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            delete_room_on_close=True,
        ),
    )
