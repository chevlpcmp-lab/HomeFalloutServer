[CmdletBinding()]
param(
    [switch]$GitOpsOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$terraformDir = Join-Path $repoRoot 'provisioning\terraform'
$secretsDir = Join-Path $repoRoot 'platform\secrets'
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$sshKey = Join-Path $sshDir 'homefallout_ed25519'
$argocdKey = Join-Path $sshDir 'homefallout_argocd_ed25519'
$tfvarsPath = Join-Path $terraformDir 'terraform.tfvars'
$toolsDir = Join-Path $repoRoot '.tools'
$kubesealVersion = '0.38.4'
$kubesealPath = Join-Path $toolsDir 'kubeseal.exe'

function New-RandomHex([int]$Bytes) {
    $buffer = [byte[]]::new($Bytes)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    } finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace '-', '').ToLowerInvariant()
}

$terraformCommand = Get-Command terraform -ErrorAction SilentlyContinue
if (-not $terraformCommand) {
    $wingetTerraform = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Hashicorp.Terraform_*" -Filter terraform.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($wingetTerraform) {
        $env:PATH = "$($wingetTerraform.DirectoryName);$env:PATH"
    } else {
        Write-Host 'Installing Terraform with winget...'
        winget install --id Hashicorp.Terraform --exact --accept-package-agreements --accept-source-agreements
        Write-Warning 'If terraform is not found in this terminal, close and reopen PowerShell after this script.'
    }
}

if (-not (Get-Command kubeseal -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $kubesealPath)) {
    Write-Host "Installing kubeseal $kubesealVersion into the repository-local .tools directory..."
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    $kubesealArchive = Join-Path ([System.IO.Path]::GetTempPath()) "kubeseal-$kubesealVersion-windows-amd64.tar.gz"
    try {
        $kubesealUrl = "https://github.com/bitnami/sealed-secrets/releases/download/v$kubesealVersion/kubeseal-$kubesealVersion-windows-amd64.tar.gz"
        try {
            Invoke-WebRequest -Uri $kubesealUrl -OutFile $kubesealArchive -UseBasicParsing
        } catch {
            if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw }
            Write-Warning 'Direct kubeseal download failed; retrying through GitHub CLI.'
            & gh release download "v$kubesealVersion" --repo bitnami/sealed-secrets --pattern "kubeseal-$kubesealVersion-windows-amd64.tar.gz" --output $kubesealArchive --clobber
            if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI could not download kubeseal.' }
        }
        & tar -xzf $kubesealArchive -C $toolsDir kubeseal.exe
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $kubesealPath)) {
            throw 'kubeseal extraction failed.'
        }
    } finally {
        if (Test-Path -LiteralPath $kubesealArchive) {
            Remove-Item -LiteralPath $kubesealArchive -Force
        }
    }
}

New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $sshKey)) {
    & ssh-keygen -t ed25519 -a 64 -f $sshKey -N '""' -C 'charles@homefallout'
}
$publicKey = (Get-Content -Raw "$sshKey.pub").Trim()

if (-not (Test-Path -LiteralPath $argocdKey)) {
    & ssh-keygen -t ed25519 -a 64 -f $argocdKey -N '""' -C 'argocd@homefallout'
}

$argocdRepoSecretPath = Join-Path $secretsDir 'argocd-repository.yaml'
if (-not (Test-Path -LiteralPath $argocdRepoSecretPath)) {
    $existingDeployKeys = gh api repos/chevlpcmp-lab/HomeFalloutServer/keys --jq '.[].title'
    if ($existingDeployKeys -notcontains 'HomeFallout Argo CD') {
        gh repo deploy-key add "$argocdKey.pub" --repo chevlpcmp-lab/HomeFalloutServer --title 'HomeFallout Argo CD'
    }
    $privateKeyLines = Get-Content -LiteralPath $argocdKey
    $indentedPrivateKey = ($privateKeyLines | ForEach-Object { "    $_" }) -join "`n"
    $argocdSecret = @"
apiVersion: v1
kind: Secret
metadata:
  name: homefallout-repository
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  name: HomeFalloutServer
  url: git@github.com:chevlpcmp-lab/HomeFalloutServer.git
  sshPrivateKey: |
$indentedPrivateKey
"@
    Set-Content -LiteralPath $argocdRepoSecretPath -Value $argocdSecret -Encoding UTF8
    Write-Host "Created $argocdRepoSecretPath (gitignored)."
}

if ($GitOpsOnly) {
    Write-Host 'Argo CD repository access is prepared.'
    exit 0
}

if (-not (Test-Path -LiteralPath $tfvarsPath)) {
    $apiToken = Read-Host 'Paste the Proxmox API token (terraform@pve!homefallout=secret)'
    if ($apiToken -notmatch '^terraform@pve!homefallout=.+$') {
        throw 'The token must use terraform@pve!homefallout=secret format.'
    }

    $k3sToken = New-RandomHex 48
    $tfvars = @"
proxmox_endpoint  = "https://10.0.0.254:8006"
proxmox_api_token = "$apiToken"
cloud_image_node  = "home"

ssh_public_keys = [
  "$publicKey",
]

k3s_token = "$k3sToken"

nodes = {
  "k3s-home-01" = {
    proxmox_node    = "home"
    vmid            = 230
    ip              = "10.0.0.10"
    role            = "server"
    pool            = "converged"
    cores           = 12
    memory_mb       = 10240
    disk_gb         = 64
    data_disk_gb    = 600
    data_disk_serial = "HOMEFALLOUT_DATA"
    description     = "Single-node k3s: media, Jellyfin, Immich, and GitOps"
    labels          = { "node-role.kubernetes.io/worker" = "true" }
  }
}
"@
    Set-Content -LiteralPath $tfvarsPath -Value $tfvars -Encoding UTF8
    Write-Host "Created $tfvarsPath (gitignored)."
}

$mediaSecretPath = Join-Path $secretsDir 'media-secrets.yaml'
if (-not (Test-Path -LiteralPath $mediaSecretPath)) {
    $vpnProvider = Read-Host 'Gluetun VPN provider name (for example protonvpn or nordvpn)'
    $vpnPrivateKey = Read-Host 'VPN WireGuard private key'
    $vpnCountry = Read-Host 'VPN server country [Canada]'
    if ([string]::IsNullOrWhiteSpace($vpnCountry)) { $vpnCountry = 'Canada' }
    $homarrKey = New-RandomHex 32
    $mediaSecret = @"
apiVersion: v1
kind: Secret
metadata:
  name: gluetun-vpn
  namespace: media
type: Opaque
stringData:
  VPN_SERVICE_PROVIDER: "$vpnProvider"
  VPN_TYPE: "wireguard"
  WIREGUARD_PRIVATE_KEY: "$vpnPrivateKey"
  SERVER_COUNTRIES: "$vpnCountry"
---
apiVersion: v1
kind: Secret
metadata:
  name: homarr-secrets
  namespace: media
type: Opaque
stringData:
  SECRET_ENCRYPTION_KEY: "$homarrKey"
"@
    Set-Content -LiteralPath $mediaSecretPath -Value $mediaSecret -Encoding UTF8
    Write-Host "Created $mediaSecretPath (gitignored)."
}

$immichSecretPath = Join-Path $secretsDir 'immich-secrets.yaml'
if (-not (Test-Path -LiteralPath $immichSecretPath)) {
    $dbPassword = New-RandomHex 32
    $immichSecret = @"
apiVersion: v1
kind: Secret
metadata:
  name: immich-database
  namespace: photos
type: Opaque
stringData:
  DB_USERNAME: "immich"
  DB_PASSWORD: "$dbPassword"
  DB_DATABASE_NAME: "immich"
"@
    Set-Content -LiteralPath $immichSecretPath -Value $immichSecret -Encoding UTF8
    Write-Host "Created $immichSecretPath (gitignored)."
}

$distros = (wsl --list --quiet) -replace "`0", ''
if ($distros -notcontains 'Ubuntu-24.04') {
    Write-Warning 'Ubuntu-24.04 WSL is not installed. Run the following in an Administrator PowerShell, reboot if requested, then launch Ubuntu once to create its Linux user:'
    Write-Host 'wsl --install --distribution Ubuntu-24.04'
} else {
    Write-Host 'Ubuntu-24.04 WSL is installed.'
}

Write-Host ''
Write-Host 'Local credentials are prepared. Plain secrets stay gitignored; only generated SealedSecret files are committed.'
