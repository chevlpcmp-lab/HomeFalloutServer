[CmdletBinding()]
param(
    [string]$Kubeconfig = (Join-Path $PSScriptRoot '..\provisioning\ansible\kubeconfig')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$rootApp = Join-Path $repoRoot 'bootstrap\root-app.yaml'
$repositorySecret = Join-Path $repoRoot 'platform\secrets\argocd-repository.yaml'

if (-not (Test-Path -LiteralPath $Kubeconfig)) {
    throw "Kubeconfig not found at $Kubeconfig. Run the Ansible cluster playbook first."
}

$kubectlArgs = @('--kubeconfig', $Kubeconfig)
& kubectl @kubectlArgs create namespace argocd --dry-run=client -o yaml | & kubectl @kubectlArgs apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Argo CD namespace.' }
# Argo CD's large ApplicationSet CRD exceeds the client-side apply annotation
# limit. Server-side apply stores field ownership without that annotation.
& kubectl @kubectlArgs apply --server-side --force-conflicts -n argocd -f 'https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml'
if ($LASTEXITCODE -ne 0) { throw 'Failed to install Argo CD.' }
& kubectl @kubectlArgs rollout status deployment/argocd-server -n argocd --timeout=5m
if ($LASTEXITCODE -ne 0) { throw 'Argo CD server did not become ready.' }
if (-not (Test-Path -LiteralPath $repositorySecret)) {
    throw "Argo CD repository credential not found at $repositorySecret. Run prepare-workstation.ps1."
}
& kubectl @kubectlArgs apply -f $repositorySecret
if ($LASTEXITCODE -ne 0) { throw 'Failed to configure the Argo CD repository credential.' }
& kubectl @kubectlArgs apply -f $rootApp
if ($LASTEXITCODE -ne 0) { throw 'Failed to apply the root Argo CD Application.' }

Write-Host 'Argo CD is installed and the root Application is reconciling.'
