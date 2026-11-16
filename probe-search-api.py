#!/usr/bin/env python3
"""GitHub-only, two-game JSON engine-source diagnostic; never uploads data.

HTML is used once solely to discover public search-client configuration.
Product names and technologies must come from matching JSON API records.
"""
import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request

BOOTSTRAP = 'https://steamdb.info/instantsearch/'
INDEX = 'steamdb'
ATTRIBUTES = ['objectID', 'name', 'technologies', 'lastUpdated']
MAX_BODY = 2_000_000
MAX_SECONDS = 90
SOURCE_JS = 'https://steamdb.info/static/js/instantsearch.js?v=b0745b3208743f8e'


class ProbeError(RuntimeError):
    pass


class ConfigParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.configs = []
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 50000:
            raise ProbeError('Bootstrap HTML complexity limit exceeded')
        attrs = dict(attrs)
        if 'container-search' in (attrs.get('class') or '').split():
            self.configs.append((attrs.get('data-a'), attrs.get('data-k')))


def parse_config(html):
    parser = ConfigParser()
    parser.feed(html)
    parser.close()
    if len(parser.configs) != 1:
        raise ProbeError('Exactly one public search configuration required')
    app, key = parser.configs[0]
    if not isinstance(app, str) or not re.fullmatch(r'[A-Z0-9]{5,32}', app):
        raise ProbeError('Invalid public search application ID')
    if not isinstance(key, str) or not 16 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
        raise ProbeError('Public search key unavailable')
    return app, key


def product(record, appid):
    if not isinstance(record, dict) or type(record.get('objectID')) not in (str, int) or str(record['objectID']) != str(appid):
        raise ProbeError('JSON record AppID does not match request')
    name, technologies = record.get('name'), record.get('technologies')
    if not isinstance(name, str) or not name.strip() or len(name) > 1000:
        raise ProbeError('Matching JSON product name unavailable')
    if not isinstance(technologies, list) or any(not isinstance(t, str) or len(t) > 200 for t in technologies):
        raise ProbeError('JSON technologies attribute unavailable')
    engines = list(dict.fromkeys(t[len('Engine.'):] for t in technologies
                                if t.startswith('Engine.') and re.fullmatch(r'Engine\.[A-Za-z0-9_+.-]{1,100}', t)))
    if not engines:
        raise ProbeError('No verified Engine. values; engine remains unknown')
    return {'appid': appid, 'status': 'verified', 'verified': True,
            'name': name, 'engines': engines, 'technologies': technologies,
            'source': 'steamdb_algolia', 'index': INDEX}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, folder, deadline):
        self.folder, self.deadline = folder, deadline
        self.events = []
        self.key = None
        self.config = None

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise ProbeError('Diagnostic time budget exhausted')
        return min(15, seconds)

    def request(self, url, label, payload=None, headers=None, tls=False):
        started = time.monotonic()
        event = {'operation': label, 'url': url, 'transport': 'curl_cffi' if tls else 'stdlib_https'}
        self.events.append(event)
        base = {'User-Agent': 'SyzozCatalogSearchProbe/1.0',
                'Accept': 'application/json' if payload is not None else 'text/html',
                'Accept-Encoding': 'identity'}
        base.update(headers or {})
        data = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
        if data is not None:
            base['Content-Type'] = 'application/json'
        try:
            if tls:
                from curl_cffi import requests
                # Let the selected compatibility profile supply its own UA;
                # there is no user browser state or persisted cookie jar.
                base.pop('User-Agent', None)
                with requests.Session(trust_env=False) as session:
                    response = session.get(url, headers=base, timeout=self.remaining(),
                                           impersonate='chrome', allow_redirects=False, stream=True)
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        self.remaining()
                        size += len(chunk)
                        if size > MAX_BODY:
                            raise ProbeError('Response body exceeds limit')
                        chunks.append(chunk)
                    status, raw = response.status_code, b''.join(chunks)
            else:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                request = urllib.request.Request(url, data=data, headers=base,
                                                 method='POST' if data is not None else 'GET')
                try:
                    response = opener.open(request, timeout=self.remaining())
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status, raw = response.status, response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ProbeError('Response body exceeds limit')
            event.update(http_status=status, bytes=len(raw))
            return status, raw
        except Exception as error:
            # Never serialize exceptions containing request headers or keys.
            event['error_type'] = type(error).__name__
            raise ProbeError(label + ' transport failed: ' + type(error).__name__) from None
        finally:
            event['milliseconds'] = round((time.monotonic() - started) * 1000, 1)

    def json(self, url, label, payload=None, headers=None):
        status, raw = self.request(url, label, payload, headers)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProbeError(label + ' returned non-JSON') from None
        # Public search keys stay in memory only, including on unusual API errors.
        if self.key:
            raw = raw.replace(self.key.encode(), b'[redacted public key]')
        (self.folder / (label + '.json')).write_bytes(raw)
        return status, value


def objects_result(value, appids):
    rows = value.get('results') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != len(appids):
        raise ProbeError('Unexpected Algolia objects response')
    results = []
    for appid, row in zip(appids, rows):
        try:
            results.append(product(row, appid))
        except ProbeError as error:
            results.append(unavailable(appid, str(error)))
    return results


def unavailable(appid, reason):
    return {'appid': appid, 'status': 'unavailable', 'verified': False,
            'engines': None, 'reason': reason}


def run(client, appids):
    status, html = client.request(BOOTSTRAP, 'bootstrap_standard')
    if status == 403:
        status, html = client.request(BOOTSTRAP, 'bootstrap_tls', tls=True)
    if status != 200:
        raise ProbeError('Public configuration unavailable: HTTP ' + str(status))
    lowered = html.lower()
    if any(token in lowered for token in (b'/cdn-cgi/challenge-platform/', b'cf-chl-', b'verify you are human')):
        raise ProbeError('Public configuration requires a challenge; no interaction attempted')
    app, key = parse_config(html.decode('utf-8', errors='replace'))
    client.key = key
    config = {'application_id': app, 'key_sha256': hashlib.sha256(key.encode()).hexdigest(),
              'index': INDEX, 'discovery_url': BOOTSTRAP, 'client_source': SOURCE_JS}
    client.config = config
    headers = {'X-Algolia-Application-Id': app, 'X-Algolia-API-Key': key}
    host = f'https://{app}-dsn.algolia.net'
    request = {'requests': [{'indexName': INDEX, 'objectID': str(appid),
                             'attributesToRetrieve': ATTRIBUTES} for appid in appids]}
    status, reply = client.json(host + '/1/indexes/*/objects', 'algolia_objects', request, headers)
    if status == 200:
        return config, objects_result(reply, appids)
    message = str(reply.get('message', '')) if isinstance(reply, dict) else ''
    # Respect explicit index/key denial. Only an unsupported retrieval operation
    # can fall back to the public client's normal search operation.
    if status not in (403, 405) or (status == 403 and 'method not allowed' not in message.lower()):
        raise ProbeError('Algolia retrieval denied: HTTP ' + str(status))
    payload = {'ids': [{'appid': appid} for appid in appids],
               'context': {'language': 'english', 'country_code': 'US'},
               'data_request': {'include_basic_info': True}}
    url = 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?' + urllib.parse.urlencode({'input_json': json.dumps(payload)})
    status, store = client.json(url, 'store_names')
    if status != 200:
        raise ProbeError('Steam JSON name discovery failed')
    rows = store.get('response', {}).get('store_items', []) if isinstance(store, dict) else []
    names = {row.get('appid', row.get('id')): row.get('name') for row in rows
             if isinstance(row, dict) and row.get('success') == 1}
    results = []
    for appid in appids:
        name = names.get(appid)
        if not isinstance(name, str) or not name.strip():
            results.append(unavailable(appid, 'Steam JSON name unavailable'))
            continue
        query = {'query': name, 'hitsPerPage': 10, 'attributesToRetrieve': ATTRIBUTES,
                 'attributesToHighlight': [], 'typoTolerance': False}
        status, found = client.json(host + '/1/indexes/steamdb/query', 'algolia_search_' + str(appid), query, headers)
        if status != 200:
            results.append(unavailable(appid, 'Algolia search denied: HTTP ' + str(status)))
            continue
        hits = found.get('hits', []) if isinstance(found, dict) else []
        matches = [hit for hit in hits if isinstance(hit, dict) and str(hit.get('objectID')) == str(appid)]
        try:
            if len(matches) != 1:
                raise ProbeError('Search did not return exactly one matching AppID')
            results.append(product(matches[0], appid))
        except ProbeError as error:
            results.append(unavailable(appid, str(error)))
    return config, results


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--appids', nargs='+', type=int, default=[620, 730])
    cli.add_argument('--output-dir', type=Path, default=Path('search-api-artifacts'))
    args = cli.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        cli.error('Live requests are permitted only in GitHub Actions; local tests must mock sources')
    if not 1 <= len(args.appids) <= 2 or len(set(args.appids)) != len(args.appids) or any(not 1 <= appid <= 4294967295 for appid in args.appids):
        cli.error('Provide one or two distinct uint32 AppIDs')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = Client(args.output_dir, time.monotonic() + MAX_SECONDS)
    report = {'schema': 1, 'diagnostic': 'public_search_json_engines', 'checked_at': int(time.time()),
              'html_usage': 'public client configuration only; no HTML product extraction', 'source_client': SOURCE_JS}
    def expired(*args):
        raise TimeoutError('Hard diagnostic deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, MAX_SECONDS)
    try:
        config, results = run(client, args.appids)
        report.update(config=config, results=results)
    except (ProbeError, TimeoutError) as error:
        report['results'] = [unavailable(appid, str(error)) for appid in args.appids]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    report['requests'] = client.events
    if client.config is not None:
        report['config'] = client.config
    report['all_verified'] = all(row['verified'] for row in report['results'])
    text = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    (args.output_dir / 'search-api-result.json').write_text(text, encoding='utf-8')
    print(text, end='')
    return 0 if report['all_verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
