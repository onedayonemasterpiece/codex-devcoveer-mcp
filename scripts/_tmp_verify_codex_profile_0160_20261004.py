import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    async with stdio_client(StdioServerParameters(command="/home/dev/.local/bin/codex-mcp-server", args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            if "codex_status" not in names:
                raise SystemExit("codex_status missing")
            status = await s.call_tool("codex_status", {})
            models = await s.call_tool("list_models", {"provider":"codex","verified_only":False})
            sp = status.structured_content or {}
            mp = models.structured_content or {}
            print(json.dumps({
                "tool_present": True,
                "quota_status": sp.get("status"),
                "admission": sp.get("admission"),
                "default_profile": sp.get("default_profile"),
                "model_ids": [m.get("id") for m in mp.get("models",[]) if isinstance(m,dict)][:20]
            }, ensure_ascii=False))
asyncio.run(main())
