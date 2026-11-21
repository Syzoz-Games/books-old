"""Collect public Steam list positions and publish each successfully validated list.
No sales/wishlist counts are inferred. Non-app rows retain their positions.
"""
import argparse
import gzip
import hashlib
import json
import os
import random
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

SOURCES = {
    'popularwishlistfile': {'filter': 'popularwishlist'},
    'popularnewrelisesfile': {'filter': 'popularnew', 'sort_by': 'Released_DESC'},
    'popularetopsellerfile': {'filter': 'globaltopsellers'},
}
PAGE_SIZE = 100
WORKERS = 60
ENDPOINT = 'https://store.steampowered.com/search/results/'
UPLOAD = 'https://syzoz.com/chart/steam-rankings-sync.php'
RETRYABLE_HTTP_STATUS = (408, 429, 500, 502, 503, 504)


class RankingValidationTimeout(TimeoutError):
    """The live validation deadline cannot accommodate more requests/retries."""


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != 'a' or 'search_result_row' not in attrs.get('class', '').split():
            return
        url = urlparse(attrs.get('href', ''))
        match = re.match(r'^/app/([1-9][0-9]*)(?:/|$)', url.path)
        app = match[1] if match and url.hostname == 'store.steampowered.com' else None
        identity = attrs.get('data-ds-itemkey') or url.path
        if not identity:
            raise ValueError('Steam row has no product identity')
        self.rows.append({'key': identity, 'appid': app})


def request(url, data=None, headers=None, deadline=None):
    for attempt in range(6):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise RankingValidationTimeout('Ranking validation budget exhausted; live files unchanged')
        try:
            req = Request(url, data=data, headers={'User-Agent': 'SyzozCatalog/1.0', **(headers or {})})
            with urlopen(req, timeout=45 if remaining is None else min(45, remaining)) as response:
                raw = response.read(12_000_001)
            if len(raw) > 12_000_000:
                raise ValueError('Response exceeds safety limit')
            return json.loads(raw)
        except HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS or attempt == 5:
                raise
            retry_after = error.headers.get('Retry-After', '')
            delay = max(min(120, (30 if error.code == 429 else 1) * 2 ** attempt), int(retry_after) if retry_after.isdigit() else 0)
            print(f'HTTP {error.code} from {urlparse(url).hostname}; retry {attempt + 1}/5 in {delay}s', flush=True)
        except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt == 5:
                raise
            delay = min(30, 2 ** attempt)
        delay += random.random()
        if deadline is not None and time.monotonic() + delay >= deadline:
            raise RankingValidationTimeout('Ranking validation budget exhausted; live files unchanged')
        time.sleep(delay)


def fetch(name, start, deadline=None):
    params = dict(SOURCES[name], start=start, count=PAGE_SIZE, infinite=1,
                  ndl=1, ignore_preferences=1, cc='us', l='english')
    data = request(ENDPOINT + '?' + urlencode(params), deadline=deadline)
    if data.get('success') != 1 or not isinstance(data.get('total_count'), int):
        raise ValueError('Steam returned an invalid search response')
    total = data['total_count']
    # Steam normalizes offsets to page boundaries. Never assign requested
    # positions to a different response page.
    if data.get('start') != start and start < total:
        raise ValueError(f'{name}: requested offset {start}, received {data.get("start")}')
    parser = Rows()
    parser.feed(data.get('results_html', ''))
    if total < 1 or len(parser.rows) != min(PAGE_SIZE, max(0, total - start)):
        raise ValueError(f'{name} page {start}: incomplete response ({len(parser.rows)} rows, total {total})')
    return dict(name=name, start=start, total=total, rows=parser.rows)


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def plan(folder):
    first = {name: fetch(name, 0) for name in SOURCES}
    result = dict(schema=1, started=int(time.time()), run_id=os.environ.get('GITHUB_RUN_ID', str(time.time_ns())),
                  attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')), region='US', first=first,
                  totals={name: page['total'] for name, page in first.items()})
    result['tasks'] = [[name, offset] for name, total in result['totals'].items()
                       for offset in range(0, total, PAGE_SIZE)]
    save(folder / 'plan.json', result)
    print('Discovered list totals:', result['totals'], flush=True)


def worker(folder, slot):
    if not 0 <= slot < WORKERS:
        raise ValueError('Invalid worker slot')
    manifest = load(folder / 'plan.json')
    digest = hashlib.sha256((folder / 'plan.json').read_bytes()).hexdigest()
    pages = []
    for name, start in manifest['tasks'][slot::WORKERS]:
        page = manifest['first'][name] if start == 0 else fetch(name, start)
        pages.append(page)
        print(f'Worker {slot}: {name} {start + len(page["rows"])}/{page["total"]}', flush=True)
        time.sleep(.25)
    save(folder / f'worker-{slot}.json', dict(slot=slot, plan=digest, pages=pages))


def verified_pages(name, first, staged, deadline):
    """Recollect changed lists without combining old and new page generations.

    Steam has no snapshot cursor. Comparing the head before and after collection
    detects visible movement, but cannot promise a frozen point-in-time ranking.
    """
    fresh = fetch(name, 0, deadline=deadline)
    candidate = dict(staged) if fresh['rows'] == first['rows'] else {}
    for attempt in range(3):
        if not candidate:
            print(f'{name}: list changed; recollecting all pages (attempt {attempt + 1}/3)', flush=True)
        candidate[0] = fresh
        # The actual short final page determines the total, including growth
        # across page boundaries or a shrinking list. Totals are never fixed.
        tail = ((fresh['total'] - 1) // PAGE_SIZE) * PAGE_SIZE
        while True:
            if time.monotonic() >= deadline:
                raise RankingValidationTimeout('Ranking validation budget exhausted; live files unchanged')
            end_page = fetch(name, tail, deadline=deadline)
            candidate[tail] = end_page
            if not end_page['rows'] and tail > end_page['total']:
                tail = ((end_page['total'] - 1) // PAGE_SIZE) * PAGE_SIZE
                continue
            if len(end_page['rows']) < PAGE_SIZE:
                total = tail + len(end_page['rows'])
                break
            tail += PAGE_SIZE
        for offset in range(0, total, PAGE_SIZE):
            expected = min(PAGE_SIZE, total - offset)
            page = candidate.get(offset)
            if page is None or len(page['rows']) != expected:
                page = fetch(name, offset, deadline=deadline)
                candidate[offset] = page
            if len(page['rows']) != expected:
                raise ValueError(f'{name}: incomplete page {offset}; live files unchanged')
        after = fetch(name, 0, deadline=deadline)
        if after['rows'] == fresh['rows']:
            return total, candidate
        # Discard every page of this list before another collection attempt.
        fresh, candidate = after, {}
    raise ValueError(f'{name}: list kept changing after 3 attempts; live files unchanged')


def assemble(folder, verify_live=True):
    manifest = load(folder / 'plan.json')
    digest = hashlib.sha256((folder / 'plan.json').read_bytes()).hexdigest()
    pages = {}
    for slot in range(WORKERS):
        output = load(folder / f'worker-{slot}.json')
        if output.get('slot') != slot or output.get('plan') != digest:
            raise ValueError('Worker belongs to a different collection')
        expected = {tuple(task) for task in manifest['tasks'][slot::WORKERS]}
        actual = {(page['name'], page['start']) for page in output['pages']}
        if actual != expected or len(actual) != len(output['pages']):
            raise ValueError(f'Worker {slot} has incomplete or duplicate pages')
        pages.update({(page['name'], page['start']): page for page in output['pages']})
    files, totals, skipped = {}, {}, {}
    for name, planned_total in manifest['totals'].items():
        try:
            total = planned_total
            if verify_live:
                # Each list gets its own budget; a slow list cannot consume the
                # time available for the following lists.
                total, verified = verified_pages(
                    name, manifest['first'][name],
                    {offset: page for (source, offset), page in pages.items() if source == name},
                    time.monotonic() + 180)
                pages.update({(name, offset): page for offset, page in verified.items()})
            ranks, identities, duplicates = {}, set(), 0
            for offset in range(0, total, PAGE_SIZE):
                page = pages.get((name, offset))
                expected = min(PAGE_SIZE, total - offset)
                if page is None or len(page['rows']) != expected:
                    raise ValueError('Incomplete page in staging')
                for index, row in enumerate(page['rows'], offset + 1):
                    if row['key'] in identities or row['appid'] is not None and row['appid'] in ranks:
                        # Keep Steam's first source position, including gaps.
                        duplicates += 1
                        continue
                    identities.add(row['key'])
                    if row['appid'] is not None:
                        ranks[row['appid']] = index
            if not ranks:
                raise ValueError('No app rankings available')
        except HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS:
                raise
            skipped[name] = f'Steam returned HTTP {error.code} after retries'
        except (URLError, TimeoutError, ConnectionError, ValueError) as error:
            skipped[name] = str(error)[:300]
        if name in skipped:
            message = f'{name}: update skipped ({skipped[name]}). Previous live list retained if available.'
            safe_message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print(f'::warning title=Steam list not updated::{safe_message}', flush=True)
            continue
        print(f'{name}: {total} source positions checked, {len(ranks)} unique apps, {duplicates} repeated rows hidden (Steam search behavior)', flush=True)
        files[name], totals[name] = ranks, total
        save(folder / (name + '.json'), ranks)
    return dict(schema=2, run_id=os.environ.get('GITHUB_RUN_ID', manifest['run_id']),
                attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', manifest['attempt'])),
                started=manifest['started'], finished=int(time.time()), region=manifest['region'],
                totals=totals, files=files, skipped=skipped)


def publication_summary(updated, skipped):
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write('### Steam rankings publication\n\n')
            for name in SOURCES:
                if name in updated:
                    output.write(f'- **Updated:** `{name}`.\n')
                elif name in skipped:
                    output.write(f'- **Not updated:** `{name}` — previous live list retained if available. See the warning in the logs.\n')
            if not updated:
                output.write('\nNo fresh rankings were published.\n')


def publish(folder):
    token = os.environ.get('STEAM_SYNC_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Upload credential is missing')
    payload = assemble(folder)
    if not payload['files']:
        publication_summary([], payload['skipped'])
        print('No lists passed validation; nothing uploaded, live files unchanged.', flush=True)
        return
    # Only validated lists are sent. The receiver merges omitted lists from
    # the previous generation without changing their individual timestamps.
    raw = gzip.compress(json.dumps(payload, separators=(',', ':')).encode())
    result = request(UPLOAD, raw, {'Content-Type': 'application/json', 'Content-Encoding': 'gzip', 'X-Steam-Token': token})
    if result.get('status') != 'published' or set(result.get('updated', [])) != set(payload['files']):
        raise ValueError('Server did not confirm publication of the successful lists')
    publication_summary(payload['files'], payload['skipped'])
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan', 'worker', 'validate', 'publish'])
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--folder', type=Path, default=Path('rankings-stage'))
    args = parser.parse_args()
    if args.mode == 'plan': plan(args.folder)
    elif args.mode == 'worker': worker(args.folder, args.slot)
    elif args.mode == 'validate':
        result = assemble(args.folder)
        print({name: len(rows) for name, rows in result['files'].items()})
    else: publish(args.folder)
