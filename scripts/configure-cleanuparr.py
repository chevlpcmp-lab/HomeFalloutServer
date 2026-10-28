#!/usr/bin/env python3
"""Run inside media-stack's bootstrap container: python3 - [dry-run|enable|status].

Reads existing config.xml/admin secret env in-pod; never emits credentials.
Settings live in config-cleanuparr PVC. Dry-run first, inspect logs, then enable.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

MODE = sys.argv[1] if len(sys.argv) > 1 else 'status'
if MODE not in ('dry-run', 'enable', 'status'):
    raise SystemExit('Expected dry-run, enable, or status')
TOKEN = None


def api(path, data=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if TOKEN:
        headers['Authorization'] = 'Bearer '+TOKEN
    req = urllib.request.Request('http://localhost:11011/api'+path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Never echo an upstream response which could contain submitted secrets.
        raise SystemExit(f'Cleanuparr {method or "GET"} {path}: HTTP {error.code}') from None


credentials = {'username': os.environ['ADMIN_USER'], 'password': os.environ['ADMIN_PASS']}
if not api('/auth/status')['setupCompleted']:
    if MODE == 'status':
        raise SystemExit('Account setup required: run dry-run first')
    api('/auth/setup/account', credentials)
    api('/auth/setup/complete', {})
TOKEN = api('/auth/login', credentials)['tokens']['accessToken']

if MODE == 'dry-run':
    general = api('/configuration/general')
    general['dryRun'] = True
    general['log']['level'] = 'Debug'
    api('/configuration/general', general, 'PUT')
    for app, port in [('sonarr', 18989), ('radarr', 17878)]:
        config = api('/configuration/'+app)
        instance = {'enabled': True, 'name': app.title(), 'url': f'http://localhost:{port}',
                    'apiKey': ET.parse('/config/'+app+'/config.xml').findtext('ApiKey'),
                    'version': 4 if app == 'sonarr' else 3,
                    'externalUrl': 'http://10.0.0.'+('232' if app == 'sonarr' else '231')}
        instances = config['instances']
        if len(instances) > 1:
            raise SystemExit('Multiple Arr instances: review manually')
        if instances:
            api('/configuration/'+app+'/instances/'+instances[0]['id'], instance, 'PUT')
        else:
            api('/configuration/'+app+'/instances', instance)
        api('/configuration/'+app, {'failedImportMaxStrikes': -1}, 'PUT')
    clients = api('/configuration/download_client')['clients']
    if not clients:
        api('/configuration/download_client', {'enabled': True, 'name': 'qBittorrent',
            'typeName': 'qBittorrent', 'host': 'http://localhost:8080',
            **credentials, 'externalUrl': 'http://10.0.0.234'})
    elif len(clients) != 1 or clients[0]['host'].rstrip('/') != 'http://localhost:8080':
        raise SystemExit('Unexpected download clients: review manually')
    # Fresh-install helper owns its two rules. Refuse to silently remove custom rules.
    for kind, strikes in [('stall', 72), ('slow', 144)]:
        rules = api('/queue-rules/'+kind)
        name = 'Stalled 6h' if kind == 'stall' else 'Below 50KB per second for 12h'
        if any(r['name'] != name for r in rules):
            raise SystemExit('Unexpected queue rules: review manually')
        rule = {'name': name, 'enabled': True, 'maxStrikes': strikes, 'privacyType': 'Both',
                'minCompletionPercentage': 0, 'maxCompletionPercentage': 99,
                'deletePrivateTorrentsFromClient': True, 'changeCategory': False,
                'resetStrikesOnProgress': True}
        if kind == 'stall':
            rule['minimumProgress'] = None
        else:
            rule.update(minSpeed='50KB', maxTimeHours=0, ignoreAboveSize=None, ignoreWhileAltSpeedActive=True)
        api('/queue-rules/'+kind+('/'+rules[0]['id'] if rules else ''), rule, 'PUT' if rules else 'POST')
    api('/configuration/download_cleaner', {'enabled': False, 'cronExpression': '0 0 * * * ?',
        'useAdvancedScheduling': True, 'ignoredDownloads': []}, 'PUT')
    queue = {'enabled': True, 'cronExpression': '0 0/5 * * * ?', 'useAdvancedScheduling': True,
        'failedImport': {'maxStrikes': 3, 'ignorePrivate': False, 'deletePrivate': True,
            'skipIfNotFoundInClient': True, 'patterns': ['No files found are eligible for import'],
            'patternMode': 'Include', 'changeCategory': False, 'forceImport': False, 'forceImportMaxTries': 3},
        'downloadingMetadataMaxStrikes': 24, 'errored': {'maxStrikes': 0, 'ignorePrivate': True,
            'deletePrivate': False, 'changeCategory': False}, 'processNoContentId': False, 'ignoredDownloads': []}
    api('/configuration/queue_cleaner', queue, 'PUT')
    api('/Jobs/QueueCleaner/trigger', {})
    print('Dry-run configured and QueueCleaner triggered; inspect cleaner and guard logs before enabling.')
elif MODE == 'enable':
    # Require guarded URLs and enabled queue rules; refuse unsafe direct Arr endpoints.
    for app, port in [('sonarr', 18989), ('radarr', 17878)]:
        instances = api('/configuration/'+app)['instances']
        if len(instances) != 1 or instances[0]['url'].rstrip('/') != f'http://localhost:{port}':
            raise SystemExit('Refusing enable: Arr instance is not behind queue guard')
    if not api('/configuration/queue_cleaner')['enabled']:
        raise SystemExit('Run dry-run configuration first')
    if api('/configuration/download_cleaner')['enabled']:
        raise SystemExit('Refusing enable: Download Cleaner must be disabled')
    general = api('/configuration/general')
    general['dryRun'] = False
    general['log']['level'] = 'Debug'
    api('/configuration/general', general, 'PUT')
    print('Live cleanup enabled. Dry-run strikes are discarded; real timing starts conservatively.')

print(json.dumps({'dryRun': api('/configuration/general')['dryRun'],
    'queueCleaner': api('/configuration/queue_cleaner'),
    'downloadCleanerEnabled': api('/configuration/download_cleaner')['enabled'],
    'stallRules': api('/queue-rules/stall'), 'slowRules': api('/queue-rules/slow')}, indent=2))
