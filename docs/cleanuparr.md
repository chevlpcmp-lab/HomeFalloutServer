# Cleanuparr queue cleanup

Deployed in namespace `media`, deployment `media-stack`, containers `cleanuparr`
and `cleanuparr-queue-guard`. UI: http://10.0.0.243 (existing media admin login).

Image: `ghcr.io/cleanuparr/cleanuparr:2.10.12@sha256:d0212e4c66bd0522e23c5e5da5d3006bd5ff0e0de1e3ecfcf892e039d55721e2`.
Guard: `python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb`.

Config, credentials, logs, Cleanuparr databases, and the guard's SQLite timing
ledger persist on `config-cleanuparr` (512Mi, `config-local-retain`). No credentials
are stored in Git. The setup helper reads Arr API keys from the existing read-only
config.xml mounts and admin credentials from the bootstrap container's secret env.
Neither new container mounts the library or downloads filesystem.

## Policy

Queue Cleaner checks every five minutes:

| Condition | Cleanuparr threshold | Independent guard floor |
| --- | --- | --- |
| Stalled incomplete torrent | 72 strikes, reset on progress | 6 hours without progress |
| Metadata-stuck magnet | 24 strikes | 2 hours with explicit zero seeds/complete peers |
| Below approximately 50 KB/s | 144 strikes, reset on recovery | 12 hours below 50 KiB/s |
| Terminal failed import | 3 strikes | 10 minutes with a whole-download rejection |

Strikes run on a five-minute schedule, so timing is approximate; the guard prevents
removal before its elapsed-time floor. A gap over 11 minutes, clock rollback,
recovery, pending import, or other protected state resets relevant guard timers.
Downtime does not count as observed bad-download time. Metadata with unknown seed
counts is protected. Stall/slow rules cover 0–99% completion and ignore alternate
speed mode for slow cleanup.

Both stalled and slow removals delete the incomplete torrent through Arr, blocklist
it, and enter Cleanuparr's durable replacement-search queue. Its Seeker processes
replacement requests automatically. Proactive missing/quality-upgrade searches
remain disabled.

Failed imports require `importBlocked`/`importFailed` plus the Arr whole-download
verdict `No files found are eligible for import...` in a status title/message.
This covers rejected language/sample-only releases when Arr declares the whole
download unimportable. A single rejected file in a mixed release does not qualify.
Failed-import cleanup removes/blocklists the Arr queue entry and requests a
replacement, while preserving the original torrent and payload so seeding continues.
Cleanuparr's event text may say it removed the client item; the guard rewrites this
operation to `removeFromClient=false`.

The guard always protects `importPending`, any torrent with an import in either
Arr's history (including partial season packs), completed/seeding torrents without
a terminal unimportable verdict, missing client/history data, and torrents with
all files skipped. Every row for a torrent must be eligible. It rechecks the live
queue, torrent, history, and elapsed time immediately before forwarding a deletion.

Download Cleaner, seeding rules, dead-torrent cleanup, orphan/unlinked cleanup,
Malware Blocker, errored-download cleanup, force-import, and no-content-ID cleanup
are disabled. Existing seeding rules, library, quality profiles, and indexers are
unchanged.

## Operation and threshold changes

Use the Cleanuparr UI for strikes/schedules, logs, dry-run, and connection settings.
Keep Sonarr at `http://localhost:18989` and Radarr at `http://localhost:17878`:
these are the safety guard ports. qBittorrent remains `http://localhost:8080`.
The real Arr ports stay unchanged for all other integrations.

For repeatable setup, pipe `scripts/configure-cleanuparr.py` into the bootstrap
container using an authenticated Kubernetes context:

```sh
kubectl exec -i -n media deploy/media-stack -c bootstrap -- python3 - dry-run < scripts/configure-cleanuparr.py
kubectl logs -n media deploy/media-stack -c cleanuparr --since=10m
kubectl logs -n media deploy/media-stack -c cleanuparr-queue-guard --since=10m
kubectl exec -i -n media deploy/media-stack -c bootstrap -- python3 - enable < scripts/configure-cleanuparr.py
kubectl exec -i -n media deploy/media-stack -c bootstrap -- python3 - status < scripts/configure-cleanuparr.py
```

The helper runs only when explicitly invoked; UI changes persist across restarts.
`dry-run` reapplies the documented baseline. `enable` checks guard URLs before
turning off dry-run. Dry-run strikes are discarded on enabling; guard observations
record real conditions even during dry-run, but deletion still needs real strikes.
Do not reset production strikes or shorten schedules to simulate elapsed time.

To **lower** a threshold, change both its UI strikes and the corresponding `LIMITS`
value in `platform/components/media-stack/cleanuparr/queue_guard.py`. To raise one,
raising UI strikes alone is sufficient; keep both aligned for clarity. Slow speed
has both the UI `minSpeed` and guard's 51200-byte/s check. Update the setup helper
if changing the baseline. After any guard code edit, regenerate its ConfigMap and
pod checksum, then test:

```sh
python3 scripts/render-cleanuparr-guard.py
python3 -m unittest discover -s platform/components/media-stack/cleanuparr -v
```

Commit only these paths and let ArgoCD roll out the checksum change. Preserve the
real-state guards if expanding failed-import messages; do not add broad single-file
language/sample substring rules.

## Deployment verification (2026-10-28)

Dry-run used the real queues without resetting strikes or simulating elapsed time.
Radarr/qBittorrent contained **Black Panther: Wakanda Forever**, in `metaDL` with
zero connected seeds and zero progress. Trackers reported two complete peers, so
the zero-seed metadata policy correctly protected it. It then acquired metadata
and became `stalledDL`; a fresh dry-run processed it and recorded Stalled strike 1
without deletion. The six-hour independent floor still applies.
Sonarr had no queue entries, including no It's Always Sunny episode. Download
Cleaner remained disabled. Live cleanup was enabled after inspecting these logs.
Two-, six-, and twelve-hour removals were not claimed as observed during deployment.

The media deployment is available with all containers ready. ArgoCD synced the
new manifests. Its pre-existing failed `phase9-capacity-stress-v1` Job keeps the
application's aggregate health Degraded; that unrelated stress workload was not
rerun or altered. The root application's existing unrelated manifest drift was
also left alone. Do not rerun that stress job casually: it invokes Jellyfin/library
refresh and broad scans outside this cleanup task.
