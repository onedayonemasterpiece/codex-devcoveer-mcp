import asyncio, importlib.util, json, sys
from pathlib import Path
R=Path('/home/dev/.local/libexec/openai-codex-mcp')
sys.path.insert(0,str(R))
spec=importlib.util.spec_from_file_location('bridge_probe',R/'codex-chatgpt-bridge.py')
m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)

def collect(value,path=''):
    out=[]
    if isinstance(value,dict):
        for k,v in value.items():
            p=f'{path}.{k}' if path else k
            if 'usage' in k.lower() or 'token' in k.lower():
                if isinstance(v,(str,int,float,bool)) or v is None: out.append((p,v))
                elif isinstance(v,dict): out.append((p,{kk:vv for kk,vv in v.items() if isinstance(vv,(str,int,float,bool)) or vv is None}))
                else: out.append((p,type(v).__name__))
            out.extend(collect(v,p))
    elif isinstance(value,list):
        for i,v in enumerate(value): out.extend(collect(v,f'{path}[{i}]'))
    return out

async def main():
    h=m.NativeHistoryClient()
    try:
        r=await h.request('thread/read',{'threadId':'01a10764-cea8-7782-874b-02675fa31e36','includeTurns':True},timeout=20)
        print(json.dumps({'usage_fields':collect(r)},ensure_ascii=False))
    finally:
        await asyncio.to_thread(h._stop_sync)
asyncio.run(main())
