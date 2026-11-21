# Zuul's build node

Zuul's bounded Kubernetes provider allocates a fresh PR-only pod per job on k3s, labelled `aid-check-pod-node`;
Bazel sends actions to the build farm over TLS. It has no Swift compiler and no trusted-cache credential: of Swift, only
the runtime libraries in `/usr/lib/swift/linux`, where the farm's toolchain links `aid` to find them, so that gate's
ingress suite runs a farm-built `aid` here (#1521, `ci/zuul/gate.md`, "The gate's jobs").
The cheap policy suite uses `cc` and libc headers for its private Linux subreaper fixtures, and `readelf` for image
checks (#1520); the node image checks these tools as its job user before publication.

**Its share of the node.** Each pod requests at most 128 MiB of memory, its idle SSH server plus room for setup.
The two-slot namespace reserves 256 MiB in total.
Its limits are 2 CPUs, 2 GiB of memory and 6 GiB of ephemeral storage; shared downloads are read-only. The provider has two slots,
so at most two jobs run (shared by check and gate non-image jobs); the trusted base job times out at 7200 seconds, also the tenant's maximum for any job.
Bazel derives local CPU, memory and test concurrency from the pod's cgroup; its heap uses a percentage of
that memory, as `.bazelrc` specifies (#1695). The pod is deleted after each job, discarding its home and scratch. The historical 2026-11-14 budget was 14156 of
14336 MiB (1072 for Zuul's services, 128 for one check node, 704 for the farm with one dev worker).
The current whole-node accounting is in [node-budget.md](../node-budget.md). It adds no
headroom to the host's limits; under 4 GB available on the host, stop the node. More nodes require a revised ThunderBox budget.

Zuul's bounded Kubernetes adapter uses the admin-installed `PodTemplate/job-node` in this namespace.
The launcher creates a fresh pod on demand and confirms deletion before releasing its slot. No static
StatefulSet or Service is installed. The template defines identity, security, resource limits and read-only
shared download cache; a job's private home is discarded after it ends. Trusted post jobs stay separate.

`check-external` cannot use this boundary. The companion config's trusted base pre-run rejects that pipeline before
checkout or any untrusted pre/run/post/cleanup playbook and disables retries. Its raw farm token cannot be protected
by a conditional Bazel helper. Keep external jobs blocked until separate nodes have no token, farm network access or
scratch shared with this pod. Deploy that trusted guard before registering this provider or starting the node.

The image carries checksum-pinned Bazel 9.3.2 beside Bazelisk, git, rsync, Python, Node, the tools in
`ci/tools.pin` and Swift's runtime libraries. The pinned binary and the shipped download-cache fixture let
its runtime acceptance check run offline in the closed image-check namespace. Its `check` target runs
as the node user before publication. The `bazel` wrapper uses that binary only when `USE_BAZEL_VERSION`
or the nearest checkout `.bazelversion` selects 9.3.2; other versions go through Bazelisk, so a checkout
pin change cannot silently run the image's older Bazel. The node authenticates with a projected Kubernetes ServiceAccount token for `aid-build-farm`,
rotated by the kubelet, valid for an hour. No Kubernetes API token or signing key is given to a job. The farm accepts
only this issuer, audience and PR ServiceAccount, and always assigns it `dev`, regardless of claims a job might request.
Main's post credential is not installed here. The existing GitHub Actions authentication remains unchanged until #1355.

## Applying it

Give the orchestrator a heads-up before changing or stopping live Zuul or farm components. Coordinate with the
GitHub-connection builder before applying anything to `zuul` or `zuul-executor`, so overlapping rollouts preserve
the current connection, secrets and tenant configuration.

Before enabling the provider or starting its pod, quiesce the pipelines and cancel queued/running `check-external`
items frozen before the trusted guard was installed. Merge the reviewed config companion with its guard, then fully
reconfigure Zuul and confirm that new external job graphs contain that base pre-run. Only then start the node and
resume events. A frozen job from the old layout must never acquire the new credential-bearing node.

The admin applies this namespace; it has no tenant RoleBinding, no API rights for `pr`, no host mounts or privileged
containers, and Pod Security `restricted`. Make the SSH keys once, save their JSON in the keychain, then apply the same
JSON on every redeploy. Key generation needs OpenSSH's `ssh-keygen` and `jq`; without them the script refuses without
printing a Secret. CI without OpenSSH checks the same Secret serializer with fixture data; matching real host/login
key pairs are checked where OpenSSH is installed. The launcher scans its SSH host key and supplies strict executor known_hosts:

```sh
kubectl apply -f deploy/zuul/node/namespace.yaml -f deploy/zuul/node/networkpolicy.yaml
deploy/zuul/node/secrets.sh | aid-keychain set zuul-node-ssh
aid-keychain get zuul-node-ssh | kubectl apply -f -
# The public key only, to compare with providers.yaml:
aid-keychain get zuul-node-ssh | jq -r '.items[] | select(.metadata.namespace=="zuul-nodes") | .data["ssh_host_ed25519_key.pub"] | @base64d'
```

The executor mounts the other namespace's `node-ssh` at `/root/.ssh`: create the Secret **before** applying the updated
executor. `StrictModes no` on the node accommodates Kubernetes' Secret mount directory ownership: the authorized keys
are an admin-owned read-only mount under the container's read-only root, which jobs cannot replace.

Apply the farm namespace's policy, and its updated server configuration (`deploy/build-farm/README.md`), which adds
only the new PR identity and keeps the existing clients. Its public signing keys come from the running k3s API:

```sh
kubectl get --raw /openid/v1/jwks > deploy/build-farm/server/config/kubernetes-jwks.json
```

Check it matches the committed public keys; a change is reviewed with the farm configuration before applying it.
After a k3s signing-key rotation keep the old and new public keys until old tokens expire, then remove the old one.
Revoke all these nodes by adding `,zuul-pr,` to the farm’s `farm-revoked` ConfigMap, preserving other entries, as `deploy/build-farm/README.md` describes for other clients. The frontend rereads it within a minute.

The node image is built and checked through `ci/zuul-node-image.mjs`: by gate's `aid-gate-zuul-node-image` before
a change to it merges, and again by `post`'s `aid-publish-images`, which publishes it on the merge (#1650,
`ci/zuul/publish.md`, "The infrastructure images"). That build's summary names the published
`aid/zuul-node:ubuntu26.04-<commit>` and its digest. Use that published, checked digest, never an unreviewed PR image. Pass that checked digest when applying (with `kubectl`, `kustomize` and
`yq` on PATH, `deploy/zuul/mise.toml`'s tools):

```sh
# Admin: the node mirror is restricted cluster-wide. Install the narrow zuul-nodes image exception
# before allowing job pods; keep the farm worker's existing exception.
kubectl apply -f deploy/build-farm/images/admission.yaml
# Checks real admission for the node and worker, wrong namespaces/images, tags and init containers.
# Every pod create is --dry-run=server: nothing is scheduled or pulled.
deploy/build-farm/test/mirror-admission.sh
deploy/zuul/node/apply.sh thunderbox-registry.internal/aid/zuul-node@sha256:<checked-digest>
kubectl -n zuul-nodes get podtemplate job-node
```

Never apply the raw `node/kustomization.yaml` to run a node: `zuul-node` is deliberately an unresolved image name.
`apply.sh` requires a digest and substitutes it. The CI check's scratch digest can be used for an isolated smoke run,
but the scratch registry is temporary, so a lasting deployment must use the published image. An image change or
restart loses the node's scratch: pause the tenant's pipelines and wait for its running job before replacing it.

The bounded `aid-check-pods` connection is in `server/config/zuul.conf`. Merge the reviewed provider in
`kristofk/aid-zuul-config` before enabling AID's jobs; #1344 supplies the GitHub connection and tenant entries. No new
provider configuration is accepted from AID's pull requests. Check `/api/tenant/aid/config-errors` and
`/api/tenant/aid/nodes`: the label `aid-check-pod-node` is ready. The real SSH nodescan must match the key served from the Secret above.

Undo: remove the provider from the trusted config project, wait until no job uses it, then delete `zuul-nodes` and
the executor's `node-ssh` mount and Secret. Remove the farm's `zuul-nodes` ingress entry and Kubernetes authentication
policy only once the nodes are stopped. The keychain backup remains until intentionally retired.
Remove only the `zuul-nodes` exception from `build-farm-mirror-images` after the last node stops; keep the policy,
its binding and the farm worker exception in place.

## Live smoke test

[`smoke/`](smoke) contains a tiny Bazel test: it requires `/usr/bin/swift`, which exists only on the farm. It has no
Swift compilation or third-party source dependency. Copy it to a throwaway repository of your own, add
`deploy/thunderbox/build-farm-ca.crt` as `ca.crt`, and copy the **proposed** trusted `providers.yaml` into `zuul.d/`.
Use a temporary tenant with that repository as its Git config project (load branch `main`), a timer pipeline and
a 600-second job on `aid-check-pod-node`. Its playbook copies the source from
`{{ zuul.executor.work_root }}/{{ zuul.project.src_dir }}/` into the node's workspace, then runs:

```sh
bazel test --config=remote --nocache_test_results //:remote_smoke
# Must fail with PermissionDenied, using the same projected PR identity:
bazel test --config=remote --remote_instance_name=main --nocache_test_results //:remote_smoke
```

Keep the build UUID, job output and test log with the PR. Check the node cannot connect to the Kubernetes API, the
host or another tenant, while TLS to the farm works. Remove the timer and temporary tenant after the run, restore the
original Zuul configuration, and archive the throwaway repository. This proves the real provider, SSH transport,
image, TLS, PR authentication and remote test without waiting for #1344's GitHub connection.

## Disposable template updates (#1698)

`apply.sh` delegates to `deploy/zuul/apply-disposable.sh` and requires a checked image by digest.
It updates `PodTemplate/job-node` and bounded prerequisites; it creates no job pod or controller.
Only the orchestrator applies clean merged main. Existing jobs keep their current pod; subsequent
allocations read the new template. Keep the two-pod quota, separate identities and read-only downloads.
The orchestrator removes retired static controllers/Services explicitly; apply does not prune them.
Proof, coordinated removal and reviewed rollback: [disposable-nodes.md](../../../ci/zuul/disposable-nodes.md).
