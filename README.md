# ODF Version Pinning: Install a Specific ODF z-stream on OpenShift

> **Testing only.** This tooling is intended for installing a specific ODF z-stream
> version on a **fresh/new cluster** in lab or test environments. It has not been
> validated for production use. Always test on a non-production cluster first.
>
> **Pinning a z-stream release is not officially supported by Red Hat.** If you
> need guidance or encounter issues, raise a support case at
> https://access.redhat.com/support/cases/ for assistance.

## Problem

On a **connected** OpenShift cluster, OLM installs the **latest z-stream** in a channel
regardless of `startingCSV`. Subscribing to `stable-4.21` with
`startingCSV: odf-operator.v4.21.2-rhodf` still installs the channel head (e.g.,
`4.21.8-rhodf`).

OLM's dependency resolver looks at the full channel graph. It treats `startingCSV` as
a "start from here and upgrade" hint, not a version pin.

## Solution: Pruned File-Based Catalog (FBC)

Instead of fighting OLM's resolution logic, **remove the newer versions from its view**.

OpenShift operators are discovered through **CatalogSource** resources that point to
container images containing a File-Based Catalog (FBC): a JSON file listing every
operator, channel, and bundle version available.

This repo provides tooling to:

1. **Render** the full Red Hat operator catalog using `opm render`
2. **Filter** the JSON to keep only ODF packages at a specific version
3. **Build** a lightweight catalog image (~200 KB) containing the filtered JSON
4. **Push** it to a registry (internal or external)
5. **Create a CatalogSource** pointing to the pruned image

When OLM resolves the subscription against this catalog, the target version is the
**only version that exists**, so it installs exactly that.

The cluster stays connected: operator container images are still pulled from
`registry.redhat.io`. Only the catalog index is custom.

This follows the officially documented FBC filtering procedure in the
[OCP docs](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html-single/operators/index)
(section 4.9.2.2: "Updating or filtering a file-based catalog image").

## How It Works

```
┌─────────────────────────────────────────────────┐
│  Red Hat operator index                         │
│  registry.redhat.io/redhat/redhat-operator-index│
│  Contains ~3,100 entries across all operators   │
└──────────────┬──────────────────────────────────┘
               │ opm render
               ▼
┌─────────────────────────────────────────────────┐
│  Full catalog JSON (full-index.json)            │
│  ~3,100 objects: packages, channels, bundles    │
└──────────────┬──────────────────────────────────┘
               │ filter-catalog.py --version <ver>
               ▼
┌─────────────────────────────────────────────────┐
│  Pruned FBC (fbc/index.json)                    │
│  Only ODF packages at the target version        │
└──────────────┬──────────────────────────────────┘
               │ podman build (Containerfile)
               ▼
┌─────────────────────────────────────────────────┐
│  Catalog container image (~200 KB)              │
│  opm serve + cached FBC index                   │
└──────────────┬──────────────────────────────────┘
               │ podman push
               ▼
┌─────────────────────────────────────────────────┐
│  Registry (internal or external)                │
└──────────────┬──────────────────────────────────┘
               │ CatalogSource (grpc)
               ▼
┌─────────────────────────────────────────────────┐
│  OLM installs the only available version        │
│  Operator images pulled from registry.redhat.io │
└─────────────────────────────────────────────────┘
```

## Prerequisites

| Component | Details |
|---|---|
| OCP cluster | 4.18+ (tested on 4.22) |
| ODF target | Any z-stream (e.g., `v4.21.2-rhodf`) |
| Storage nodes | 3+ worker nodes labeled for ODF |
| Tools | `oc`, `opm`, `podman`, `python3` |

## Quick Start

### 1. Extract `opm` (if not installed)

```bash
OCP_VERSION=v4.22  # match your cluster version

podman run --rm --entrypoint cat \
  registry.redhat.io/openshift4/ose-operator-registry-rhel9:${OCP_VERSION} \
  /usr/bin/opm > /tmp/opm
chmod +x /tmp/opm
```

### 2. Render the full catalog

```bash
mkdir -p catalog fbc

/tmp/opm render \
  registry.redhat.io/redhat/redhat-operator-index:${OCP_VERSION} \
  > catalog/full-index.json
```

### 3. Filter to a specific ODF version

The filter script auto-discovers all ODF sub-operators (no hardcoded list). It finds
every package with a bundle matching the target version suffix.

```bash
python3 scripts/filter-catalog.py \
  --version v4.21.2-rhodf \
  --channel stable-4.21

# Use --help for all options
python3 scripts/filter-catalog.py --help
```

Examples for other versions:

```bash
# Different z-stream
python3 scripts/filter-catalog.py --version v4.21.5-rhodf --channel stable-4.21

# Different y-stream
python3 scripts/filter-catalog.py --version v4.22.0-rhodf --channel stable-4.22
```

### 4. Validate and build the catalog image

```bash
/tmp/opm validate fbc/

podman build -t odf-pinned-index:v4.21.2 -f manifests/Containerfile .
```

### 5. Push to a registry

You can use any registry accessible from the cluster. The simplest option on a
connected cluster is the built-in internal registry.

**Option A: Internal registry (no external registry needed)**

```bash
# Expose the internal registry
oc patch configs.imageregistry.operator.openshift.io/cluster \
  --type merge -p '{"spec":{"defaultRoute":true}}'

REGISTRY=$(oc get route default-route -n openshift-image-registry \
  -o jsonpath='{.spec.host}')

# Login (kubeadmin requires explicit oc login for OAuth token)
oc login -u kubeadmin -p "$(cat auth/kubeadmin-password)" \
  "$(oc whoami --show-server)" --insecure-skip-tls-verify

podman login --tls-verify=false -u kubeadmin -p "$(oc whoami -t)" "$REGISTRY"

# Create namespace and grant pull permissions
oc new-project odf-catalog
oc policy add-role-to-group system:image-puller \
  system:serviceaccounts:openshift-marketplace -n odf-catalog

# Tag and push
podman tag odf-pinned-index:v4.21.2 \
  "$REGISTRY/odf-catalog/odf-pinned-index:v4.21.2"
podman push --tls-verify=false \
  "$REGISTRY/odf-catalog/odf-pinned-index:v4.21.2"
```

**Option B: External registry (Quay, etc.)**

```bash
podman tag odf-pinned-index:v4.21.2 \
  quay.io/<your-org>/odf-pinned-index:v4.21.2
podman push quay.io/<your-org>/odf-pinned-index:v4.21.2
```

### 6. Label storage nodes

```bash
oc label node <node1> <node2> <node3> \
  cluster.ocs.openshift.io/openshift-storage="" --overwrite
```

### 7. Deploy ODF

Update `manifests/01-catalogsource.yaml` if you used an external registry, then apply:

```bash
oc apply -f manifests/01-catalogsource.yaml
oc apply -f manifests/02-odf-subscription.yaml

# Wait for all CSVs to succeed
watch oc get csv -n openshift-storage
```

Once all CSVs show `Succeeded`, create the StorageCluster:

```bash
# Edit 03-storagecluster.yaml: set storageClassName for your platform
oc apply -f manifests/03-storagecluster.yaml
```

### 8. Verify

```bash
# All CSVs at the pinned version
oc get csv -n openshift-storage

# Ceph health
oc get cephcluster -n openshift-storage

# StorageClasses created
oc get sc
```

## Updating ODF After Pinning

> **Note:** The update procedures below describe the expected workflow but have
> **not been tested yet**. Validate on a non-production cluster before relying
> on them.

With the pruned catalog, OLM cannot auto-update (only one version exists).
Updates are manual but controlled.

### z-stream update (e.g., 4.21.2 to 4.21.5)

```bash
# Rebuild catalog with new version
python3 scripts/filter-catalog.py --version v4.21.5-rhodf --channel stable-4.21
/tmp/opm validate fbc/
podman build -t odf-pinned-index:v4.21.5 -f manifests/Containerfile .
# Push to your registry (same steps as initial push)

# Update CatalogSource image
oc patch catalogsource odf-pinned-catalog -n openshift-marketplace \
  --type merge \
  -p '{"spec":{"image":"<new-image-ref>"}}'
```

OLM picks up the new version on the next catalog refresh (up to 30 min, or
delete the catalog pod to force it). You can skip z-streams (e.g., 4.21.2
directly to 4.21.7).

### y-stream update (e.g., 4.21 to 4.22)

Same rebuild process, plus update the Subscription channel:

```bash
python3 scripts/filter-catalog.py --version v4.22.0-rhodf --channel stable-4.22

oc patch sub odf-operator -n openshift-storage \
  --type merge -p '{"spec":{"channel":"stable-4.22"}}'
```

### Returning to the default catalog

To stop pinning and let OLM manage ODF normally:

```bash
oc patch sub odf-operator -n openshift-storage \
  --type merge -p '{"spec":{"source":"redhat-operators"}}'

oc delete catalogsource odf-pinned-catalog -n openshift-marketplace
```

## Files

| File | Purpose |
|---|---|
| `manifests/01-catalogsource.yaml` | CatalogSource pointing to pruned catalog image |
| `manifests/02-odf-subscription.yaml` | ODF Namespace + OperatorGroup + Subscription |
| `manifests/03-storagecluster.yaml` | Example StorageCluster (edit for your platform) |
| `manifests/Containerfile` | Multi-stage build for the catalog image |
| `scripts/filter-catalog.py` | Filters OPM catalog to a single ODF version |

## References

- [Managing custom catalogs (OCP 4.22)](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html-single/operators/index) (section 4.9.2.2)
- [File-Based Catalog (FBC) format](https://olm.operatorframework.io/docs/reference/file-based-catalogs/)
- [opm CLI reference](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/cli_tools/opm-cli)

## License

Apache-2.0
