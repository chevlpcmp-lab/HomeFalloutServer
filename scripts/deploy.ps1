[CmdletBinding()]
param(
    [switch]$SkipTerraform,
    [switch]$SkipAnsible,
    [switch]$SkipGitOps
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$terraformDir = Join-Path $repoRoot 'provisioning\terraform'
$tfvars = Join-Path $terraformDir 'terraform.tfvars'
$kubeconfig = Join-Path $repoRoot 'provisioning\ansible\kubeconfig'

function Wait-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$ComputerName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 300
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connection = $client.ConnectAsync($ComputerName, $Port)
            if ($connection.Wait(2000) -and $client.Connected) { return }
        } catch {
            # The VM is still booting; retry until the deadline.
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for ${ComputerName}:$Port"
}

function Wait-KubernetesResource {
    param(
        [Parameter(Mandatory = $true)][string]$Kubeconfig,
        [Parameter(Mandatory = $true)][string]$Resource,
        [int]$TimeoutSeconds = 300
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        & kubectl --kubeconfig $Kubeconfig get $Resource -o name 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for Kubernetes resource $Resource"
}

if (-not (Test-Path -LiteralPath $tfvars)) {
    throw 'Run scripts/prepare-workstation.ps1 first.'
}

if (-not $SkipTerraform) {
    $terraformCommand = Get-Command terraform -ErrorAction SilentlyContinue
    if ($terraformCommand) {
        $terraform = $terraformCommand.Source
    } else {
        $wingetTerraform = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Hashicorp.Terraform_*" -Filter terraform.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $wingetTerraform) { throw 'Terraform is missing. Run scripts/prepare-workstation.ps1 first.' }
        $terraform = $wingetTerraform.FullName
    }

    Push-Location $terraformDir
    try {
        & $terraform init '-input=false'
        if ($LASTEXITCODE -ne 0) { throw 'terraform init failed.' }
        & $terraform plan '-input=false' '-out=homefallout.tfplan'
        if ($LASTEXITCODE -ne 0) { throw 'terraform plan failed.' }
        Write-Host 'Review the plan above. Applying in 10 seconds; press Ctrl+C to stop.'
        Start-Sleep -Seconds 10
        & $terraform apply '-input=false' 'homefallout.tfplan'
        if ($LASTEXITCODE -ne 0) { throw 'terraform apply failed.' }
    } finally {
        Pop-Location
    }
}

if (-not $SkipAnsible) {
    $clusterIps = @('10.0.0.10', '10.0.0.11', '10.0.0.12')
    foreach ($clusterIp in $clusterIps) {
        Write-Host "Waiting for cloud-init to bring SSH online at $clusterIp..."
        Wait-TcpPort -ComputerName $clusterIp -Port 22 -TimeoutSeconds 600
    }

    $distros = (wsl --list --quiet) -replace "`0", ''
    if ($distros -notcontains 'Ubuntu-24.04') {
        throw 'Ubuntu-24.04 WSL is required. Follow the instruction from prepare-workstation.ps1.'
    }

    # wsl.exe passes backslashes through a Linux shell where they are escape characters.
    # Forward slashes preserve Windows drive paths for wslpath on PowerShell 5.1.
    $repoRootForWsl = $repoRoot.Replace('\', '/')
    $wslRepo = (wsl -d Ubuntu-24.04 -- wslpath -a $repoRootForWsl | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($wslRepo)) {
        throw "Could not translate repository path $repoRoot into WSL."
    }
    $windowsSshKey = Join-Path $env:USERPROFILE '.ssh\homefallout_ed25519'
    if (-not (Test-Path -LiteralPath $windowsSshKey)) {
        throw 'The HomeFallout SSH key is missing. Run prepare-workstation.ps1 first.'
    }
    $windowsSshKeyForWsl = $windowsSshKey.Replace('\', '/')
    $wslSshKey = (wsl -d Ubuntu-24.04 -- wslpath -a $windowsSshKeyForWsl | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($wslSshKey)) {
        throw "Could not translate SSH key path $windowsSshKey into WSL."
    }
    wsl -d Ubuntu-24.04 -- bash -lc "mkdir -p /root/.ssh && chmod 700 /root/.ssh && install -m 600 '$wslSshKey' /root/.ssh/id_ed25519"
    wsl -d Ubuntu-24.04 -- bash -lc "command -v ansible-playbook >/dev/null || (sudo apt-get update && sudo apt-get install -y ansible-core)"
    # /mnt/c is world-writable from Linux's perspective, so Ansible intentionally ignores
    # ansible.cfg there. Pass the generated inventory explicitly.
    wsl -d Ubuntu-24.04 -- bash -lc "cd '$wslRepo/provisioning/ansible' && ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory.generated.yml playbooks/cluster.yml"
    if ($LASTEXITCODE -ne 0) { throw 'Ansible cluster configuration failed.' }
}

if (-not $SkipGitOps) {
    if (-not (Test-Path -LiteralPath $kubeconfig)) {
        throw "Ansible did not produce $kubeconfig"
    }
    & (Join-Path $PSScriptRoot 'bootstrap.ps1') -Kubeconfig $kubeconfig
    foreach ($namespace in @('media', 'photos', 'secrets')) {
        Wait-KubernetesResource -Kubeconfig $kubeconfig -Resource "namespace/$namespace" -TimeoutSeconds 300
    }
    kubectl --kubeconfig $kubeconfig wait --for=jsonpath='{.status.phase}'=Active namespace/media namespace/photos namespace/secrets --timeout=5m
    if ($LASTEXITCODE -ne 0) { throw 'Application namespaces did not become active.' }
    Wait-KubernetesResource -Kubeconfig $kubeconfig -Resource 'crd/sealedsecrets.bitnami.com' -TimeoutSeconds 300
    kubectl --kubeconfig $kubeconfig wait --for=condition=Established crd/sealedsecrets.bitnami.com --timeout=5m
    if ($LASTEXITCODE -ne 0) { throw 'The Sealed Secrets CRD did not become established.' }
    kubectl --kubeconfig $kubeconfig rollout status deployment/sealed-secrets-controller -n secrets --timeout=5m
    if ($LASTEXITCODE -ne 0) { throw 'The Sealed Secrets controller did not become ready.' }

    & (Join-Path $PSScriptRoot 'seal-secrets.ps1') -Kubeconfig $kubeconfig
    $sealedSecrets = @(
        (Join-Path $repoRoot 'platform\components\media-stack\resources\sealed-secret-gluetun-vpn.yaml'),
        (Join-Path $repoRoot 'platform\components\homarr\resources\sealed-secret-homarr-secrets.yaml'),
        (Join-Path $repoRoot 'platform\components\immich\resources\sealed-secret-immich-database.yaml')
    )
    foreach ($sealedSecret in $sealedSecrets) {
        kubectl --kubeconfig $kubeconfig apply -f $sealedSecret
        if ($LASTEXITCODE -ne 0) { throw "Failed to apply $sealedSecret" }
    }
    & (Join-Path $PSScriptRoot 'backup-sealing-key.ps1') -Kubeconfig $kubeconfig
}

Write-Host ''
Write-Host "Deployment workflow finished. Test access with: kubectl --kubeconfig '$kubeconfig' get nodes -o wide"
Write-Host 'Commit and push the generated sealed-secret-*.yaml files so Argo CD owns them permanently.'
