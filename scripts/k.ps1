$kubeconfig = Join-Path $PSScriptRoot '..\provisioning\ansible\kubeconfig'
if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw "Kubeconfig not found at $kubeconfig. Complete scripts/deploy.ps1 first."
}

& kubectl --kubeconfig $kubeconfig @args
exit $LASTEXITCODE
