"""Temporary manual repair; normal host collection must remain paused.

Durable host leases and receipts make rerunning safe. Empty source values never
replace saved values. Unsupported fields remain unknown, not fabricated.
"""
import concurrent.futures
import json
import os
import time
import uuid
from urllib.error import HTTPError, URLError

from apkpure.admin_mobile import Http, METADATA, SyncError, catalog_credential, download_metadata
from play_discovery import listing
from play_history import CheckpointPending, post_checkpoint

FIELDS = {'title': 'title', 'developer': 'developer', 'icon': 'icon_url',
          'description': 'description_short', 'version': 'version_name',
          'updated_date': 'update_date', 'size': 'size'}


def normalize(raw, package):
    if not raw:
        return {}
    if raw.get('package_name', package) != package:
        raise ValueError('Source package mismatch')
    out = {'package_name': package}
    for key, limit in [('title', 1000), ('developer', 1000), ('description_short', 50000),
                       ('version_name', 200), ('update_date', 100), ('create_date', 100)]:
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v[:limit]
    icon = raw.get('icon_url') or ((raw.get('icon') or {}).get('thumbnail') or {}).get('url')
    if isinstance(icon, str) and icon.startswith('https://') and len(icon) <= 4096:
        out['icon_url'] = icon
    size = (raw.get('asset') or {}).get('size')
    if isinstance(size, str) and size.isdigit():
        size = int(size)
    if type(size) is int and 0 < size <= 9007199254740991:
        out['asset'] = {'size': size}
    return out if len(out) > 1 else {}


def missing_from_source(missing, detail):
    return [f for f in missing if f in FIELDS and not
            (detail.get('asset', {}).get('size') if f == 'size' else detail.get(FIELDS[f]))]


def repair_one(item, http, play_fetch=listing, apk_fetch=download_metadata):
    pkg = item['package']
    detail, errors = {}, []
    try:
        result = play_fetch(pkg)
        if result:
            detail = normalize(result['metadata'], pkg)
    except (HTTPError, URLError, TimeoutError, ConnectionError, ValueError, SyncError) as exc:
        errors.append('Play: ' + type(exc).__name__ + ' ' + str(exc)[:220])
    # APKPure supplies download size and sometimes fields not exposed by Play.
    if not detail or missing_from_source(item['missing'], detail):
        try:
            extra = normalize(apk_fetch(http, pkg), pkg)
            for key, value in extra.items():
                if key not in detail:
                    detail[key] = value
        except (HTTPError, URLError, TimeoutError, ConnectionError, ValueError, SyncError) as exc:
            errors.append('APKPure: ' + type(exc).__name__ + ' ' + str(exc)[:220])
    remaining = missing_from_source(item['missing'], detail)
    outcome = 'retry' if errors and (remaining or not detail) else ('ok' if detail else 'unavailable')
    return {'detail': detail or None, 'outcome': outcome, 'error': '; '.join(errors)[:1000]}


def progress_line(p):
    return (f"Checked {p['processed']}/{p['total']} | repaired {p['fixed']} "
            f"({p['fields_filled']} fields) | complete {p['complete']} | partial {p['partial']} "
            f"| unchanged {p['unchanged']} | unavailable {p['unavailable']} | failed {p['failed']} "
            f"| pending {p['pending']} | active {p['leased']}")


def post_until(http, token, payload, deadline):
    # Reuse identical bytes/claim IDs after a busy host or a lost upload receipt.
    deadline = min(deadline, http.deadline - 5)
    while time.monotonic() < deadline - 2:
        try:
            return post_checkpoint(http, token, payload, deadline)
        except CheckpointPending:
            print('::warning::Host still busy; keeping this worker alive and retrying the same request.', flush=True)
    raise CheckpointPending('Request budget exhausted; unfinished leases remain resumable.')


def main():
    minutes = min(320, max(1, int(os.environ.get('REPAIR_MINUTES', '300'))))
    max_batches = max(0, int(os.environ.get('REPAIR_MAX_BATCHES', '0')))
    slot = os.environ.get('SLOT', '0')
    run = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'] + '-' + slot
    token = catalog_credential(os.environ['STEAM_SYNC_TOKEN'])
    http = Http(minutes * 60)
    def post(action, claim=None, until=None, **fields):
        payload = dict(scope='metadata_repair', action=action, run_id=run,
                       claim_id=claim or uuid.uuid4().hex, **fields)
        return post_until(http, token, payload, until or min(http.deadline - 30, time.monotonic() + 240))
    time.sleep(int(slot) * .5)
    batches, latest = 0, None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            while http.deadline - time.monotonic() > 600 and (not max_batches or batches < max_batches):
                claim = uuid.uuid4().hex
                try:
                    batch = post('claim', claim, limit=10)
                    latest = batch['progress']
                    if batch['status'] == 'empty':
                        if latest['leased']:
                            print('Waiting for active or abandoned leases before checking the queue again.', flush=True)
                            time.sleep(30)
                            continue
                        print('No pending repair work remains.', flush=True)
                        break
                    if batch['status'] != 'claimed':
                        raise RuntimeError('Unexpected repair claim status')
                    futures = {pool.submit(repair_one, item, http): item['package'] for item in batch['items']}
                    results = {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}
                    # Stop resending before the host can give this lease to another worker.
                    upload_until = min(http.deadline - 30, time.monotonic() + max(0, batch['lease_until'] - time.time() - 30))
                    ack = post('submit', claim, until=upload_until, results=results)
                    if ack.get('status') != 'submitted' or set(ack.get('results', {})) != set(results):
                        raise RuntimeError('Unconfirmed repair upload')
                    latest = ack['progress'];batches += 1
                    for pkg, result in ack['results'].items():
                        print(f"{pkg}: {result['state']}; +{result['fields_filled']} fields; missing: {','.join(result['missing']) or 'none'}", flush=True)
                    print(progress_line(latest), flush=True)
                    time.sleep(.5)
                except CheckpointPending as exc:
                    print(f'::warning::{exc} Continuing with the repair queue; saved work is retained.', flush=True)
                    time.sleep(2)
        latest = post('status')['progress']
        if latest['pending'] or latest['leased']:
            print('::warning::Repair has unfinished records. Rerun this manual workflow to resume; saved repairs are retained.', flush=True)
        if latest['failed'] or latest['unavailable'] or latest['partial'] or latest['unchanged']:
            print('::warning::Some fields remain unavailable from the sources. See the repair counts; missing values were not fabricated.', flush=True)
    except CheckpointPending as exc:
        print(f'::warning::{exc} Rerun this workflow to resume the repair queue.', flush=True)
    finally:
        if latest and os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('### Catalog metadata repair\n\n' + progress_line(latest) + '\n\n')
                summary.write('Existing values are preserved. Normal collection remains paused. Rerun this workflow to resume pending work.\n')
    if latest:
        print(progress_line(latest), flush=True)


if __name__ == '__main__':
    main()
