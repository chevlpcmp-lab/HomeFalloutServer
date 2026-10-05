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

# Phase 9 capacity guard: keep Argo CD on the apps worker and reserve realistic
# memory so Kubernetes cannot consume EC's host-service envelope.
$argoResources = @(
    @('statefulset/argocd-application-controller', 'argocd-application-controller', 'cpu=20m,memory=256Mi', 'cpu=1000m,memory=512Mi'),
    @('deployment/argocd-repo-server', 'argocd-repo-server', 'cpu=10m,memory=96Mi', 'cpu=1000m,memory=512Mi'),
    @('deployment/argocd-server', 'argocd-server', 'cpu=10m,memory=64Mi', 'cpu=500m,memory=256Mi'),
    @('deployment/argocd-applicationset-controller', 'argocd-applicationset-controller', 'cpu=10m,memory=64Mi', 'cpu=500m,memory=256Mi'),
    @('deployment/argocd-dex-server', 'dex', 'cpu=10m,memory=64Mi', 'cpu=250m,memory=128Mi'),
    @('deployment/argocd-notifications-controller', 'argocd-notifications-controller', 'cpu=10m,memory=64Mi', 'cpu=250m,memory=128Mi'),
    @('deployment/argocd-redis', 'redis', 'cpu=10m,memory=32Mi', 'cpu=250m,memory=128Mi')
)
foreach ($resource in $argoResources) {
    & kubectl @kubectlArgs set resources $resource[0] -n argocd --containers=$resource[1] --requests=$resource[2] --limits=$resource[3]
    if ($LASTEXITCODE -ne 0) { throw "Failed to size $($resource[0])." }
    & kubectl @kubectlArgs patch $resource[0] -n argocd --type merge -p '{"spec":{"template":{"spec":{"nodeSelector":{"homelab.charles/pool":"apps"}}}}}'
    if ($LASTEXITCODE -ne 0) { throw "Failed to pin $($resource[0]) to the apps worker." }
}
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
