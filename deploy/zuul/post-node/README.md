# Zuul's post node

A fresh pod per job on k3s for the jobs that publish from `main` (#1350, [`ci/zuul/publish.md`](../../../ci/zuul/publish.md)),
allocated afresh by Zuul's bounded Kubernetes provider as `aid-post-pod-node`. Only kristofk/aid-zuul-config's jobs with secrets name that
label, and each of them is final, post-review and guarded ([`../job-secrets.md`](../job-secrets.md), rule 5: "its own
nodes, or none"). No pull request's job runs here, and nothing here is shared with the PR node (`../node/`): another
namespace, another account, another host key, and an image of its own. Its rotating projected farm token maps only this exact account to client `zuul-post` on the main cache instance;
PR/gate subjects cannot use that instance. Worker execution and action-cache writes stay within their trust tier
under #1811. The release builds locally and clean.

**Its share of the node.** The post node requests at most 128 MiB of memory and 25m CPU for
its idle SSH server. Its existing limits remain 3 CPUs, 6 GiB of memory and 16 GiB of ephemeral
storage, including AID's clean release build. The [whole-node budget](../node-budget.md) includes
this reservation (#1700). Keep the old BuildKit allocation until its retirement. Actual use may burst above requests; under
4 GB available on the host, stop this node before a release, as the PR node's README says.

**The image** is the node image's `post` stage (`../node/image/`, #1633), published beside the PR node's in
`aid/zuul-node` under a tag of its own, `post-ubuntu26.04-<commit>`, by digest. It is the PR node's image (Bazelisk,
pinned Bazel, git, Node, and the OCI transport tool and `kubectl` at `ci/tools.pin`'s versions) with what Bazel's Swift toolchain needs from the
host, swift.org's Ubuntu package list as the farm's workers have it (`post-install.sh`): the release folder builds here
with that toolchain, locally and clean. Its check (`post-check.sh`) runs the node's, then finds each library the
toolchain loads, using a checksum-pinned fixture under `/opt/zuul-node-check/swift` so the runtime
check needs no external network. That fixture stays off PATH; clean release builds still fetch and use
their own toolchain. The PR node's image has neither these host packages nor the full Swift fixture.

## What ThunderBox grants

The image jobs reach ThunderBox's image services as its `swift-image` runners do, and those are ThunderBox's, not
AID's. This node has a rotating `aid-build-farm` token with main access, and directly uploads Bazel layouts to
scratch. [The image runbook](../../../build/images/README.md) gives the grants and migration order.
ThunderBox:

1. **Lets it reach**, beside this folder's policy (DNS, the proxy, the farm and scratch): the Kubernetes API and the pusher.
   The scratch registry admits this node directly; the image-check pods are managed through the API.
   A job reaches the API as `kubernetes.default.svc:443`, which `ci/zuul/publish/job-env.sh` gives kubectl: sshd
   passes a job's session none of the pod's variables, and the API's IP is not in `NO_PROXY` (#1622).
2. **Binds `zuul-post`'s account `post`** in the image-check namespace to the role its `swift-image` runners have
   there: creating, reading and deleting the check's pods. `ci/image-run.mjs` labels each with the build's UUID
   (`thunderbox.ci/run`), so its cleanup deletes only that build's pods.
3. **Accepts Zuul's OIDC token at the pusher** (#1350, [`ci/zuul/publish.md`](../../../ci/zuul/publish.md), "The pusher"):
   issuer `http://web.zuul.svc.cluster.local:9000` (zuul-web in the cluster, #1637), its keys at `<issuer>/oidc/jwks`, audience `thunderbox-registry`, `sub`
   `secret:aid/github.com/kristofk/aid-zuul-config/aid-pusher`, `pipeline` `post`, and `job-name` `aid-publish-apps`,
   `aid-release` or `aid-publish-images` (#1650). Nothing else Zuul signs is a publisher's. ThunderBox lets the pusher reach Zuul's web for the keys,
   `web.zuul.svc.cluster.local:9000`: its egress only. The ingress is AIDepartment's, the policy `web-pusher` in
   `zuul` ([`../namespace/networkpolicy.yaml`](../namespace/networkpolicy.yaml), #1601), which the admin applies
   with the namespace (`ci/zuul/publish.md`, "Turning it on", step 3).
4. **Says its endpoints**, for aid-zuul-config's jobs' variables: `CI_SCRATCH_REGISTRY`, `CI_RUN_NAMESPACE` and
   `CI_PUBLISH_URL`, the values its `swift-image` runners have. No BuildKit endpoint is needed.

App index recovery (#1760) also reads the host's anonymous **read-only** registry at `172.31.250.1:5002`,
`CI_RELEASE_REGISTRY` in `ci/zuul/publish/job-env.sh`. This folder's policy permits that exact host/port; it gives
no access to the authenticated writer at 5001. The planner makes direct HEAD requests, keeping GitHub on the public
proxy. After merging #1760, the orchestrator uses a clean checkout of merged main and applies only
`kubectl apply -f deploy/zuul/post-node/networkpolicy.yaml` with the admin context. No image, PodTemplate, key or
quota update is needed for this route. Then replay a failed post item if it reached the lookup before the rollout,
and require a successful lookup in its build log. A builder does not apply it live.

## Applying it

Give the orchestrator a heads-up first, as for any live Zuul change. The admin applies the namespace and the policy,
then the host key, then the node, with the tools of `deploy/zuul/mise.toml`:

```sh
kubectl apply -f deploy/zuul/post-node/namespace.yaml -f deploy/zuul/post-node/networkpolicy.yaml
# Once: the post node's host key, letting in the executor's one login key (secrets.sh says why).
aid-keychain get zuul-node-ssh | deploy/zuul/post-node/secrets.sh | aid-keychain set zuul-post-node-ssh
aid-keychain get zuul-post-node-ssh | kubectl apply -f -
# The public key, for aid-zuul-config's providers.yaml:
aid-keychain get zuul-post-node-ssh | jq -r '.data["ssh_host_ed25519_key.pub"] | @base64d'
# The admission policy lets the node image run only in zuul-nodes: add zuul-post beside it
# (deploy/build-farm/images/admission.yaml), and check it with deploy/build-farm/test/mirror-admission.sh.
deploy/zuul/post-node/apply.sh thunderbox-registry.internal/aid/zuul-node:post-ubuntu26.04-<commit>@sha256:<its digest>
kubectl -n zuul-post get podtemplate job-node
```

Then ThunderBox's grants
above, and aid-zuul-config's provider entry with the host key
([`ci/zuul/publish.md`](../../../ci/zuul/publish.md), "Turning it on"). The executor needs no change: it logs in with
its one key, which this node lets in.

Undo: take the label's jobs out of aid-zuul-config's `post`, wait until no job uses the node, remove its provider entry,
and delete `zuul-post`. ThunderBox removes its binding and policy.

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
