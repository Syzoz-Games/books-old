"""GitHub-only bounded JSON/API sample; no HTML pages or browser services."""
import argparse,io,json,os,re,signal,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from unittest.mock import patch
MAX_BODY=2_000_000
MAX_PAYLOAD=6_000_000


class SampleError(RuntimeError):
    pass

class HardDeadline(BaseException):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def timeout(self, maximum=15):
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            raise SampleError('Sample deadline exhausted')
        return min(maximum, remaining)

class Sources:
 def __init__(self,budget,timeout_factory):self.budget=budget;self.timeout_factory=timeout_factory;self.events=[];self.opener=urllib.request.build_opener(NoRedirect())
 def json(self,url,label):
  timeout=self.budget.timeout(15);event={'source':label,'url':url};self.events.append(event);start=time.monotonic()
  try:
   with self.timeout_factory(timeout,SampleError(label+' timed out')):
    request=urllib.request.Request(url,headers={'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'SyzozBoundedJsonSample/1.0'})
    try:response=self.opener.open(request,timeout=timeout)
    except urllib.error.HTTPError as error:response=error
    with response:raw=response.read(MAX_BODY+1);event.update(http_status=response.status,bytes=len(raw),content_type=response.headers.get('Content-Type',''))
   if event['http_status']!=200:raise SampleError(label+' returned HTTP '+str(event['http_status']))
   if len(raw)>MAX_BODY:raise SampleError(label+' response exceeds limit')
   if 'json' not in event['content_type'].lower():raise SampleError(label+' did not identify JSON')
   data=json.loads(raw,parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
   if not isinstance(data,dict):raise SampleError(label+' response is not an object')
   return data
  except (OSError,ValueError):raise SampleError(label+' unavailable or invalid JSON') from None
  finally:event['seconds']=round(time.monotonic()-start,4)


def github_metadata(env):
    if env.get('GITHUB_ACTIONS') != 'true':
        raise SampleError('Source collection and upload are restricted to GitHub Actions')
    result = {'repository': env.get('GITHUB_REPOSITORY', ''), 'run_id': env.get('GITHUB_RUN_ID', ''),
              'sha': env.get('GITHUB_SHA', '').lower()}
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', result['repository'])
            or not re.fullmatch(r'[1-9][0-9]*', result['run_id'])
            or not re.fullmatch(r'[0-9a-f]{40}', result['sha'])):
        raise SampleError('GitHub run provenance is missing or invalid')
    return result

def load_worker():
    if __package__:
        from . import collect
    else:
        import collect
    return collect

def validate_appids(appids):
    if (not 1 <= len(appids) <= 2 or len(set(appids)) != len(appids)
            or any(type(appid) is not int or not 1 <= appid <= 4294967295 for appid in appids)):
        raise SampleError('Provide one or two distinct uint32 AppIDs')

def pics_data(worker, sources, appids):
    client = None
    try:
        client = worker.SteamClient()
        with worker.Timeout(sources.budget.timeout(35), SampleError('Steam CM login timed out')):
            if client.anonymous_login() != worker.EResult.OK:
                raise SampleError('Steam CM anonymous login rejected')
        with worker.Timeout(sources.budget.timeout(35), SampleError('Steam CM product request timed out')):
            response = client.get_product_info(apps=appids)
        apps = response.get('apps') if isinstance(response, dict) else None
        result = {appid: apps.get(appid, apps.get(str(appid))) for appid in appids} if isinstance(apps, dict) else {}
        if len(result) != len(appids) or any(not isinstance(result.get(appid), dict) for appid in appids):
            raise SampleError('Steam CM returned incomplete product data')
        sources.events.append({'source': 'steam_cm_anonymous', 'status': 'received', 'appids': list(appids)})
        return result
    except Exception:
        sources.events.append({'source': 'steam_cm_anonymous', 'status': 'unavailable', 'appids': list(appids)})
    finally:
        if client is not None:
            try:
                with worker.Timeout(2, SampleError('Steam CM logout timed out')):
                    client.logout()
            except Exception:
                pass
    result = {}
    for appid in appids:
        response = sources.json(f'https://api.steamcmd.net/v1/info/{appid}', 'public_pics_proxy')
        data = response.get('data')
        item = data.get(str(appid)) if isinstance(data, dict) else None
        if response.get('status') != 'success' or not isinstance(item, dict):
            raise SampleError('Public PICS proxy identity could not be verified')
        if 'appid' in item and str(item['appid']) != str(appid):
            raise SampleError('Public PICS proxy returned another AppID')
        result[appid] = item
    return result

def build_sample(worker,sources,appids,github):
 validate_appids(appids);pics=pics_data(worker,sources,appids);records=[]
 for appid in appids:
  record=worker.normalize(appid,pics[appid])
  if not isinstance(record,dict) or record.get('type')!='game' or not record.get('name'):raise SampleError('PICS did not supply the requested named game')
  records.append(record)
 request={'ids':[{'appid':appid} for appid in appids],'context':{'language':'english','country_code':'US'},'data_request':dict(worker.STORE_DATA_REQUEST)}
 browse=sources.json('https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?'+urllib.parse.urlencode({'input_json':json.dumps(request)}),'storebrowse')
 response=browse.get('response');items=response.get('store_items') if isinstance(response,dict) else None
 if not isinstance(items,list):raise SampleError('StoreBrowse did not return game records')
 returned={item.get('appid',item.get('id')):item for item in items if isinstance(item,dict)}
 if any(appid not in returned or returned[appid].get('success')!=1 for appid in appids):raise SampleError('StoreBrowse omitted a requested game')
 dictionary_diagnostic={'source':'steam_tag_dictionary','status':'unavailable'};names={}
 try:
  dictionary=sources.json('https://api.steampowered.com/IStoreService/GetTagList/v1/?'+urllib.parse.urlencode({'input_json':json.dumps({'language':'english'})}),'steam_tag_dictionary')
  names=worker.parse_store_tags(dictionary);dictionary_diagnostic.update(status='verified',count=len(names),version_hash=dictionary.get('response',{}).get('version_hash'))
 except (SampleError,ValueError):pass
 official={}
 for appid in appids:
  if 'is_free' not in returned[appid]:
   try:official[appid]=sources.json('https://store.steampowered.com/api/appdetails?'+urllib.parse.urlencode({'appids':appid,'cc':'us','l':'english'}),'store_free_status')
   except SampleError:pass
 def cached(url,timeout):
  parsed=urllib.parse.urlsplit(url);query=urllib.parse.parse_qs(parsed.query)
  if parsed.scheme=='https' and parsed.netloc=='api.steampowered.com' and parsed.path=='/IStoreBrowseService/GetItems/v1/':return io.BytesIO(json.dumps(browse).encode())
  if parsed.scheme=='https' and parsed.netloc=='store.steampowered.com' and parsed.path=='/api/appdetails':
   ids=query.get('appids',[])
   if len(ids)==1 and ids[0].isdigit() and int(ids[0]) in official:return io.BytesIO(json.dumps(official[int(ids[0])]).encode())
  raise OSError('No successful same-run source response')
 with patch.object(worker.urllib.request,'urlopen',cached):details=worker.store_dates(records,deadline=sources.budget.deadline,tag_names=names,include_details=True)
 byid={detail['appid']:detail for detail in details}
 if set(byid)!=set(appids):raise SampleError('Rich JSON detail documents were incomplete')
 histories=[];owners=[];reviews=[];releases=[];categories=[];next_request=0.0
 for record in records:
  appid=record['appid'];wait=max(0,next_request-time.monotonic())
  if wait:sources.budget.timeout(wait+1);time.sleep(wait)
  sources.budget.timeout(8);next_request=time.monotonic()+1;checked_at=int(time.time());owner_url='https://steamspy.com/api.php?'+urllib.parse.urlencode({'request':'appdetails','appid':appid})
  try:
   owner_data=sources.json(owner_url,'steamspy');checked_at=int(time.time());record.update(worker.owner_estimates(worker.owner_fields(appid,owner_data,checked_at)));owners.append({'appid':appid,'url':owner_url,'status':'verified'})
  except (SampleError,ValueError,TypeError):owners.append({'appid':appid,'url':owner_url,'status':'unavailable','reason':'No verified ownership estimate; no samples invented'})
  record['architectures']=None;byid[appid]['checked_at']=checked_at
  points=[{'observed_at':checked_at,'total_installs':record['installs'],'period':1}] if record.get('installs') is not None else []
  histories.append({'package':'steam:'+str(appid),'revision':checked_at,'points':points})
  item=returned[appid];summary=(item.get('reviews') or {}).get('summary_filtered',{}) if isinstance(item.get('reviews'),dict) else {}
  reviews.append({'appid':appid,'source':'steam_store_summary','summary_filtered':summary,'summary_language_specific':(item.get('reviews') or {}).get('summary_language_specific')})
  common=pics[appid].get('common') or {};releases.append({'appid':appid,'pics_release_ts':worker.timestamp(common.get('steam_release_date')),'storebrowse_release':item.get('release'),'release_ts':record.get('release_ts')})
  categories.append({'appid':appid,'source':'steam_store_tag_dictionary','labels':record.get('tags')})
 payload={'action':'sample','records':records,'details':[byid[appid] for appid in appids],'install_history':{'schema':1,'mode':'replace','games':histories},'github':github}
 diagnostic={'github':github,'sources':sources.events,'tag_dictionary':dictionary_diagnostic,'owner_diagnostics':owners,'review_diagnostics':reviews,'release_diagnostics':releases,'category_diagnostics':categories,'engine_diagnostics':[{'appid':appid,'status':'unavailable','reason':'No verified JSON engine source; no HTML or browser request made','html_requests':0,'browser_requests':0} for appid in appids]}
 return payload,diagnostic


def payload_bytes(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_PAYLOAD:
        raise SampleError('Sample payload exceeds the receiver limit')
    if any(len(json.dumps(detail, ensure_ascii=False).encode()) > MAX_BODY for detail in payload['details']):
        raise SampleError('Sample detail exceeds the receiver limit')
    return raw

def upload_sample(payload, sources, url, token):
    if github_metadata(os.environ) != payload['github']:
        raise SampleError('Upload provenance does not match the current GitHub run')
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'syzoz.com'
            or parsed.path != '/game-explorer/steam-sync.php' or parsed.query or parsed.fragment):
        raise SampleError('Unexpected sample receiver URL')
    if len(token) < 32 or '\r' in token or '\n' in token:
        raise SampleError('Steam upload credential is missing or invalid')
    request = urllib.request.Request(url, payload_bytes(payload),
        {'Content-Type': 'application/json', 'X-Steam-Token': token})
    try:
        timeout = sources.budget.timeout(25)
        with sources.timeout_factory(timeout, SampleError('Sample upload timed out')):
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise SampleError('Sample receipt exceeds limit')
        result = json.loads(raw)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise SampleError('Sample receiver returned HTTP ' + str(code)) from None
    except (OSError, ValueError):
        raise SampleError('Sample upload did not return a valid receipt') from None
    receipt = result.get('receipt') if isinstance(result, dict) else None
    expected_ids = sorted(record['appid'] for record in payload['records'])
    receipt_ids = receipt.get('appids') if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in payload['github'].items())
            or not isinstance(receipt_ids, list) or any(type(appid) is not int for appid in receipt_ids)
            or sorted(receipt_ids) != expected_ids
            or not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('payload_hash', '')))):
        raise SampleError('Sample receipt does not match this GitHub run')
    return result

def write_public_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(path)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--appids', nargs='+', required=True, type=int)
    parser.add_argument('--seconds', type=int, default=240)
    parser.add_argument('--url', default=os.environ.get('STEAM_SYNC_URL', 'https://syzoz.com/game-explorer/steam-sync.php'))
    parser.add_argument('--output', type=Path, default=Path('sample-payload.json'))
    parser.add_argument('--result', type=Path, default=Path('sample-result.json'))
    parser.add_argument('--dry-run', action='store_true', help='Build fresh GitHub artifacts without uploading')
    args = parser.parse_args(argv)
    result = {'status': 'failed'}
    previous = None
    try:
        validate_appids(args.appids)
        if not 1 <= args.seconds <= 240:
            raise SampleError('Sample budget must be 1 to 240 seconds')
        github = github_metadata(os.environ)
        result['github'] = github
        token = os.environ.get('STEAM_SYNC_TOKEN', '')
        if not args.dry_run and len(token) < 32:
            raise SampleError('Steam upload credential is missing')
        if not hasattr(signal, 'setitimer'):
            raise SampleError('Sample requires a Unix hard deadline')
        def expired(signum, frame):
            raise HardDeadline('Sample hard deadline exhausted')
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.seconds)
        worker = load_worker()
        sources = Sources(Budget(args.seconds), worker.Timeout)
        try:
            payload, diagnostics = build_sample(worker, sources, args.appids, github)
            worker.fetch_engines(payload['records'], sources.budget.deadline)
            diagnostics['engine_diagnostics'] = [{'appid': r['appid'], 'source': 'wikidata', 'engines': r.get('architectures')} for r in payload['records']]
            result.update(diagnostics)
            payload_bytes(payload)
            write_public_json(args.output, payload)
            if args.dry_run:
                result.update(status='dry-run', appids=args.appids)
            else:
                result.update(status='uploaded', response=upload_sample(payload, sources, args.url, token))
        finally:
            result.setdefault('sources', sources.events)
        print(json.dumps({'status': result['status'], 'appids': args.appids, 'github': github}))
        return 0
    except (HardDeadline, SampleError, OSError, ValueError) as error:
        result['error'] = str(error) if isinstance(error, (HardDeadline, SampleError)) else 'Bounded sample failed: ' + type(error).__name__
        result['error_type'] = type(error).__name__
        print(json.dumps({'status': 'failed', 'error': result['error'], 'error_type': result['error_type']}), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        write_public_json(args.result, result)

if __name__=='__main__':
 raise SystemExit(main())
