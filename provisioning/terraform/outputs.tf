locals {
  servers = { for name, node in var.nodes : name => node if node.role == "server" }
  agents  = { for name, node in var.nodes : name => node if node.role == "agent" }

  primary_server_ip = sort([for node in local.servers : node.ip])[0]

  host_vars = {
    for name, node in var.nodes : name => {
      ansible_host       = node.ip
      node_pool          = node.pool
      node_zone          = node.proxmox_node
      node_labels        = node.labels
      node_taints        = node.taints
      data_disk_serial   = node.data_disk_gb == null ? "" : node.data_disk_serial
      data_mount_path    = "/mnt/data"
      config_disk_serial = node.config_disk_gb == null ? "" : node.config_disk_serial
      config_mount_path  = "/mnt/config"
    }
  }

  inventory = {
    k3s_cluster = {
      children = {
        server = { hosts = { for name, node in local.servers : name => local.host_vars[name] } }
        agent  = { hosts = { for name, node in local.agents : name => local.host_vars[name] } }
      }
      vars = {
        ansible_user      = var.cloud_init_user
        api_endpoint      = local.primary_server_ip
        primary_server_ip = local.primary_server_ip
        k3s_token         = var.k3s_token
      }
    }
  }
}

resource "local_sensitive_file" "ansible_inventory" {
  content         = yamlencode(local.inventory)
  filename        = "${path.module}/../ansible/inventory.generated.yml"
  file_permission = "0600"
}

output "cluster_summary" {
  value = {
    for name, node in var.nodes : name => {
      vmid                  = node.vmid
      ip                    = node.ip
      role                  = node.role
      pool                  = node.pool
      data_disk_gb          = node.data_disk_gb
      data_disk_datastore   = node.data_disk_datastore
      config_disk_gb        = node.config_disk_gb
      config_disk_datastore = node.config_disk_datastore
    }
  }
}

output "primary_server_ip" {
  value = local.primary_server_ip
}
