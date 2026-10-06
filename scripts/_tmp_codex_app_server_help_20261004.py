import json, subprocess
cmd="/home/dev/.local/bin/codex"
for args in (["app-server","--help"],["app-server","generate-json-schema","--help"],["app-server","generate-ts","--help"]):
    try:
        p=subprocess.run([cmd,*args],capture_output=True,text=True,timeout=15)
        print(json.dumps({"args":args,"rc":p.returncode,"stdout":p.stdout[:12000],"stderr":p.stderr[:4000]},ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"args":args,"error":type(e).__name__}))
