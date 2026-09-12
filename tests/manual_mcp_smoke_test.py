"""Manual smoke test for alr_mcp_server.py — not part of the unittest
suite (starts a real subprocess and speaks the actual MCP protocol over
stdio, which is overkill for CI but is exactly what's needed to confirm
the server is wired correctly, not just that the underlying functions
work). Run directly:

    python3 tests/manual_mcp_smoke_test.py
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(REPO_ROOT, "alr_mcp_server.py")],
        cwd=REPO_ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Tools exposed:", [t.name for t in tools.tools])

            result = await session.call_tool("route_task", {"task": "git status"})
            print("route_task('git status') ->")
            for block in result.content:
                print(" ", block.text if hasattr(block, "text") else block)

            summary = await session.call_tool("get_alr_usage_summary", {})
            print("get_alr_usage_summary() ->")
            for block in summary.content:
                print(" ", block.text if hasattr(block, "text") else block)


if __name__ == "__main__":
    asyncio.run(main())
