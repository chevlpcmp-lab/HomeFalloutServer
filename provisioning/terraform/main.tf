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

# Terraform owns the reusable base template. Nodes are full clones so each VM has
# an independent OS disk while sharing the same tested cloud image and hardware model.
resource "proxmox_virtual_environment_vm" "ubuntu_template" {
  name      = var.template_name
  node_name = var.cloud_image_node
  vm_id     = var.template_vmid
  template  = true
  started   = false
  on_boot   = false

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 1024
  }

  disk {
    datastore_id = var.template_datastore
    import_from  = proxmox_download_file.ubuntu_cloud_image.id
    interface    = "scsi0"
    size         = 4
    file_format  = "qcow2"
    discard      = "on"
    iothread     = true
    ssd          = true
  }

  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  scsi_hardware = "virtio-scsi-single"

  serial_device {
    device = "socket"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "proxmox_virtual_environment_vm" "k3s" {
  for_each = var.nodes

  name        = each.key
  node_name   = each.value.proxmox_node
  vm_id       = each.value.vmid
  description = each.value.description
  tags        = ["k3s", "terraform", each.value.pool]

  clone {
    vm_id        = proxmox_virtual_environment_vm.ubuntu_template.vm_id
    node_name    = proxmox_virtual_environment_vm.ubuntu_template.node_name
    datastore_id = each.value.datastore
    full         = true
  }

  cpu {
    cores = each.value.cores
    type  = "host"
  }

  memory {
    dedicated = each.value.memory_mb
  }

  disk {
    datastore_id = each.value.datastore
    interface    = "scsi0"
    size         = each.value.disk_gb
    file_format  = "qcow2"
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
      file_format  = "raw"
      discard      = "on"
      iothread     = true
      ssd          = true
      backup       = false
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
    interface    = "ide2"
    file_format  = "qcow2"

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
      network_device,
      clone,
    ]
  }
}
