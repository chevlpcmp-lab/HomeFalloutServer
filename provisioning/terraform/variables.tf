variable "proxmox_endpoint" {
  description = "Proxmox API endpoint, for example https://10.0.0.2:8006"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox token in user@realm!tokenid=secret format"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Allow Proxmox's self-signed TLS certificate"
  type        = bool
  default     = true
}

variable "cloud_image_node" {
  description = "Proxmox node that downloads the Ubuntu cloud image and hosts the VM"
  type        = string
  default     = "home"
}

variable "cloud_image_datastore" {
  description = "File-based Proxmox storage that accepts import files"
  type        = string
  default     = "local"
}

variable "cloud_image_url" {
  description = "Ubuntu 24.04 LTS cloud image downloaded directly by Proxmox"
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}

variable "template_vmid" {
  description = "VMID of the Terraform-owned Ubuntu cloud-init template"
  type        = number
  default     = 9000
}

variable "template_name" {
  description = "Name of the reusable Ubuntu cloud-init template"
  type        = string
  default     = "ubuntu-2404-cloudinit-template"
}

variable "template_datastore" {
  description = "Directory-backed Proxmox storage for the reusable template"
  type        = string
  default     = "local"
}

variable "qemu_guest_agent_enabled" {
  description = "Set true on the second Terraform apply, after Ansible installs the agent"
  type        = bool
  default     = false
}

variable "network_bridge" {
  type    = string
  default = "vmbr0"
}

variable "network_gateway" {
  type    = string
  default = "10.0.0.1"

  validation {
    condition     = var.network_gateway == "10.0.0.1"
    error_message = "This repository's address plan expects the LAN gateway at 10.0.0.1."
  }
}

variable "nameservers" {
  type    = list(string)
  default = ["10.0.0.1", "1.1.1.1"]
}

variable "cloud_init_user" {
  type    = string
  default = "charles"
}

variable "ssh_public_keys" {
  type = list(string)

  validation {
    condition     = length(var.ssh_public_keys) > 0
    error_message = "At least one SSH public key is required."
  }
}

variable "k3s_token" {
  description = "Cluster token; generate with: openssl rand -base64 48"
  type        = string
  sensitive   = true
}

variable "nodes" {
  description = "k3s VMs keyed by their permanent hostname"
  type = map(object({
    proxmox_node        = string
    vmid                = number
    ip                  = string
    role                = string
    pool                = string
    cores               = number
    memory_mb           = number
    disk_gb             = number
    datastore           = optional(string, "local")
    data_disk_gb        = optional(number)
    data_disk_datastore = optional(string, "local-lvm")
    data_disk_serial    = optional(string, "HOMEFALLOUT_DATA")
    description         = optional(string, "")
    labels              = optional(map(string), {})
    taints              = optional(list(string), [])
  }))

  validation {
    condition     = alltrue([for node in var.nodes : node.disk_gb >= 20])
    error_message = "An OS disk must be at least 20 GB."
  }

  validation {
    condition     = length([for node in var.nodes : node if node.role == "server"]) % 2 == 1
    error_message = "The control-plane count must be odd."
  }

  validation {
    condition     = alltrue([for node in var.nodes : contains(["server", "agent"], node.role)])
    error_message = "Node role must be server or agent."
  }

  validation {
    condition     = alltrue([for node in var.nodes : contains(["control", "media", "apps"], node.pool)])
    error_message = "Node pool must be control, media, or apps."
  }

  validation {
    condition     = alltrue([for node in var.nodes : can(regex("^10\\.0\\.0\\.([1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$", node.ip))])
    error_message = "Every node IP must be a usable address in 10.0.0.0/24."
  }

  validation {
    condition     = length(distinct([for node in var.nodes : node.ip])) == length(var.nodes)
    error_message = "Node IPs must be unique."
  }

  validation {
    condition     = length(distinct([for node in var.nodes : "${node.proxmox_node}/${node.vmid}"])) == length(var.nodes)
    error_message = "VMIDs must be unique on each Proxmox node."
  }

  validation {
    condition     = alltrue([for node in var.nodes : node.data_disk_gb == null || node.data_disk_gb >= 100])
    error_message = "A data disk, when configured, must be at least 100 GB."
  }

}
