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


def refresh(tr, deadline, dated_only=False):
    totals = dict(checked=0, released=0, upcoming=0, failed=0)
    next_request = 0.0
    while deadline - time.monotonic() > 5:
        reply = tr.call('release-candidates', limit=20, dated_only=dated_only)
        candidates = reply.get('records')
        if not isinstance(candidates, list) or len(candidates) > 20:
            raise ValueError('Invalid release candidates')
        if not candidates:
            break
        ids = [r.get('appid') for r in candidates if isinstance(r, dict)]
        if len(ids) != len(candidates) or len(set(ids)) != len(ids) or any(type(i) is not int or not 0 < i < 2**32 for i in ids):
            raise ValueError('Invalid release AppIDs')
        rows, failed, stop = [], [], False
        for appid in ids:
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
                print(f'::warning::Steam release check {appid}: {type(error).__name__}' + (f' HTTP {code}' if code else '') + '; saved status unchanged.', flush=True)
                if code in (403, 429, 503):
                    stop = True
                    break
        if rows or failed:
            receipt = tr.call('releases', records=rows, failed_appids=failed)
            totals['checked'] += len(rows)
            totals['released'] += receipt['released']
            totals['upcoming'] += receipt['upcoming']
            totals['failed'] += len(failed)
            print(json.dumps(dict(phase='release_status', **totals, remaining_due=receipt['remaining_due'])), flush=True)
        if stop:
            break
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=1200)
    parser.add_argument('--dated-only', action='store_true')
    args = parser.parse_args()
    if not 10 <= args.seconds <= 3600:
        parser.error('seconds must be between 10 and 3600')
    from collect import Transport
    tr = Transport(os.environ.get('STEAM_SYNC_URL', 'https://syzoz.com/chart/steam-sync.php'), os.environ['STEAM_SYNC_TOKEN'])
    print(json.dumps(refresh(tr, time.monotonic() + args.seconds, args.dated_only)), flush=True)


if __name__ == '__main__':
    main()
