# FALLOUT capacity expansion - 2026-09

Live audit on 2026-09-28:

- Proxmox host: i7-14700, 16 GB DDR4-3200, 1 TB NVMe.
- DIMM1: Samsung M378A2G43BB3-CWE 16 GB; DIMM2 is empty.
- Host had about 15 GiB usable RAM, 8 GiB swap with about 4.2 GiB occupied.
- local: ~98.5 GB total / ~21.2 GB free.
- local-lvm: ~855.9 GB total / ~632.2 GB free.
- VM101 personalos: 4 GB RAM, 40 GB root.
- VM220 k3s-cp-01: 2 GB RAM, 20 GB root.
- VM230 k3s-media-01: 5 GB RAM, 48 GB root + 600 GB data disk.
- VM240 k3s-apps-01: 3 GB RAM, 20 GB root; about 2.1 GiB RAM already used and ~8.1 GB root free.
- VM240 already hosts Argo CD, Home Assistant, Homarr, AdGuard, Traefik, Tailscale, MetalLB and supporting controllers.

## Hardware gate

Do not add another large persistent workload to the current 16 GB host. Upgrade to 32 GB first. The lowest-risk observed path is adding a compatible 16 GB DDR4-3200 UDIMM in empty DIMM2, ideally matching M378A2G43BB3-CWE.

## Target after 32 GB

- VM101 personalos: 4 GB
- VM220 control plane: 2 GB
- VM230 media: 8 GB
- VM240 apps: 12 GB
- leave roughly 6 GB for Proxmox/cache/burst headroom

Move VM240 scsi0 from local to local-lvm and expand it to about 64 GB before adding more apps.

## Placement

WINDMILL: interactive desktop/dev; EC Inspector; WinApps on demand. Keep WinApps, GROBID, Stirling, Penpot and historical Phase19 test DBs stopped with restart=no until remote replacements are proven.

FALLOUT/VM240 after upgrade: EC durable control plane/DBOS state; CWI SearXNG + pgvector; DD Stirling; GROBID; Penpot stack. Migrate in that order and validate each stage.

VM101 personalos: keep canonical MOSAIC/PersonalOS service/database role separate.

Studio3070: GPU/creative worker only (inference, SigLIP/UIClip, ComfyUI, OCR/VLM, Blender/rendering). Do not place canonical databases there. Current audit: 16 GB RAM and only ~9 GB free on C:.

## Remote access

From WINDMILL / EC Inspector:
- ssh studio3070
- ssh personalos
- ssh fallout
- ssh proxmox

The fallout and proxmox aliases use ProxyJump personalos. Use these aliases instead of hard-coded addresses.
