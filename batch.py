"""Manual GitHub-only bounded JSON benchmark. No production catalog writes."""
import argparse,hashlib,io,json,math,os,re,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from unittest.mock import patch
MAX_ID=2**32-1
MAX_RECORDS=10000
MAX_LOGICAL=100
MAX_RUNNERS=4
MAX_BODY=12000000
class BatchError(RuntimeError):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def digest(value):return hashlib.sha256(compact(value)).hexdigest()
def write_json(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name('.'+path.name+'.tmp');temp.write_bytes(compact(value));temp.replace(path)
def github_metadata(env):
 out={key:env.get(source,'') for key,source in [('repository','GITHUB_REPOSITORY'),('run_id','GITHUB_RUN_ID'),('sha','GITHUB_SHA')]}
 if env.get('GITHUB_ACTIONS')!='true' or not re.fullmatch(r'[\w.-]+/[\w.-]+',out['repository']) or not re.fullmatch(r'[1-9][0-9]*',out['run_id']) or not re.fullmatch(r'[0-9a-f]{40}',out['sha']):raise BatchError('Valid GitHub run provenance is required')
 return out
class JsonClient:
 def __init__(self,seconds,interval=1,slot=None):self.deadline=time.monotonic()+seconds;self.interval=interval;self.slot=slot;self.next_request=0;self.events=[];self.opener=urllib.request.build_opener(NoRedirect())
 def remaining(self):return self.deadline-time.monotonic()
 def request(self,url,label):
  wait=max(0,self.next_request-time.monotonic())
  if self.slot is not None:
   # Physical runners use different wall-clock second slots within one shared
   # period. This avoids aligned bursts without extra coordination traffic.
   now=time.time();target=math.ceil((now+wait-self.slot)/self.interval)*self.interval+self.slot;wait=max(wait,target-now)
  if self.remaining()<=wait+1:raise BatchError('Bounded source deadline exhausted')
  if wait:time.sleep(wait)
  self.next_request=time.monotonic()+self.interval;started=time.monotonic();event={'source':label,'status':'failed'};self.events.append(event)
  try:
   req=urllib.request.Request(url,headers={'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'SyzozBoundedJsonBatch/1.0'})
   try:response=self.opener.open(req,timeout=min(20,self.remaining()))
   except urllib.error.HTTPError as error:response=error
   with response:status=response.status;kind=response.headers.get('Content-Type','');raw=response.read(MAX_BODY+1)
   event.update(http_status=status,bytes=len(raw))
   if status!=200:raise BatchError(label+' returned HTTP '+str(status))
   if len(raw)>MAX_BODY or 'json' not in kind.lower():raise BatchError(label+' returned non-JSON or oversized data')
   result=json.loads(raw,parse_constant=lambda n:(_ for _ in ()).throw(ValueError()))
   if not isinstance(result,dict):raise BatchError(label+' root is not an object')
   event['status']='received';return result
  except (ValueError,OSError):raise BatchError(label+' unavailable or invalid JSON') from None
  finally:event['seconds']=round(time.monotonic()-started,4)
def api_url(service,method,payload):return 'https://api.steampowered.com/'+service+'/'+method+'/v1/?'+urllib.parse.urlencode({'input_json':compact(payload).decode()})
def valid_game(item):
 return isinstance(item,dict) and type(item.get('success')) is int and item['success']==1 and type(item.get('appid',item.get('id'))) is int and 1<=item.get('appid',item.get('id'))<=MAX_ID and type(item.get('type')) is int and item['type']==0 and type(item.get('item_type')) is int and item['item_type']==0 and isinstance(item.get('name'),str) and bool(item['name'].strip())
def plan_manifest(client,limit,logical_workers,runners,github,start=0,include_unreleased=False,resume=None):
 if type(limit) is not int or not 1<=limit<=MAX_RECORDS or type(logical_workers) is not int or not 1<=logical_workers<=min(MAX_LOGICAL,limit) or type(runners) is not int or not 1<=runners<=min(MAX_RUNNERS,logical_workers):raise BatchError('Invalid bounded plan dimensions')
 if type(start) is not int or not 0<=start<=2147483647-limit or type(include_unreleased) is not bool:raise BatchError('Invalid query start or release scope')
 previous=None
 if resume is not None:
  validate_manifest(resume);planning=resume.get('planning')
  if not isinstance(planning,dict) or type(planning.get('next_start')) is not int or type(planning.get('include_unreleased')) is not bool:raise BatchError('Resume manifest has no verified continuation')
  if planning.get('exhausted') is True:raise BatchError('Prior query scope is exhausted; start a new explicit scan')
  if start!=0 or include_unreleased and not planning['include_unreleased']:raise BatchError('Cannot change query scope or start while resuming')
  start=planning['next_start'];include_unreleased=planning['include_unreleased'];previous={'github':resume['github'],'manifest_hash':resume['manifest_hash']}
 if start>2147483647-limit:raise BatchError('Continuation exceeds bounded query offsets')
 ids=[];seen=set();pages=[];cursor=start;exhausted=False
 # At most ceil(limit/100) pages. Filtering/duplicate results may yield fewer;
 # never scan indefinitely to fill a requested cap or assume a stable catalog.
 for page in range((limit+99)//100):
  count=min(100,limit-page*100)
  payload={'query_name':'syzoz_manual_bounded_json_batch','query':{'start':cursor,'count':count,'sort':0,'filters':{'released_only':not include_unreleased,'type_filters':{'include_games':True}}},'context':{'language':'english','country_code':'US'},'data_request':{'include_basic_info':True}}
  doc=client.request(api_url('IStoreQueryService','Query',payload),'store_query');response=doc.get('response');items=response.get('store_items') if isinstance(response,dict) else None
  if not isinstance(items,list) or len(items)>count:raise BatchError('Query did not honor bounded count')
  metadata=response.get('metadata');pages.append({'start':cursor,'count':count,'returned':len(items),'total_matching_records':metadata.get('total_matching_records') if isinstance(metadata,dict) else None})
  for item in items:
   if not valid_game(item):continue
   appid=item.get('appid',item.get('id'))
   if appid not in seen:seen.add(appid);ids.append(appid)
  cursor+=count
  total=metadata.get('total_matching_records') if isinstance(metadata,dict) else None
  if isinstance(total,str) and total.isascii() and total.isdecimal():total=int(total)
  exhausted=len(items)<count or type(total) is int and total>=0 and cursor>=total
  if exhausted:break
 if not ids:raise BatchError('Planner found no verified game identities')
 # Share the one dictionary with every physical runner; no repeated lookup/app.
 dictionary=client.request(api_url('IStoreService','GetTagList',{'language':'english'}),'tag_dictionary')
 result={'schema':1,'github':github,'appids':ids,'requested_limit':limit,'logical_workers':logical_workers,'runners':runners,'query_pages':pages,'tag_dictionary':dictionary,'source_events':client.events,'sort_semantics':'Provider default sort0; no release-order promise','created_at':int(time.time()),'planning':{'start':start,'next_start':cursor,'exhausted':exhausted,'include_unreleased':include_unreleased,'query_scope':{'sort':0,'released_only':not include_unreleased,'type_filters':{'include_games':True},'language':'english','country':'US'},'previous':previous,'consistency':'Offset continuation is not a stable provider snapshot; production must upsert by AppID and periodically rescan'}}
 result['manifest_hash']=digest(result);return result
def validate_manifest(manifest,github=None):
 if not isinstance(manifest,dict) or manifest.get('schema')!=1:raise BatchError('Invalid manifest schema')
 source=dict(manifest);claimed=source.pop('manifest_hash',None)
 if claimed!=digest(source):raise BatchError('Manifest digest mismatch')
 ids=manifest.get('appids');logical=manifest.get('logical_workers');runners=manifest.get('runners')
 if not isinstance(ids,list) or not 1<=len(ids)<=MAX_RECORDS or any(type(i) is not int or not 1<=i<=MAX_ID for i in ids) or len(set(ids))!=len(ids):raise BatchError('Manifest IDs are invalid or overlap')
 if type(logical) is not int or not 1<=logical<=MAX_LOGICAL or type(runners) is not int or not 1<=runners<=min(MAX_RUNNERS,logical):raise BatchError('Invalid manifest worker dimensions')
 planning=manifest.get('planning')
 if planning is not None:
  if not isinstance(planning,dict) or type(planning.get('start')) is not int or type(planning.get('next_start')) is not int or not 0<=planning['start']<planning['next_start']<=2147483647 or type(planning.get('exhausted')) is not bool or type(planning.get('include_unreleased')) is not bool:raise BatchError('Invalid manifest query continuation')
  scope={'sort':0,'released_only':not planning['include_unreleased'],'type_filters':{'include_games':True},'language':'english','country':'US'}
  if planning.get('query_scope')!=scope:raise BatchError('Manifest query scope does not match continuation')
 if github is not None and manifest.get('github')!=github:raise BatchError('Manifest does not belong to this GitHub run')
 return ids
def assigned_shards(manifest,runner):
 ids=validate_manifest(manifest)
 if type(runner) is not int or not 0<=runner<manifest['runners']:raise BatchError('Runner is outside manifest')
 return [(shard,[appid for index,appid in enumerate(ids) if index%manifest['logical_workers']==shard]) for shard in range(manifest['logical_workers']) if shard%manifest['runners']==runner]
def paid_evidence(item):
 option=item.get('best_purchase_option');name=item.get('name')
 if not isinstance(option,dict) or not isinstance(name,str) or option.get('purchase_option_name')!=name or option.get('included_game_count')!=1 or option.get('must_purchase_as_set') is True:return False
 price=option.get('final_price_in_cents')
 return (type(price) is int or isinstance(price,str) and price.isascii() and price.isdecimal()) and 0<int(price)<=9007199254740991 and type(price) is not bool

def run_shards(worker,client,manifest,runner,output,owners_limit=0,engine_seconds=0,engine_fallbacks=False):
 validate_manifest(manifest);overall_deadline=client.deadline;engine_seconds=min(max(0,engine_seconds),max(0,client.remaining()-2));client.deadline=overall_deadline-engine_seconds;assignments=assigned_shards(manifest,runner);names=worker.parse_store_tags(manifest['tag_dictionary']);source_pics=None;client_cm=None;owner_count=0;report={'schema':1,'github':manifest['github'],'manifest_hash':manifest['manifest_hash'],'runner':runner,'completed_shards':[],'failed_shards':[],'source_events':client.events,'coverage':{}}
 try:
  client_cm=worker.SteamClient()
  with worker.Timeout(min(12,max(1,client.remaining())),BatchError('PICS login timed out')):
   if client_cm.anonymous_login()!=worker.EResult.OK:raise BatchError('PICS login rejected')
 except Exception:
  if client_cm is not None:
   try:client_cm.logout()
   except Exception:pass
  client_cm=None
 for shard,ids in assignments:
  if not ids:
   write_json(Path(output)/('shard-%03d.json'%shard),{'schema':1,'github':manifest['github'],'manifest_hash':manifest['manifest_hash'],'shard':shard,'appids':[],'records':[],'details':[],'outcomes':[],'history':[]});report['completed_shards'].append(shard);continue
  if client.remaining()<=2:report['failed_shards'].append({'shard':shard,'reason':'deadline'});continue
  records=[];details=[];outcomes=[];histories=[]
  try:
   for begin in range(0,len(ids),100):
    batch_ids=ids[begin:begin+100];request={'ids':[{'appid':i} for i in batch_ids],'context':{'language':'english','country_code':'US'},'data_request':dict(worker.STORE_DATA_REQUEST)}
    doc=client.request(api_url('IStoreBrowseService','GetItems',request),'store_items');items=doc.get('response',{}).get('store_items')
    if not isinstance(items,list) or len(items)>len(batch_ids):raise BatchError('GetItems did not honor requested bound')
    byid={item.get('appid',item.get('id')):item for item in items if valid_game(item) and item.get('appid',item.get('id')) in batch_ids};pics={}
    if client_cm is not None and client.remaining()>2:
     try:
      with worker.Timeout(min(25,client.remaining()-1),BatchError('PICS batch timed out')):reply=client_cm.get_product_info(apps=batch_ids)
      pics=reply.get('apps',{}) if isinstance(reply,dict) else {}
     except Exception:pics={}
    batch_records=[]
    for appid in batch_ids:
     item=byid.get(appid)
     if item is None:outcomes.append({'appid':appid,'status':'store_unavailable_or_not_game'});continue
     source=pics.get(appid,pics.get(str(appid)))
     if not isinstance(source,dict) or not (source.get('common') or {}).get('name'):source={'common':{'name':item['name'],'type':'game'}}
     record=worker.normalize(appid,source)
     if not isinstance(record,dict) or record.get('type')!='game':outcomes.append({'appid':appid,'status':'pics_not_game'});continue
     batch_records.append(record)
     # A matching, one-game base purchase with a positive amount proves paid.
     # Omitted optional flags without this evidence stay unknown, no per-app fanout.
     if 'is_free' not in item and paid_evidence(item):item['is_free']=False
    def cached(url,timeout):
     if urllib.parse.urlsplit(url).path=='/IStoreBrowseService/GetItems/v1/':return io.BytesIO(compact(doc))
     raise OSError('Auxiliary per-app fallback disabled for bounded batch')
    with patch.object(worker.urllib.request,'urlopen',cached):batch_details=worker.store_dates(batch_records,deadline=client.deadline,tag_names=names,include_details=True)
    for record in batch_records:
     checked=int(time.time());appid=record['appid']
     if owner_count<owners_limit and not record.get('coming_soon') and client.remaining()>2:
      try:
       data=client.request('https://steamspy.com/api.php?'+urllib.parse.urlencode({'request':'appdetails','appid':appid}),'steamspy_optional');record.update(worker.owner_estimates(worker.owner_fields(appid,data,checked)))
      except (BatchError,ValueError):pass
      owner_count+=1
     if record.get('installs') is not None:histories.append({'package':'steam:'+str(appid),'revision':checked,'points':[{'observed_at':checked,'total_installs':record['installs'],'period':1}]})
     outcomes.append({'appid':appid,'status':'record'});records.append(record)
    details.extend(batch_details)
   shard_doc={'schema':1,'github':manifest['github'],'manifest_hash':manifest['manifest_hash'],'shard':shard,'appids':ids,'records':records,'details':details,'outcomes':outcomes,'history':histories}
   write_json(Path(output)/('shard-%03d.json'%shard),shard_doc);report['completed_shards'].append(shard)
   for detail in details:
    for field in ('screenshots','trailers'):
     if detail.get(field):report['coverage'][field]=report['coverage'].get(field,0)+1
   for record in records:
    for field in ('icon','is_free','price','release_ts','updated_date','version','size','category','tags','rating','description','iap','architectures','installs'):
     if record.get(field) is not None and record.get(field)!='':report['coverage'][field]=report['coverage'].get(field,0)+1
  except (BatchError,ValueError,OSError) as error:report['failed_shards'].append({'shard':shard,'reason':str(error)[:200]})
 if client_cm is not None:
  try:client_cm.logout()
  except Exception:pass
 client.deadline=overall_deadline
 engine_verified=0
 if engine_seconds>0 and client.remaining()>1:
  shard_docs=[];engine_records=[]
  for shard in report['completed_shards']:
   path=Path(output)/('shard-%03d.json'%shard);document=json.loads(path.read_text());shard_docs.append((path,document));engine_records.extend(document['records'])
  engine_deadline=min(overall_deadline,time.monotonic()+engine_seconds);client.deadline=engine_deadline
  def engine_json(request,timeout):
   url=request.full_url if isinstance(request,urllib.request.Request) else request
   if not engine_fallbacks and urllib.parse.urlsplit(url).netloc!='query.wikidata.org':raise OSError('Per-app engine fallback disabled')
   try:return io.BytesIO(compact(client.request(url,'engine_json')))
   except BatchError as error:raise OSError(str(error)) from None
  try:
   with patch.object(worker.urllib.request,'urlopen',engine_json):worker.fetch_engines(engine_records,engine_deadline,allow_fallbacks=engine_fallbacks)
  finally:client.deadline=overall_deadline
  for path,document in shard_docs:
   engine_verified+=sum(bool(record.get('architectures')) for record in document['records']);write_json(path,document)
 report['coverage']['architectures']=engine_verified
 report['engine_budget_seconds']=engine_seconds;report['engine_fallbacks_enabled']=engine_fallbacks
 report['owners_attempted']=owner_count;report['status']='complete' if not report['failed_shards'] else 'incomplete';write_json(Path(output)/('runner-%d.json'%runner),report);return report

def merged_shards(manifest,directory):
 ids=validate_manifest(manifest);expected=set(ids);seen=set();records=[];details=[];history=[];outcomes=[]
 for shard in range(manifest['logical_workers']):
  path=Path(directory)/('shard-%03d.json'%shard)
  if not path.exists():raise BatchError('Missing completed logical shard '+str(shard))
  row=json.loads(path.read_text());assigned=[appid for index,appid in enumerate(ids) if index%manifest['logical_workers']==shard]
  if row.get('schema')!=1 or row.get('github')!=manifest['github'] or row.get('manifest_hash')!=manifest['manifest_hash'] or row.get('shard')!=shard or row.get('appids')!=assigned:raise BatchError('Shard does not match immutable manifest')
  results=row.get('outcomes');actual=[item.get('appid') for item in results] if isinstance(results,list) else []
  if len(actual)!=len(assigned) or set(actual)!=set(assigned) or seen.intersection(actual):raise BatchError('Shard outcomes overlap or omit assigned IDs')
  seen.update(actual);r=row.get('records');d=row.get('details');h=row.get('history')
  if not isinstance(r,list) or not isinstance(d,list) or not isinstance(h,list):raise BatchError('Invalid shard result lists')
  rids=[item.get('appid') for item in r];dids=[item.get('appid') for item in d]
  if len(set(rids))!=len(rids) or not set(rids).issubset(set(actual)) or len(set(dids))!=len(dids) or not set(dids).issubset(set(rids)):raise BatchError('Shard data IDs are not disjoint')
  if any(item.get('status') not in ('record','store_unavailable_or_not_game','pics_not_game') for item in results) or {item['appid'] for item in results if item['status']=='record'}!=set(rids):raise BatchError('Shard record outcomes do not match saved records')
  packages=[item.get('package') for item in h]
  if len(set(packages))!=len(packages) or not set(packages).issubset({'steam:'+str(i) for i in rids}):raise BatchError('Shard history does not match saved records')
  records.extend(r);details.extend(d);history.extend(h);outcomes.extend(results)
 if seen!=expected:raise BatchError('Finalizer coverage does not match manifest')
 return {'records':records,'details':details,'history':history,'outcomes':outcomes}

def receiver_detail_size(value):
 raw=compact(value).replace(b'/',b'\\/')
 # Older PHP JSON encoders escape U+2028/U+2029 even with Unicode unescaped.
 # Count the longer representation so both old/new receiver deployments fit.
 return len(raw)+3*(raw.count(b'\xe2\x80\xa8')+raw.count(b'\xe2\x80\xa9'))
def isolated_chunks(manifest,data,max_body=5500000,max_detail=2000000,max_details=4000000):
 # Assemble units by immutable ID so records/outcomes/history never overlap.
 records={row['appid']:row for row in data['records']};details={row['appid']:row for row in data['details']};histories={row['package']:row for row in data['history']};outcomes={row['appid']:row for row in data['outcomes']}
 chunks=[];chunk={'records':[],'details':[],'outcomes':[],'history':[]};size=1000;detail_bytes=0
 for appid in manifest['appids']:
  unit={'records':[],'details':[],'outcomes':[],'history':[]}
  if appid in records:
   unit['records']=[records[appid]]
   if appid in details:unit['details']=[details[appid]]
   if 'steam:'+str(appid) in histories:unit['history']=[histories['steam:'+str(appid)]]
  else:
   outcome=outcomes[appid];unit['outcomes']=[{'appid':appid,'status':'unavailable','error':str(outcome.get('status','source unavailable'))[:500]}]
  measured=sum(receiver_detail_size(row) for row in unit['details'])
  if measured>max_detail:raise BatchError('One detail document exceeds isolated receiver limit')
  extra=sum(len(compact(row))+1 for values in unit.values() for row in values)
  if any(chunk.values()) and (size+extra>max_body or detail_bytes+measured>max_details):chunks.append(chunk);chunk={'records':[],'details':[],'outcomes':[],'history':[]};size=1000;detail_bytes=0
  if size+extra>max_body or detail_bytes+measured>max_details:raise BatchError('One record exceeds isolated receiver body limit')
  for key in chunk:chunk[key].extend(unit[key])
  size+=extra;detail_bytes+=measured
 if any(chunk.values()):chunks.append(chunk)
 return chunks
def public_receiver_error(raw,token=''):
 # Only the receiver's explicit public JSON error string is reported; never
 # include HTML, request headers, arbitrary exception reprs or full responses.
 if len(raw)>4096:return ''
 try:document=json.loads(raw)
 except (ValueError,UnicodeError):return ''
 message=document.get('error') if isinstance(document,dict) else None
 if not isinstance(message,str):return ''
 if token:message=message.replace(token,'[redacted]')
 return ''.join(c for c in message if c.isprintable())[:240]
class IsolatedReceiver:
 def __init__(self,url,token,github,seconds=240):
  parsed=urllib.parse.urlsplit(url)
  if parsed.scheme!='https' or parsed.netloc!='syzoz.com' or parsed.path!='/game-explorer/steam-sync.php' or parsed.query or parsed.fragment:raise BatchError('Unexpected isolated receiver URL')
  if len(token)<32 or '\r' in token or '\n' in token:raise BatchError('Missing isolated upload credential')
  self.url=url;self.token=token;self.github=github;self.deadline=time.monotonic()+seconds;self.opener=urllib.request.build_opener(NoRedirect())
 def call(self,action,**fields):
  payload={'action':action,'github':self.github,**fields};raw=compact(payload)
  if len(raw)>5500000:raise BatchError('Isolated upload body exceeds limit')
  request=urllib.request.Request(self.url,raw,{'Content-Type':'application/json','X-Steam-Token':self.token})
  for attempt in range(3):
   remaining=self.deadline-time.monotonic()
   if remaining<=1:raise BatchError('Finalizer upload deadline exhausted')
   try:
    with self.opener.open(request,timeout=min(30,remaining)) as response:body=response.read(1000001)
    if len(body)>1000000:raise BatchError('Isolated receipt exceeds limit')
    result=json.loads(body)
    if not isinstance(result,dict):raise BatchError('Isolated receipt is not an object')
    expected=hashlib.sha256(raw).hexdigest()
    # Exact receipt shape is shared by all isolated actions.
    if result.get('test') is not True or result.get('action')!=action or result.get('github')!=self.github or result.get('payload_hash')!=expected:raise BatchError('Isolated receipt identity or request hash mismatch')
    if action=='test-batch' and result.get('chunk_index')!=fields.get('chunk_index'):raise BatchError('Isolated receipt chunk mismatch')
    return result
   except urllib.error.HTTPError as error:
    code=error.code
    try:message=public_receiver_error(error.read(4097),self.token)
    finally:error.close()
    if code<500 or attempt==2:raise BatchError('Isolated receiver returned HTTP '+str(code)+(': '+message if message else '')) from None
   except (ValueError,OSError):
    if attempt==2:raise BatchError('Isolated receiver unavailable or invalid JSON') from None
   time.sleep(attempt+1)
  raise BatchError('Isolated receiver retry budget exhausted')
def upload_isolated(receiver,manifest,data):
 chunks=isolated_chunks(manifest,data);begin=receiver.call('test-begin',manifest={key:manifest[key] for key in ('schema','appids','logical_workers','runners')});receipts=[]
 for index,chunk in enumerate(chunks):
  payload={'chunk_index':index,'records':chunk['records'],'details':chunk['details'],'outcomes':chunk['outcomes']}
  if chunk['history']:payload['install_history']={'schema':1,'mode':'merge','games':chunk['history']}
  receipts.append(receiver.call('test-batch',**payload))
 finish=receiver.call('test-finish')
 if finish.get('complete') is not True or finish.get('success') is not True:raise BatchError('Isolated receiver reported incomplete or failed source coverage')
 return {'begin':begin,'chunks':receipts,'finish':finish}

def load_worker():
 if __package__:from . import collect
 else:import collect
 return collect

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
 p=sub.add_parser('plan');p.add_argument('--limit',type=int,default=1000);p.add_argument('--logical-workers',type=int,default=100);p.add_argument('--runners',type=int,default=2);p.add_argument('--seconds',type=int,default=240);p.add_argument('--output',type=Path,default=Path('manifest.json'));p.add_argument('--start',type=int,default=0);p.add_argument('--include-unreleased',action='store_true');p.add_argument('--resume-manifest',type=Path)
 p=sub.add_parser('work');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--runner',type=int,required=True);p.add_argument('--seconds',type=int,default=600);p.add_argument('--owners-limit',type=int,default=0);p.add_argument('--output',type=Path,default=Path('batch-results'));p.add_argument('--engine-seconds',type=int,default=20);p.add_argument('--engine-fallbacks',action='store_true')
 p=sub.add_parser('finalize');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('batch-final-result.json'));p.add_argument('--upload',action='store_true')
 args=parser.parse_args(argv)
 try:
  github=github_metadata(os.environ)
  if args.command=='plan':
   if not 1<=args.seconds<=240:raise BatchError('Planner budget must be <=240 seconds')
   resume=json.loads(args.resume_manifest.read_text()) if args.resume_manifest else None;result=plan_manifest(JsonClient(args.seconds,.5),args.limit,args.logical_workers,args.runners,github,args.start,args.include_unreleased,resume);write_json(args.output,result);print(compact({'planned':len(result['appids']),'manifest_hash':result['manifest_hash'],'logical_workers':result['logical_workers'],'runners':result['runners']}).decode())
   if os.environ.get('GITHUB_OUTPUT'):
    with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('matrix='+json.dumps({'runner':list(range(result['runners']))},separators=(',',':'))+'\n')
  elif args.command=='work':
   manifest=json.loads(args.manifest.read_text());validate_manifest(manifest,github)
   if not 1<=args.seconds<=900 or not 0<=args.owners_limit<=60 or not 0<=args.engine_seconds<=60:raise BatchError('Worker <=900 seconds; optional owners <=60 and engine seconds <=60 required')
   result=run_shards(load_worker(),JsonClient(args.seconds,max(1,manifest['runners']),slot=args.runner),manifest,args.runner,args.output,args.owners_limit,args.engine_seconds,args.engine_fallbacks);print(compact({k:result[k] for k in ('runner','status','coverage','owners_attempted')}).decode())
   if result['status']!='complete':return 1
  else:
   manifest=json.loads(args.manifest.read_text());validate_manifest(manifest,github);data=merged_shards(manifest,args.directory);result={'status':'staged','github':github,'manifest_hash':manifest['manifest_hash'],'planned':len(manifest['appids']),'records':len(data['records']),'details':len(data['details']),'outcomes':len(data['outcomes']),'production_writes':0,'coverage':{field:sum(row.get(field) is not None and row.get(field)!='' for row in data['records']) for field in ('icon','is_free','price','release_ts','updated_date','version','size','category','tags','rating','description','iap','architectures','installs')},'media_coverage':{field:sum(bool(row.get(field)) for row in data['details']) for field in ('screenshots','trailers')}}
   if args.upload:
    receiver=IsolatedReceiver(os.environ.get('STEAM_SYNC_URL','https://syzoz.com/game-explorer/steam-sync.php'),os.environ.get('STEAM_SYNC_TOKEN',''),github);result['isolated_upload']=upload_isolated(receiver,manifest,data);result['status']='uploaded_to_isolated_run'
   write_json(args.output,result);print(compact(result).decode())
  return 0
 except (BatchError,ValueError,OSError) as error:
  failure={'status':'failed','error':str(error)[:250],'production_writes':0}
  if args.command=='finalize':write_json(args.output,failure)
  print(compact(failure).decode(),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
