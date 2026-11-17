"""Refresh ratings and daily install observations for saved Android games."""
import argparse
import os
import time
import uuid

from apkpure.admin_mobile import Http, METADATA, SyncError, catalog_credential, fetch_play_metrics


def collect(slot, smoke):
    run = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    credential = catalog_credential(os.environ["STEAM_SYNC_TOKEN"])
    seconds = 90 if smoke else 5 * 3600 + 40 * 60
    http = Http(seconds + 60)
    deadline = time.monotonic() + seconds

    def post(action, claim, **data):
        request = dict(scope="daily_play_history", run_id=run, slot=slot,
                       claim_id=claim, action=action, **data)
        return http.website(METADATA, request, credential, metadata=True, timeout=35)

    batches = checked = points = ratings = 0
    try:
        while time.monotonic() < deadline:
            claim = uuid.uuid4().hex
            batch = post("claim", claim)
            if batch["status"] in ("idle", "paused"):
                break
            packages = batch["packages"]
            results = fetch_play_metrics(http, packages)
            missing = [pkg for pkg in packages if pkg not in results]
            if missing:
                results.update(fetch_play_metrics(http, missing))
            if not results:
                raise SyncError("Play returned no identifiable results. Batch retained.", "play.google.com")
            receipt = post("submit", claim, results=results)
            if receipt["status"] == "paused":
                break
            batches += 1
            checked += len(packages)
            points += receipt["points"]
            ratings += receipt["ratings"]
            if smoke or batches % 30 == 0:
                print(f"slot={slot} batches={batches} checked={checked} observations={points} ratings={ratings}", flush=True)
            if smoke:
                break
    finally:
        post("stop", uuid.uuid4().hex)
    print(f"Saved checkpoint. Slot {slot} checked {checked} packages in this run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, type=int, choices=range(20))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    collect(args.slot, args.smoke)
