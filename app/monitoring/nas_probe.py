"""Fixed, read-only NAS probe. Run via DSM scheduler; emits only allowlisted metrics.

No Docker socket is mounted in the web application. Credentials remain on the host.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

BASE = Path('/volume1/docker/family-workbench')
PROXY = Path('/volume1/docker/family-workbench-proxy')
DOCKER = '/volume1/@appstore/ContainerManager/usr/bin/docker'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    return values


def api(path, key):
    req = Request('http://127.0.0.1:18790' + path, headers={'Authorization':'Bearer '+key})
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
        raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024: raise ValueError('response size')
    return json.loads(raw)


def collect():
    now=datetime.now(timezone.utc)
    result={'sampled_at':now.isoformat(), 'proxy_ok':False}
    disk=shutil.disk_usage(BASE)
    result.update(disk_total=disk.total,disk_free=disk.free)
    backups=[p for p in (BASE/'backups').glob('*.dump') if p.is_file() and p.stat().st_size>0]
    if backups:
        latest=max(backups,key=lambda p:p.stat().st_mtime)
        result.update(backup_at=datetime.fromtimestamp(latest.stat().st_mtime,timezone.utc).isoformat(),backup_name=latest.name)
    result['log_activity']={}
    for name in ('program-subscriptions','reading-jobs','knowledge-jobs','research-digest','research-sec-sync','daily-portfolio-valuation','option-wheel-watch'):
        p=BASE/'logs'/(name+'.log')
        if p.is_file(): result['log_activity'][name]=datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()
    try:
        credentials=read_env(PROXY/'.env')
        key=credentials.get('CLASH_SECRET','')
        if not key: raise ValueError('missing proxy credentials')
        stats=api('/connections',key)
        result.update(proxy_ok=True,upload_total=stats['uploadTotal'],download_total=stats['downloadTotal'])
        providers=api('/providers/proxies',key).get('providers',{})
        result['subscriptions']=[]
        for name, provider in providers.items():
            info=provider.get('subscriptionInfo') or {}
            if info:
                result['subscriptions'].append({'name':str(name)[:80], **{k.lower():info[k] for k in ('Upload','Download','Total','Expire') if k in info}})
        # Container restarts cannot create fictitious traffic; no configuration is changed.
        proc=subprocess.run([DOCKER,'inspect','--format','{{.State.StartedAt}}','family-workbench-proxy'],capture_output=True,text=True,timeout=10)
        if proc.returncode==0: result['session_id']=hashlib.sha256(proc.stdout.encode()).hexdigest()[:32]
    except Exception:
        result['proxy_ok']=False
        result['message']='代理采集失败；请检查代理服务或采集权限。'
    return result


def main():
    result=collect()
    if '--print' in sys.argv:
        print(json.dumps(result,ensure_ascii=False))
        return
    payload=json.dumps(result).encode()
    proc=subprocess.run([DOCKER,'exec','-i','family_finance_web','python','manage.py','collect_monitoring','--host-json'],
                        input=payload, timeout=160)
    sys.exit(proc.returncode or (0 if result.get('proxy_ok') and not result.get('message') else 1))


if __name__=='__main__':
    try: main()
    except Exception:
        print('监控采集未完成；请检查 DSM 任务权限及运行日志。',file=sys.stderr)
        sys.exit(1)
