provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure
}

# The image is downloaded by Proxmox itself, not copied through this laptop.
resource "proxmox_download_file" "ubuntu_cloud_image" {
  content_type = "import"
  datastore_id = var.cloud_image_datastore
  node_name    = var.cloud_image_node
  url          = var.cloud_image_url
  file_name    = "noble-server-cloudimg-amd64.qcow2"
  overwrite    = false
}

resource "proxmox_virtual_environment_vm" "k3s" {
  for_each = var.nodes

  name        = each.key
  node_name   = each.value.proxmox_node
  vm_id       = each.value.vmid
  description = each.value.description
  tags        = ["k3s", "terraform", each.value.pool]

  cpu {
    cores = each.value.cores
    type  = "host"
  }

  memory {
    dedicated = each.value.memory_mb
  }

  disk {
    datastore_id = each.value.datastore
    import_from  = proxmox_download_file.ubuntu_cloud_image.id
    interface    = "scsi0"
    size         = each.value.disk_gb
    discard      = "on"
    iothread     = true
    ssd          = true
  }

  dynamic "disk" {
    for_each = each.value.data_disk_gb == null ? [] : [each.value.data_disk_gb]
    content {
      datastore_id = each.value.data_disk_datastore
      interface    = "scsi1"
      size         = disk.value
      serial       = each.value.data_disk_serial
      discard      = "on"
      iothread     = true
      ssd          = true
      backup       = false
    }
  }

  # Application databases and configuration live on Proxmox's directory-backed `local`
  # storage. Bulk media and photos stay isolated on the `local-lvm` data disk above.
  dynamic "disk" {
    for_each = each.value.config_disk_gb == null ? [] : [each.value.config_disk_gb]
    content {
      datastore_id = each.value.config_disk_datastore
      interface    = "scsi2"
      size         = disk.value
      serial       = each.value.config_disk_serial
      discard      = "on"
      iothread     = true
      ssd          = true
      backup       = true
    }
  }

  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  scsi_hardware = "virtio-scsi-single"

  # Required by current Ubuntu cloud images when their imported disk is enlarged.
  serial_device {
    device = "socket"
  }

  initialization {
    datastore_id = each.value.datastore

    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = var.nameservers
    }

    user_account {
      username = var.cloud_init_user
      keys     = var.ssh_public_keys
    }
  }

  agent {
    # Enable on a later apply after Ansible has installed qemu-guest-agent.
    enabled = var.qemu_guest_agent_enabled
  }

  on_boot         = true
  started         = true
  stop_on_destroy = true

  lifecycle {
    prevent_destroy = true

    # Proxmox and cloud-init normalize these fields after first boot.
    ignore_changes = [
      initialization,
      network_device,
    ]
  }
}
