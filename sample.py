"""GitHub-only, bounded one/two-game source collection and paused sample upload.

Never calls collect.main(), reads a fixture, or accepts manually supplied records.
"""
import argparse
from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

MAX_BODY = 2_000_000
MAX_PAYLOAD = 6_000_000
VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())


class SampleError(RuntimeError):
    pass


class HardDeadline(BaseException):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class EngineRedirects(urllib.request.HTTPRedirectHandler):
    max_repeats = 1
    max_redirections = 2

    def __init__(self, appid):
        self.appid = appid

    def redirect_request(self, request, fp, code, message, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if (url.scheme != 'https' or url.netloc != 'steamdb.info'
                or url.path not in (f'/app/{self.appid}/', f'/app/{self.appid}/info/')
                or url.query or url.fragment):
            raise SampleError('Unexpected engine-source redirect')
        return super().redirect_request(request, fp, code, message, headers, newurl)


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def timeout(self, maximum=15):
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            raise SampleError('Sample deadline exhausted')
        return min(maximum, remaining)


class Sources:
    def __init__(self, budget, timeout_factory):
        self.budget = budget
        self.timeout_factory = timeout_factory
        self.events = []
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url, label, *, engine_appid=None):
        timeout = self.budget.timeout(10 if engine_appid else 15)
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}), EngineRedirects(engine_appid))
                  if engine_appid else self.opener)
        event = {'source': label, 'url': url}
        self.events.append(event)
        try:
            with self.timeout_factory(timeout, SampleError(label + ' timed out')):
                request = urllib.request.Request(url, headers={'Accept': 'text/html' if engine_appid else 'application/json',
                    'Accept-Encoding': 'identity', 'User-Agent': 'SyzozCatalogBoundedSample/1.0'})
                try:
                    response = opener.open(request, timeout=timeout)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    raw = response.read(MAX_BODY + 1)
                    status = response.status
                    headers = dict(response.headers.items())
                    final_url = response.geturl()
            event.update(http_status=status, bytes=len(raw))
            if len(raw) > MAX_BODY:
                raise SampleError(label + ' response exceeds limit')
            if status != 200 and not engine_appid:
                raise SampleError(label + ' returned HTTP ' + str(status))
            return raw, status, headers, final_url
        except (OSError, ValueError) as error:
            event['unavailable'] = True
            raise SampleError(label + ' unavailable') from None

    def json(self, url, label):
        raw, _, _, _ = self.get(url, label)
        try:
            value = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise SampleError(label + ' did not return JSON') from None
        if not isinstance(value, dict):
            raise SampleError(label + ' has invalid response shape')
        return value


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return ' '.join(part.text() if isinstance(part, Node) else part for part in self.children)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 50000 or len(self.stack) > 128:
            raise ValueError('HTML complexity limit exceeded')
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip()


def parse_engines(html, appid, page_url):
    """Return verified engines from a matching app's actual Technologies row.

    The public app client reads .scope-app[data-appid]. If unavailable, the
    table's App ID row is mandatory. Any conflicting identity rejects the page.
    Engine labels are anchor text, never mapped from an AppID or game name.
    """
    parser = Document()
    parser.feed(html)
    parser.close()
    nodes = list(parser.root.walk())
    scopes = [n.attrs.get('data-appid') for n in nodes
              if 'scope-app' in (n.attrs.get('class') or '').split()]
    app_rows = []
    technology_rows = []
    for row in (n for n in nodes if n.tag == 'tr'):
        cells = [n for n in row.children if isinstance(n, Node) and n.tag in ('td', 'th')]
        if len(cells) < 2:
            continue
        label = normalized(cells[0].text()).rstrip(':').casefold()
        if label == 'app id':
            app_rows.append(normalized(cells[1].text()))
        elif label == 'technologies':
            technology_rows.append(cells[1])
    expected = str(appid)
    if scopes:
        if any(value != expected for value in scopes):
            raise ValueError('Page AppID scope does not match requested AppID')
    elif not app_rows:
        raise ValueError('Page AppID could not be verified')
    if any(value != expected for value in app_rows):
        raise ValueError('Page App ID row does not match requested AppID')
    if not technology_rows:
        raise ValueError('Technologies row not present; engine unavailable')
    engines = []
    seen = set()
    for cell in technology_rows:
        for anchor in (n for n in cell.walk() if n.tag == 'a'):
            target = urllib.parse.urljoin(page_url, anchor.attrs.get('href') or '')
            url = urllib.parse.urlsplit(target)
            segments = url.path.strip('/').split('/')
            if (url.scheme != 'https' or url.netloc != 'steamdb.info'
                    or len(segments) != 3 or segments[:2] != ['tech', 'Engine']
                    or not segments[2] or url.query or url.fragment):
                continue
            label = normalized(anchor.text())
            slug = urllib.parse.unquote(segments[2])
            if not label or len(label) > 120 or '/' in slug or len(slug) > 100:
                continue
            if target not in seen:
                engines.append({'label': label, 'slug': slug, 'url': target})
                seen.add(target)
    if not engines:
        raise ValueError('No verifiable engine anchors in Technologies row')
    return engines


def is_challenge(body, headers):
    lowered = body.lower()
    return (headers.get('cf-mitigated', '').lower() == 'challenge'
            or any(token in lowered for token in (
                '/cdn-cgi/challenge-platform/', 'cf-chl-', 'challenge-form',
                '<title>just a moment', 'verify you are human')))



class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('br', 'p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def plaintext(value):
    if not isinstance(value, str):
        return ''
    parser = PlainText()
    parser.feed(value)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(parser.parts)).strip()


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


def store_detail(appid, response):
    item = response.get(str(appid)) if isinstance(response, dict) else None
    data = item.get('data') if isinstance(item, dict) and item.get('success') is True else None
    if (not isinstance(data, dict) or type(data.get('steam_appid')) is not int
            or data['steam_appid'] != appid or data.get('type') != 'game'
            or type(data.get('is_free')) is not bool or not isinstance(data.get('name'), str)
            or not data['name'].strip()):
        raise SampleError('Official Store game identity or free flag is unavailable')
    return data


def detail_document(record, store, checked_at):
    def labels(name):
        return [entry['description'] for entry in store.get(name, [])
                if isinstance(entry, dict) and isinstance(entry.get('description'), str)]
    return {'schema': 1, 'appid': record['appid'], 'is_free': store['is_free'],
        'short_description': store.get('short_description'),
        'description': plaintext(store.get('detailed_description') or store.get('about_the_game') or store.get('short_description')),
        'genres': labels('genres'), 'categories': labels('categories'),
        'screenshots': [entry['path_full'] for entry in store.get('screenshots', [])
                        if isinstance(entry, dict) and isinstance(entry.get('path_full'), str)],
        'trailers': store.get('movies', []), 'release_label': store.get('release_date', {}).get('date'),
        'release_ts': record.get('release_ts'), 'original_release_ts': record.get('original_release_ts'),
        'checked_at': checked_at, 'platforms': store.get('platforms', {}),
        'publishers': store.get('publishers', []), 'developers': store.get('developers', []),
        'country': 'US', 'store_url': f"https://store.steampowered.com/app/{record['appid']}/",
        'source': 'Steam Store', 'header_image': store.get('header_image'),
        'reviews': {'review_count': record.get('rating_count'),
                    'percent_positive': record['rating'] * 20 if record.get('rating') is not None else None,
                    'review_score': None, 'review_score_label': None}}


def enrich_fresh_store(worker, records, browse, official, deadline):
    """Reuse worker helpers with fresh same-run responses, never fixture input."""
    def cached(url, timeout):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https':
            raise SampleError('Unexpected worker source URL')
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.netloc == 'api.steampowered.com' and parsed.path == '/IStoreBrowseService/GetItems/v1/':
            return io.BytesIO(json.dumps(browse).encode())
        if parsed.netloc == 'store.steampowered.com' and parsed.path == '/api/appdetails':
            ids = query.get('appids', [])
            if len(ids) == 1 and ids[0].isdigit() and int(ids[0]) in official:
                return io.BytesIO(json.dumps(official[int(ids[0])]).encode())
        raise SampleError('Worker requested a source outside this fresh sample')
    with patch.object(worker.urllib.request, 'urlopen', cached):
        worker.store_dates(records, deadline=deadline)


def browser_source(sources, url, endpoint):
    # A browser is never started here. This endpoint must be explicitly enabled
    # by the approved workflow; no cookies, headers, or raw HTML enter logs.
    if endpoint != 'http://127.0.0.1:8191/v1':
        raise SampleError('Browser endpoint must be the approved loopback service')
    timeout = sources.budget.timeout(60)
    body = json.dumps({'cmd': 'request.get', 'url': url, 'maxTimeout': int(timeout * 1000)}).encode()
    sources.events.append({'source': 'steamdb_browser', 'url': url, 'endpoint': endpoint})
    try:
        request = urllib.request.Request(endpoint, body, {'Content-Type': 'application/json'})
        with sources.timeout_factory(timeout, SampleError('Engine browser request timed out')):
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise SampleError('Browser response exceeds limit')
        result = json.loads(raw)
        solution = result.get('solution') if isinstance(result, dict) and result.get('status') == 'ok' else None
        if not isinstance(solution, dict) or not isinstance(solution.get('response'), str):
            raise SampleError('Browser did not return a successful source response')
        if len(solution['response'].encode('utf-8')) > MAX_BODY:
            raise SampleError('Browser HTML exceeds limit')
        return solution
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        sources.events[-1]['http_status'] = code
        raise SampleError('Browser service returned HTTP ' + str(code)) from None
    except (OSError, ValueError):
        raise SampleError('Browser source unavailable') from None


def verified_engine_labels(body, status, headers, final_url, appid, *, require_html_header=True):
    if type(status) is not int or status != 200 or is_challenge(body, headers):
        raise SampleError('HTTP or challenge response; no engine verified')
    content_type = headers.get('content-type', '').lower()
    if (require_html_header and not content_type) or (content_type and 'text/html' not in content_type):
        raise SampleError('Engine source did not return HTML')
    parsed = urllib.parse.urlsplit(final_url)
    if (parsed.scheme != 'https' or parsed.netloc != 'steamdb.info'
            or parsed.path not in (f'/app/{appid}/', f'/app/{appid}/info/')
            or parsed.query or parsed.fragment):
        raise SampleError('Engine source URL identity did not match')
    engines = parse_engines(body, appid, final_url)
    if (not 1 <= len(engines) <= 20
            or any(len(engine['label']) > 80 or any(unicodedata.category(c).startswith('C') for c in engine['label'])
                   for engine in engines)):
        raise SampleError('Engine labels do not fit the shared slot')
    return engines


def engine_result(sources, appid, github):
    url = f'https://steamdb.info/app/{appid}/info/'
    result = {'appid': appid, 'url': url, 'status': 'unavailable', 'verified': False,
              'engines': None, 'github_run_id': github['run_id']}
    try:
        raw, status, headers, final_url = sources.get(url, 'steamdb_engine', engine_appid=appid)
        result.update(http_status=status, bytes=len(raw))
        body = raw.decode('utf-8', errors='replace')
        normalized_headers = {name.lower(): value for name, value in headers.items()}
        result['challenge'] = is_challenge(body, normalized_headers)
        engines = verified_engine_labels(body, status, normalized_headers, final_url, appid)
        result.update(status='verified', verified=True, engines=engines, verification_source='public_http')
        return result
    except (SampleError, ValueError, OSError):
        result['reason'] = 'Public engine source unavailable or identity not verified'
    endpoint = os.environ.get('STEAMDB_BROWSER_URL', '')
    if endpoint:
        result['browser_attempted'] = True
        try:
            solution = browser_source(sources, url, endpoint)
            headers = solution.get('headers') or {}
            if not isinstance(headers, dict):
                raise SampleError('Browser source headers are malformed')
            headers = {str(name).lower(): str(value) for name, value in headers.items()}
            result['browser_http_status'] = solution.get('status')
            engines = verified_engine_labels(solution['response'], solution.get('status'), headers,
                                              solution.get('url', ''), appid, require_html_header=False)
            result.update(status='verified', verified=True, engines=engines, verification_source='browser_service')
            result.pop('reason', None)
        except (SampleError, ValueError, OSError):
            result['browser_reason'] = 'Browser source unavailable or identity not verified'
    return result


def genre_labels(store):
    genres = store.get('genres')
    if not isinstance(genres, list):
        return []
    labels = []
    seen = set()
    for entry in genres:
        name = entry.get('description') if isinstance(entry, dict) else None
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(unicodedata.category(c).startswith('C') for c in name)):
            continue
        name = name.strip()
        if name.casefold() not in seen:
            labels.append(name)
            seen.add(name.casefold())
        if len(labels) == 20:
            break
    return labels


def build_sample(worker, sources, appids, github):
    validate_appids(appids)
    pics = pics_data(worker, sources, appids)
    records = []
    for appid in appids:
        record = worker.normalize(appid, pics[appid])
        if not isinstance(record, dict) or record.get('type') != 'game' or not record.get('name'):
            raise SampleError('PICS did not supply the requested named game')
        records.append(record)
    request = {'ids': [{'appid': appid} for appid in appids],
        'context': {'language': 'english', 'country_code': 'US'},
        'data_request': {'include_release': True, 'include_basic_info': True, 'include_assets': True}}
    browse = sources.json('https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?' + urllib.parse.urlencode({'input_json': json.dumps(request)}), 'storebrowse')
    items = browse.get('response', {}).get('store_items')
    if not isinstance(items, list):
        raise SampleError('StoreBrowse did not return game records')
    returned = {item.get('appid', item.get('id')): item for item in items if isinstance(item, dict)}
    if any(appid not in returned or returned[appid].get('success') != 1 for appid in appids):
        raise SampleError('StoreBrowse omitted a requested game')
    official = {}
    store = {}
    for appid in appids:
        response = sources.json('https://store.steampowered.com/api/appdetails?' + urllib.parse.urlencode({'appids': appid, 'cc': 'us', 'l': 'english'}), 'store_appdetails')
        store[appid] = store_detail(appid, response)
        official[appid] = response
    enrich_fresh_store(worker, records, browse, official, sources.budget.deadline)
    details = []
    histories = []
    engines = []
    owners = []
    reviews = []
    releases = []
    categories = []
    next_request = 0.0
    for record in records:
        appid = record['appid']
        wait = max(0, next_request - time.monotonic())
        if wait:
            sources.budget.timeout(wait + 1)
            time.sleep(wait)
        sources.budget.timeout(8)
        next_request = time.monotonic() + 1
        checked_at = int(time.time())
        owner_url = 'https://steamspy.com/api.php?' + urllib.parse.urlencode({'request': 'appdetails', 'appid': appid})
        try:
            response = sources.json(owner_url, 'steamspy')
            checked_at = int(time.time())
            record.update(worker.owner_fields(appid, response, checked_at))
            owners.append({'appid': appid, 'url': owner_url, 'status': 'verified', 'github_run_id': github['run_id']})
        except (SampleError, ValueError, TypeError):
            owners.append({'appid': appid, 'url': owner_url, 'status': 'unavailable',
                           'reason': 'No matching verified owner range; no observations invented', 'github_run_id': github['run_id']})
        if not record.get('tags'):
            labels = genre_labels(store[appid])
            if labels:
                record.update(category=labels[0], tags=labels)
                categories.append({'appid': appid, 'source': 'steam_store_genres', 'labels': labels})
        else:
            categories.append({'appid': appid, 'source': 'steamspy_tags', 'labels': record['tags']})
        if record.get('rating') is None:
            review_url = f'https://store.steampowered.com/appreviews/{appid}?' + urllib.parse.urlencode(
                {'json': 1, 'language': 'all', 'purchase_type': 'all', 'num_per_page': 1, 'filter': 'all'})
            diagnostic = {'appid': appid, 'url': review_url, 'status': 'unavailable'}
            try:
                response = sources.json(review_url, 'steam_reviews')
                summary = response.get('query_summary') if isinstance(response.get('query_summary'), dict) else {}
                diagnostic['query_summary'] = {name: summary[name] for name in
                    ('total_positive', 'total_negative', 'total_reviews', 'review_score', 'review_score_desc', 'num_reviews')
                    if name in summary and (type(summary[name]) is int or isinstance(summary[name], str))}
                fields = worker.review_fields(appid, response, checked_at)
                record.update(fields)
                diagnostic['status'] = 'verified' if fields else 'no_reviews'
            except (SampleError, ValueError, TypeError):
                diagnostic['reason'] = 'No valid nonzero global review summary; rating remains unknown'
            reviews.append(diagnostic)
        common = pics[appid].get('common') or {}
        releases.append({'appid': appid, 'pics_release_ts': worker.timestamp(common.get('steam_release_date')),
                         'release_ts': record.get('release_ts'),
                         'official_release_label': store[appid].get('release_date', {}).get('date')})
        if type(record.get('is_free')) is not bool or record['is_free'] != store[appid]['is_free']:
            raise SampleError('Store sources disagree or free status is unavailable')
        engine = engine_result(sources, appid, github)
        engines.append(engine)
        record['architectures'] = [value['label'] for value in engine['engines']] if engine['verified'] else None
        details.append(detail_document(record, store[appid], checked_at))
        points = ([{'observed_at': checked_at, 'total_installs': record['installs'], 'period': 1}]
                  if record.get('installs') is not None else [])
        histories.append({'package': f'steam:{appid}', 'revision': checked_at, 'points': points})
    payload = {'action': 'sample', 'records': records, 'details': details,
        'install_history': {'schema': 1, 'mode': 'replace', 'games': histories}, 'github': github}
    return payload, {'github': github, 'engine_diagnostics': engines, 'owner_diagnostics': owners,
                     'review_diagnostics': reviews, 'release_diagnostics': releases,
                     'category_diagnostics': categories, 'sources': sources.events}


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


if __name__ == '__main__':
    raise SystemExit(main())
