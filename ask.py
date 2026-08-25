import asyncio
import sys

sys.path.insert(0, "src")

from ichatbio.agent_response import ResponseContext
from ichatbio.test_utils import InMemoryResponseChannel

from agent import USGSWaterAgent

ENTRYPOINT = "answer_daily_query"


async def ask(question: str) -> None:
    messages = []
    channel = InMemoryResponseChannel(messages)
    context = ResponseContext(channel)

    agent = USGSWaterAgent()
    await agent.run(context, question, ENTRYPOINT, None, None)

    for message in messages:
        print(f"[{type(message).__name__}] {message}")


def main():
    if len(sys.argv) < 2:
        print('Usage: python ask.py "<question>"')
        sys.exit(1)

    asyncio.run(ask(sys.argv[1]))


if __name__ == "__main__":
    main()
