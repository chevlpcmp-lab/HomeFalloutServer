#!/usr/bin/env python3
"""Render the guard ConfigMap and pod checksum after changing queue_guard.py."""
from pathlib import Path
import hashlib
import re
root = Path(__file__).resolve().parents[1] / 'platform/components/media-stack'
source = (root / 'cleanuparr/queue_guard.py').read_text()
(root / 'resources/cleanuparr-guard-configmap.yaml').write_text(
    'apiVersion: v1\nkind: ConfigMap\nmetadata: { name: cleanuparr-queue-guard, namespace: media }\ndata:\n  queue_guard.py: |\n' +
    ''.join('    '+line+'\n' for line in source.splitlines()))
manifest = root / 'resources/media-stack.yaml'
manifest.write_text(re.sub(r'(checksum/cleanuparr-guard: )[a-f0-9]+',
    lambda m: m[1]+hashlib.sha256(source.encode()).hexdigest(), manifest.read_text()))
