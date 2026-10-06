import json, pathlib, re, subprocess, tempfile
cmd="/home/dev/.local/bin/codex"
with tempfile.TemporaryDirectory(prefix="codex-schema-") as d:
    p=subprocess.run([cmd,"app-server","generate-json-schema","--experimental","--out",d],capture_output=True,text=True,timeout=30)
    if p.returncode:
        raise SystemExit(p.stderr)
    hits=[]
    for path in pathlib.Path(d).rglob("*.json"):
        text=path.read_text(encoding="utf-8",errors="replace")
        low=text.lower()
        if "usage" not in low and "token" not in low:
            continue
        data=json.loads(text)
        def walk(v,prefix=""):
            if isinstance(v,dict):
                for k,val in v.items():
                    q=f"{prefix}.{k}" if prefix else k
                    kl=k.lower()
                    if "usage" in kl or "token" in kl:
                        if isinstance(val,(str,int,float,bool)) or val is None:
                            hits.append((path.name,q,val))
                        elif isinstance(val,dict):
                            summary={x:y for x,y in val.items() if isinstance(y,(str,int,float,bool)) or y is None}
                            hits.append((path.name,q,summary))
                    walk(val,q)
            elif isinstance(v,list):
                for i,val in enumerate(v): walk(val,f"{prefix}[{i}]")
        walk(data)
    for row in hits[:300]:
        print(json.dumps(row,ensure_ascii=False))
