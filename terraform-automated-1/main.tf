terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "project_id" {
  type = string
}

variable "zone" {
  type        = string
  description = "Single GCP zone used for all VMs in this benchmark run"
}

variable "core_counts" {
  type        = list(number)
  description = "Which VM sizes (in vCPUs) to create — comment/uncomment in terraform.tfvars"
}

# ---------------------------------------------------------------------------
# Firewall rules
# ---------------------------------------------------------------------------

resource "google_compute_firewall" "allow_iap_ssh" {
  name          = "allow-iap-ssh-neo4j-benchmark"
  network       = "default"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["neo4j-benchmark"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "allow_ssh_direct" {
  name          = "allow-ssh-direct-neo4j-benchmark"
  network       = "default"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["neo4j-benchmark"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------

locals {
  vms = {
    for pair in flatten([
      for cores in var.core_counts : [
        for variant in ["baseline", "sev"] : {
          key          = "${cores}-${variant}"
          name         = "neo4j-${variant}-${cores}"
          cores        = cores
          confidential = variant == "sev"
        }
      ]
    ]) : pair.key => pair
  }
}

resource "google_compute_instance" "vm" {
  for_each = local.vms

  name         = each.value.name
  zone         = var.zone
  machine_type = "n2d-standard-${each.value.cores}"

  min_cpu_platform = "AMD Milan"
  tags             = ["neo4j-benchmark"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 100
      type  = "pd-ssd"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  dynamic "confidential_instance_config" {
    for_each = each.value.confidential ? [1] : []
    content {
      enable_confidential_compute = true
      confidential_instance_type  = "SEV_SNP"
    }
  }

  scheduling {
    on_host_maintenance = each.value.confidential ? "TERMINATE" : "MIGRATE"
    automatic_restart   = true
  }

  metadata = {
    enable-oslogin = "TRUE"

    startup-script = templatefile("${path.module}/startup-script.tpl", {
      cores   = each.value.cores
      variant = each.value.confidential ? "sev" : "baseline"
    })

    # Bash scripts stored as metadata keys — fetched by startup script
    run-benchmark-script = file("${path.module}/run-benchmark.sh")
    setup-and-run-script = file("${path.module}/setup-and-run.sh")
  }

  labels = {
    benchmark = "ldbc-snb-neo4j"
    cores     = tostring(each.value.cores)
    variant   = each.value.confidential ? "sev" : "baseline"
  }

  allow_stopping_for_update = true
}

# ---------------------------------------------------------------------------
# Outputs — just enough for orchestrate-pair.sh to find each VM's IP.
# Everything else (SSH commands, benchmark commands, etc.) lives in the
# orchestration scripts.
# ---------------------------------------------------------------------------

output "vms" {
  value = {
    for k, vm in google_compute_instance.vm : k => {
      name = vm.name
      zone = vm.zone
      ip   = vm.network_interface[0].access_config[0].nat_ip
    }
  }
}
