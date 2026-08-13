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
  "k3s-cp-01" = {
    proxmox_node = "home"
    vmid = 220
    ip = "10.0.0.10"
    role = "server"
    pool = "control"
    cores = 4
    memory_mb = 2048
    disk_gb = 20
    datastore = "local"
    description = "k3s control plane"
    taints = ["node-role.kubernetes.io/control-plane=true:NoSchedule"]
  }
  "k3s-apps-01" = {
    proxmox_node = "home"
    vmid = 240
    ip = "10.0.0.11"
    role = "agent"
    pool = "apps"
    cores = 8
    memory_mb = 3072
    disk_gb = 20
    datastore = "local"
    description = "k3s applications worker"
  }
  "k3s-media-01" = {
    proxmox_node = "home"
    vmid = 230
    ip = "10.0.0.12"
    role = "agent"
    pool = "media"
    cores = 16
    memory_mb = 7168
    disk_gb = 28
    datastore = "local"
    data_disk_gb = 600
    data_disk_datastore = "local-lvm"
    data_disk_serial = "HOMEFALLOUT_DATA"
    description = "k3s media, Jellyfin, and Immich worker"
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
    $qbPassword = New-RandomHex 16
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
# HOMARR_API_KEY and JELLYFIN_API_KEY start empty: the media-stack bootstrap skips
# Homarr provisioning until they are filled in. After the first deployment, create
# the Homarr admin account and an API key (Management > Tools > API) plus a Jellyfin
# API key (Dashboard > API Keys), paste them here, re-run seal-secrets.ps1, and
# restart the media-stack deployment. See docs/configuration.md.
apiVersion: v1
kind: Secret
metadata:
  name: homarr-secrets
  namespace: media
type: Opaque
stringData:
  SECRET_ENCRYPTION_KEY: "$homarrKey"
  HOMARR_API_KEY: ""
  JELLYFIN_API_KEY: ""
---
# The bootstrap sidecar pushes these credentials into qBittorrent's WebUI so LAN
# logins use a known password instead of the random one qBittorrent generates.
apiVersion: v1
kind: Secret
metadata:
  name: qbittorrent-auth
  namespace: media
type: Opaque
stringData:
  WEBUI_USERNAME: "admin"
  WEBUI_PASSWORD: "$qbPassword"
"@
    Set-Content -LiteralPath $mediaSecretPath -Value $mediaSecret -Encoding UTF8
    Write-Host "Created $mediaSecretPath (gitignored)."
}

$tailscaleSecretPath = Join-Path $secretsDir 'tailscale-secrets.yaml'
if (-not (Test-Path -LiteralPath $tailscaleSecretPath)) {
    $tailscaleSecret = @"
# Paste a Tailscale auth key here before enabling the tailscale component.
# Create it in the admin console (https://login.tailscale.com/admin/settings/keys):
# reusable OFF, ephemeral OFF, pre-approved ON if your tailnet uses device approval.
# The key is only used for the first login; identity then lives in the state PVC.
apiVersion: v1
kind: Secret
metadata:
  name: tailscale-auth
  namespace: networking
type: Opaque
stringData:
  TS_AUTHKEY: ""
"@
    Set-Content -LiteralPath $tailscaleSecretPath -Value $tailscaleSecret -Encoding UTF8
    Write-Host "Created $tailscaleSecretPath (gitignored)."
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
