"""Anonymous Steam collector. Website owns durable checkpoints; no empty-gap stop."""
import argparse,base64,gzip,json,os,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
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
def normalize(appid,data):
 c=data.get('common') or {};name=c.get('name');kind=str(c.get('type','unknown')).lower()
 if not name:return None
 associations=c.get('associations') or {};developers=[x.get('name','') for x in associations.values() if isinstance(x,dict) and x.get('type')=='developer'] if isinstance(associations,dict) else []
 return {'appid':int(appid),'name':str(name)[:500],'type':kind,'release_ts':timestamp(c.get('steam_release_date')),'original_release_ts':None,'coming_soon':c.get('releasestate')=='prerelease','windows':'windows' in str(c.get('oslist','')).lower(),'developer':', '.join(developers)[:500],'icon':'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/'+str(appid)+'/header.jpg','restricted':bool(data.get('_missing_token',False))}
def store_dates(records):
 games=[r for r in records if r['type']=='game' and not r['release_ts']]
 for start in range(0,len(games),100):
  rows=games[start:start+100];payload={'ids':[{'appid':r['appid']} for r in rows],'context':{'language':'english','country_code':'US'},'data_request':{'include_release':True,'include_basic_info':True,'include_assets':True}}
  url='https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?'+urllib.parse.urlencode({'input_json':json.dumps(payload)})
  try:
   with urllib.request.urlopen(url,timeout=25) as response:d=json.load(response)
   byid={r['appid']:r for r in rows}
   for a in d.get('response',{}).get('store_items',[]):
    r=byid.get(a.get('appid',a.get('id')))
    if not r or a.get('success')!=1:continue
    rel=a.get('release',{});r['release_ts']=timestamp(rel.get('steam_release_date'));r['original_release_ts']=timestamp(rel.get('original_release_date'));r['coming_soon']=bool(rel.get('is_coming_soon'))
    assets=a.get('assets',{});fmt=assets.get('asset_url_format','');asset=assets.get('header')
    if fmt.startswith('steam/apps/') and asset:r['icon']='https://shared.fastly.steamstatic.com/store_item_assets/'+fmt.replace('${FILENAME}',asset)
    r['developer']=', '.join(x.get('name','') for x in a.get('basic_info',{}).get('developers',[]))[:500]
  except (OSError,ValueError):
   for r in rows:r['store_retry']=True
  time.sleep(.5)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--seconds',type=int,default=1500);ap.add_argument('--batch',type=int,default=1000);ap.add_argument('--local');ap.add_argument('--scan-to',type=int,default=MAX_APP_ID);args=ap.parse_args()
 if not 1<=args.batch<=5000 or not 1<=args.scan_to<=MAX_APP_ID:raise ValueError('Invalid scan bounds')
 tr=Transport(os.environ.get('STEAM_SYNC_URL','https://syzoz.com/game-explorer/steam-sync.php'),os.environ.get('STEAM_SYNC_TOKEN',''),args.local)
 if not args.local and len(tr.token)<32:raise ValueError('Missing Steam upload credential')
 state=initial()|tr.call('state');state.update(status='starting',heartbeat=int(time.time()),run_id=os.environ.get('GITHUB_RUN_ID','local')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1'));tr.call('begin',state=state)
 client=SteamClient();started=time.monotonic();error=None
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
  while time.monotonic()-started<args.seconds:
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
    with Timeout(65,RuntimeError('PICS batch timed out')):info=client.get_product_info(apps=ids)
    if not isinstance(info,dict) or 'apps' not in info:raise RuntimeError('Incomplete PICS response')
    apps=info.get('apps') or {};records=[r for i,a in apps.items() if (r:=normalize(i,a))];store_dates(records)
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
  state.update(status='checkpointed',phase='Waiting for next run',heartbeat=int(time.time()));tr.call('finish',state=state)
 except BaseException as e:
  error=e;state.update(status='failed',last_error=type(e).__name__+': '+str(e),heartbeat=int(time.time()));
  try:tr.call('finish',state=state)
  except Exception:pass
 finally:client.logout()
 if error:raise error
if __name__=='__main__':main()
