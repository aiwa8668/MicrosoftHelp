from dotenv import load_dotenv
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from start_server import start_server


load_dotenv(override=True)


AGENT_APP = AgentApplication[TurnState](
    storage=MemoryStorage(),
    adapter=CloudAdapter(),
)


async def _help(context: TurnContext, _: TurnState) -> None:
    await context.send_activity(
        "Welcome to the Echo Agent sample. "
        "Type /help for help or send a message to see the echo feature in action."
    )


AGENT_APP.conversation_update("membersAdded")(_help)
AGENT_APP.message("/help")(_help)


@AGENT_APP.activity("message")
async def on_message(context: TurnContext, _: TurnState) -> None:
    await context.send_activity(f"you said: {context.activity.text}")


if __name__ == "__main__":
    start_server(AGENT_APP, None)
