# Secret management

Plain Kubernetes Secrets live only under `platform/secrets/`, which is gitignored. Argo CD deploys
the Bitnami Sealed Secrets controller from `platform/components/sealed-secrets/values/`, and the
encrypted `SealedSecret` resources live beside their applications under `resources/`.

## First deployment

`scripts/deploy.ps1` handles the initial sequence:

1. Bootstrap Argo CD and the root Application.
2. Wait for `sealed-secrets-controller` in the `secrets` namespace.
3. Convert the existing plaintext inputs into cluster-bound encrypted manifests.
4. Apply those manifests so the workloads can start.
5. Export the controller's private sealing key to
   `platform/secrets/sealed-secrets-key-backup.yaml`.

Commit only these generated encrypted files:

```text
platform/components/media-stack/resources/sealed-secret-gluetun-vpn.yaml
platform/components/media-stack/resources/sealed-secret-qbittorrent-auth.yaml
platform/components/homarr/resources/sealed-secret-homarr-secrets.yaml
platform/components/immich/resources/sealed-secret-immich-database.yaml
```

Never commit `platform/secrets/*.yaml`, `terraform.tfvars`, or the controller-key backup.

## Updating a secret

Edit or regenerate the gitignored plaintext input, reseal it, and commit the changed ciphertext:

```powershell
.\scripts\seal-secrets.ps1
git add platform/components/*/resources/sealed-secret-*.yaml
git commit -m "Update sealed application secrets"
git push
```

Strict sealing scope is used. A sealed value is bound to its Secret name and namespace, so renaming
or moving it requires resealing.

## Disaster recovery

The controller automatically renews sealing keys, and committed ciphertext cannot be decrypted
after a total cluster loss unless the relevant private key survives. After initial deployment and
occasionally after key renewal, run:

```powershell
.\scripts\backup-sealing-key.ps1
```

Copy the generated backup to an encrypted location outside the Proxmox host. Treat it like a master
password: anyone who gets it can recover the protected values. During a rebuild, restore the key
Secrets before relying on the committed `SealedSecret` files, then restart the controller.
