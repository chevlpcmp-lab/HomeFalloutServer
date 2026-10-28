"""Local Arr API filter for Cleanuparr's destructive queue operations.

Fail closed on missing client/history data. Never expose pending/imported items.
Only Cleanuparr uses these ports; existing Arr integrations stay on real ports.
"""
import json
from contextlib import closing
import logging
import re
import sqlite3
import time
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger('queue-guard')
APPS = {'radarr': 7878, 'sonarr': 8989}
# Plain substrings, deliberately excludes space/permissions/temporary I/O errors.
FAILED_PATTERNS = ('no files found are eligible for import',)
QUEUE_FLAGS = {
    'radarr': '&includeUnknownMovieItems=true&includeMovie=true',
    'sonarr': '&includeUnknownSeriesItems=true&includeSeries=true&includeEpisode=true',
}


def request_json(url, key=None):
    headers = {'X-Api-Key': key} if key else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return json.load(r)


def arr_key(app):
    return ET.parse('/config/' + app + '/config.xml').findtext('ApiKey')


def terminal(record):
    # Require an Arr whole-download verdict. A rejected sample/language on one
    # file does not prove that every file in a season pack is unimportable.
    messages = [text.lower().strip() for group in record.get('statusMessages', [])
                for text in [group.get('title', ''), *group.get('messages', [])]]
    return (record.get('trackedDownloadState') in ('importBlocked', 'importFailed')
            and record.get('trackedDownloadStatus', '').lower() == 'warning'
            and any(m.startswith(FAILED_PATTERNS) for m in messages))


def eligible(record, torrent, files, imported):
    """Pure safety policy; thresholds/strikes remain Cleanuparr's responsibility."""
    if imported or not torrent or record.get('trackedDownloadState') == 'importPending':
        return False
    if record.get('protocol') != 'torrent':
        return False
    if files and all(f.get('priority') == 0 for f in files):
        return False  # Cleanuparr otherwise removes these without waiting for strikes.
    state = torrent.get('state', '')
    if state in ('metaDL', 'forcedMetaDL'):
        return (torrent.get('num_seeds') == 0 and torrent.get('num_complete') == 0
                and torrent.get('progress') == 0)
    if not files:
        return False
    if terminal(record):
        return True
    # Completed/seeding torrents are only considered for a terminal, unimported failure.
    if torrent.get('progress', 1) >= 1 or state.lower().endswith('up'):
        return False
    return (record.get('trackedDownloadState') == 'downloading'
            and state in ('downloading', 'forcedDL', 'stalledDL'))


class Ledger:
    """Independent consecutive-observation floor, including across stale Arr strikes."""
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS observations (item TEXT, policy TEXT, since REAL, last REAL, progress REAL, PRIMARY KEY(item, policy))')

    def observe(self, item, active, progress, now=None):
        now = time.time() if now is None else now
        elapsed = {}
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            for policy in ('metadata', 'stall', 'slow', 'failed'):
                row = db.execute('SELECT since,last,progress FROM observations WHERE item=? AND policy=?', (item, policy)).fetchone()
                if not active.get(policy):
                    db.execute('DELETE FROM observations WHERE item=? AND policy=?', (item, policy))
                    continue
                since = row[0] if row else now
                if row and (now < row[1] or now-row[1] > 660 or (policy == 'stall' and progress != row[2])):
                    since = now
                db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)', (item, policy, since, now, progress))
                elapsed[policy] = now-since
        return elapsed


LEDGER = None
POLICY_LOCK = threading.Lock()
LIMITS = {'metadata': 2*3600, 'stall': 6*3600, 'slow': 12*3600, 'failed': 10*60}


def safe(app, record, deleting=False, allow_failed=True):
    download_id = record.get('downloadId', '')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', download_id):
        return False
    query = urllib.parse.urlencode({'hashes': download_id})
    torrents = request_json('http://localhost:8080/api/v2/torrents/info?' + query)
    torrent = next((t for t in torrents if t.get('hash', '').lower() == download_id.lower()), None)
    files = request_json('http://localhost:8080/api/v2/torrents/files?' +
                         urllib.parse.urlencode({'hash': download_id}))
    imported = False
    # Protect partial season-pack imports and imports recorded by either Arr.
    for other, port in APPS.items():
        history = request_json(f'http://localhost:{port}/api/v3/history?' +
                               urllib.parse.urlencode({'downloadId': download_id,
                                   'eventType': 3, 'page': 1, 'pageSize': 1}), arr_key(other))
        if not isinstance(history.get('totalRecords'), int):
            raise ValueError('Missing history count')
        imported |= history['totalRecords'] > 0
    allowed = eligible(record, torrent, files, imported)
    state = torrent.get('state') if torrent else None
    progress = torrent.get('progress', 0) if torrent else 0
    active = {
        'metadata': allowed and state in ('metaDL', 'forcedMetaDL'),
        'stall': allowed and state == 'stalledDL' and torrent.get('dlspeed') == 0,
        'slow': allowed and state in ('downloading', 'forcedDL', 'stalledDL') and torrent.get('dlspeed', 51200) < 51200,
        'failed': allowed and terminal(record) and allow_failed,
    }
    elapsed = LEDGER.observe(app+':'+download_id.lower(), active, progress)
    if deleting:
        allowed = allowed and any(elapsed.get(policy, -1) >= limit for policy, limit in LIMITS.items())
    LOG.info('%s %s state=%s torrent=%s seeds=%s imported=%s allowed=%s elapsed=%s',
             app, record.get('title'), record.get('trackedDownloadState'),
             torrent.get('state') if torrent else None,
             torrent.get('num_seeds') if torrent else None, imported, allowed, {p: int(v) for p,v in elapsed.items()})
    return allowed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log headers, keys, or query strings.

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self):
        with POLICY_LOCK:
            self._handle_api()

    def _handle_api(self):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if self.headers.get('X-Api-Key') != arr_key(app):
                return self.respond(401, {'error': 'Unauthorized'})
            if self.command == 'GET' and path == '/api/v3/queue':
                # Filter the full queue before paginating, so hidden rows never skip later rows.
                records = []
                page = 1
                while True:
                    q = request_json(f'http://localhost:{APPS[app]}/api/v3/queue?page={page}&pageSize=100' + QUEUE_FLAGS[app], arr_key(app))
                    records.extend(q['records'])
                    if len(records) >= q['totalRecords'] or not q['records']:
                        break
                    page += 1
                groups = {}
                for r in records:
                    groups.setdefault(r.get('downloadId', '').lower(), []).append(r)
                checks = []
                for group in groups.values():
                    allow_failed = all(terminal(r) for r in group)
                    checks.extend((r, safe(app, r, allow_failed=allow_failed)) for r in group)
                protected = {r.get('downloadId', '').lower() for r, ok in checks if not ok}
                for download_id in protected:
                    LEDGER.observe(app+':'+download_id, {}, 0)
                allowed = [r for r, ok in checks if ok and r.get('downloadId', '').lower() not in protected]
                params = urllib.parse.parse_qs(parsed.query)
                page = max(1, int(params.get('page', ['1'])[0]))
                size = max(1, min(1000, int(params.get('pageSize', ['100'])[0])))
                q.update(records=allowed[(page-1)*size:page*size], totalRecords=len(allowed), page=page, pageSize=size)
                return self.respond(200, q)
            if self.command == 'DELETE':
                match = re.fullmatch(r'/api/v3/queue/(\d+)', path)
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                expected = {'blocklist': ['true'], 'removeFromClient': ['true'], 'skipRedownload': ['true']}
                # Cleanuparr omits changeCategory when false.
                canonical = dict(params)
                canonical.setdefault('changeCategory', ['false'])
                expected['changeCategory'] = ['false']
                if not match or canonical != expected:
                    return self.respond(403, {'error': 'Only blocklisted client removals allowed'})
                # Re-read all records immediately before deletion; reject if any row in the
                # torrent group is pending/imported/protected, including partial season packs.
                records = []
                page = 1
                while True:
                    q = request_json(f'http://localhost:{APPS[app]}/api/v3/queue?page={page}&pageSize=100' + QUEUE_FLAGS[app], arr_key(app))
                    records.extend(q['records'])
                    if len(records) >= q['totalRecords'] or not q['records']:
                        break
                    page += 1
                record = next((r for r in records if str(r['id']) == match[1]), None)
                group = [r for r in records if record and r.get('downloadId', '').lower() == record.get('downloadId', '').lower()]
                allow_failed = all(terminal(r) for r in group)
                basic = [safe(app, r, allow_failed=allow_failed) for r in group]
                if not all(basic) and record:
                    LEDGER.observe(app+':'+record.get('downloadId', '').lower(), {}, 0)
                if not group or not all(basic) or not all(safe(app, r, deleting=True, allow_failed=allow_failed) for r in group):
                    return self.respond(409, {'error': 'Queue item is protected, changed, or below time threshold'})
                # A completed failed import is removed/blocklisted in Arr only;
                # its torrent and payload keep seeding untouched.
                if terminal(record):
                    params['removeFromClient'] = ['false']
                canonical['removeFromClient'] = params['removeFromClient']
                self.path = path + '?' + urllib.parse.urlencode(canonical, doseq=True)
            elif self.command == 'POST':
                if path != '/api/v3/command':
                    return self.respond(403, {'error': 'Only searches allowed'})
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.respond(413, {'error': 'Invalid command size'})
                body = self.rfile.read(length)
                command = json.loads(body)
                if command.get('name', '').lower() not in ('moviessearch', 'episodesearch', 'seriessearch', 'seasonsearch'):
                    return self.respond(403, {'error': 'Only searches allowed'})
            elif self.command != 'GET' or not path.startswith('/api/v3/'):
                return self.respond(403, {'error': 'Unsupported operation'})
            body = body if self.command == 'POST' else None
            req = urllib.request.Request(f'http://localhost:{APPS[app]}' + self.path,
                    data=body, method=self.command,
                    headers={'X-Api-Key': arr_key(app), 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        except urllib.error.HTTPError as error:
            self.respond(error.code, {'error': 'Upstream request failed'})
        except Exception as error:
            LOG.error('%s request blocked: %s', app, type(error).__name__)
            self.respond(503, {'error': 'Safety checks unavailable; no changes made'})

    do_GET = do_POST = do_DELETE = do_PUT = handle_api


if __name__ == '__main__':
    LEDGER = Ledger(os.environ.get('GUARD_STATE', '/state/queue-guard.sqlite3'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for app, port in [('radarr', 17878), ('sonarr', 18989)]:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        server.app = app
        threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info('Arr safety guards listening on localhost:17878 and localhost:18989')
    threading.Event().wait()
