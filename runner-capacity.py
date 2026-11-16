"""Measure Steam network time and one runner's local metadata processing capacity."""
import hashlib
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import batch
import collect


def fetch_sample():
    client = batch.JsonClient(120, interval=1)
    begin = time.monotonic()
    manifest = batch.plan_manifest(client, 1000, 1, 1, batch.github_metadata(os.environ))
    names = collect.parse_store_tags(manifest['tag_dictionary'])
    ids = manifest['appids']
    rows = []
    for start in range(0, len(ids), 100):
        group = ids[start:start + 100]
        payload = {'ids': [{'appid': appid} for appid in group],
                   'context': {'country_code': 'US', 'language': 'english'},
                   'data_request': collect.STORE_DATA_REQUEST}
        doc = client.request(batch.api_url('IStoreBrowseService', 'GetItems', payload), 'store_items')
        for item in doc['response']['store_items']:
            if batch.valid_game(item) and item.get('appid', item.get('id')) in group:
                rows.append(batch.compact(item))
    if len(rows) < 50:
        raise RuntimeError('Too few usable Steam games for the 50-task trial')
    return names, rows, time.monotonic() - begin, len(client.events)


def measure(names, rows):
    checked = int(time.time())
    def process(raw):
        item = json.loads(raw)
        appid = item.get('appid', item.get('id'))
        record = collect.normalize(appid, {'common': {'name': item['name'], 'type': 'game'}})
        record.update(collect.store_json_fields(item, names, checked))
        detail = collect.store_detail_json(record, item, checked)
        return hashlib.sha256(batch.compact([record, detail])).digest()

    # Keep the sample identical at each setting. All trials run in this one VM.
    workload = rows * 3
    reference = None
    results = {}
    def trial(n):
        nonlocal reference
        if n in results:
            return
        durations = []
        for _ in range(3):
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n) as pool:
                checksum = hashlib.sha256(b''.join(pool.map(process, workload))).hexdigest()
            durations.append(time.perf_counter() - start)
            if reference is None:
                reference = checksum
            elif checksum != reference:
                raise ValueError('Concurrency changed the output')
        seconds = statistics.median(durations)
        results[n] = {'tasks': n, 'seconds': round(seconds, 3),
                      'games_per_second': round(len(workload) / seconds, 1)}
        print(json.dumps(results[n]), flush=True)

    # Probe 50 explicitly, then adapt to measured scaling of this workload.
    trial(1)
    trial(10)
    trial(50)
    n = 2
    plateau = 0
    best = results[1]['games_per_second']
    while n <= len(rows) and plateau < 2:
        trial(n)
        rate = results[n]['games_per_second']
        plateau = 0 if rate > best * 1.05 else plateau + 1
        best = max(best, rate)
        n *= 2
    return list(results.values()), len(workload), reference


if __name__ == '__main__':
    names, rows, fetch_seconds, requests = fetch_sample()
    results, work, digest = measure(names, rows)
    report = {'cpu_count': len(os.sched_getaffinity(0)), 'unique_games': len(rows),
              'live_requests': requests, 'live_fetch_seconds': round(fetch_seconds, 2),
              'records_per_trial': work, 'repeats': 3, 'output_sha256': digest,
              'trials': sorted(results, key=lambda r: r['tasks']),
              'fastest_measured': max(results, key=lambda r: r['games_per_second'])}
    Path('capacity.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
        print(f"Fetched {len(rows)} games in {fetch_seconds:.2f}s. Replayed {work} records per trial, median of 3 trials.\n", file=f)
        print('| Tasks | Replay time (s) | Games/s |\n|---:|---:|---:|', file=f)
        for row in report['trials']:
            print(f"| {row['tasks']} | {row['seconds']} | {row['games_per_second']} |", file=f)
