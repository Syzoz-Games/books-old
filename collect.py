"""Anonymous Steam collector. Website owns durable checkpoints; no empty-gap stop."""
import argparse,base64,gzip,json,os,re,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime,timezone
from steam.client import SteamClient
from steam.enums import EResult
from gevent import Timeout
MAX_APP_ID=2**32-1
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kw):return None
class Transport:
 def __init__(self,url,token,local=None):self.url=url;self.token=token;self.local=Path(local) if local else None
 def call(self,action,**kw):
  data=dict(action=action,**kw)
  if self.local:
   self.local.mkdir(parents=True,exist_ok=True);p=self.local/'checkpoint.json';old=json.loads(p.read_text()) if p.exists() else {}
   if action=='state':return old
   if action=='batch':
    with (self.local/'batches.jsonl').open('a') as f:f.write(json.dumps(data)+'\n')
   state=kw.get('state',old);temp=p.with_suffix('.tmp');temp.write_text(json.dumps(state));temp.replace(p);return state
  req=urllib.request.Request(self.url,json.dumps(data).encode(),{'Content-Type':'application/json','X-Steam-Token':self.token})
  for attempt in range(3):
   try:
    with urllib.request.build_opener(NoRedirect).open(req,timeout=90) as r:return json.load(r)
   except urllib.error.HTTPError as e:
    if e.code<500:raise RuntimeError('Catalog checkpoint rejected: HTTP '+str(e.code)) from None
    if attempt==2:raise
   except OSError:
    if attempt==2:raise
   time.sleep(2*(attempt+1))
def initial():return {'cursor':1,'scan_pass':1,'change_number':0,'pending_changes':[],'change_target':0,'failed_batches':[],'scanned_ids':0,'unknown_ids':0,'restricted_records':0,'transport_failures':0,'status':'ready'}
def timestamp(v):
 try:n=int(v);return n if 0<n<MAX_APP_ID else None
 except (ValueError,TypeError):return None
def public_update(data):
 result={'updated_date':None,'updated_source':None};depots=data.get('depots')
 if not isinstance(depots,dict):return result
 branches=depots.get('branches')
 if not isinstance(branches,dict) or not isinstance(branches.get('public'),dict):return result
 public=branches['public'];key='timebuildupdated' if 'timebuildupdated' in public else 'timeupdated';value=public.get(key)
 if type(value) is not int and not (isinstance(value,str) and value.isascii() and value.isdecimal()):return result
 try:seconds=int(value)
 except ValueError:return result
 if not 1063324800<=seconds<=int(time.time())+300:return result
 return {'updated_date':datetime.fromtimestamp(seconds,timezone.utc).isoformat().replace('+00:00','Z'),'updated_source':'steam_public_build' if key=='timebuildupdated' else 'steam_public_branch'}
def normalize(appid,data):
 c=data.get('common') or {};name=c.get('name');kind=str(c.get('type','unknown')).lower()
 if not name:return None
 associations=c.get('associations') or {};developers=[x.get('name','') for x in associations.values() if isinstance(x,dict) and x.get('type')=='developer'] if isinstance(associations,dict) else []
 return {'appid':int(appid),'name':str(name)[:500],'type':kind,'release_ts':timestamp(c.get('steam_release_date')),'original_release_ts':None,'coming_soon':c.get('releasestate')=='prerelease','windows':'windows' in str(c.get('oslist','')).lower(),'developer':', '.join(developers)[:500],'icon':'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/'+str(appid)+'/header.jpg','restricted':bool(data.get('_missing_token',False)),'is_free':None,'installs':None,'installs_max':None,'installs_estimated':None,'installs_source':None,'installs_checked_at':None,'rating':None,'rating_count':None,'rating_source':None,'rating_checked_at':None,**public_update(data)}
def store_dates(records,deadline=None):
 # Commerce applies to every named game, including records already dated by PICS.
 # Only an explicit StoreBrowse boolean classifies free/paid; missing is unknown.
 games=[r for r in records if r['type']=='game']
 for start in range(0,len(games),100):
  rows=games[start:start+100];payload={'ids':[{'appid':r['appid']} for r in rows],'context':{'language':'english','country_code':'US'},'data_request':{'include_release':True,'include_basic_info':True,'include_assets':True}}
  remaining=25 if deadline is None else deadline-time.monotonic()
  if remaining<=1:
   for r in games[start:]:r['store_retry']=True
   break
  request_timeout=min(25,remaining)
  url='https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?'+urllib.parse.urlencode({'input_json':json.dumps(payload)})
  try:
   with (Timeout(request_timeout,OSError('Store request timed out')) if deadline is not None else nullcontext()):
    with urllib.request.urlopen(url,timeout=request_timeout) as response:d=json.load(response)
   byid={r['appid']:r for r in rows}
   for a in d.get('response',{}).get('store_items',[]):
    r=byid.get(a.get('appid',a.get('id')))
    if not r or a.get('success')!=1:continue
    if type(a.get('is_free')) is bool:r['is_free']=a['is_free']
    rel=a.get('release') or {};released=timestamp(rel.get('steam_release_date'));original=timestamp(rel.get('original_release_date'))
    if not r['release_ts'] and released:r['release_ts']=released
    if original:r['original_release_ts']=original
    if type(rel.get('is_coming_soon')) is bool:r['coming_soon']=rel['is_coming_soon']
    assets=a.get('assets',{});fmt=assets.get('asset_url_format','');asset=assets.get('header')
    if fmt.startswith('steam/apps/') and asset:r['icon']='https://shared.fastly.steamstatic.com/store_item_assets/'+fmt.replace('${FILENAME}',asset)
    developers=', '.join(x.get('name','') for x in (a.get('basic_info') or {}).get('developers',[]) if isinstance(x,dict))[:500]
    if developers:r['developer']=developers
  except (OSError,ValueError):
   for r in rows:r['store_retry']=True
  if deadline is None:time.sleep(.5)
  elif deadline-time.monotonic()>.5:time.sleep(.5)
def owner_fields(appid,data,checked_at):
 # Shared installs field deliberately carries the LOWER owner estimate, never
 # an exact download count, a midpoint, concurrent players, or a revenue proxy.
 if not isinstance(data,dict) or type(data.get('appid')) is not int or data['appid']!=appid or data.get('success') is False:raise ValueError('Unavailable ownership response')
 owners=data.get('owners');number=r'(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)'
 match=re.fullmatch(r'\s*('+number+r')\s*\.\.\s*('+number+r')\s*',owners) if isinstance(owners,str) else None
 if not match:raise ValueError('Invalid ownership range')
 low,high=(int(value.replace(',','')) for value in match.groups())
 if not 0<=low<=high<=9007199254740991:raise ValueError('Invalid ownership bounds')
 if type(checked_at) is not int or not 1<=checked_at<=9007199254740991:raise ValueError('Invalid ownership observation time')
 record={'appid':appid,'installs':low,'installs_max':high,'installs_estimated':True,'installs_source':'steamspy','installs_checked_at':checked_at}
 positive=data.get('positive');negative=data.get('negative')
 # A missing/malformed review response must not discard a valid owner estimate.
 if type(positive) is int and type(negative) is int and 0<=positive<=9007199254740991 and 0<=negative<=9007199254740991 and 0<positive+negative<=9007199254740991:
  record.update(rating=5*positive/(positive+negative),rating_count=positive+negative,rating_source='steamspy_reviews',rating_checked_at=checked_at)
 return record
def owner_phase(tr,state,deadline):
 # Server selects due saved games; no ever-growing pending-AppID checkpoint.
 if deadline-time.monotonic()<=1:return {'collected':0,'failed':0,'deferred':0}
 reply=tr.call('owner-candidates',limit=50);candidates=reply.get('records') if isinstance(reply,dict) else None
 if not isinstance(candidates,list) or len(candidates)>50:raise RuntimeError('Invalid ownership candidates response')
 ids=[r.get('appid') if isinstance(r,dict) else None for r in candidates]
 if any(type(i) is not int or not 1<=i<=MAX_APP_ID for i in ids) or len(set(ids))!=len(ids):raise RuntimeError('Invalid ownership candidate IDs')
 records=[];games=[];failed=[];next_request=0.0
 for appid in ids:
  wait=max(0.0,next_request-time.monotonic())
  if deadline-time.monotonic()<=wait+1:break
  if wait:time.sleep(wait)
  remaining=deadline-time.monotonic()
  if remaining<=1:break
  timeout=min(8.0,remaining);next_request=time.monotonic()+1.0
  try:
   url='https://steamspy.com/api.php?'+urllib.parse.urlencode({'request':'appdetails','appid':appid})
   with Timeout(timeout,OSError('Ownership request timed out')):
    with urllib.request.urlopen(url,timeout=timeout) as response:raw=response.read(1000001)
   if len(raw)>1000000:raise ValueError('Ownership response too large')
   checked_at=int(time.time());record=owner_fields(appid,json.loads(raw),checked_at)
   records.append(record);games.append({'package':'steam:'+str(appid),'revision':checked_at,'points':[{'observed_at':checked_at,'total_installs':record['installs'],'period':1}]})
  except urllib.error.HTTPError as error:
   failed.append(appid)
   error.close()
   if error.code in (429,503):break
  except (OSError,ValueError,OverflowError):failed.append(appid)
 if records or failed:
  state.update(status='running',phase='owners',heartbeat=int(time.time()))
  payload={'state':state,'records':records,'failed_appids':failed}
  if games:payload['install_history']={'schema':1,'mode':'merge','games':games}
  tr.call('owners',**payload)
 return {'collected':len(records),'failed':len(failed),'deferred':len(ids)-len(records)-len(failed)}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--seconds',type=int,default=1500);ap.add_argument('--owners-seconds',type=int,default=60);ap.add_argument('--batch',type=int,default=1000);ap.add_argument('--local');ap.add_argument('--scan-to',type=int,default=MAX_APP_ID);args=ap.parse_args()
 if not 1<=args.batch<=5000 or not 1<=args.scan_to<=MAX_APP_ID or args.seconds<1 or not 0<=args.owners_seconds<=300:raise ValueError('Invalid scan bounds or ownership budget')
 tr=Transport(os.environ.get('STEAM_SYNC_URL','https://syzoz.com/game-explorer/steam-sync.php'),os.environ.get('STEAM_SYNC_TOKEN',''),args.local)
 if not args.local and len(tr.token)<32:raise ValueError('Missing Steam upload credential')
 state=initial()|tr.call('state');state.update(status='starting',heartbeat=int(time.time()),run_id=os.environ.get('GITHUB_RUN_ID','local')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1'));tr.call('begin',state=state)
 client=SteamClient();started=time.monotonic();error=None;owner_budget=0 if args.local else min(args.owners_seconds,max(0,args.seconds-30));catalog_deadline=started+args.seconds-owner_budget
 try:
  with Timeout(60,RuntimeError('Steam anonymous login timed out')):
   if client.anonymous_login()!=EResult.OK:raise RuntimeError('Anonymous login rejected')
  if not state['change_number']:state['change_number']=client.get_changes_since(0,app_changes=True,package_changes=False).current_change_number
  # Snapshot all changes before advancing the persisted change cursor.
  if not state['pending_changes']:
   changes=client.get_changes_since(state['change_number'],app_changes=True,package_changes=False)
   if getattr(changes,'force_full_app_update',False) or getattr(changes,'force_full_update',False):
    state['cursor']=1;state['scan_pass']+=1;state['change_number']=changes.current_change_number;state['rescan_reason']='Steam change history expired; rebuilding accessible catalog'
   else:state['pending_changes']=sorted({c.appid for c in changes.app_changes});state['change_target']=changes.current_change_number
  retry_snapshot=list(state['failed_batches']);state['failed_batches']=list(retry_snapshot)
  while time.monotonic()<catalog_deadline:
   remaining=catalog_deadline-time.monotonic()
   if remaining<=1:break
   previous=json.loads(json.dumps(state))
   retry=False
   if state['pending_changes']:ids=state['pending_changes'][:args.batch];kind='changes'
   elif retry_snapshot:
    ids=retry_snapshot.pop(0);state['failed_batches'].remove(ids);kind='retry';retry=True
   else:
    if state['change_target']:state['change_number']=state['change_target'];state['change_target']=0
    if state['cursor']>args.scan_to:break
    ids=list(range(state['cursor'],min(args.scan_to,state['cursor']+args.batch-1)+1));kind='scan'
   state.update(status='running',phase=kind,heartbeat=int(time.time()),batch_from=min(ids),batch_to=max(ids))
   uploading=False
   try:
    with Timeout(min(65,remaining),RuntimeError('PICS batch timed out')):info=client.get_product_info(apps=ids)
    if not isinstance(info,dict) or 'apps' not in info:raise RuntimeError('Incomplete PICS response')
    apps=info.get('apps') or {};records=[r for i,a in apps.items() if (r:=normalize(i,a))];store_dates(records,deadline=catalog_deadline)
    returned={int(i) for i in apps};unknown=[i for i in ids if i not in returned];unnamed=[int(i) for i,a in apps.items() if not (a.get('common') or {}).get('name')]
    if kind=='scan':state['cursor']=max(ids)+1;state['scanned_ids']+=len(ids);state['unknown_ids']+=len(unknown);state['restricted_records']+=len(unnamed)
    elif kind=='changes':state['pending_changes']=state['pending_changes'][len(ids):]
    # Unknown/restricted outcomes are audit data, never successful catalog records.
    audit=base64.b64encode(gzip.compress(json.dumps({'unknown':unknown,'unnamed':unnamed}).encode())).decode()
    state['heartbeat']=int(time.time());uploading=True;tr.call('batch',state=state,records=records,audit=audit)
    print(json.dumps({'phase':kind,'from':min(ids),'to':max(ids),'named':len(records),'games':sum(r['type']=='game' for r in records),'unknown':len(unknown),'unnamed':len(unnamed),'next_id':state['cursor']}),flush=True)
   except Exception as e:
    # Never swallow a failed server checkpoint: replay the last saved batch next run.
    if uploading:
     state=previous
     raise
    state['transport_failures']+=1;state['last_error']=str(e);state['failed_batches'].append(ids)
    if kind=='scan':state['cursor']=max(ids)+1
    elif kind=='changes':state['pending_changes']=state['pending_changes'][len(ids):]
    tr.call('checkpoint',state=state);time.sleep(3)
   time.sleep(1)
  if owner_budget:
   summary=owner_phase(tr,state,min(started+args.seconds,time.monotonic()+owner_budget));print(json.dumps(dict(phase='owners',**summary)),flush=True)
  state.update(status='checkpointed',phase='Waiting for next run',heartbeat=int(time.time()));tr.call('finish',state=state)
 except BaseException as e:
  error=e;state.update(status='failed',last_error=type(e).__name__+': '+str(e),heartbeat=int(time.time()));
  try:tr.call('finish',state=state)
  except Exception:pass
 finally:client.logout()
 if error:raise error
if __name__=='__main__':main()
