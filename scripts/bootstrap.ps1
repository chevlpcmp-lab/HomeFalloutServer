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
& kubectl @kubectlArgs apply -n argocd -f 'https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml'
& kubectl @kubectlArgs rollout status deployment/argocd-server -n argocd --timeout=5m
if (-not (Test-Path -LiteralPath $repositorySecret)) {
    throw "Argo CD repository credential not found at $repositorySecret. Run prepare-workstation.ps1."
}
& kubectl @kubectlArgs apply -f $repositorySecret
& kubectl @kubectlArgs apply -f $rootApp

Write-Host 'Argo CD is installed and the root Application is reconciling.'
