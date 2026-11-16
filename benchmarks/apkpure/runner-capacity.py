"""Benchmark APKPure sitemap validation, serialization and compression."""
import base64
import gzip
import hashlib
import json
import os
import statistics
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'
ET.register_namespace('', NS)

def prepare(document):
    root = ET.fromstring(document)
    if root.tag != f'{{{NS}}}urlset':
        raise ValueError('Unexpected sitemap root')
    packages = []
    for row in root.findall(f'{{{NS}}}url'):
        url = row.findtext(f'{{{NS}}}loc', '')
        if not url.startswith('https://apkpure.net/'):
            raise ValueError('Unexpected game URL')
        packages.append(url.rsplit('/', 1)[-1])
    body = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    prepared = base64.b64encode(gzip.compress(body, mtime=0))
    return packages, hashlib.sha256(body).digest() + hashlib.sha256(prepared).digest()

def main():
    path = Path(__file__).with_name('sitemap-sample.b64')
    documents = json.loads(gzip.decompress(base64.b64decode(path.read_bytes())))
    trials = []
    reference = None
    for threads in (1, 2, 4, 10, 50, 100):
        times = []
        for _ in range(5):
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=threads) as pool:
                result = list(pool.map(prepare, documents))
            times.append(time.perf_counter() - start)
            packages = [p for row, _ in result for p in row]
            if len(packages) != 1000 or len(set(packages)) != 1000:
                raise ValueError('Expected exactly 1000 distinct packages')
            digest = hashlib.sha256(b''.join(h for _, h in result)).hexdigest()
            if reference is not None and digest != reference:
                raise ValueError('Different output between trials')
            reference = digest
        seconds = statistics.median(times)
        trials.append({'threads':threads,'seconds':seconds,'games_per_second':1000/seconds})
    Path('apkpure-capacity.json').write_text(json.dumps(trials))
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
        print('APKPure: 1,000 saved games in 100 sitemap slices; median of five trials.\n',file=f)
        print('| Threads | Seconds per 1,000 | Games/s |\n|---:|---:|---:|',file=f)
        for trial in trials:
            print('| {threads} | {seconds:.4f} | {games_per_second:.1f} |'.format(**trial),file=f)

if __name__ == '__main__':
    main()
