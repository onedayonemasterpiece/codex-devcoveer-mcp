import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CMD="/home/dev/.local/bin/codex-mcp-server"

async def call_once(name,args):
    async with stdio_client(StdioServerParameters(command=CMD,args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            result=await s.call_tool(name,args)
            return result.structured_content or {}

async def main():
    started=await call_once("start_task",{
        "project":"projects-hub",
        "prompt":"READ ONLY lifecycle acceptance. Do not modify files, Git, services or external state. Read the repository README title and respond with exactly LIFECYCLE_OK.",
        "access":"read",
        "provider":"codex",
        "model":"gpt-6.1-sol",
        "reasoning_effort":"medium",
    })
    task=started.get("taskId")
    if not task:
        raise SystemExit("no taskId: "+json.dumps(started))
    print("started="+task, flush=True)
    await asyncio.sleep(6)
    for attempt in range(1,13):
        result=await call_once("read_task",{"project":"projects-hub","task":task,"detail":"summary"})
        print(json.dumps({"attempt":attempt,"status":result.get("status"),"executionStatus":result.get("executionStatus"),"progressPhase":result.get("progressPhase"),"finalResponse":result.get("finalResponse"),"content":result.get("content")},ensure_ascii=False),flush=True)
        if str(result.get("status") or "").lower() in {"completed","failed","cancelled","canceled"} or str(result.get("executionStatus") or "").lower() in {"completed","failed","cancelled","canceled"}:
            return
        await asyncio.sleep(5)
    raise SystemExit("task did not reach terminal status")
asyncio.run(main())
