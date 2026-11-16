"""Anonymous Steam collector. Website owns durable checkpoints; no empty-gap stop."""
import argparse,base64,gzip,json,os,re,time,unicodedata,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime,timezone
from steam.client import SteamClient
from steam.enums import EResult
from gevent import Timeout
MAX_APP_ID=2**32-1
class BatchDataError(ValueError):pass
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
  req=urllib.request.Request(self.url,wire_json(data),{'Content-Type':'application/json','X-Steam-Token':self.token})
  for attempt in range(3):
   try:
    with urllib.request.build_opener(NoRedirect).open(req,timeout=90) as r:return json.load(r)
   except urllib.error.HTTPError as e:
    if action=='batch' and e.code in (400,413,422):raise BatchDataError('Batch rejected: HTTP '+str(e.code)) from None
    if e.code<500:raise RuntimeError('Catalog checkpoint rejected: HTTP '+str(e.code)) from None
    if attempt==2:raise
   except OSError:
    if attempt==2:raise
   time.sleep(2*(attempt+1))
def wire_json(value):
 return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def send_catalog_batch(tr,previous,state,records,details,audit,max_body=5500000,max_details=4000000,max_detail=2000000):
 # Validate and partition before sending. Only the last chunk commits the new
 # scan cursor/counters; earlier writes replay safely after an interrupted send.
 byid={row['appid']:row for row in details}
 if len(byid)!=len(details) or not set(byid).issubset({row['appid'] for row in records}):raise BatchDataError('Unmatched or duplicate detail document')
 interim=dict(previous)
 for key in ('run_id','status','phase','heartbeat','batch_from','batch_to'):
  if key in state:interim[key]=state[key]
 base=max(len(wire_json(dict(action='batch',state=checkpoint,records=[],details=[],audit=audit))) for checkpoint in (interim,state))
 chunks=[];rows=[];docs=[];body_size=base;detail_size=2
 for row in records:
  detail=byid.get(row['appid']);row_bytes=len(wire_json(row));doc_bytes=len(wire_json(detail)) if detail is not None else 0
  # PHP validators may escape non-ASCII/slashes when measuring decoded docs.
  measured=len(json.dumps(detail,separators=(',',':'),ensure_ascii=True).replace('/','\\/').encode()) if detail is not None else 0
  if measured>max_detail:raise BatchDataError('One detail document exceeds the receiver limit')
  extra=row_bytes+(1 if rows else 0)+doc_bytes+(1 if detail is not None and docs else 0)
  extra_detail=measured+(1 if detail is not None and docs else 0)
  if rows and (body_size+extra>max_body or detail_size+extra_detail>max_details):
   chunks.append((rows,docs));rows=[];docs=[];body_size=base;detail_size=2;extra=row_bytes+doc_bytes;extra_detail=measured
  if body_size+extra>max_body or detail_size+extra_detail>max_details:raise BatchDataError('One catalog record exceeds the receiver limit')
  rows.append(row);body_size+=extra;detail_size+=extra_detail
  if detail is not None:docs.append(detail)
 if rows or not chunks:chunks.append((rows,docs))
 for index,(rows,docs) in enumerate(chunks):tr.call('batch',state=state if index==len(chunks)-1 else interim,records=rows,details=docs,audit=audit)
 return len(chunks)
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
def public_size(data):
 # One shared download-byte field: public Windows64/English, no optional DLC
 # or shared-app redistributables. Installed bytes/cloud quota are not sizes.
 unknown={'size':None};depots=data.get('depots')
 if not isinstance(depots,dict):return unknown
 total=0;found=False
 for depotid,depot in depots.items():
  if not (isinstance(depotid,str) and depotid.isascii() and depotid.isdecimal()):continue
  if not isinstance(depot,dict):return unknown
  config=depot.get('config') or {}
  if not isinstance(config,dict):return unknown
  if any(value not in (None,'',0,'0',False) for value in (depot.get('dlcappid'),config.get('optionaldlc'),depot.get('sharedinstall'),depot.get('depotfromapp'))):continue
  oslist=config.get('oslist') or '';language=config.get('language') or '';arch=config.get('osarch') or ''
  if not isinstance(oslist,str) or not isinstance(language,str) or type(arch) not in (str,int):return unknown
  if oslist.strip() and 'windows' not in oslist.lower().replace(',',' ').split():continue
  if language.strip().lower() not in ('','english') or str(arch).strip() not in ('','64'):continue
  manifests=depot.get('manifests') or {}
  if not isinstance(manifests,dict):return unknown
  if 'public' not in manifests:continue
  manifest=manifests['public']
  if not isinstance(manifest,dict):return unknown
  value=manifest.get('download')
  if type(value) is not int and not (isinstance(value,str) and value.isascii() and value.isdecimal()):return unknown
  try:size=int(value)
  except ValueError:return unknown
  if not 0<=size<=9007199254740991 or total+size>9007199254740991:return unknown
  total+=size;found=True
 return {'size':total if found else None}
def normalize(appid,data):
 c=data.get('common') or {};name=c.get('name');kind=str(c.get('type','unknown')).lower()
 if not name:return None
 associations=c.get('associations') or {};developers=[x.get('name','') for x in associations.values() if isinstance(x,dict) and x.get('type')=='developer'] if isinstance(associations,dict) else []
 icon=''
 for field,ext,lengths in [('clienticon','ico',(32,40)),('icon','jpg',(40,))]:
  image_hash=c.get(field)
  if isinstance(image_hash,str) and len(image_hash) in lengths and all(x in '0123456789abcdef' for x in image_hash.lower()):
   icon='https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/'+str(appid)+'/'+image_hash.lower()+'.'+ext;break
 version=None;depots=data.get('depots');branches=depots.get('branches') if isinstance(depots,dict) else None;public=branches.get('public') if isinstance(branches,dict) else None;build=public.get('buildid') if isinstance(public,dict) else None
 if (type(build) is int or isinstance(build,str) and 1<=len(build)<=16 and build.isascii() and build.isdecimal()) and 0<int(build)<=9007199254740991:version='Build '+str(int(build))
 return {'appid':int(appid),'name':str(name)[:500],'type':kind,'release_ts':timestamp(c.get('steam_release_date')),'original_release_ts':None,'coming_soon':c.get('releasestate')=='prerelease','windows':'windows' in str(c.get('oslist','')).lower(),'developer':', '.join(developers)[:500],'icon':icon,'restricted':bool(data.get('_missing_token',False)),'is_free':None,'installs':None,'installs_max':None,'installs_estimated':None,'installs_source':None,'installs_checked_at':None,'rating':None,'rating_count':None,'rating_source':None,'rating_checked_at':None,'category':None,'tags':None,'version':version,'description':None,'price':None,'ads_supported':None,'iap':None,'architectures':None,**public_update(data),**public_size(data)}
STORE_TAG_NAMES=None
STORE_DATA_REQUEST={'include_release':True,'include_basic_info':True,'include_assets':True,'include_platforms':True,'include_reviews':True,'include_tag_count':20,'include_screenshots':True,'include_trailers':True,'include_full_description':True,'include_best_purchase_option':True}
def parse_store_tags(document):
 response=document.get('response') if isinstance(document,dict) else None;tags=response.get('tags') if isinstance(response,dict) else None
 if not isinstance(tags,list) or len(tags)>10000:raise ValueError('Invalid tag dictionary')
 names={}
 for row in tags:
  if not isinstance(row,dict):raise ValueError('Invalid tag dictionary entry')
  tagid=row.get('tagid');name=row.get('name')
  if type(tagid) is not int or not 1<=tagid<=MAX_APP_ID or not isinstance(name,str) or not 1<=len(name.strip())<=80 or any(unicodedata.category(c).startswith('C') for c in name):raise ValueError('Invalid tag dictionary identity')
  if tagid in names and names[tagid]!=name.strip():raise ValueError('Conflicting tag names')
  names[tagid]=name.strip()
 return names
def store_tag_names(deadline=None):
 global STORE_TAG_NAMES
 if STORE_TAG_NAMES is not None:return STORE_TAG_NAMES
 STORE_TAG_NAMES={};remaining=15 if deadline is None else deadline-time.monotonic()
 if remaining<=1:return STORE_TAG_NAMES
 try:
  url='https://api.steampowered.com/IStoreService/GetTagList/v1/?'+urllib.parse.urlencode({'input_json':json.dumps({'language':'english'})})
  timeout=min(15,remaining)
  with Timeout(timeout,OSError('Tag dictionary timed out')):
   with urllib.request.urlopen(url,timeout=timeout) as response:raw=response.read(1000001)
  if len(raw)>1000000:raise ValueError('Tag dictionary too large')
  STORE_TAG_NAMES=parse_store_tags(json.loads(raw))
 except urllib.error.HTTPError as error:error.close()
 except (OSError,ValueError):pass
 return STORE_TAG_NAMES
def store_json_fields(item,names,checked_at):
 out={};reviews=item.get('reviews') if isinstance(item.get('reviews'),dict) else {};summary=reviews.get('summary_filtered')
 if isinstance(summary,dict):
  count=summary.get('review_count');percent=summary.get('percent_positive')
  if type(count) is int and 0<count<=9007199254740991 and type(percent) is int and 0<=percent<=100:out.update(rating=5*percent/100,rating_count=count,rating_source='steam_store_summary',rating_checked_at=checked_at)
 ids=item.get('tagids')
 if not isinstance(ids,list) or not ids:
  tags=item.get('tags');ids=[row.get('tagid') for row in tags if isinstance(row,dict)] if isinstance(tags,list) else []
 labels=[];seen=set()
 for tagid in ids:
  label=names.get(tagid) if type(tagid) is int else None
  if isinstance(label,str) and label.casefold() not in seen:labels.append(label);seen.add(label.casefold())
  if len(labels)==20:break
 if labels:out.update(category=labels[0],tags=labels)
 return out
def bbcode_text(value):
 if not isinstance(value,str):return ''
 value=re.sub(r'\[(?:img|video|previewyoutube)(?:=[^\]]*)?\].*?\[/(?:img|video|previewyoutube)\]','',value,flags=re.I|re.S)
 value=re.sub(r'\[/?(?:h[1-6]|p|list|olist|quote)(?:=[^\]]*)?\]','\n',value,flags=re.I).replace('[*]','\n• ')
 value=re.sub(r'\[/?[a-z][a-z0-9_]*(?:=[^\]]*)?\]','',value,flags=re.I)
 return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+','\n\n',value).strip()
def store_asset(path):
 if not isinstance(path,str) or not path.startswith('steam/apps/') or '://' in path or '..' in path:return None
 return 'https://shared.fastly.steamstatic.com/store_item_assets/'+path
def store_detail_json(record,item,checked_at):
 appid=record['appid'];basic=item.get('basic_info') if isinstance(item.get('basic_info'),dict) else {};release=item.get('release') if isinstance(item.get('release'),dict) else {};assets=item.get('assets') if isinstance(item.get('assets'),dict) else {}
 screenshots=item.get('screenshots') if isinstance(item.get('screenshots'),dict) else {};shots=[]
 for row in screenshots.get('all_ages_screenshots',[])+screenshots.get('mature_content_screenshots',[]):
  filename=row.get('filename') if isinstance(row,dict) else None
  if isinstance(filename,str) and filename.startswith('steam/apps/'+str(appid)+'/') and (url:=store_asset(filename)) and url not in shots:shots.append(url)
 trailer_data=item.get('trailers') if isinstance(item.get('trailers'),dict) else {};trailers=[]
 for kind in ('highlights','other_trailers'):
  for row in trailer_data.get(kind,[]):
   if not isinstance(row,dict):continue
   trailer={'id':row.get('trailer_id'),'name':row.get('trailer_name'),'highlight':kind=='highlights'};fmt=row.get('trailer_url_format','');query=urllib.parse.urlsplit(fmt).query if isinstance(fmt,str) else ''
   thumb=row.get('screenshot_medium') or row.get('screenshot_full')
   if isinstance(fmt,str) and isinstance(thumb,str):trailer['thumbnail']=store_asset(fmt.replace('${FILENAME}',thumb))
   for video in row.get('adaptive_trailers',[]):
    path=video.get('cdn_path') if isinstance(video,dict) else None;encoding=video.get('encoding') if isinstance(video,dict) else None
    if encoding in ('hls_h264','dash_h264','dash_av1') and isinstance(path,str) and path.startswith(str(appid)+'/') and '..' not in path and '://' not in path:trailer[encoding]='https://video.akamai.steamstatic.com/store_trailers/'+path+('?' +query if query else '')
   if any(key in trailer for key in ('hls_h264','dash_h264','dash_av1')):trailers.append(trailer)
 summary=(item.get('reviews') or {}).get('summary_filtered',{}) if isinstance(item.get('reviews'),dict) else {};platforms=item.get('platforms') if isinstance(item.get('platforms'),dict) else {};fmt=assets.get('asset_url_format');header=assets.get('header')
 label=release.get('custom_release_date_message') or release.get('coming_soon_display')
 if not label and record.get('release_ts'):label=datetime.fromtimestamp(record['release_ts'],timezone.utc).strftime('%b %d, %Y')
 return {'schema':1,'appid':appid,'is_free':record.get('is_free'),'short_description':basic.get('short_description'),'description':bbcode_text(item.get('full_description_bbcode')) or record.get('description') or '', 'genres':[],'categories':['In-App Purchases'] if record.get('iap') is True else [],'screenshots':shots,'trailers':trailers,'release_label':label,'release_ts':record.get('release_ts'),'original_release_ts':record.get('original_release_ts'),'checked_at':checked_at,'platforms':{target:platforms[source] for source,target in [('windows','windows'),('mac','mac'),('steamos_linux','linux')] if type(platforms.get(source)) is bool},'publishers':[row['name'] for row in basic.get('publishers',[]) if isinstance(row,dict) and isinstance(row.get('name'),str)],'developers':[row['name'] for row in basic.get('developers',[]) if isinstance(row,dict) and isinstance(row.get('name'),str)],'country':'US','store_url':'https://store.steampowered.com/app/'+str(appid)+'/','source':'Steam Store JSON','header_image':store_asset(fmt.replace('${FILENAME}',header)) if isinstance(fmt,str) and isinstance(header,str) else None,'reviews':{key:summary.get(key) for key in ('review_count','percent_positive','review_score','review_score_label')}}
def owner_estimates(record):
 return {key:record[key] for key in ('appid','installs','installs_max','installs_estimated','installs_source','installs_checked_at')}

def store_free_status(appid,deadline=None):
 # Optional protobuf-field absence alone does not prove a paid game. Only an
 # explicit boolean from this matching successful public listing may fill it.
 remaining=8 if deadline is None else deadline-time.monotonic()
 if remaining<=1:return None
 timeout=min(8,remaining);url='https://store.steampowered.com/api/appdetails?'+urllib.parse.urlencode({'appids':appid,'cc':'us','l':'english'})
 try:
  with Timeout(timeout,OSError('Store free-status request timed out')):
   with urllib.request.urlopen(url,timeout=timeout) as response:raw=response.read(1000001)
  if len(raw)>1000000:return None
  reply=json.loads(raw);item=reply.get(str(appid)) if isinstance(reply,dict) else None
  data=item.get('data') if isinstance(item,dict) and item.get('success') is True else None
  if isinstance(data,dict) and type(data.get('steam_appid')) is int and data['steam_appid']==appid and data.get('type')=='game' and type(data.get('is_free')) is bool:return data['is_free']
 except urllib.error.HTTPError as error:
  error.close();return None
 except (OSError,ValueError):return None
 return None
def fetch_engines(records, deadline, allow_fallbacks=True):
    games = {r['appid']: r for r in records if r.get('type') == 'game'}
    ids = list(games)
    for start in range(0, len(ids), 80):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        batch = ids[start:start + 80]
        values = ' '.join(json.dumps(str(i)) for i in batch)
        query = ('SELECT DISTINCT ?id ?name WHERE { VALUES ?id { ' + values +
                 ' } ?game wdt:P1733 ?id ; wdt:P408 ?engine . '
                 '?engine rdfs:label ?name . FILTER(LANG(?name)="en") } ORDER BY ?id ?name')
        url = 'https://query.wikidata.org/sparql?' + urllib.parse.urlencode({'query': query, 'format': 'json'})
        req = urllib.request.Request(url, headers={'User-Agent': 'SyzozPlay/1.0 (https://syzoz.com/game-explorer/)'})
        try:
            with urllib.request.urlopen(req, timeout=min(8, remaining)) as response:
                bindings = json.load(response)['results']['bindings']
            for row in bindings:
                appid = int(row['id']['value'])
                name = row['name']['value']
                if appid in batch and isinstance(name,str) and 0 < len(name) <= 80 and name.isprintable():
                    labels = games[appid].get('architectures') or []
                    if name not in labels and len(labels) < 20:
                        games[appid]['architectures'] = labels + [name]
        except (OSError, ValueError, TypeError, KeyError):
            break

    if not allow_fallbacks:
        return
    missing = [r for r in games.values() if not r.get('architectures')]
    for start in range(0, len(missing), 20):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        batch = missing[start:start + 20]
        indexed = {str(r['appid']): r for r in batch}
        titles = sorted({title for r in batch for title in (r['name'], r['name'].title()) if '|' not in title})
        params = {'action':'query', 'format':'json', 'prop':'revisions', 'rvprop':'content',
                  'rvslots':'main', 'redirects':1, 'titles':'|'.join(titles)}
        req = urllib.request.Request('https://www.pcgamingwiki.com/w/api.php?' + urllib.parse.urlencode(params),
                                     headers={'User-Agent':'SyzozPlay/1.0 (https://syzoz.com/game-explorer/)'})
        try:
            with urllib.request.urlopen(req, timeout=min(8, remaining)) as response:
                pages = json.load(response)['query']['pages']
            for page in pages.values():
                for rev in page.get('revisions', []):
                    content = rev['slots']['main']['*']
                    identity = re.search(r'(?im)^\|steam appid\s*=\s*(\d+)[ \t]*$', content)
                    if identity and identity[1] in indexed:
                        names = re.findall(r'(?i)\{\{Infobox game/row/engine\|([^|{}\n]{1,80})(?:\||\}\})', content)
                        labels = list(dict.fromkeys(n.strip() for n in names if n.strip().isprintable() and n.strip()))
                        if labels:
                            indexed[identity[1]]['architectures'] = labels[:20]
        except (OSError, ValueError, TypeError, KeyError):
            continue

    for row in games.values():
        if row.get('architectures') or not row.get('coming_soon'):
            continue
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        params = {'appid': row['appid'], 'count': 30, 'feeds': 'steam_community_announcements'}
        url = 'https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?' + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=min(6, remaining)) as response:
                news = json.load(response)['appnews']
            if news.get('appid') != row['appid']:
                continue
            for article in news.get('newsitems', []):
                if article.get('feedname') != 'steam_community_announcements' or article.get('appid') != row['appid']:
                    continue
                declaration = re.search(r"(?i)we(?:['’]re| are) developing (?:this|the game) (?:in|using) (Unity|Unreal Engine(?: [45])?|Godot)\b", article.get('contents',''))
                if declaration:
                    row['architectures'] = [declaration[1]]
                    break
        except (OSError, ValueError, TypeError, KeyError):
            continue

def store_dates(records,deadline=None,tag_names=None,include_details=False):
 # Commerce applies to every named game, including records already dated by PICS.
 # Missing StoreBrowse flags use a bounded explicit Store-listing fallback.
 games=[r for r in records if r['type']=='game'];details=[]
 for start in range(0,len(games),100):
  rows=games[start:start+100];payload={'ids':[{'appid':r['appid']} for r in rows],'context':{'language':'english','country_code':'US'},'data_request':dict(STORE_DATA_REQUEST)}
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
    # Store aliases can share an AppID. Match each requested listing once.
    listing_id=a.get('id',a.get('appid'))
    r=byid.get(listing_id)
    if not r or a.get('success')!=1:continue
    del byid[listing_id]
    is_free=a.get('is_free')
    if 'is_free' not in a:
     is_free=store_free_status(r['appid'],deadline)
     if is_free is None:r['store_retry']=True
    if type(is_free) is bool:r['is_free']=is_free
    basic=a.get('basic_info');basic=basic if isinstance(basic,dict) else {};description=basic.get('short_description')
    if isinstance(description,str) and 0<len(description.strip())<=50000 and '\x00' not in description and not any(0xD800<=ord(c)<=0xDFFF for c in description):r['description']=description.strip()
    categories=a.get('categories');features=categories.get('feature_categoryids') if isinstance(categories,dict) else None
    if isinstance(features,list) and all(type(i) is int and 0<=i<=MAX_APP_ID for i in features):r['iap']=35 in features
    # A free game's optional upgrade/edition is not the base-game price.
    purchase=a.get('best_purchase_option');price=purchase.get('formatted_final_price') if isinstance(purchase,dict) else None
    if is_free is True:r['price']='0'
    elif isinstance(price,str) and 0<len(price.strip())<=200 and not any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in price):r['price']=price.strip()
    rel=a.get('release') or {};released=timestamp(rel.get('steam_release_date'));original=timestamp(rel.get('original_release_date'))
    # StoreBrowse reports the current public date; PICS can retain a planned date after launch.
    if released:
     now=datetime.now(timezone.utc).timestamp()
     listed_as_released=rel.get('is_coming_soon',r.get('coming_soon')) is False
     if not r['release_ts'] or (listed_as_released and released<=now<r['release_ts']):
      r['release_ts']=released
    if original:r['original_release_ts']=original
    if type(rel.get('is_coming_soon')) is bool:r['coming_soon']=rel['is_coming_soon']
    assets=a.get('assets');assets=assets if isinstance(assets,dict) else {};image_hash=assets.get('community_icon')
    if not str(r.get('icon') or '').endswith('.ico') and isinstance(image_hash,str) and len(image_hash)==40 and all(x in '0123456789abcdef' for x in image_hash.lower()):r['icon']='https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/'+str(r['appid'])+'/'+image_hash.lower()+'.jpg'
    developer_rows=basic.get('developers');developers=', '.join(x['name'] for x in developer_rows if isinstance(x,dict) and isinstance(x.get('name'),str))[:500] if isinstance(developer_rows,list) else ''
    if developers:r['developer']=developers
    if any(key in a for key in ('reviews','tagids','tags')):
     if tag_names is None:tag_names=store_tag_names(deadline)
     r.update(store_json_fields(a,tag_names,int(time.time())))
    if include_details:details.append(store_detail_json(r,a,int(time.time())))
  except (OSError,ValueError):
   for r in rows:r['store_retry']=True
  if deadline is None:time.sleep(.5)
  elif deadline-time.monotonic()>.5:time.sleep(.5)
 return details
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
 tags=data.get('tags');ranked=[]
 if isinstance(tags,dict) and tags:
  for name,votes in tags.items():
   if not isinstance(name,str) or not 1<=len(name.strip())<=80 or any(unicodedata.category(c).startswith('C') for c in name) or type(votes) is not int or not 0<=votes<=9007199254740991:
    ranked=[];break
   ranked.append((name.strip(),votes))
 if ranked:
  ranked.sort(key=lambda item:(-item[1],item[0].casefold(),item[0]));names=[];seen=set()
  for name,votes in ranked:
   if name.casefold() not in seen:names.append(name);seen.add(name.casefold())
   if len(names)==20:break
  record.update(category=names[0],tags=names)
 return record
def review_fields(appid,data,checked_at):
 # The public review summary is global across the requested all-language scope.
 if type(appid) is not int or not 1<=appid<=MAX_APP_ID or type(checked_at) is not int or not 1<=checked_at<=9007199254740991:raise ValueError('Invalid review identity or timestamp')
 if not isinstance(data,dict) or type(data.get('success')) is not int or data['success']!=1 or not isinstance(data.get('query_summary'),dict):raise ValueError('Unavailable Steam review summary')
 summary=data['query_summary'];positive=summary.get('total_positive');negative=summary.get('total_negative');total=summary.get('total_reviews')
 if any(type(n) is not int or not 0<=n<=9007199254740991 for n in (positive,negative,total)) or positive+negative!=total:raise ValueError('Invalid Steam review counts')
 if total==0:return {}
 return {'rating':5*positive/total,'rating_count':total,'rating_source':'steam_reviews','rating_checked_at':checked_at}
def owner_phase(tr,state,deadline,engine_seconds=0):
 # Refill from saved records until the bounded budget ends, independent of PICS.
 # One shared timer spans every page: SteamSpy appdetails starts <= once/second.
 totals={'collected':0,'failed':0,'deferred':0};seen=set();next_request=0.0;engine_left=max(0,engine_seconds)
 while deadline-time.monotonic()>1:
  reply=tr.call('owner-candidates',limit=50);candidates=reply.get('records') if isinstance(reply,dict) else None
  if not isinstance(candidates,list) or len(candidates)>50:raise RuntimeError('Invalid ownership candidates response')
  ids=[r.get('appid') if isinstance(r,dict) else None for r in candidates]
  if any(type(i) is not int or not 1<=i<=MAX_APP_ID for i in ids) or len(set(ids))!=len(ids):raise RuntimeError('Invalid ownership candidate IDs')
  candidates=[r for r in candidates if r['appid'] not in seen]
  if not candidates:break
  seen.update(r['appid'] for r in candidates)
  engines=[]
  if engine_left>1 and deadline-time.monotonic()>2:
   pending=[dict(appid=r['appid'],type='game',name=r['name'],coming_soon=False,architectures=None) for r in candidates if r.get('engine_due') is True and isinstance(r.get('name'),str) and r['name']]
   if pending:
    before=time.monotonic();fetch_engines(pending,min(deadline-1,before+engine_left));engine_left=max(0,engine_left-(time.monotonic()-before))
    engines=[{'appid':r['appid'],'architectures':r.get('architectures') or None,'checked_at':int(time.time())} for r in pending]
  records=[];games=[];failed=[];stopped=False;attempted=0
  for candidate in candidates:
   if candidate.get('owners_due') is False:continue
   appid=candidate['appid'];wait=max(0.0,next_request-time.monotonic())
   if deadline-time.monotonic()<=wait+1:break
   if wait:time.sleep(wait)
   remaining=deadline-time.monotonic()
   if remaining<=1:break
   timeout=min(8.0,remaining);next_request=time.monotonic()+1.0;attempted+=1
   try:
    url='https://steamspy.com/api.php?'+urllib.parse.urlencode({'request':'appdetails','appid':appid})
    with Timeout(timeout,OSError('Ownership request timed out')):
     with urllib.request.urlopen(url,timeout=timeout) as response:raw=response.read(1000001)
    if len(raw)>1000000:raise ValueError('Ownership response too large')
    checked_at=int(time.time());record=owner_estimates(owner_fields(appid,json.loads(raw),checked_at))
    records.append(record);games.append({'package':'steam:'+str(appid),'revision':checked_at,'points':[{'observed_at':checked_at,'total_installs':record['installs'],'period':1}]})
   except urllib.error.HTTPError as error:
    failed.append(appid);code=error.code;error.close()
    if code in (403,429,503):stopped=True;break
   except (OSError,ValueError,OverflowError):failed.append(appid)
  if records or failed or engines:
   state.update(status='running',phase='owners',heartbeat=int(time.time()));payload={'state':state,'records':records,'failed_appids':failed}
   if games:payload['install_history']={'schema':1,'mode':'merge','games':games}
   if engines:payload['engine_records']=engines
   tr.call('owners',**payload)
  totals['collected']+=len(records);totals['failed']+=len(failed)
  deferred=sum(r.get('owners_due') is not False for r in candidates)-attempted;totals['deferred']+=deferred
  print(json.dumps({'phase':'enrichment','owners_saved':len(records),'owners_failed':len(failed),'engines_saved':sum(bool(r['architectures']) for r in engines),'engines_checked':len(engines),'deferred':deferred}),flush=True)
  if stopped or deferred or (not records and not failed and not engines):break
 return totals

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--seconds',type=int,default=21600);ap.add_argument('--owners-seconds',type=int,default=300);ap.add_argument('--engine-seconds',type=int,default=60);ap.add_argument('--enrich-only',action='store_true');ap.add_argument('--batch',type=int,default=1000);ap.add_argument('--local');ap.add_argument('--scan-to',type=int,default=MAX_APP_ID);args=ap.parse_args()
 if not 1<=args.batch<=5000 or not 1<=args.scan_to<=MAX_APP_ID or not 1<=args.seconds<=21600 or args.enrich_only and args.local or not 0<=args.owners_seconds<=600 or not 0<=args.engine_seconds<=120:raise ValueError('Invalid scan bounds or ownership budget')
 tr=Transport(os.environ.get('STEAM_SYNC_URL','https://syzoz.com/game-explorer/steam-sync.php'),os.environ.get('STEAM_SYNC_TOKEN',''),args.local)
 if not args.local and len(tr.token)<32:raise ValueError('Missing Steam upload credential')
 state=initial()|tr.call('state');state.update(status='starting',heartbeat=int(time.time()),run_id=os.environ.get('GITHUB_RUN_ID','local')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1'));tr.call('begin',state=state)
 client=None;started=time.monotonic();error=None;owner_budget=0 if args.local else min(args.owners_seconds,max(0,args.seconds-1));owner_remaining=owner_budget
 try:
  if owner_budget:
   before=time.monotonic();budget=owner_budget if args.enrich_only else owner_budget/2
   summary=owner_phase(tr,state,min(started+args.seconds,before+budget),args.engine_seconds if args.enrich_only else args.engine_seconds/2);owner_remaining=max(0,owner_budget-(time.monotonic()-before));print(json.dumps(dict(phase='owners',**summary)),flush=True)
  if args.enrich_only:
   state.update(status='checkpointed',phase='Enrichment checkpoint saved',heartbeat=int(time.time()));tr.call('finish',state=state);return
  catalog_deadline=started+args.seconds-owner_remaining;client=SteamClient()
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
    apps=info.get('apps') or {};records=[r for i,a in apps.items() if (r:=normalize(i,a))];details=store_dates(records,deadline=catalog_deadline,include_details=True)
    fetch_engines(records,catalog_deadline)
    returned={int(i) for i in apps};unknown=[i for i in ids if i not in returned];unnamed=[int(i) for i,a in apps.items() if not (a.get('common') or {}).get('name')]
    if kind=='scan':state['cursor']=max(ids)+1;state['scanned_ids']+=len(ids);state['unknown_ids']+=len(unknown);state['restricted_records']+=len(unnamed)
    elif kind=='changes':state['pending_changes']=state['pending_changes'][len(ids):]
    # Unknown/restricted outcomes are audit data, never successful catalog records.
    audit=base64.b64encode(gzip.compress(json.dumps({'unknown':unknown,'unnamed':unnamed}).encode())).decode()
    state['heartbeat']=int(time.time());uploading=True;send_catalog_batch(tr,previous,state,records,details,audit)
    print(json.dumps({'phase':kind,'from':min(ids),'to':max(ids),'named':len(records),'games':sum(r['type']=='game' for r in records),'unknown':len(unknown),'unnamed':len(unnamed),'next_id':state['cursor']}),flush=True)
   except Exception as e:
    state=previous
    if uploading and not isinstance(e,BatchDataError):raise
    # Retry data failures on a later run. Smaller groups isolate bad records.
    if retry:state['failed_batches'].remove(ids)
    middle=len(ids)//2
    groups=[ids[:middle],ids[middle:]] if middle and isinstance(e,BatchDataError) else [ids]
    state['failed_batches'].extend(groups)
    state['transport_failures']+=1;state['last_error']=str(e)
    print(f'::warning::Steam {kind} batch {min(ids)}-{max(ids)} queued for retry: {type(e).__name__}: {e}',flush=True)
    if kind=='scan':state['cursor']=max(ids)+1
    elif kind=='changes':state['pending_changes']=state['pending_changes'][len(ids):]
    tr.call('checkpoint',state=state);time.sleep(3)
   time.sleep(1)
  if owner_remaining:
   summary=owner_phase(tr,state,min(started+args.seconds,time.monotonic()+owner_remaining),args.engine_seconds/2);print(json.dumps(dict(phase='owners',**summary)),flush=True)
  state.update(status='checkpointed',phase='Waiting for next run',heartbeat=int(time.time()));tr.call('finish',state=state)
 except BaseException as e:
  error=e;state.update(status='failed',last_error=type(e).__name__+': '+str(e),heartbeat=int(time.time()));
  try:tr.call('finish',state=state)
  except Exception:pass
 finally:
  if client is not None:client.logout()
 if error:raise error
if __name__=='__main__':main()
