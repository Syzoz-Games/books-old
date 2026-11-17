"""Discover missing Android games from Google's published sitemap, without staging files."""
import concurrent.futures
import datetime as dt
import gzip
from email.utils import parsedate_to_datetime
import html
import json
import os
import re
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from xml.parsers import expat
import xml.etree.ElementTree as ET

from apkpure.admin_mobile import Http, METADATA, catalog_credential

BASE = "https://play.google.com/sitemaps/"
NAME = re.compile(r"play_sitemaps_(\d{4}-\d{2}-\d{2}_\d{8,12})-(\d+)-of-(\d+)\.xml\.gz")
PACKAGE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
GAME = re.compile(r"GAME_[A-Z]+(?:_[A-Z]+)*")
DATA = re.compile(r"AF_initDataCallback\(\{key:\s*'ds:5',\s*hash:[^,]*,\s*data:\s*")


TRANSIENT_HTTP = (408, 429, 500, 502, 503, 504)


def retry_delay(error):
    value = (error.headers.get("Retry-After", "") if error.headers else "").strip()
    if value.isdigit():
        return int(value)
    try:
        return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()) + 1)
    except (ValueError, TypeError, OverflowError):
        return 0


def get(url, parser, missing_ok=False):
    for attempt in range(4):
        try:
            with urlopen(url, timeout=30) as response:
                return parser(response)
        except HTTPError as err:
            if missing_ok and err.code in (404, 410):
                return None
            if err.code not in TRANSIENT_HTTP or attempt == 3:
                raise
            delay = retry_delay(err)
            if missing_ok and delay > 30:
                raise
            time.sleep(delay or 2 ** (attempt + 1))
        except (TimeoutError, URLError, ConnectionError):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))


def manifest():
    indexes = get("https://play.google.com/robots.txt", lambda r: [
        line.split(":", 1)[1].strip() for line in r.read().decode().splitlines()
        if line.startswith("Sitemap: " + BASE + "sitemaps-index-")])
    found = set()
    snapshots = set()
    for url in indexes:
        for loc in get(url, lambda r: [e.text for e in ET.parse(r).findall(".//{*}loc")]):
            match = NAME.fullmatch(loc.removeprefix(BASE)) if loc.startswith(BASE) else None
            if not match:
                raise ValueError("Unexpected Google sitemap filename")
            tag, index, total = match.groups()
            found.add(int(index))
            snapshots.add((tag, int(total)))
    if len(snapshots) != 1:
        raise ValueError("Missing or inconsistent snapshot indexes")
    tag, total = snapshots.pop()
    if found != set(range(total)):
        raise ValueError("Incomplete sitemap index")
    return tag, total


def packages(response):
    parser = expat.ParserCreate()
    parts = None
    ids = set()

    def start(name, attrs):
        nonlocal parts
        if name == "loc":
            parts = []

    def end(name):
        nonlocal parts
        if name != "loc" or parts is None:
            return
        loc = urlsplit("".join(parts))
        parts = None
        if loc.hostname == "play.google.com" and loc.path in ("/store/apps/details", "/pc-store/games/details"):
            ids.update(p for p in parse_qs(loc.query).get("id", []) if len(p) <= 250 and PACKAGE.fullmatch(p))

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = lambda text: parts.append(text) if parts is not None else None
    with gzip.GzipFile(fileobj=response) as stream:
        parser.ParseFile(stream)
    return sorted(ids)


def field(value, *path):
    try:
        for key in path:
            value = value[key]
        return value
    except (IndexError, KeyError, TypeError):
        return None


class ListingDataError(ValueError):
    """The response did not contain a verified listing for the requested package."""


def listing(pkg):
    raw = get("https://play.google.com/store/apps/details?id=" + pkg + "&hl=en&gl=US",
              lambda r: r.read().decode("utf-8"), missing_ok=True)
    if raw is None:
        return None
    match = DATA.search(raw)
    if not match:
        raise ListingDataError("missing Play listing data")
    data = json.JSONDecoder().raw_decode(raw, match.end())[0]
    if field(data, 1, 3, "12", 0, 0) != pkg:
        raise ListingDataError("unexpected Play listing identity")
    values = data[1][2]

    def value(index, *path):
        entry = field(values, index)
        if entry is None or isinstance(entry, dict):
            entry = field(values, -1, str(index + 1))
        return field(entry, *path)

    primary = value(79, 0, 0, 2)
    # Read categories only from this listing's genre and category fields.
    def category_ids(entry):
        if isinstance(entry, list):
            for item in entry:
                yield from category_ids(item)
        elif isinstance(entry, str) and GAME.fullmatch(entry):
            yield entry

    categories = list(dict.fromkeys(category_ids([primary, value(118)])))
    if not categories:
        if not isinstance(primary, str):
            print(f"Skipping unclassified listing: {pkg}", flush=True)
        return None
    def plain(s):
        return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)))
    now = int(time.time())
    updated = value(145, 0, 1, 0)
    publisher = value(68, 1, 4, 2)
    description = plain(value(72, 0, 1))
    shots = [field(s, 3, 2) for s in value(78, 0) or []]
    price = value(57, 0, 0, 0, 0, 1, 0, 0)
    metadata = dict(package_name=pkg, title=value(0, 0), developer=value(68, 0),
                    description_short=plain(value(73, 0, 1)) or description[:1000],
                    icon_url=value(95, 0, 3, 2), version_name=value(140, 0, 0, 0),
                    update_date=dt.datetime.fromtimestamp(updated,dt.timezone.utc).isoformat() if updated else None)
    play = dict(package=pkg, source="Google Play · US",checked_at=now,
                install_display=value(13, 0),install_floor=value(13, 1),install_count=value(13, 2),
                rating=value(51, 0, 1) or None,rating_count=value(51, 2, 1),release_date=value(10, 0),
                release_ts=value(10, 1, 0),category=value(79, 0, 0, 0),description=description,
                publisher_url="https://play.google.com"+publisher if publisher and publisher.startswith("/store/apps/") else None,
                screenshots=[s for s in shots if s and s.startswith("https://")],video=value(100, 0, 0, 3, 2),
                header_image=value(96, 0, 3, 2))
    commerce=dict(is_free=None if price is None else int(price==0),price=value(57,0,0,0,0,1,0,2),
                  ads_supported=int(bool(value(48))),iap=int(bool(value(19,0))))
    return dict(metadata=metadata,play=play,categories=categories,commerce=commerce)


def process_batch(pool, post, packages_to_check, file, retry=False):
    batch = packages_to_check
    while batch:
        claim = post("filter", file=file, packages=batch, retry=retry)
        if claim["status"] == "done":
            return True
        if claim["status"] != "ready":
            return False
        wanted = claim["packages"]
        waiting = [pkg for pkg in batch if pkg not in wanted] if claim["busy"] else []
        if wanted:
            games, delays = {}, {}
            futures = {pool.submit(listing, pkg): pkg for pkg in wanted}
            for future in concurrent.futures.as_completed(futures):
                pkg = futures[future]
                try:
                    game = future.result()
                except (ListingDataError, json.JSONDecodeError, UnicodeDecodeError) as error:
                    # The same package may return a valid listing on the next request.
                    delays[pkg] = 300
                    print(f"Deferring {pkg}: {error}", flush=True)
                except HTTPError as error:
                    if error.code not in TRANSIENT_HTTP:
                        raise
                    delays[pkg] = max(300, retry_delay(error))
                    print(f"Deferring {pkg}: HTTP {error.code}", flush=True)
                except (TimeoutError, URLError, ConnectionError) as error:
                    delays[pkg] = 300
                    print(f"Deferring {pkg}: {type(error).__name__}", flush=True)
                else:
                    if game:
                        games[pkg] = game
            ack = post("submit", file=file, packages=wanted, games=games, retry=retry, retry_after=delays)
            if ack["status"] == "done":
                return True
            if ack["status"] != "saved":
                return False
            for pkg in ack["inserted"]:
                print(games[pkg]["metadata"]["title"], pkg, flush=True)
            waiting.extend(ack["unowned"])
        batch = waiting
        if batch:
            time.sleep(3)
    return True


def collect(slot):
    token = catalog_credential(os.environ["STEAM_SYNC_TOKEN"])
    run = os.environ["GITHUB_RUN_ID"]+"-"+os.environ["GITHUB_RUN_ATTEMPT"]
    http = Http(5*3600+40*60)
    context = dict(scope="play_discovery",slot=slot,run_id=run)
    def post(action, **fields):
        payload=dict(context,action=action,claim_id=uuid.uuid4().hex,**fields)
        return http.website(METADATA,payload,token,metadata=True,timeout=45)
    first=post("status")
    if first["status"]!="ready":
        print(first["status"],first.get("saved",""))
        return
    context["generation"]=first["generation"]
    tag,total=manifest()
    opened=post("open",tag=tag,total=total)
    if opened["status"]!="ready":
        return
    tag,total=opened["snapshot"]["tag"],opened["snapshot"]["total"]
    index=opened["next"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        context["tag"] = tag
        while time.monotonic() < http.deadline-180:
            retries = post("retries")
            if retries["status"] != "ready":
                return
            if retries["packages"]:
                if not process_batch(pool, post, retries["packages"], retries["file"], retry=True):
                    return
            if index >= total:
                if retries["packages"]:
                    continue
                return
            ids = get(BASE+f"play_sitemaps_{tag}-{index:05}-of-{total}.xml.gz", packages)
            for offset in range(0, len(ids), 40):
                if not process_batch(pool, post, ids[offset:offset+40], index):
                    return
            done = post("done", file=index)
            if done["status"] != "done":
                return
            index = done["next"]


if __name__=="__main__":
    collect(int(os.environ["SLOT"]))
