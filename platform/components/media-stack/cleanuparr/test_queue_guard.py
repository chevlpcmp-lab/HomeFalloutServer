import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import queue_guard as guard

HASH = 'a'*40
RECORD = {'id': 1, 'downloadId': HASH, 'protocol': 'torrent', 'title': 'test',
          'trackedDownloadState': 'downloading', 'trackedDownloadStatus': 'ok'}
TORRENT = {'hash': HASH, 'state': 'stalledDL', 'progress': .1, 'dlspeed': 0,
           'num_seeds': 0, 'num_complete': 0}
FILES = [{'priority': 1}]


class PolicyTests(unittest.TestCase):
    def check(self, record=None, torrent=None, files=None, imported=False):
        return guard.eligible(record or RECORD, torrent or TORRENT,
                              FILES if files is None else files, imported)

    def test_pending_and_imported_are_protected(self):
        self.assertFalse(self.check({**RECORD, 'trackedDownloadState': 'importPending'}))
        self.assertFalse(self.check(imported=True))

    def test_completed_seeders_protected(self):
        for state in ['uploading', 'stalledUP', 'pausedUP', 'stoppedUP', 'forcedUP']:
            self.assertFalse(self.check(torrent={**TORRENT, 'state': state, 'progress': 1}))

    def test_no_empty_or_all_skipped_files_outside_metadata(self):
        self.assertFalse(self.check(files=[]))
        self.assertFalse(self.check(files=[{'priority': 0}]))

    def test_metadata_requires_explicit_zero_seed_evidence(self):
        torrent = {**TORRENT, 'state': 'metaDL', 'progress': 0}
        self.assertTrue(self.check(torrent=torrent, files=[]))
        for change in [{'num_seeds': 1}, {'num_complete': 1}, {'num_complete': -1}, {'num_complete': None}]:
            self.assertFalse(self.check(torrent={**torrent, **change}, files=[]))

    def test_only_whole_download_import_failure_is_terminal(self):
        failed = {**RECORD, 'trackedDownloadState': 'importBlocked', 'trackedDownloadStatus': 'warning'}
        for message in ['This is a sample file', 'Wrong language', 'Permission denied']:
            self.assertFalse(guard.terminal({**failed, 'statusMessages': [{'messages': [message]}]}))
        self.assertTrue(guard.terminal({**failed, 'statusMessages': [{'title': 'No files found are eligible for import in /downloads/test'}]}))

    def test_ledger_continuity_recovery_gap_and_progress(self):
        with tempfile.TemporaryDirectory() as path:
            ledger = guard.Ledger(path+'/state.db')
            self.assertEqual(ledger.observe(HASH, {'metadata': True}, 0, 0), {'metadata': 0})
            for now in range(300, 7500, 300):
                elapsed = ledger.observe(HASH, {'metadata': True}, 0, now)
            self.assertEqual(elapsed['metadata'], 7200)
            ledger.observe(HASH, {}, 0, 7500)
            self.assertEqual(ledger.observe(HASH, {'metadata': True}, 0, 7800)['metadata'], 0)
            self.assertEqual(ledger.observe(HASH, {'metadata': True}, 0, 9000)['metadata'], 0)
            ledger.observe(HASH, {'stall': True}, .1, 9300)
            self.assertEqual(ledger.observe(HASH, {'stall': True}, .2, 9600)['stall'], 0)
            self.assertEqual(guard.Ledger(path+'/state.db').observe(HASH, {'stall': True}, .2, 9900)['stall'], 300)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        guard.LEDGER = guard.Ledger(self.temp.name+'/state.db')
        self.records = [copy.deepcopy(RECORD)]
        self.torrent = copy.deepcopy(TORRENT)
        self.imported = 0
        self.upstream = []
        def data(url, key=None):
            if '/api/v3/queue?' in url:
                self.assertIn('includeMovie=true', url)
                return {'records': copy.deepcopy(self.records), 'totalRecords': len(self.records)}
            if '/history?' in url:
                return {'totalRecords': self.imported}
            if '/torrents/info?' in url:
                return [self.torrent]
            if '/torrents/files?' in url:
                return FILES
            raise AssertionError(url)
        self.mocks = [patch.object(guard, 'arr_key', return_value='test-key'),
                      patch.object(guard, 'request_json', side_effect=data),
                      patch.object(guard.urllib.request, 'urlopen', side_effect=lambda req, **kw: self.forward(req))]
        for m in self.mocks: m.start()
        self.server = guard.ThreadingHTTPServer(('127.0.0.1', 0), guard.Handler)
        self.server.app = 'radarr'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def forward(self, req):
        self.upstream.append(req)
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return b'{}'
        return Response()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        for m in self.mocks: m.stop()
        self.temp.cleanup()

    def call(self, method, path):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request(method, path, headers={'X-Api-Key': 'test-key'})
        response = conn.getresponse(); result = response.status, json.loads(response.read())
        conn.close(); return result

    def test_pending_sibling_hides_group_and_resets_ledger(self):
        pending = {**RECORD, 'id': 2, 'downloadId': HASH.upper(), 'trackedDownloadState': 'importPending'}
        self.records = [pending, RECORD]
        status, result = self.call('GET', '/api/v3/queue?page=1&pageSize=100')
        self.assertEqual(status, 200); self.assertEqual(result['totalRecords'], 0)
        self.assertEqual(guard.LEDGER.observe('radarr:'+HASH, {'stall': True}, .1)['stall'], 0)

    def test_early_or_protected_delete_never_reaches_upstream(self):
        path = '/api/v3/queue/1?blocklist=true&removeFromClient=true&skipRedownload=true'
        self.assertEqual(self.call('DELETE', path)[0], 409)
        self.records = [{**RECORD, 'trackedDownloadState': 'importPending'}]
        self.assertEqual(self.call('DELETE', path)[0], 409)
        self.imported = 1
        self.assertEqual(self.call('DELETE', path)[0], 409)
        self.assertEqual(self.upstream, [])

    def test_unsafe_query_is_rejected(self):
        for query in ['blocklist=false', 'blocklist=true&removeFromClient=true&skipRedownload=false',
                      'blocklist=true&removeFromClient=true&skipRedownload=true&changeCategory=true']:
            self.assertEqual(self.call('DELETE', '/api/v3/queue/1?'+query)[0], 403)
        self.assertEqual(self.upstream, [])

    def test_completed_terminal_removal_preserves_torrent(self):
        self.records = [{**RECORD, 'trackedDownloadState': 'importBlocked', 'trackedDownloadStatus': 'warning',
                         'statusMessages': [{'title': 'No files found are eligible for import in test'}]}]
        self.torrent = {**TORRENT, 'progress': 1, 'state': 'stalledUP'}
        import time
        now = time.time()
        with guard.closing(guard.sqlite3.connect(guard.LEDGER.path)) as db, db:
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('radarr:'+HASH, 'failed', now-601, now, 1))
        self.assertEqual(self.call('DELETE', '/api/v3/queue/1?blocklist=true&removeFromClient=true&skipRedownload=true')[0], 200)
        self.assertEqual(len(self.upstream), 1)
        self.assertIn('removeFromClient=false', self.upstream[0].full_url)


if __name__ == '__main__':
    unittest.main()
