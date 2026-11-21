# Zuul's gate image node

A fresh pod per job on k3s for gate's image jobs (#1521, [`ci/zuul/gate.md`](../../../ci/zuul/gate.md), "The gate's jobs"),
allocated afresh by Zuul's bounded Kubernetes provider as `aid-gate-pod-node`, which AID's nodeset `aid-gate-image` names
([`ci/zuul/zuul.d/jobs.yaml`](../../../ci/zuul/zuul.d/jobs.yaml)): an application's image, AID's release folder and
its three images, the build farm's worker image and Zuul's node images. Bazel builds their deterministic OCI
layouts on the farm; the node uploads them by digest to scratch and runs acceptance checks in the image-check
namespace. AID's release images alone come from the release folder's clean, uncached local Bazel invocation.

**Its trust.** A pull request's code runs here, as on the PR node. A projected, rotating `aid-build-farm` token gives
this exact ServiceAccount dev access only; it grants no main access or publishing right. The API token runs image
checks. Its namespace, account, SSH key and state remain separate from the post node's.


**Its share of the node.** The gate node requests at most 128 MiB of memory and 25m CPU for
its idle SSH server. Its existing limits remain 3 CPUs, 6 GiB of memory and 16 GiB of ephemeral
storage, including AID's clean release build. The [whole-node budget](../node-budget.md) includes
this reservation (#1700). Keep the old BuildKit allocation until its retirement. Actual use may burst above requests; under
4 GB available on the host, stop this node before a release, as the PR node's README says.

**The image** is the node image's `post` stage (`../node/image/`, #1633), by the same digest as the post node's: the
PR node's image with what Bazel's Swift toolchain needs from the host for the release folder, and the OCI transport tool and
`kubectl`. One image, so the two nodes never differ in what they build with.

**Where its services are.** sshd gives a job's session none of the pod's variables
(`../node/image/sshd_config`), so the addresses of the scratch registry and the image-check namespace are
a file, `/etc/aid-gate/endpoints.env`, from the ConfigMap `gate-endpoints` the admin makes: `CI_SCRATCH_REGISTRY` and `CI_RUN_NAMESPACE`, one `KEY=value` a line. [`ci/zuul/gate.sh`](../../../ci/zuul/gate.sh)
reads them, and an image job on a node without them fails.

## What ThunderBox grants

As for the post node ([`../post-node/README.md`](../post-node/README.md), "What ThunderBox grants"), with `zuul-gate`
and its account `gate`, and without the pusher:

1. **Lets it reach**, beside this folder's policy (DNS, the proxy, the farm and scratch): the Kubernetes API, as
   `kubernetes.default.svc:443`. Never the pusher.
2. **Binds `zuul-gate`'s account `gate`** in the image-check namespace to the role its `swift-image` runners have
   there: creating, reading and deleting the check's pods, which `ci/image-run.mjs` labels with the build's UUID.
3. **Says its endpoints**: `CI_SCRATCH_REGISTRY` and `CI_RUN_NAMESPACE`, the values its `swift-image` runners have.

## Applying it

Give the orchestrator a heads-up first, as for any live Zuul change. The admin applies the namespace and the policy,
then the host key and the endpoints, then the node, with the tools of `deploy/zuul/mise.toml`:

```sh
kubectl apply -f deploy/zuul/gate-node/namespace.yaml -f deploy/zuul/gate-node/networkpolicy.yaml
# Once: the node's host key, letting in the executor's one login key (secrets.sh says why).
aid-keychain get zuul-node-ssh | deploy/zuul/gate-node/secrets.sh | aid-keychain set zuul-gate-node-ssh
aid-keychain get zuul-gate-node-ssh | kubectl apply -f -
# The public key, for aid-zuul-config's providers.yaml:
aid-keychain get zuul-gate-node-ssh | jq -r '.data["ssh_host_ed25519_key.pub"] | @base64d'
# The endpoints, with ThunderBox's values for the two it says:
printf '%s\n' 'CI_SCRATCH_REGISTRY=<ThunderBox'"'"'s>' \
  'CI_RUN_NAMESPACE=<ThunderBox'"'"'s>' > endpoints.env
kubectl -n zuul-gate create configmap gate-endpoints --from-file=endpoints.env && rm endpoints.env
# The admission policy lets the node image run only where it names: add zuul-gate beside zuul-post
# (deploy/build-farm/images/admission.yaml), and check it with deploy/build-farm/test/mirror-admission.sh.
deploy/zuul/gate-node/apply.sh thunderbox-registry.internal/aid/zuul-node:post-ubuntu26.04-<commit>@sha256:<its digest>
kubectl -n zuul-gate get podtemplate job-node
```

Then ThunderBox's grants
above, and aid-zuul-config's provider entry with the host key ([`ci/zuul/gate.md`](../../../ci/zuul/gate.md),
"Turning the gate's jobs on"). The executor needs no change: it logs in with its one key, which this node lets in.

Undo: take the provider entry out of aid-zuul-config (gate's image jobs then get no node, and fail), wait until no job
uses the node, and delete `zuul-gate`. ThunderBox removes its binding and policy.

## Disposable template updates (#1698)

`apply.sh` delegates to `deploy/zuul/apply-disposable.sh` and requires a checked image by digest.
It updates `PodTemplate/job-node` and bounded prerequisites; it creates no job pod or controller.
Only the orchestrator applies clean merged main. Existing jobs keep their current pod; subsequent
allocations read the new template. Keep the one-pod quota, separate identities and read-only downloads.
The orchestrator removes retired static controllers/Services explicitly; apply does not prune them.
Proof, coordinated removal and reviewed rollback: [disposable-nodes.md](../../../ci/zuul/disposable-nodes.md).

For migration from BuildKit, apply only after merge and follow [the image rollout](../../../build/images/README.md#rollout-after-merge).

## Scratch lifecycle hook (#1817)

Before applying this PodTemplate, the operator's merged grants must publish
`ConfigMap/aid-scratch-control` in this namespace, with key `aid-scratch-control`.
The template mounts that external ConfigMap as the read-only executable
`/usr/local/libexec/aid-scratch-control` (0555, file subPath). This repository does
not duplicate or regenerate the operator-owned hook code. No image rebuild is needed.
A missing ConfigMap prevents a new node from starting; create it before applying the
mounts, and remove the mounts before deleting it during rollback. Existing pods keep
their installed file until replaced (subPath mounts do not receive updates).

The trusted companion serializes scratch post/maintenance jobs under the same
`aid-scratch-registry` semaphore as gate image jobs. Admission and cleanup remain
inactive until the orchestrator enables lifecycle in the coordinated quiet rollout;
merging the mount alone activates no admission, rotation or DELETE.
