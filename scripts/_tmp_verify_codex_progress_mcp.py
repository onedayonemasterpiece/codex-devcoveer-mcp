import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    async with stdio_client(StdioServerParameters(command="/home/dev/.local/bin/codex-mcp-server", args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            result = await s.call_tool("read_task", {
                "project":"projects-hub",
                "task":"01a106f5-f7f7-7c80-8818-8184ba3492d9",
                "detail":"summary"
            })
            payload = result.structured_content or {}
            turn = payload.get("latestTurn") or {}
            print(json.dumps({
                "status": payload.get("status"),
                "progressPhase": turn.get("progressPhase"),
                "hasHistory": "history" in payload,
                "hasRawCommand": "command" in json.dumps(turn),
            }, ensure_ascii=False))
asyncio.run(main())
