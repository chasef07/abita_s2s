"""Shared tool helpers: call binding and caller announcements before external effects."""

from livekit.agents import RunContext


def bound(owner, context: RunContext) -> bool:
    """Whether this call's session owns the per-call owner a tool delegates to."""
    return owner is not None and owner.state is context.userdata


async def say_only(context: RunContext, news: str) -> bool:
    """Tell the caller one short piece of news after current playout; report whether it completed."""
    await context.wait_for_playout()
    speech = context.session.generate_reply(
        instructions=f"In one short sentence, tell the caller {news}.",
        tool_choice="none",
    )
    await speech.wait_for_playout()
    return not speech.interrupted and speech.exception() is None
