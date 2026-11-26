"""Sixty cooperating catalog jobs. Plans contain no credentials; the server owns progress."""
import argparse,importlib,json,os,re,time,uuid
from pathlib import Path

def load_plan(path, provider):
 p=json.loads(Path(path).read_text())
 if p.get('schema')!=1 or p.get('provider')!=provider or provider=='steam' and (p.get('query_sort')!=2 or p.get('listing_protocol')!=1) or p.get('workers')!=60 or not re.fullmatch(r'[0-9]+-[0-9]+',str(p.get('run_id',''))):raise ValueError('Invalid shared catalog plan')
 return p

def credential(provider, module):
 secret=os.environ.get('STEAM_SYNC_TOKEN','')
 if len(secret)<32:raise ValueError('Missing catalog credential')
 return module.catalog_credential(secret) if provider=='mobile' else secret

def mobile_plan(m, args, run):
 token=credential('mobile',m);http=m.Http(args.seconds)
 plan={'schema':1,'provider':'mobile','workers':60,'run_id':run,'mode':args.mode,'pending':[]}
 if args.mode in ('all','release_dates'):
  http.website(m.METADATA,{'scope':'release_dates','action':'seed','run_id':run,'claim_id':run+'-seed'},token,timeout=120)
 if args.mode not in ('metadata','release_dates'):
  browser=m.SitemapBrowser(http)
  try:
   root=browser.fetch(m.INDEX,'sitemapindex');manifest={}
   for row in root.findall('{'+m.NS+'}sitemap'):
    url=row.findtext('{'+m.NS+'}loc','');modified=row.findtext('{'+m.NS+'}lastmod','')
    if m.GAME.fullmatch(url):
     if len(modified)>80:raise ValueError('Invalid sitemap marker')
     manifest[url]=max(modified,manifest.get(url,''))
   if not manifest or len(manifest)>20000:raise ValueError('Invalid game manifest')
   reply=http.website(m.MIRROR,{'action':'begin','run_id':run,'manifest':manifest},token)
   pending=reply.get('pending')
   if pending==[]:pending={}
   if not isinstance(pending,dict) or any(url not in manifest or value!=manifest[url] for url,value in pending.items()):raise ValueError('Invalid pending sitemap list')
   plan['pending']=list(pending)
  finally:browser.close()
 return plan

def mobile_work(m,args,plan):
 try:return mobile_work_saved(m,args,plan)
 except m.WebsiteUnavailable as error:
  print(f'::warning::Mobile server request unavailable: {error}. Unconfirmed work remains pending for a later scan.',flush=True)
  return {'worker':args.worker,'deferred':True,'retry_pending':True}

def mobile_work_saved(m,args,plan):
 token=credential('mobile',m);http=m.Http(args.seconds);http.website_retry_seconds=60
 if plan['mode']=='release_dates':return m.release_dates(http,token,f"{plan['run_id']}-worker-{args.worker}")
 browser=m.SitemapBrowser(http);round_number=0;metadata_deferred=False
 http.metadata_scan_id=plan['run_id']
 def metadata():
  nonlocal round_number,metadata_deferred
  if metadata_deferred or plan['mode']=='sitemaps' or not args.metadata_batches:return {'batches':0,'stored':0,'dated':0}
  claim_run=f"{plan['run_id']}-worker-{args.worker}-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT','1')}-round-{round_number}";round_number+=1
  result=m.sync_metadata(http,token,claim_run,args.metadata_batches)
  metadata_deferred=bool(result.get('deferred'))
  return result
 try:
  metadata()
  state=http.website(m.MIRROR,{'action':'parallel-status','run_id':plan['run_id']},token) if plan['mode']!='metadata' else {'pending':[]}
  remaining=set(state['pending'])
  urls=[url for url in plan['pending'][args.worker::plan['workers']] if url in remaining]
  for number,url in enumerate(urls):
   if http.deadline-time.monotonic()<60:break
   root=None
   for attempt in range(3):
    try:root=browser.fetch(url,'urlset');break
    except m.SyncError as error:
     if error.status in (403,429) or attempt==2:raise
     time.sleep(attempt+1)
   m.ET.register_namespace('',m.NS);body=m.ET.tostring(root,encoding='utf-8',xml_declaration=True)
   zipped=m.gzip.compress(body,mtime=0)
   if len(body)>8_000_000 or len(zipped)>4_000_000:raise ValueError('Sitemap exceeds receiver limits')
   saved=http.website(m.MIRROR,{'action':'file','run_id':plan['run_id'],'url':url,'sha256':m.hashlib.sha256(body).hexdigest(),'gzip':m.base64.b64encode(zipped).decode('ascii')},token)
   print(json.dumps({'worker':args.worker,'file':url.rsplit('/',1)[-1],'files_done':saved.get('files_done')}),flush=True)
   metadata()
  while http.deadline-time.monotonic()>60:
   result=metadata()
   if not result['batches']:break
  if plan['mode']=='all' and not metadata_deferred and http.deadline-time.monotonic()>40:m.release_dates(http,token,f"{plan['run_id']}-worker-{args.worker}")
 finally:browser.close()
 return {'worker':args.worker,'checkpointed':True,'metadata_deferred':metadata_deferred}

def mobile_finish(m,args,plan):
 if plan['mode']=='release_dates':return m.Http(120).website(m.METADATA,{'scope':'release_dates','action':'status','run_id':plan['run_id'],'claim_id':plan['run_id']+'-status'},credential('mobile',m))
 if plan['mode']=='metadata':return {'metadata':'workers finished; any deferred claims remain queued'}
 token=credential('mobile',m);http=m.Http(900)
 payload={'run_id':plan['run_id']}
 def status():return http.website(m.MIRROR,{'action':'parallel-status',**payload},token)
 state=status()
 if state.get('complete'):return state
 if state.get('pending_files')!=0:return http.website(m.MIRROR,{'action':'parallel-checkpoint',**payload},token)
 return m.finish_mirror(http,token,plan['run_id'])

def steam_transport(m):return m.Transport(os.environ.get('STEAM_SYNC_URL','https://syzoz.com/chart/steam-sync.php'),credential('steam',m))

def steam_deferred(error, **fields):
 print(f'::warning::{error}. Skipping unavailable work; unconfirmed pages remain at their saved server checkpoints for a later sync.',flush=True)
 return dict(fields,deferred=True,retry_pending=True)

def steam_plan(m,args,run):
 _,_,_,total,_=m.store_catalog_page(0,time.monotonic()+90,count=1,select_ids=lambda ids:[])
 plan={'schema':1,'provider':'steam','workers':60,'query_sort':2,'listing_protocol':1,'run_id':run}
 try:reply=steam_transport(m).call('parallel-begin',run_id=run,workers=60,total=total,query_sort=2,listing_protocol=1)
 except m.CheckpointUnavailable as error:return steam_deferred(error,**plan)
 return dict(plan,total=reply['total'],offsets=reply['offsets'],generation=reply['generation'])

def steam_work(m,args,plan):
 if plan.get('deferred'):return steam_deferred('Shared Steam plan unavailable',worker=args.worker)
 try:return steam_work_pages(m,args,plan)
 except m.CheckpointUnavailable as error:return steam_deferred(error,worker=args.worker)

def steam_work_pages(m,args,plan):
 tr=steam_transport(m);deadline=time.monotonic()+args.seconds
 session=uuid.uuid4().hex;receipt=tr.call('parallel-worker',run_id=plan['run_id'],worker=args.worker,generation=plan['generation'],session=session);offset=receipt['next_offset']
 while offset<plan['total'] and deadline-time.monotonic()>60:
  records=details=None
  selection={}
  def select_ids(ids):
   reply=tr.call('parallel-filter',run_id=plan['run_id'],worker=args.worker,generation=plan['generation'],session=session,offset=offset,appids=ids)
   needed=reply.get('needed_ids');selection.update(candidates=len(ids),needed=len(needed) if isinstance(needed,list) else 0)
   return needed
  for attempt in range(3):
   try:records,details,_,_,_,listings=m.store_catalog_page(offset,deadline,include_listings=True,select_ids=select_ids);break
   except m.CheckpointUnavailable:raise
   except (OSError,ValueError,RuntimeError):
    if attempt==2:raise
    time.sleep(attempt+1)
  if records:m.fetch_engines(records,min(deadline,time.monotonic()+args.engine_seconds))
  class PageTransport:
   def __init__(self):self.chunk=0;self.receipt=None
   def call(self,action,**payload):
    self.receipt=tr.call('parallel-batch',run_id=plan['run_id'],worker=args.worker,generation=plan['generation'],session=session,offset=offset,chunk=self.chunk,final=payload['state']['page_done'],listings=listings if payload['state']['page_done'] else [],records=payload['records'],details=payload['details']);self.chunk+=1
    return self.receipt
  page=PageTransport();base={'run_id':plan['run_id'],'page_done':False}
  m.send_catalog_batch(page,base,{**base,'page_done':True},records,details,'',max_body=5_400_000)
  next_offset=page.receipt.get('next_offset')
  if type(next_offset) is not int or next_offset!=offset+100*plan['workers']:raise ValueError('Invalid Steam page receipt')
  print(json.dumps({'worker':args.worker,'offset':offset,'next_offset':next_offset,'games':len(records),'already_saved':selection.get('candidates',0)-selection.get('needed',0),'exceptions':[r for r in listings if 'outcome' in r]}),flush=True);offset=next_offset
 return {'worker':args.worker,'next_offset':offset,'checkpointed':True}

def steam_finish(m,args,plan):
 if plan.get('deferred'):return steam_deferred('Shared Steam plan unavailable')
 try:return steam_finish_saved(m,args,plan)
 except m.CheckpointUnavailable as error:return steam_deferred(error)

def steam_finish_saved(m,args,plan):
 tr=steam_transport(m)
 reply=tr.call('parallel-finish',run_id=plan['run_id'],generation=plan['generation'])
 from steam_releases import refresh
 refresh(tr,time.monotonic()+args.seconds-args.owners_seconds-60)
 if args.owners_seconds:
  state=tr.call('state');m.owner_phase(tr,state,time.monotonic()+args.owners_seconds,args.engine_seconds)
  state.update(status='checkpointed',phase='Parallel Store catalog checkpoint saved',heartbeat=int(time.time()));tr.call('finish',state=state)
 return reply

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('stage',choices=['plan','work','finish']);parser.add_argument('--provider',choices=['mobile','steam'],required=True)
 parser.add_argument('--plan',default='work-plan.json');parser.add_argument('--worker',type=int,default=0)
 parser.add_argument('--seconds',type=int,default=21600);parser.add_argument('--mode',choices=['all','sitemaps','metadata','release_dates'],default='all')
 parser.add_argument('--metadata-batches',type=int,default=20);parser.add_argument('--owners-seconds',type=int,default=300);parser.add_argument('--engine-seconds',type=int,default=60)
 args=parser.parse_args()
 if not 60<=args.seconds<=21600 or not 0<=args.worker<60 or not 0<=args.metadata_batches<=200 or not 0<=args.owners_seconds<=600 or not 0<=args.engine_seconds<=120:raise ValueError('Invalid worker bounds')
 module=importlib.import_module('apkpure.admin_mobile' if args.provider=='mobile' else 'collect')
 if args.stage=='plan':
  run=os.environ.get('GITHUB_RUN_ID','')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
  if not re.fullmatch(r'[0-9]+-[0-9]+',run):raise ValueError('GitHub run identity required')
  plan=(mobile_plan if args.provider=='mobile' else steam_plan)(module,args,run)
  Path(args.plan).write_text(json.dumps(plan));print(json.dumps({'provider':args.provider,'workers':plan['workers'],'plan_saved':True}),flush=True)
 else:
  plan=load_plan(args.plan,args.provider)
  if args.worker>=plan['workers']:raise ValueError('Worker is outside the shared plan')
  function={('mobile','work'):mobile_work,('mobile','finish'):mobile_finish,('steam','work'):steam_work,('steam','finish'):steam_finish}[(args.provider,args.stage)]
  print(json.dumps(function(module,args,plan)),flush=True)
if __name__=='__main__':main()
