"""Refresh only saved upcoming games; never infer release from a planned date."""
import argparse
from datetime import datetime, timezone
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


def release_fields(appid, document, checked_at):
    entry = document.get(str(appid)) if isinstance(document, dict) else None
    if not isinstance(entry, dict) or entry.get('success') is not True:
        raise ValueError('Steam listing unavailable')
    data = entry.get('data')
    release = data.get('release_date') if isinstance(data, dict) else None
    if not isinstance(release, dict) or type(release.get('coming_soon')) is not bool:
        raise ValueError('Steam did not provide an explicit release status')
    label = release.get('date')
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError('Invalid Steam release label')
    stamp = None
    for fmt in ('%b %d, %Y', '%d %b, %Y', '%d %B, %Y', '%B %d, %Y'):
        try:
            stamp = int(datetime.strptime(label, fmt).replace(tzinfo=timezone.utc).timestamp())
            break
        except ValueError:
            pass
    # An explicit release flag is independent of whether the date is parseable.
    return dict(appid=appid, coming_soon=release['coming_soon'],
                release_ts=stamp, release_label=label, checked_at=checked_at)


def fetch_release(appid, timeout):
    params = urllib.parse.urlencode(dict(appids=appid, cc='us', l='english', filters='release_date'))
    request = urllib.request.Request('https://store.steampowered.com/api/appdetails?' + params,
                                    headers={'User-Agent': 'SyzozCatalog/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1000001)
    if len(raw) > 1000000:
        raise ValueError('Steam response too large')
    return release_fields(appid, json.loads(raw), int(time.time()))


def store_items(appids, timeout):
    payload = {'ids': [{'appid': i} for i in appids],
               'context': {'language': 'english', 'country_code': 'US'},
               'data_request': {'include_release': True, 'include_reviews': True}}
    url = 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?' + urllib.parse.urlencode({'input_json': json.dumps(payload)})
    with urllib.request.urlopen(url, timeout=timeout) as response:
        document = json.load(response)
    items = document.get('response', {}).get('store_items')
    if not isinstance(items, list):
        raise ValueError('Invalid Steam Store batch response')
    return {item.get('id', item.get('appid')): item for item in items
            if isinstance(item, dict) and item.get('success') == 1
            and item.get('id', item.get('appid')) in appids}


def review_fields(appid, item, checked_at):
    summary = (item.get('reviews') or {}).get('summary_filtered')
    if not isinstance(summary, dict):
        return None
    count, percent = summary.get('review_count'), summary.get('percent_positive')
    if type(count) is not int or not 0 <= count <= 9007199254740991:
        return None
    if count and (type(percent) is not int or not 0 <= percent <= 100):
        return None
    return dict(appid=appid, rating=percent / 20 if count else None,
                rating_count=count, rating_source='steam_store_summary', rating_checked_at=checked_at)


def refresh_ratings(tr, appids, deadline):
    if not appids or deadline - time.monotonic() <= 2:
        return
    items = store_items(appids, min(25, deadline - time.monotonic()))
    now = int(time.time())
    records = [review_fields(i, item, now) for i, item in items.items()]
    records = [row for row in records if row is not None]
    if records:
        receipt = tr.call('ratings', records=records)
        print(json.dumps(dict(phase='steam_ratings', **receipt)), flush=True)


def refresh(tr, deadline, dated_only=False, batch_size=100):
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    totals = dict(checked=0, released=0, upcoming=0, failed=0)
    next_request = 0.0
    remaining_due = None
    while deadline - time.monotonic() > 30:
        reply = tr.call('release-candidates', limit=batch_size, dated_only=dated_only)
        candidates = reply.get('records')
        if not isinstance(candidates, list) or len(candidates) > batch_size:
            raise ValueError('Invalid release candidates')
        if not candidates:
            print(json.dumps(dict(phase='release_status', status='no_unclaimed_work', **totals)), flush=True)
            return totals
        ids = [r.get('appid') for r in candidates if isinstance(r, dict)]
        if len(ids) != len(candidates) or len(set(ids)) != len(ids) or any(type(i) is not int or not 0 < i < 2**32 for i in ids):
            raise ValueError('Invalid release AppIDs')
        wait = max(0, next_request - time.monotonic())
        if wait:
            time.sleep(wait)
        next_request = time.monotonic() + 1.5
        try:
            items = store_items(ids, min(25, deadline - time.monotonic()))
        except (OSError, ValueError) as error:
            print(f'::warning::Steam release batch unavailable ({type(error).__name__}); claimed work retained for retry.', flush=True)
            tr.call('releases', records=[], failed_appids=ids)
            break
        rows, ratings, failed, stop = [], [], [], False
        now = int(time.time())
        for appid in ids:
            item = items.get(appid, {})
            rating = review_fields(appid, item, now)
            if rating is not None:
                ratings.append(rating)
            rel = item.get('release')
            if not isinstance(rel, dict):
                failed.append(appid)
                continue
            if rel.get('is_coming_soon') is True:
                stamp = rel.get('steam_release_date')
                stamp = stamp if type(stamp) is int and 0 < stamp < 2**32 else None
                label = rel.get('custom_release_date_message') or (datetime.fromtimestamp(stamp, timezone.utc).strftime('%b %d, %Y') if stamp else 'Coming soon')
                rows.append(dict(appid=appid, coming_soon=True, release_ts=stamp, release_label=label, checked_at=now))
                continue
            # Protobuf JSON omits false flags. Confirm launches through the
            # explicit appdetails flag instead of guessing from an absent field.
            wait = max(0, next_request - time.monotonic())
            if deadline - time.monotonic() <= wait + 3:
                stop = True
                break
            if wait:
                time.sleep(wait)
            next_request = time.monotonic() + 1.5
            try:
                rows.append(fetch_release(appid, min(15, deadline - time.monotonic())))
            except (OSError, ValueError) as error:
                failed.append(appid)
                code = getattr(error, 'code', None)
                print(f'::warning::Steam release check {appid}: {type(error).__name__}; saved status unchanged.', flush=True)
                if code in (403, 429, 503):
                    stop = True
                    break
        if ratings:
            tr.call('ratings', records=ratings)
        if rows or failed:
            receipt = tr.call('releases', records=rows, failed_appids=failed)
            totals['checked'] += len(rows)
            totals['released'] += receipt['released']
            totals['upcoming'] += receipt['upcoming']
            totals['failed'] += len(failed)
            remaining_due = receipt['remaining_due']
            print(json.dumps(dict(phase='release_status', **totals, remaining_due=remaining_due)), flush=True)
        if stop:
            break
    print(f'::warning::Steam release pass interrupted; remaining records are checkpointed for the next normal run (last remaining_due={remaining_due}).', flush=True)
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=1200)
    parser.add_argument('--dated-only', action='store_true')
    parser.add_argument('--batch', type=int, default=50, help='Games per atomic server claim; repeat until no work remains')
    args = parser.parse_args()
    if not 60 <= args.seconds <= 21300:
        parser.error('seconds must be between 60 and 21300')
    if not 1 <= args.batch <= 100:
        parser.error('batch must be between 1 and 100')
    from collect import Transport
    tr = Transport(os.environ.get('STEAM_SYNC_URL', 'https://syzoz.com/chart/steam-sync.php'), os.environ['STEAM_SYNC_TOKEN'])
    print(json.dumps(refresh(tr, time.monotonic() + args.seconds, args.dated_only, args.batch)), flush=True)


if __name__ == '__main__':
    main()
