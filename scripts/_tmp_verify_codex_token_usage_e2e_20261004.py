import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
CMD="/home/dev/.local/bin/codex-mcp-server"

async def main():
    async with stdio_client(StdioServerParameters(command=CMD,args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            status=(await s.call_tool("codex_status",{})).structured_content or {}
            admission=status.get("admission") or {}
            if admission.get("eligible") is not True:
                raise SystemExit("quota not eligible")
            started=(await s.call_tool("start_task",{
                "project":"projects-hub",
                "prompt":"READ ONLY token-usage acceptance. Do not modify files, Git, services, databases or external state. Respond with exactly TOKEN_USAGE_OK.",
                "access":"read",
                "provider":"codex",
                "model":"gpt-6.1-sol",
                "reasoning_effort":"medium"
            })).structured_content or {}
            task=started.get("taskId") or started.get("taskReference")
            if not task:
                raise SystemExit("missing task")
            print(json.dumps({"started":task,"remaining":admission.get("effective_remaining_percent")}))
            for i in range(18):
                await asyncio.sleep(4)
                result=(await s.call_tool("read_task",{"project":"projects-hub","task":task,"detail":"summary"})).structured_content or {}
                latest=result.get("latestTurn") or {}
                print(json.dumps({
                    "attempt":i+1,
                    "status":result.get("status"),
                    "progress":result.get("progressPhase") or latest.get("progressPhase"),
                    "tokenUsage":latest.get("tokenUsage"),
                    "finalResponse":latest.get("finalResponse") or result.get("finalResponse")
                },ensure_ascii=False),flush=True)
                if str(result.get("status") or "").lower() in {"completed","failed","interrupted","cancelled","canceled"}:
                    if result.get("status")=="completed" and latest.get("tokenUsage") is None:
                        await asyncio.sleep(2)
                        again=(await s.call_tool("read_task",{"project":"projects-hub","task":task,"detail":"summary"})).structured_content or {}
                        print(json.dumps({"post_terminal_tokenUsage":(again.get("latestTurn") or {}).get("tokenUsage")},ensure_ascii=False))
                    return
            raise SystemExit("timeout")
asyncio.run(main())
