terraform {
  required_version = ">= 1.9"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.99"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

