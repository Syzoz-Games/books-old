"""GitHub-only, two-request Valve JSON field probe. No HTML or server writes."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY = 4_000_000
FLAGS = {'include_assets': True, 'include_release': True, 'include_platforms': True,
         'include_basic_info': True, 'include_reviews': True, 'include_tag_count': 20,
         'include_screenshots': True, 'include_trailers': True, 'include_full_description': True,
         'include_ratings': True, 'include_links': True, 'include_best_purchase_option': True}


class ProbeError(RuntimeError):
    pass


class HardDeadline(BaseException):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def github_metadata(env):
    if env.get('GITHUB_ACTIONS') != 'true':
        raise ProbeError('This source probe runs only in GitHub Actions')
    result = {'repository': env.get('GITHUB_REPOSITORY', ''), 'run_id': env.get('GITHUB_RUN_ID', ''),
              'sha': env.get('GITHUB_SHA', '').lower()}
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', result['repository'])
            or not re.fullmatch(r'[1-9][0-9]*', result['run_id'])
            or not re.fullmatch(r'[0-9a-f]{40}', result['sha'])):
        raise ProbeError('Missing GitHub run provenance')
    return result


def validate_appids(appids):
    if (not 1 <= len(appids) <= 2 or len(set(appids)) != len(appids)
            or any(type(appid) is not int or not 1 <= appid <= 4294967295 for appid in appids)):
        raise ProbeError('Provide one or two distinct uint32 AppIDs')


def source_json(url, deadline, events):
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise ProbeError('Source deadline exhausted')
    event = {'url': url}
    events.append(event)
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                                                      'User-Agent': 'SyzozJsonFieldProbe/1.0'})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=min(20, remaining)) as response:
            raw = response.read(MAX_BODY + 1)
            event.update(http_status=response.status, content_type=response.headers.get('Content-Type'), bytes=len(raw))
        if len(raw) > MAX_BODY:
            raise ProbeError('Source JSON exceeds response limit')
        if 'json' not in str(event['content_type']).lower():
            raise ProbeError('Source did not identify a JSON response')
        data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ProbeError('Source JSON root is not an object')
        return data
    except urllib.error.HTTPError as error:
        event['http_status'] = error.code
        error.close()
        raise ProbeError('Source returned HTTP ' + str(event['http_status'])) from None
    except (OSError, ValueError):
        raise ProbeError('Source request or JSON decoding failed') from None
    finally:
        event['seconds'] = round(time.monotonic() - started, 4)


def tag_dictionary(document):
    response = document.get('response')
    tags = response.get('tags') if isinstance(response, dict) else None
    if not isinstance(tags, list) or len(tags) > 10000:
        raise ProbeError('Tag dictionary shape is unavailable')
    names = {}
    for tag in tags:
        if not isinstance(tag, dict):
            raise ProbeError('Tag dictionary entry is malformed')
        tagid, name = tag.get('tagid'), tag.get('name')
        if (type(tagid) is not int or not 1 <= tagid <= 4294967295
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80):
            raise ProbeError('Tag dictionary identity or label is invalid')
        if tagid in names and names[tagid] != name.strip():
            raise ProbeError('Tag dictionary has conflicting names')
        names[tagid] = name.strip()
    return names, response.get('version_hash')


def review_summary(value):
    if not isinstance(value, dict):
        return {'available': False}
    count, percent = value.get('review_count'), value.get('percent_positive')
    usable = type(count) is int and 0 < count <= 9007199254740991 and type(percent) is int and 0 <= percent <= 100
    return {'available': usable, 'review_count': count, 'percent_positive': percent,
            'review_score': value.get('review_score'), 'review_score_label': value.get('review_score_label'),
            'candidate_rating': 5 * percent / 100 if usable else None,
            'candidate_rating_source': 'steam_store_summary' if usable else None,
            'precision': 'published integer approval percentage; positive counts are not reconstructed'}


def item_fields(item, names):
    ids = item.get('tagids')
    tag_source = 'tagids'
    if not isinstance(ids, list) or not ids:
        weighted = item.get('tags')
        ids = [tag.get('tagid') for tag in weighted if isinstance(tag, dict)] if isinstance(weighted, list) else []
        tag_source = 'tags'
    ids = [tagid for tagid in ids if type(tagid) is int and 1 <= tagid <= 4294967295]
    resolved = [{'tagid': tagid, 'name': names[tagid]} for tagid in ids if tagid in names]
    reviews = item.get('reviews') if isinstance(item.get('reviews'), dict) else {}
    basic = item.get('basic_info') if isinstance(item.get('basic_info'), dict) else {}
    categories = item.get('categories') if isinstance(item.get('categories'), dict) else {}
    features = categories.get('feature_categoryids')
    complete_features = isinstance(features, list) and all(type(value) is int and 0 <= value <= 4294967295 for value in features)
    purchase = item.get('best_purchase_option') if isinstance(item.get('best_purchase_option'), dict) else {}
    return {'appid': item.get('appid', item.get('id')), 'success': item.get('success'), 'response_keys': list(item),
        'is_free_present': 'is_free' in item, 'is_free': item.get('is_free') if type(item.get('is_free')) is bool else None,
        'title': item.get('name'), 'description': basic.get('short_description'),
        'iap': 35 in features if complete_features else None,
        'best_formatted_price': purchase.get('formatted_final_price'),
        'tag_source': tag_source, 'tagids': ids, 'named_tags': resolved,
        'unresolved_tagids': [tagid for tagid in ids if tagid not in names],
        'global_filtered_reviews': review_summary(reviews.get('summary_filtered')),
        'global_unfiltered_reviews': review_summary(reviews.get('summary_unfiltered')),
        'language_specific_reviews': review_summary(reviews.get('summary_language_specific')),
        'counts_all_purchases': reviews.get('counts_all_purchases'),
        'full_description_bbcode_length': len(item.get('full_description_bbcode') or '') if isinstance(item.get('full_description_bbcode'), str) else 0,
        'assets_keys': list(item['assets']) if isinstance(item.get('assets'), dict) else [],
        'screenshots_keys': list(item['screenshots']) if isinstance(item.get('screenshots'), dict) else [],
        'trailers_keys': list(item['trailers']) if isinstance(item.get('trailers'), dict) else [],
        'not_in_this_response': ['public build version', 'depot download bytes', 'verified engine technology']}


def run_probe(appids, deadline, fetch=source_json, *, raw=None, events=None):
    validate_appids(appids)
    events = [] if events is None else events
    raw = {} if raw is None else raw
    request = {'ids': [{'appid': appid} for appid in appids],
               'context': {'language': 'english', 'country_code': 'US'}, 'data_request': FLAGS}
    browse_url = 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?' + urllib.parse.urlencode({'input_json': json.dumps(request)})
    tags_url = 'https://api.steampowered.com/IStoreService/GetTagList/v1/?' + urllib.parse.urlencode({'input_json': json.dumps({'language': 'english'})})
    raw['get_items_request'] = request
    raw['get_items'] = fetch(browse_url, deadline, events)
    raw['tag_dictionary'] = fetch(tags_url, deadline, events)
    names, version = tag_dictionary(raw['tag_dictionary'])
    response = raw['get_items'].get('response')
    items = response.get('store_items') if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ProbeError('GetItems store_items array is unavailable')
    byid = {item.get('appid', item.get('id')): item for item in items if isinstance(item, dict)
            and type(item.get('appid', item.get('id'))) is int}
    fields = [item_fields(byid[appid], names) if appid in byid else {'appid': appid, 'unavailable': True}
              for appid in appids]
    return {'status': 'completed', 'request_count': len(events), 'requests': events,
            'tag_dictionary_count': len(names), 'tag_dictionary_version_hash': version,
            'appids': appids, 'fields': fields, 'html_requests': 0, 'server_writes': 0}, raw


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--appids', nargs='+', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--output', type=Path, default=Path('json-probe-result.json'))
    parser.add_argument('--raw', type=Path, default=Path('json-probe-raw.json'))
    args = parser.parse_args(argv)
    result = {'status': 'failed'}
    raw = {}
    events = []
    previous = None
    try:
        validate_appids(args.appids)
        if not 1 <= args.seconds <= 60:
            raise ProbeError('Budget must be 1 to 60 seconds')
        github = github_metadata(os.environ)
        result['github'] = github
        if not hasattr(signal, 'setitimer'):
            raise ProbeError('Probe requires a Unix hard deadline')
        def expired(signum, frame):
            raise HardDeadline('JSON probe hard deadline exhausted')
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.seconds)
        measured, raw = run_probe(args.appids, time.monotonic() + args.seconds, raw=raw, events=events)
        result.update(measured)
        print(json.dumps({'status': result['status'], 'github': github, 'request_count': result['request_count'],
                          'appids': args.appids, 'tag_dictionary_count': result['tag_dictionary_count']}))
        return 0
    except (HardDeadline, ProbeError, OSError, ValueError) as error:
        result['error'] = str(error) if isinstance(error, (HardDeadline, ProbeError)) else type(error).__name__
        print(json.dumps({'status': 'failed', 'error': result['error']}), file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        result.setdefault('requests', events)
        result.setdefault('request_count', len(events))
        write_json(args.output, result)
        write_json(args.raw, raw)


if __name__ == '__main__':
    raise SystemExit(main())
