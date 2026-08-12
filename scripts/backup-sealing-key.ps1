[CmdletBinding()]
param(
    [string]$Kubeconfig = (Join-Path $PSScriptRoot '..\provisioning\ansible\kubeconfig')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backupPath = Join-Path $repoRoot 'platform\secrets\sealed-secrets-key-backup.yaml'

if (-not (Test-Path -LiteralPath $Kubeconfig)) {
    throw "Kubeconfig not found at $Kubeconfig"
}

$keyYaml = & kubectl --kubeconfig $Kubeconfig get secret -n secrets -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml
if ($LASTEXITCODE -ne 0) {
    throw 'Could not export the Sealed Secrets controller key.'
}

[System.IO.File]::WriteAllLines($backupPath, $keyYaml, [System.Text.UTF8Encoding]::new($false))
Write-Host "Backed up the controller key to $backupPath (gitignored)."
Write-Warning 'Copy this file to an encrypted backup outside the server. Anyone with it can decrypt your sealed secrets.'
