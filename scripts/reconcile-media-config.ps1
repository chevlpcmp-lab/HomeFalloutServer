[CmdletBinding()]
param(
    [string]$Kubeconfig = (Join-Path $PSScriptRoot '..\provisioning\ansible\kubeconfig'),
    [ValidateRange(60, 1800)]
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Kubeconfig)) {
    throw "Kubeconfig not found at $Kubeconfig. Complete scripts/deploy.ps1 first."
}

$kubectlArgs = @('--kubeconfig', $Kubeconfig)

Write-Host 'Restarting the media stack to force a complete desired-state reconciliation...'
& kubectl @kubectlArgs rollout restart deployment/media-stack -n media
if ($LASTEXITCODE -ne 0) { throw 'Failed to restart the media-stack Deployment.' }

& kubectl @kubectlArgs rollout status deployment/media-stack -n media "--timeout=$($TimeoutSeconds)s"
if ($LASTEXITCODE -ne 0) { throw 'The media-stack Deployment did not become ready.' }

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $pod = & kubectl @kubectlArgs get pods -n media -l app=media-stack `
        -o 'jsonpath={.items[0].metadata.name}'
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pod)) {
        Start-Sleep -Seconds 5
        continue
    }

    $logs = & kubectl @kubectlArgs logs -n media $pod -c bootstrap --tail=300
    if ($LASTEXITCODE -eq 0 -and ($logs -match 'Bootstrap complete!')) {
        if ($logs -match 'Bootstrap reconciliation failed') {
            throw "The bootstrap reported a failed reconciliation. Inspect: .\scripts\k.ps1 logs -n media $pod -c bootstrap"
        }
        Write-Host 'Media application configuration reconciled successfully.'
        & kubectl @kubectlArgs get pod -n media $pod
        exit 0
    }
    Start-Sleep -Seconds 5
} while ((Get-Date) -lt $deadline)

throw "Timed out waiting for the bootstrap to complete. Inspect: .\scripts\k.ps1 logs -n media deploy/media-stack -c bootstrap"
