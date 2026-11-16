"""Time live, read-only game metadata downloads; no website receiver or secrets."""
import ast
import concurrent.futures as futures
import hashlib
import json
import os
import random
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import batch

STEAM = next(ast.literal_eval(node.value) for node in ast.parse(Path('collect.py').read_text()).body
             if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id == 'STORE_DATA_REQUEST')
CTX = ssl.create_default_context()

def request(provider, ident):
    headers = {'Accept': 'application/json', 'User-Agent': 'SyzozCatalogDownloader/1'}
    if provider == 'steam':
        payload = {'ids':[{'appid':ident}], 'context':{'language':'english','country_code':'US'},
                   'data_request':STEAM}
        return urllib.request.Request(batch.api_url('IStoreBrowseService','GetItems',payload), headers=headers)
    headers.update({'Content-Type':'application/json','Ual-Access-Businessid':'projecta',
                    'Ual-Access-ProjectA':'{"device_info":{"os_ver":"33"}}'})
    return urllib.request.Request('https://tapi.pureapk.com/v3/batch_get_app_detail',
                                  json.dumps({'package_names':[ident]}).encode(), headers)

def present(provider, ident, data):
    if provider == 'steam':
        return any(batch.valid_game(x) and x.get('appid',x.get('id')) == ident
                   for x in data['response']['store_items'])
    if data.get('retcode') != 0 or not isinstance(data.get('app_detail_map'), dict):
        raise ValueError('API error')
    return bool(data['app_detail_map'].get(ident))

def plan():
    apk = Path('benchmarks/downloads/packages.txt').read_text().splitlines()
    manifest = batch.plan_manifest(batch.JsonClient(200, interval=1), 3400, 1, 1, batch.github_metadata(os.environ))
    steam = manifest['appids'][:2500]
    random.Random(731).shuffle(steam)
    groups = []
    matrix = []
    for provider, ids in [('steam',steam),('apkpure',apk)]:
        if len(ids) != 2500 or len(set(ids)) != 2500:
            raise ValueError('Expected 2500 unique game IDs per source')
        # Check ordinary metadata access before allocating 50 jobs for the source.
        with urllib.request.urlopen(request(provider,ids[0]),context=CTX,timeout=15) as res:
            present(provider,ids[0],json.load(res))
        for shard in range(50):
            key = len(groups)
            groups.append(ids[50*shard:50*(shard+1)])
            matrix.append({'index':key,'provider':provider,'slots':50})
    Path('groups.json').write_text(json.dumps(groups))
    with open(os.environ['GITHUB_OUTPUT'],'a') as f:
        print('matrix='+json.dumps({'include':matrix},separators=(',',':')),file=f)

def download():
    provider, index, slots = sys.argv[2],int(sys.argv[3]),int(sys.argv[4])
    ids = json.loads(Path('groups.json').read_text())[index]
    stop = threading.Event()
    lock = threading.Lock()
    active = peak = 0
    def get(ident):
        nonlocal active,peak
        if stop.is_set():
            return ident,'skipped',b'',None
        req = request(provider,ident)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            with urllib.request.urlopen(req,context=CTX,timeout=18) as res:
                raw=res.read(4_000_001)
            if len(raw)>4_000_000:
                return ident,'oversize',b'',time.perf_counter()
            return ident,'ok',raw,time.perf_counter()
        except urllib.error.HTTPError as e:
            with e:
                if e.code in (401,403,429): stop.set()
                return ident,str(e.code),b'',time.perf_counter()
        except OSError:
            return ident,'network_error',b'',time.perf_counter()
        finally:
            with lock: active-=1
    start=time.perf_counter()
    with futures.ThreadPoolExecutor(max_workers=slots) as pool:
        responses=list(pool.map(get,ids))
    wall=max((t for _,_,_,t in responses if t is not None),default=start)-start
    statuses=Counter()
    for ident,code,raw,_ in responses:
        if code=='ok':
            try: code='valid' if present(provider,ident,json.loads(raw)) else 'unavailable'
            except (KeyError,ValueError,TypeError): code='invalid'
        statuses[code]+=1
    row={'index':index,'provider':provider,'slots':slots,'peak_in_flight':peak,
         'seconds':wall,'bytes':sum(len(r[2]) for r in responses),'outcomes':dict(statuses)}
    print(json.dumps(row))
    Path(f'result-{index}.json').write_text(json.dumps(row))
    if stop.is_set(): raise SystemExit('Provider declined requests; stopping the benchmark')

def summarize():
    rows=[json.loads(f.read_text()) for f in Path('results').glob('result-*.json')]
    with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as out:
        print(f'{len(rows)} of 100 download jobs reported. Times cover live HTTP downloads only.\n',file=out)
        print('| Source | Slots | Jobs | Median seconds / 50 | Peak in flight | Valid | Unavailable | Errors/skipped | Median MB/job |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|',file=out)
        for provider in ['apkpure','steam']:
            for slots in [10,20,50]:
                group=[r for r in rows if r['slots']==slots and r['provider']==provider]
                if not group:continue
                counts=Counter()
                for r in group:counts.update(r['outcomes'])
                print(f"| {provider} | {slots} | {len(group)} | {statistics.median(r['seconds'] for r in group):.3f} | {max(r['peak_in_flight'] for r in group)} | {counts['valid']} | {counts['unavailable']} | {sum(counts.values())-counts['valid']-counts['unavailable']} | {statistics.median(r['bytes'] for r in group)/1e6:.2f} |",file=out)
        for provider in ['apkpure','steam']:
            subset = [r for r in rows if r['provider'] == provider]
            reached = sum(r['peak_in_flight'] == 50 for r in subset)
            print(f"\n{provider}: {reached} of {len(subset)} jobs reached 50 in-flight requests.", file=out)
    if len(rows) != 100 or {r['index'] for r in rows} != set(range(100)):
        raise SystemExit('Incomplete benchmark')
    for provider in ['apkpure','steam']:
        subset = [r for r in rows if r['provider'] == provider]
        if len(subset) != 50 or any(r['slots'] != 50 or sum(r['outcomes'].values()) != 50 for r in subset):
            raise SystemExit('Unexpected job configuration or request count')

if __name__=='__main__':
    {'plan':plan,'download':download,'summarize':summarize}[sys.argv[1]]()
