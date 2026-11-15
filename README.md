# Steam catalog collector

Own anonymous Steam PICS collector. It is independent from the Mobile/APKPure updater.

The website privately stores each batch and its checkpoint in one SQLite transaction. A new runner loads that checkpoint, so no catalog needs to be bundled with the repository. Failed transport batches are queued separately; unknown/unnamed IDs are compressed audit data, never games or success counts. There is no empty-gap stopping rule and no target game count. Scan IDs advance to the uint32 AppID bound (4,294,967,295); this is not a claim every ID exists, or that complete access is possible. A 25-minute run budget allows clean resumptions; hourly scheduled runs continue independently of visitors.

Steam changes are queued before the change cursor advances. Expired change history resets the scan for reconciliation. The catalog includes all visible product types internally, while the website Windows view selects games supporting Windows. Release timestamps come from PICS with public StoreBrowse fallback. Original-release timestamps remain separate. Missing dates remain missing; private/token-restricted records can be incomplete.

## Production

Configure `STEAM_SYNC_TOKEN` as a GitHub Actions secret. The corresponding server credential is private outside the web root. Upload scope is restricted to the separate Steam catalog endpoint. GitHub concurrency and a server-side lease prevent duplicate workers.

## Local bounded check

```
pip install -r requirements.txt
python collect.py --local test-state --seconds 60 --scan-to 5000
```

Default production has no artificial scan ceiling. `--scan-to` is only an explicit bounded test option. Local test outputs are excluded from Git.

PICS algorithm reference: https://github.com/Austrum-lab/steam-appdb . Steam client library: https://github.com/ValvePython/steam .
