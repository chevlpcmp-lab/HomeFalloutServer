[CmdletBinding()]
param(
    [string]$Kubeconfig = (Join-Path $PSScriptRoot '..\provisioning\ansible\kubeconfig')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$localKubeseal = Join-Path $repoRoot '.tools\kubeseal.exe'
$kubesealCommand = Get-Command kubeseal -ErrorAction SilentlyContinue
if ($kubesealCommand) {
    $kubeseal = $kubesealCommand.Source
} elseif (Test-Path -LiteralPath $localKubeseal) {
    $kubeseal = $localKubeseal
} else {
    throw 'kubeseal is missing. Run scripts/prepare-workstation.ps1 first.'
}

if (-not (Test-Path -LiteralPath $Kubeconfig)) {
    throw "Kubeconfig not found at $Kubeconfig"
}

$secretSources = @(
    (Join-Path $repoRoot 'platform\secrets\media-secrets.yaml'),
    (Join-Path $repoRoot 'platform\secrets\immich-secrets.yaml')
)
$secretDestinations = @{
    'gluetun-vpn' = Join-Path $repoRoot 'platform\components\media-stack\resources'
    'homarr-secrets' = Join-Path $repoRoot 'platform\components\homarr\resources'
    'immich-database' = Join-Path $repoRoot 'platform\components\immich\resources'
}

foreach ($secretSource in $secretSources) {
    if (-not (Test-Path -LiteralPath $secretSource)) {
        throw "Plaintext source secret not found at $secretSource. Run prepare-workstation.ps1 first."
    }

    $documents = (Get-Content -LiteralPath $secretSource -Raw) -split '(?m)^\s*---\s*$'
    foreach ($document in $documents) {
        if ([string]::IsNullOrWhiteSpace($document)) { continue }

        $metadata = [regex]::Match($document, '(?ms)^metadata:\s*.*?^\s{2}name:\s*["'']?([^\s"'']+)')
        if (-not $metadata.Success) {
            throw "Could not find metadata.name in $secretSource."
        }

        $secretName = $metadata.Groups[1].Value
        if (-not $secretDestinations.ContainsKey($secretName)) {
            throw "No component destination is configured for Secret $secretName."
        }
        $destination = Join-Path $secretDestinations[$secretName] "sealed-secret-$secretName.yaml"
        $temporarySecret = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($temporarySecret, $document, [System.Text.UTF8Encoding]::new($false))
            & $kubeseal '--format=yaml' '--controller-name=sealed-secrets-controller' '--controller-namespace=secrets' "--kubeconfig=$Kubeconfig" "--secret-file=$temporarySecret" "--sealed-secret-file=$destination"
            if ($LASTEXITCODE -ne 0) {
                throw "kubeseal failed for Secret $secretName."
            }
            Write-Host "Created $destination"
        } finally {
            Remove-Item -LiteralPath $temporarySecret -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ''
Write-Host 'The generated SealedSecret YAML is encrypted and safe to commit.'
Write-Host 'Commit and push it so Argo CD remains the source of truth.'
