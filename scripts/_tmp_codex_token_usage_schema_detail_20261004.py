import json, pathlib, subprocess, tempfile
cmd="/home/dev/.local/bin/codex"
with tempfile.TemporaryDirectory(prefix="codex-schema-") as d:
    p=subprocess.run([cmd,"app-server","generate-json-schema","--experimental","--out",d],capture_output=True,text=True,timeout=30,check=True)
    root=pathlib.Path(d)
    doc=json.loads((root/"ThreadTokenUsageUpdatedNotification.json").read_text())
    print(json.dumps({
      "top_properties": doc.get("properties",{}),
      "required": doc.get("required"),
      "thread_usage": doc.get("definitions",{}).get("ThreadTokenUsage"),
      "breakdown": doc.get("definitions",{}).get("TokenUsageBreakdown")
    },ensure_ascii=False))
    server=json.loads((root/"ServerNotification.json").read_text())
    text=json.dumps(server,ensure_ascii=False)
    idx=text.find("thread/tokenUsage")
    print(json.dumps({"method_context":text[max(0,idx-500):idx+700]},ensure_ascii=False))
