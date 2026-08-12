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

if (-not (Test-Path -LiteralPath $tfvars)) {
    throw 'Run scripts/prepare-workstation.ps1 first.'
}

if (-not $SkipTerraform) {
    terraform -chdir=$terraformDir init
    terraform -chdir=$terraformDir plan -out homefallout.tfplan
    Write-Host 'Review the plan above. Applying in 10 seconds; press Ctrl+C to stop.'
    Start-Sleep -Seconds 10
    terraform -chdir=$terraformDir apply homefallout.tfplan
}

if (-not $SkipAnsible) {
    $distros = (wsl --list --quiet) -replace "`0", ''
    if ($distros -notcontains 'Ubuntu-24.04') {
        throw 'Ubuntu-24.04 WSL is required. Follow the instruction from prepare-workstation.ps1.'
    }

    $wslRepo = (wsl -d Ubuntu-24.04 -- wslpath -a $repoRoot).Trim()
    $windowsSshKey = Join-Path $env:USERPROFILE '.ssh\homefallout_ed25519'
    if (-not (Test-Path -LiteralPath $windowsSshKey)) {
        throw 'The HomeFallout SSH key is missing. Run prepare-workstation.ps1 first.'
    }
    $wslSshKey = (wsl -d Ubuntu-24.04 -- wslpath -a $windowsSshKey).Trim()
    wsl -d Ubuntu-24.04 -- bash -lc "mkdir -p /root/.ssh && chmod 700 /root/.ssh && install -m 600 '$wslSshKey' /root/.ssh/id_ed25519"
    wsl -d Ubuntu-24.04 -- bash -lc "command -v ansible-playbook >/dev/null || (sudo apt-get update && sudo apt-get install -y ansible-core)"
    # /mnt/c is world-writable from Linux's perspective, so Ansible intentionally ignores
    # ansible.cfg there. Pass the generated inventory explicitly.
    wsl -d Ubuntu-24.04 -- bash -lc "cd '$wslRepo/provisioning/ansible' && ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory.generated.yml playbooks/cluster.yml"
}

if (-not $SkipGitOps) {
    if (-not (Test-Path -LiteralPath $kubeconfig)) {
        throw "Ansible did not produce $kubeconfig"
    }
    & (Join-Path $PSScriptRoot 'bootstrap.ps1') -Kubeconfig $kubeconfig
    kubectl --kubeconfig $kubeconfig wait --for=jsonpath='{.status.phase}'=Active namespace/media namespace/photos --timeout=2m
    kubectl --kubeconfig $kubeconfig apply -f (Join-Path $repoRoot 'platform\secrets\media-secrets.yaml')
    kubectl --kubeconfig $kubeconfig apply -f (Join-Path $repoRoot 'platform\secrets\immich-secrets.yaml')
}

Write-Host ''
Write-Host "Deployment workflow finished. Test access with: kubectl --kubeconfig '$kubeconfig' get nodes -o wide"
