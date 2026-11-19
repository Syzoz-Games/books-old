"""Manual APKPure sync: FlareSolverr sitemaps and direct JSON metadata API."""
import argparse
import base64
import gzip
import hashlib
import html
import http.client
import hmac
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'
INDEX = 'https://apkpure.net/sitemap.xml'
GAME = re.compile(r'https://apkpure\.net/apkpure-net-sitemaps/game_[a-z_]+(?:-\d+)?\.xml\Z')
PACKAGE = re.compile(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+\Z')
MIRROR = 'https://syzoz.com/chart/mirror.php'
METADATA = 'https://syzoz.com/chart/metadata-sync.php'
DETAILS = 'https://tapi.pureapk.com/v3/batch_get_app_detail'
MAX_UPLOAD = 5_500_000

class SyncError(RuntimeError):
    def __init__(self, reason, source, status=None, challenge=False):
        super().__init__(reason)
        self.source, self.status, self.challenge = source, status, challenge

class WebsiteUnavailable(SyncError):
    """Temporary website transport or service failure; saved work remains pending."""

class WebsiteChallenge(WebsiteUnavailable):
    """The host still returned a browser challenge after the retry budget."""

def is_website_challenge(raw):
    # Inspect the response we already received; a separate preflight request
    # cannot guarantee that the following authenticated request will succeed.
    return bool(re.search(br'<title[^>]*>\s*One moment,\s*please\s*\.{0,3}\s*</title>', raw, re.I))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')

def catalog_credential(secret):
    if not isinstance(secret, str) or len(secret) < 32 or '\r' in secret or '\n' in secret:
        raise SyncError('STEAM_SYNC_TOKEN is missing or invalid', 'configuration')
    return hmac.new(secret.encode('utf-8'), b'syzoz-admin-mobile-v1', hashlib.sha256).hexdigest()

class Http:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds
        self.local = threading.local()

    def request(self, url, source, payload=None, credential=None, headers=None, maximum=8_000_000, reserve=0, timeout=45):
        remaining = self.deadline - time.monotonic()
        if remaining <= reserve + 1:
            raise SyncError('Time budget reached; saved checkpoints are retained', source)
        body = None if payload is None else payload if isinstance(payload, bytes) else compact(payload)
        if body is not None and source == 'website' and len(body) > MAX_UPLOAD:
            raise SyncError('Upload exceeds the bounded website payload limit', source)
        request_headers = {'User-Agent': 'SyzozCatalogAdmin/1.0 (+https://syzoz.com/chart/)', 'Accept-Encoding': 'identity'}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        if credential:
            request_headers['X-Catalog-Token'] = credential
        request_headers.update(headers or {})
        request = urllib.request.Request(url, body, request_headers)
        try:
            try:
                if not hasattr(self.local, 'opener'):
                    self.local.opener = urllib.request.build_opener(NoRedirect())
                response = self.local.opener.open(request, timeout=min(timeout, remaining - reserve))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status
                kind = response.headers.get('Content-Type', '')
                challenge = response.headers.get('cf-mitigated', '') == 'challenge'
                raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise SyncError('Response exceeds the bounded size limit', source, status)
            if status != 200:
                reason = f'{source} returned HTTP {status}'
                if source == 'website' and 'json' in kind.lower():
                    try:
                        message = json.loads(raw).get('error')
                        if isinstance(message, str):
                            reason += ': ' + re.sub(r'[\x00-\x1f\x7f]', ' ', message)[:300]
                    except (ValueError, AttributeError):
                        pass
                error_type = WebsiteUnavailable if source == 'website' and (status in (408, 425, 429) or status >= 500) else SyncError
                raise error_type(reason, source, status, challenge)
            return raw
        except SyncError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            cause = getattr(error, 'reason', error)
            detail = type(cause).__name__
            if isinstance(getattr(cause, 'errno', None), int):
                detail += f' errno={cause.errno}'
            error_type = WebsiteUnavailable if source == 'website' else SyncError
            raise error_type('HTTP transport failed: ' + detail, source) from None

    def website(self, url, payload, credential, metadata=False, timeout=45, attempts=3, retry_seconds=None):
        raw_request = compact(payload)
        if retry_seconds is None:
            retry_seconds = getattr(self, 'website_retry_seconds', max(120, timeout))
        retry_until = min(self.deadline, time.monotonic() + retry_seconds)
        attempt = 0
        self.local.challenge_retries = 0
        while True:
            try:
                remaining = retry_until - time.monotonic()
                if remaining <= 0:
                    raise WebsiteUnavailable('Website request retry budget reached', 'website')
                raw = self.request(url, 'website', raw_request, credential, maximum=1_000_000, timeout=min(timeout, remaining))
                try:
                    result = json.loads(raw)
                except ValueError:
                    if is_website_challenge(raw):
                        raise WebsiteChallenge('Host returned the One moment, please browser challenge', 'website', challenge=True) from None
                    raise WebsiteUnavailable('Website returned invalid JSON', 'website') from None
                if not isinstance(result, dict) or result.get('error'):
                    raise SyncError('Website rejected this checkpoint', 'website')
                if metadata and (result.get('claim_id') != payload['claim_id'] or result.get('run_id') != payload['run_id'] or result.get('payload_hash') != hashlib.sha256(raw_request).hexdigest()):
                    raise SyncError('Metadata receipt identity or checksum mismatch', 'website')
                return result
            except SyncError as error:
                if error.challenge:
                    remaining = retry_until - time.monotonic()
                    if remaining <= 3:
                        raise WebsiteChallenge('Host browser challenge persisted for the request budget; checkpoint retained', 'website', error.status, True) from None
                    self.local.challenge_retries += 1
                    print(f'Host browser challenge: waiting 3 seconds before retry {self.local.challenge_retries}', flush=True)
                    time.sleep(3)
                    continue
                if (error.status is not None and error.status < 500 and error.status not in (408, 425, 429)) or attempt >= attempts - 1:
                    raise
                if retry_until - time.monotonic() <= attempt + 2:
                    raise
                time.sleep(attempt + 1)
                attempt += 1

def finish_mirror(http, token, run_id):
    """Resume short, durable catalog batches until the server confirms completion."""
    payload = {'action': 'finish', 'run_id': run_id}
    while http.deadline - time.monotonic() > 10:
        try:
            state = http.website(MIRROR, payload, token, timeout=45, attempts=1)
        except SyncError as error:
            if error.status is not None and error.status < 500 and error.status != 429:
                raise
            print(f'::warning::Finalization request interrupted: {error}; resuming saved batches.', flush=True)
            time.sleep(1)
            continue
        if state.get('complete') is True:
            return state
        if state.get('finalizing') is not True:
            raise SyncError('Invalid finalization progress response', 'website')
        print(json.dumps({'finalization': state.get('phase'), 'processed': state.get('processed'), 'changed': state.get('changed'), 'busy': bool(state.get('busy'))}), flush=True)
        if state.get('busy'):
            time.sleep(min(3, max(1, float(state.get('retry_after', 1)))))
    raise SyncError('Finalization time budget reached; saved batches will resume on retry', 'website')

def xml_document(raw, root_name):
    if not isinstance(raw, bytes) or re.search(br'<!DOCTYPE|<!ENTITY', raw, re.I):
        raise SyncError('Sitemap contains unsupported markup', 'apkpure.net')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SyncError('APKPure did not return valid sitemap XML', 'apkpure.net') from None
    if root.tag != f'{{{NS}}}{root_name}':
        raise SyncError('Unexpected sitemap root or namespace', 'apkpure.net')
    tag = 'sitemap' if root_name == 'sitemapindex' else 'url'
    rows = root.findall(f'{{{NS}}}{tag}')
    if not rows or any(not row.findtext(f'{{{NS}}}loc', '').startswith('https://apkpure.net/') for row in rows):
        raise SyncError('Sitemap is empty or contains an unexpected host', 'apkpure.net')
    return root

class SitemapBrowser:
    """One local browser session, restricted to APKPure's published game maps."""
    ENDPOINT = 'http://127.0.0.1:8191/v1'

    def __init__(self, http):
        self.http, self.session = http, None

    def command(self, payload):
        raw = self.http.request(self.ENDPOINT, 'flaresolverr', payload, maximum=32_000_000)
        try:
            result = json.loads(raw)
        except ValueError:
            raise SyncError('FlareSolverr returned invalid JSON', 'flaresolverr') from None
        if not isinstance(result, dict) or result.get('status') != 'ok':
            raise SyncError('FlareSolverr could not complete the sitemap request', 'flaresolverr')
        return result

    def fetch(self, url, root_name):
        if url != INDEX and not GAME.fullmatch(url):
            raise SyncError('Browser request is outside the APKPure sitemap allowlist', 'configuration')
        if not self.session:
            self.session = self.command({'cmd': 'sessions.create'}).get('session')
            if not isinstance(self.session, str) or not self.session:
                raise SyncError('FlareSolverr did not create a session', 'flaresolverr')
        remaining = self.http.deadline - time.monotonic()
        if remaining <= 12:
            raise SyncError('Time budget reached; saved checkpoints are retained', 'apkpure.net')
        result = self.command({'cmd': 'request.get', 'url': url, 'session': self.session,
                               'maxTimeout': int(min(35, remaining - 10) * 1000)})
        solution = result.get('solution') or {}
        if not isinstance(solution, dict) or solution.get('status') != 200:
            raise SyncError('Browser sitemap response was not HTTP 200', 'apkpure.net',
                            solution.get('status') if isinstance(solution, dict) else None)
        body = solution.get('response')
        if not isinstance(body, str):
            raise SyncError(f'Browser sitemap response is missing (type {type(body).__name__})', 'apkpure.net')
        body_bytes = len(body.encode('utf-8'))
        print(json.dumps({'phase': 'sitemap_response', 'document': root_name, 'response_bytes': body_bytes}), flush=True)
        if body_bytes > 24_000_000:
            raise SyncError(f'Browser sitemap response exceeds 24000000 bytes (received {body_bytes})', 'apkpure.net')
        # Chromium wraps rendered XML in HTML; retain only the strict XML document.
        pattern = rf'<(?:[\w.-]+:)?{root_name}\b.*?</(?:[\w.-]+:)?{root_name}\s*>'
        for candidate in (body, html.unescape(body)):
            match = re.search(pattern, candidate, re.S)
            if match:
                raw = match.group().encode('utf-8')
                if len(raw) > 8_000_000:
                    raise SyncError('Sitemap exceeds the XML size limit', 'apkpure.net')
                return xml_document(raw, root_name)
        raise SyncError('Browser did not return sitemap XML', 'apkpure.net')

    def close(self):
        if not self.session:
            return
        # Cleanup has its own short timeout, even when the sync budget has expired.
        try:
            request = urllib.request.Request(self.ENDPOINT, compact({'cmd': 'sessions.destroy', 'session': self.session}),
                                             {'Content-Type': 'application/json'})
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
                response.read(100_000)
        except (OSError, ValueError):
            pass
        finally:
            self.session = None

def sync_sitemaps(http, credential, run, metadata_batches=0):
    begun = False
    browser = SitemapBrowser(http)
    metadata = {'batches': 0, 'stored': 0, 'dated': 0}
    def index_metadata(checkpoint):
        if not metadata_batches:
            return
        # Each round has a distinct claim namespace, including empty claims.
        result = sync_metadata(http, credential, f'{run}-{checkpoint}', metadata_batches)
        for field in metadata:
            metadata[field] += result[field]
        print(json.dumps({'phase': 'metadata_checkpoint', 'checkpoint': checkpoint, **metadata}), flush=True)
    def upload(action, **fields):
        return http.website(MIRROR, {'action': action, 'run_id': run, **fields}, credential)
    try:
        root = browser.fetch(INDEX, 'sitemapindex')
        manifest = {}
        for row in root.findall(f'{{{NS}}}sitemap'):
            url = row.findtext(f'{{{NS}}}loc', '')
            if not GAME.fullmatch(url):
                continue
            modified = row.findtext(f'{{{NS}}}lastmod', '')
            if len(modified) > 80:
                raise SyncError('Sitemap modification marker exceeds the receiver limit', 'apkpure.net')
            manifest[url] = max(modified, manifest.get(url, ''))
        if not manifest or len(manifest) > 20000:
            raise SyncError('Index does not contain a supported game manifest', 'apkpure.net')
        result = upload('begin', manifest=manifest)
        begun = True
        pending = result.get('pending')
        if pending == []:  # PHP encodes an empty associative array as [].
            pending = {}
        if not isinstance(pending, dict) or any(url not in manifest or modified != manifest[url] for url, modified in pending.items()):
            raise SyncError('Website returned invalid pending sitemap checkpoints', 'website')
        print(json.dumps({'phase': 'sitemaps', 'manifest_files': len(manifest), 'pending_files': len(pending), 'unchanged_files': len(manifest) - len(pending)}), flush=True)
        # Resume saved metadata immediately, then enrich after every saved map.
        index_metadata('backlog')
        for number, url in enumerate(pending, 1):
            root = browser.fetch(url, 'urlset')
            ET.register_namespace('', NS)
            body = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            if len(body) > 8_000_000:
                raise SyncError('Sitemap exceeds the receiver XML limit', 'apkpure.net')
            zipped = gzip.compress(body, mtime=0)
            if len(zipped) > 4_000_000:
                raise SyncError('Sitemap exceeds the receiver compressed limit', 'apkpure.net')
            saved = upload('file', url=url, sha256=hashlib.sha256(body).hexdigest(), gzip=base64.b64encode(zipped).decode('ascii'))
            print(json.dumps({'phase': 'sitemaps', 'file': url.rsplit('/', 1)[-1], 'saved_this_run': number, 'pending_at_start': len(pending), 'entries': saved.get('entries'), 'files_done': saved.get('files_done')}), flush=True)
            index_metadata(f'file-{number}')
        result = finish_mirror(http, credential, run)
        if result.get('complete') is not True:
            raise SyncError('Website did not confirm the complete sitemap checkpoint', 'website')
        return {'status': 'complete', 'manifest_files': len(manifest), 'downloaded_files': len(pending), 'metadata': metadata}
    except Exception:
        if begun:
            try:
                upload('fail')
            except SyncError:
                pass
        raise
    finally:
        browser.close()

def metadata_version(value):
    if type(value) is int or type(value) is float and float('-inf') < value < float('inf'):
        value = str(value)
    if not isinstance(value, str):
        return None
    try:
        value.encode('utf-8')
    except UnicodeError:
        return None
    return value[:200]

def metadata_detail(detail, package):
    if detail is None:
        return None
    if not isinstance(detail, dict) or detail.get('package_name', package) != package:
        raise SyncError('Metadata response does not match its requested package', 'tapi.pureapk.com')
    # Send exactly the existing record-date index fields; no unrelated binary/download blobs.
    fields = ('package_name', 'create_date', 'update_date', 'title', 'developer', 'version_name', 'description_short', 'icon_url')
    out = {key: detail[key] for key in fields if key in detail}
    if 'version_name' in out:
        out['version_name'] = metadata_version(out['version_name'])
    if isinstance(detail.get('asset'), dict) and 'size' in detail['asset']:
        out['asset'] = {'size': detail['asset']['size']}
    if isinstance(detail.get('icon'), dict) and isinstance(detail['icon'].get('thumbnail'), dict) and 'url' in detail['icon']['thumbnail']:
        out['icon'] = {'thumbnail': {'url': detail['icon']['thumbnail']['url']}}
    return out

def download_metadata(http, package):
    raw = http.request(DETAILS, 'tapi.pureapk.com', {'package_names': [package]}, headers={'Ual-Access-Businessid': 'projecta', 'Ual-Access-ProjectA': '{"device_info":{"os_ver":"33"}}'}, reserve=10)
    try:
        response = json.loads(raw)
    except ValueError:
        raise SyncError('Metadata API returned invalid JSON', 'tapi.pureapk.com') from None
    if not isinstance(response, dict) or type(response.get('retcode')) is not int or response['retcode'] != 0 or not isinstance(response.get('app_detail_map'), dict):
        code = response.get('retcode') if isinstance(response, dict) else None
        suffix = f' (retcode {code})' if type(code) is int else ''
        raise SyncError('Metadata API rejected an ordinary single-app request' + suffix, 'tapi.pureapk.com')
    return metadata_detail(response['app_detail_map'].get(package), package)

def parse_play_metrics(body, packages, checked_at):
    """Only matching RPC identities and explicit not-found results are completed."""
    results = {}
    for line in body.splitlines():
        if not line.startswith(b'[['):
            continue
        for row in json.loads(line):
            if not isinstance(row, list) or len(row) < 7 or row[:2] != ['wrb.fr', 'Ws7gDc'] or row[6] not in packages:
                continue
            package = row[6]
            if package in results:
                raise ValueError('Duplicate Google Play response')
            if row[2] is None and row[5] == [5]:
                results[package] = {'play_status': 'unavailable', 'metrics_checked_at': checked_at}
                continue
            if not isinstance(row[2], str):
                continue
            try:
                data = json.loads(row[2])[1]
                if data[3]['12'][0][0] != package:
                    continue
                values = data[2]
                def field(number):
                    entry = values[number - 1] if len(values) >= number else None
                    tail = values[-1] if values else None
                    return entry if entry is not None else tail.get(str(number)) if isinstance(tail, dict) else None
                release, installs, scores = field(11), field(14), field(52)
                metrics = {'play_status': 'no_date', 'metrics_checked_at': checked_at}
                if release is not None:
                    stamp = release[1][0]
                    if type(stamp) is not int or not 1 <= stamp <= 253402300799:
                        continue
                    metrics.update(release_ts=stamp, play_status='ok')
                if isinstance(installs, list) and len(installs) >= 3 and type(installs[2]) is int and type(installs[1]) is int and 0 <= installs[1] <= installs[2] <= 9007199254740991:
                    metrics['installs'] = installs[2]
                if scores and scores[0] and len(scores[0]) > 1:
                    rating = scores[0][1]
                    if type(rating) in (int, float) and 0 < rating <= 5:
                        metrics['rating'] = rating
                results[package] = metrics
            except (ValueError, TypeError, IndexError, KeyError, AttributeError):
                continue
    return results


def fetch_play_metrics(http, packages):
    from urllib.parse import urlencode
    if not packages:
        return {}
    calls = [['Ws7gDc', json.dumps([None, None, [[11, 14, 52]], None, None, [[p, 7]]]), None, p] for p in packages]
    url = 'https://play.google.com/_/PlayStoreUi/data/batchexecute?rpcids=Ws7gDc&hl=en&gl=US&rt=c'
    payload = urlencode({'f.req': json.dumps([calls])}).encode()
    raw = http.request(url, 'play.google.com', payload, headers={'Content-Type': 'application/x-www-form-urlencoded'}, maximum=2_000_000, reserve=10, timeout=20)
    try:
        return parse_play_metrics(raw, packages, int(time.time()))
    except (ValueError, TypeError):
        raise SyncError('Google Play returned an invalid batch', 'play.google.com') from None


def attach_play_metrics(http, details):
    packages = [p for p, detail in details.items() if isinstance(detail, dict)]
    try:
        results = fetch_play_metrics(http, packages)
    except SyncError:
        results = {}
    for package, metrics in results.items():
        details[package].update(metrics)
    print(json.dumps({'phase': 'play_metrics', 'requested': len(packages), 'received': len(results), 'release_dates': sum(r.get('release_ts') is not None for r in results.values())}), flush=True)


def release_dates(http, credential, run, batches=None):
    number = stored = dated = 0
    while http.deadline - time.monotonic() > 40 and (batches is None or number < batches):
        base = {'scope': 'release_dates', 'run_id': run, 'claim_id': f'{run}-dates-{number}'}
        claim = http.website(METADATA, {'action': 'claim', **base, 'limit': 50}, credential, metadata=True)
        packages = claim.get('packages')
        if not isinstance(packages, list) or len(packages) > 50 or len(set(packages)) != len(packages) or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in packages):
            raise SyncError('Invalid release date claim', 'website')
        if not packages:
            return {'idle': True, 'batches': number, 'stored': stored, 'dated': dated, 'remaining': claim.get('remaining')}
        try:
            results = fetch_play_metrics(http, packages)
        except SyncError as error:
            # Persist the failure as retryable; do not count a transport error as unavailable.
            http.website(METADATA, {'action': 'submit', **base, 'results': {}, 'failed_packages': packages}, credential, metadata=True)
            print(json.dumps({'phase': 'release_dates', 'checkpointed': True, 'retry_pending': len(packages), 'upstream_status': error.status}), flush=True)
            return {'checkpointed': True, 'retry_pending': len(packages), 'batches': number}
        reply = http.website(METADATA, {'action': 'submit', **base, 'results': results, 'failed_packages': [p for p in packages if p not in results]}, credential, metadata=True)
        number += 1; stored += reply['stored']; dated += reply['dated']
        print(json.dumps({'phase': 'release_dates', 'batches': number, **reply}), flush=True)
        time.sleep(.5)
    return {'batches': number, 'stored': stored, 'dated': dated, 'checkpointed': True}


def sync_metadata(http, credential, run, batches):
    try:
        return sync_metadata_batches(http, credential, run, batches)
    except WebsiteUnavailable as error:
        print(f'::warning::Metadata server unavailable: {error}. Pending work is retained for a later scan.', flush=True)
        return {'status': 'deferred', 'batches': 0, 'stored': 0, 'dated': 0, 'deferred': True}


def sync_metadata_batches(http, credential, run, batches):
    stored = dated = processed = 0
    for number in range(batches):
        claim = f'{run}-metadata-{number}'
        base = {'run_id': run, 'claim_id': claim, 'scan_id': getattr(http, 'metadata_scan_id', run)}
        result = http.website(METADATA, {'action': 'claim', **base, 'limit': 50}, credential, metadata=True, timeout=20, retry_seconds=60)
        packages = result.get('packages')
        if not isinstance(packages, list) or len(packages) > 50 or len(set(packages)) != len(packages) or any(not isinstance(p, str) or len(p) > 250 or not PACKAGE.fullmatch(p) for p in packages):
            raise SyncError('Website returned an invalid metadata claim', 'website')
        if not packages:
            return {'status': 'idle', 'batches': processed, 'stored': stored, 'dated': dated}
        if result.get('status') != 'claimed' or type(result.get('lease_until')) is not int or result['lease_until'] <= time.time():
            raise SyncError('Metadata claim is unavailable or expired', 'website')
        submitted = False
        try:
            details, failures = {}, {}
            # One runner, up to 50 simultaneous ordinary single-ID HTTP downloads.
            with ThreadPoolExecutor(max_workers=50) as pool:
                futures = {pool.submit(download_metadata, http, package): package for package in packages}
                for future in as_completed(futures):
                    package = futures[future]
                    try:
                        details[package] = future.result()
                    except CancelledError:
                        failures[package] = SyncError('Metadata download cancelled after provider denial', 'tapi.pureapk.com')
                    except SyncError as error:
                        failures[package] = error
                        if error.status in (403, 429):
                            for pending in futures:
                                pending.cancel()
                    except (ValueError, TypeError, OSError):
                        failures[package] = SyncError('Metadata download returned invalid data', 'tapi.pureapk.com')
            attach_play_metrics(http, details)
            receipt = http.website(METADATA, {'action': 'submit', **base, 'details': details, 'failed_packages': list(failures)}, credential, metadata=True, timeout=20, retry_seconds=60)
            if receipt.get('status') != 'submitted':
                raise SyncError('Website did not acknowledge metadata submission', 'website')
            submitted = True
            processed += 1
            stored += receipt.get('stored', 0)
            dated += receipt.get('dated', 0)
            print(json.dumps({'phase': 'metadata', 'batch': processed, 'concurrency': 50, 'claimed': len(packages), 'stored': receipt.get('stored'), 'dated': receipt.get('dated'), 'failed': len(failures), 'requeued': receipt.get('requeued'), 'remaining': receipt.get('remaining')}), flush=True)
            if failures:
                denial = next((e for e in failures.values() if e.status in (403, 429)), None)
                if denial is not None:
                    print(f'::warning::Metadata provider returned HTTP {denial.status}; failed games deferred to the next scan.', flush=True)
                    return {'status': 'deferred', 'batches': processed, 'stored': stored, 'dated': dated, 'deferred': True}
                print(json.dumps({'phase': 'metadata_retry', 'deferred': len(failures), 'reason': 'Failed games deferred to the next scan', 'errors': sorted({f'{e.source}: HTTP {e.status}' if e.status else f'{e.source}: {e}' for e in failures.values()})[:5]}), flush=True)
        finally:
            if not submitted:
                try:
                    http.website(METADATA, {'action': 'release', **base}, credential, metadata=True, timeout=5, attempts=1, retry_seconds=5)
                except SyncError:
                    pass
    return {'status': 'bounded', 'batches': processed, 'stored': stored, 'dated': dated}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('all', 'sitemaps', 'metadata'), default='all')
    parser.add_argument('--seconds', type=int, default=21600)
    parser.add_argument('--metadata-batches', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        max_seconds = 21600
        if not 60 <= args.seconds <= max_seconds or not 0 <= args.metadata_batches <= 200:
            raise SyncError('Invalid bounded runtime or metadata batch count', 'configuration')
        credential = catalog_credential(os.environ.get('STEAM_SYNC_TOKEN'))
        run = os.environ.get('GITHUB_RUN_ID', '') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        if not re.fullmatch(r'[0-9]+-[0-9]+', run):
            raise SyncError('A GitHub Actions run identity is required', 'configuration')
        http = Http(args.seconds)
        results, errors = {}, []
        if args.mode in ('all', 'sitemaps'):
            try:
                results['sitemaps'] = sync_sitemaps(http, credential, run, args.metadata_batches if args.mode == 'all' else 0)
            except SyncError as error:
                errors.append(error)
                results['sitemaps'] = {'status': 'failed', 'source': error.source, 'http_status': error.status, 'cloudflare_challenge': error.challenge, 'error': str(error)}
        # A blocked sitemap does not prevent independent indexing of saved jobs.
        if args.mode in ('all', 'metadata') and args.metadata_batches:
            try:
                results['metadata'] = sync_metadata(http, credential, run, args.metadata_batches)
            except SyncError as error:
                errors.append(error)
                results['metadata'] = {'status': 'failed', 'source': error.source, 'http_status': error.status, 'error': str(error)}
        print(json.dumps({'status': 'failed' if errors else 'finished', 'mode': args.mode, 'results': results, 'checkpoints_preserved': True}), file=sys.stderr if errors else sys.stdout, flush=True)
        return 1 if errors else 0
    except SyncError as error:
        print(json.dumps({'status': 'failed', 'source': error.source, 'http_status': error.status, 'cloudflare_challenge': error.challenge, 'error': str(error), 'checkpoints_preserved': True}), file=sys.stderr, flush=True)
        return 1
    except (ValueError, TypeError, OSError):
        print(json.dumps({'status': 'failed', 'error': 'Invalid data or local runtime failure', 'checkpoints_preserved': True}), file=sys.stderr, flush=True)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
