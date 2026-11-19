"""Refresh ratings and daily install observations for saved Android games."""
import argparse
import os
import random
import time
import uuid

from apkpure.admin_mobile import Http, METADATA, SyncError, WebsiteUnavailable, catalog_credential, fetch_play_metrics


class CheckpointPending(Exception):
    """The retry budget ended; the host still owns the unfinished batch."""


def retryable(error):
    # Transport, temporary HTTP failures, invalid JSON and browser challenges
    # share this type. Authentication and invalid receipts remain hard errors.
    return isinstance(error, WebsiteUnavailable)


def post_checkpoint(http, credential, request, deadline):
    # Keep the exact claim/payload for retries, including a lost submit receipt.
    deadline = min(deadline, time.monotonic() + 60)
    attempt = 0
    while time.monotonic() < deadline - 2:
        try:
            return http.website(METADATA, request, credential, metadata=True,
                                timeout=min(35, deadline - time.monotonic()), attempts=1,
                                retry_seconds=max(0, deadline - time.monotonic()))
        except SyncError as error:
            if not retryable(error):
                raise
            attempt += 1
            delay = min(20, 2 ** min(attempt, 4)) + random.uniform(0, 2)
            print(f"::warning::Host {request['action']} retry {attempt}: {error}. Checkpoint retained.", flush=True)
            time.sleep(max(0, min(delay, deadline - time.monotonic())))
    raise CheckpointPending("Host request remains pending; saved progress will resume next run.")


def read_observations(http, packages, deadline):
    results = {}
    for attempt in range(3):
        missing = [pkg for pkg in packages if pkg not in results]
        if not missing or time.monotonic() >= deadline - 35:
            break
        try:
            results.update(fetch_play_metrics(http, missing))
        except SyncError as error:
            print(f"::warning::Google Play reading failed: {error}", flush=True)
        if any(pkg not in results for pkg in packages) and attempt < 2:
            time.sleep(min(2 ** attempt + random.uniform(0, 1), max(0, deadline - time.monotonic())))
    failed = len(packages) - len(results)
    if failed:
        print(f"::warning::{failed} readings skipped after retries; recorded as failed, not removed or zero installs.", flush=True)
    return results


def collect(slot, smoke):
    run = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    credential = catalog_credential(os.environ["STEAM_SYNC_TOKEN"])
    seconds = 90 if smoke else 5 * 3600 + 40 * 60
    http = Http(seconds + 60)
    deadline = time.monotonic() + seconds

    def post(action, claim, until=None, **data):
        request = dict(scope="daily_play_history", run_id=run, slot=slot,
                       claim_id=claim, action=action, **data)
        return post_checkpoint(http, credential, request, deadline if until is None else until)

    batches = checked = points = ratings = failed = 0
    try:
        while time.monotonic() < deadline - 40:
            claim = uuid.uuid4().hex
            batch = post("claim", claim, force=os.environ.get("MODE") == "force")
            if batch["status"] in ("idle", "paused"):
                break
            packages = batch["packages"]
            results = read_observations(http, packages, deadline)
            receipt = post("submit", claim, results=results)
            if receipt["status"] == "paused":
                break
            batches += 1
            checked += len(packages)
            failed += len(packages) - len(results)
            points += receipt["points"]
            ratings += receipt["ratings"]
            if smoke or batches % 30 == 0:
                print(f"slot={slot} batches={batches} checked={checked} observations={points} ratings={ratings} failed_readings={failed}", flush=True)
            if smoke:
                break
    except CheckpointPending as error:
        print(f"::warning::{error}", flush=True)
    finally:
        # Cleanup is best-effort. It must never turn completed work into failure
        # or replace the original error. The host retains any unsubmitted batch.
        try:
            post("stop", uuid.uuid4().hex, until=min(http.deadline - 1, time.monotonic() + 20))
        except (SyncError, CheckpointPending) as error:
            print(f"::warning::Stop notification not acknowledged: {error}. Saved data is retained.", flush=True)
    print(f"Saved checkpoint. Slot {slot} checked {checked} packages in this run; {failed} readings failed.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, type=int, choices=range(60))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    collect(args.slot, args.smoke)
