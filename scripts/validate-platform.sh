#!/usr/bin/env bash
# Render the platform chart and check that every generated Application points at
# repo paths that actually exist.
#
#   ./scripts/validate-platform.sh          # prod
#   ENV=dev ./scripts/validate-platform.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ENVIRONMENT="${ENV:-prod}"
VALUES="platform/values/values-${ENVIRONMENT}.yaml"
OUT="$(mktemp -d)/render.yaml"
fail=0

[[ -f "$VALUES" ]] || { echo "no such values file: $VALUES" >&2; exit 1; }

echo "==> helm template (${ENVIRONMENT})"
helm template platform platform -f "$VALUES" > "$OUT"
apps=$(grep -c '^kind: Application' "$OUT" || true)
projects=$(grep -c '^kind: AppProject' "$OUT" || true)
echo "    ${apps} Applications, ${projects} AppProjects"

echo "==> component directories exist for every values entry"
# Every Application sourcing this repo must point at a path that is actually here.
while read -r path; do
  [[ -z "$path" ]] && continue
  if [[ ! -d "$path" ]]; then
    echo "    MISSING: $path" >&2
    fail=1
  fi
done < <(grep -oE 'path: platform/components/[a-z0-9-]+/[a-z-]+' "$OUT" | awk '{print $2}' | sort -u)

echo "==> chart components have a chart values file"
while read -r vf; do
  [[ -z "$vf" ]] && continue
  vf="${vf#\$values/}"
  if [[ ! -f "$vf" ]]; then
    echo "    MISSING: $vf" >&2
    fail=1
  fi
done < <(grep -oE '\$values/platform/components/[a-z0-9-]+/values/chart-[a-z]+\.yaml' "$OUT" | sort -u)

if command -v kubeconform >/dev/null 2>&1; then
  echo "==> kubeconform (rendered chart)"
  kubeconform -strict -ignore-missing-schemas -summary \
    -skip "Application,AppProject" "$OUT" || fail=1
else
  echo "==> kubeconform not installed, skipping schema validation"
fi

echo
if [[ "$fail" -ne 0 ]]; then
  echo "FAILED"
  exit 1
fi
echo "OK - rendered to $OUT"
