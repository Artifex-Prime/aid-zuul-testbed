# Disposable job nodes

Issue #1698 replaces reused check, gate-image and post SSH nodes with one fresh pod per job. The launcher creates
it on demand, waits for SSH readiness and deletes it after the job, including a failed, timed-out or aborted job.
It waits for deletion before releasing the slot. No home directory, Bazel server or job process is reused. The
Docker staging VM retains its existing reset before and after every job; its label stays `aid-staging-node`.

## Provisioning and trust

Zuul 14.3.0's stock Kubernetes driver creates a namespace, ServiceAccount, token Secret, Role and RoleBinding for
every node. It cannot use a preconfigured namespace. AID instead loads a small fixed-namespace adapter in the
launcher only, [`bounded.py`](../../deploy/zuul/server/launcher/bounded.py). It keeps the upstream Kubernetes
provider, node serialization, quota accounting, allocation, SSH nodescan and leak collector. Its create/delete
state machines manage pods in the connection's fixed namespace, never namespaces or RBAC. The entrypoint refuses
other Zuul versions until this compatibility has been rechecked.

| Connection | Namespace | Job label | Account | Pod budget |
| --- | --- | --- | --- | --- |
| `aid-check-pods` | `zuul-nodes` | `aid-node` | `pr` | 2 CPUs, 2 GiB RAM, 6 GiB ephemeral storage |
| `aid-gate-pods` | `zuul-gate` | `aid-gate-node` | `gate` | 3 CPUs, 6 GiB RAM, 16 GiB ephemeral storage |
| `aid-post-pods` | `zuul-post` | `aid-post-node` | `post` | 3 CPUs, 6 GiB RAM, 16 GiB ephemeral storage |

The admin installs a `PodTemplate/job-node` in each namespace, with `apply.sh <checked-image>@sha256:<digest>`.
Applying a template starts no pod. The launcher reads that template for every launch; the provider's `spec: {}`
is only a schema placeholder and cannot override it. The template supplies the image, security context, mounts,
labels, resources and identity. PR code can define jobs but cannot change the deployed template or provider.

The launcher's ServiceAccount has pod get/list/watch/create/delete and get on `PodTemplate/job-node` in those
three namespaces. It cannot read Secrets, modify policy/templates/RBAC or create namespaces. Its admin-installed
NetworkPolicy permits the k3s API route, in addition to existing DNS/proxy/node routes. Jobs receive none of the
launcher's token. Check's existing projected farm identity stays `zuul-nodes/pr`, and only reaches `dev`; gate/post
retain their separate image-check accounts, BuildKit daemons and network policies. `check-external` remains blocked
by the trusted base guard. The trust model is unchanged by giving every job its own pod.

Each provider has `resource-limits: {pods: 1}` and `reuse: false`. The namespace quota admits exactly that one pod
and its requests/limits. Excess work waits in Zuul; no warm spare pod consumes the slot. Readiness checks the pod's
SSH probe. A launch retry adopts only a pod with the same Zuul identity. A failed or interrupted create is removable
by its deterministic name, even if the API succeeded before ZooKeeper saved the response. Pod tags let the upstream
leak collector remove abandoned allocations after a launcher outage. API failures propagate; a failed delete does
not make a slot available.

## Caches and resource limits

`prepare-workspace-git` gets the speculative source from the executor's shared repository cache into the fresh pod.
No job modifies the executor cache. Buildbarn's existing action cache and trusted/untrusted instance split remain.

A shared host-backed cache at `/var/lib/zuul-downloads/repository-cache` contains **downloads only**, from approved
main builds. The three prebound `ReadOnlyMany` claims mount it at `/var/cache/bazel`, read-only. At node startup,
existing content-addressed SHA-256 entries are symlinked into the pod's private repository cache. Hits read the
share; misses create private entries. Jobs cannot populate or poison the share. No job writes to another job's
home or promotes private entries automatically. Gate/post BuildKit continue to have their separate bounded caches.

The PodTemplate is the source of a node's size. The downward API passes its CPU and memory limits to `start-node`,
which records them for SSH sessions. The image's `bazel` wrapper budgets its JVM and local scheduling from these
values. Remote `--jobs` stays the farm's concurrency. Issue #1695 removes the remaining repository-wide machine
budgets; this change supplies the node side of that contract. Node sizes and quota changes must still fit the host
budget; this rollout adds no node slot and no memory request.

## Coordinated rollout

The orchestrator applies shared infrastructure **only from clean commits already merged into each repository's
main**, following [`deploy/thunderbox/zuul.md`](../../deploy/thunderbox/zuul.md). Before merge, use an isolated lab;
this PR is not authorization to apply to live check, gate, post or dev workers.

1. Review and merge the AID and aid-zuul-config companions, coordinating the switch while pipelines are paused and
   existing jobs have drained. Preserve the trusted external-check guard. Do not let the scheduler use new provider
   connections before the launcher/configuration is deployed. Keep the old node image digests and manifests for rollback.
2. Publish/check the updated node and post images from merged AID main, with the ordinary publishing path. The
   `start-node`/Bazel wrapper changes require those images. Keep the registry's digest-only namespace exceptions.
3. On ThunderBox create a 10 GiB bounded download cache owned by root, writable only by the admin. Bind its host
   directory read-only into k3s at `/var/lib/zuul-downloads` (do not restart k3s while jobs run). Initially empty is
   safe. Populate `repository-cache/content_addressable/sha256/` from the download cache of an approved main build,
   verifying each `file` against its directory's SHA-256 before promotion; replace entries atomically, never in place.
   This share holds no compiled objects or action results. It may be deleted and rebuilt.
4. Stop the old controllers and wait for their pods to disappear:
   `kubectl -n zuul-nodes delete statefulset node`, `kubectl -n zuul-gate delete statefulset gate-node`, and
   `kubectl -n zuul-post delete statefulset post-node`. Remove their obsolete headless Services too. Do not delete
   the namespaces, accounts, Secrets, endpoint ConfigMap or existing host-side network/image-check grants.
5. Apply the three node directories with their checked digests using their `apply.sh`. These install templates,
   quotas, read-only cache claims, policies and launcher Roles, with no controller or job pod. Check every template's
   digest, account, `restartPolicy: Never`, cache mount and budget. Apply the admin namespace API policy, then the
   reviewed server configuration/launcher. Check readiness and `/api/components`.
6. Full-reconfigure against the merged trusted providers. `/api/tenant/aid/config-errors` must be empty. Resume
   pipelines. `kubectl -n <namespace> get pods -w` should show a new `np<node-uuid>` for each allocated job and its
   deletion after the job. Verify the launcher identity can manage pods but cannot create namespaces, read Secrets,
   write PodTemplates or change Roles. Verify check cannot use gate/post identities or the main farm instance.
7. Keep an ordinary gate build's URL, a post publishing build's URL, build/node UUIDs and pod creation/deletion
   events on #1698. Check cancellation once: an aborted job's pod and all its processes disappear, then the next
   job receives a different pod UID and empty home. Verify extra jobs wait rather than creating a second pod.
   Completion requires a green gate and a successful post publication using this lifecycle; unit tests alone do
   not establish it. Do not close #1698 until that evidence is recorded.

Rollback: pause/drain, restore the reviewed static providers and original image-pinned StatefulSets/Services from
the previous main commit, then restore the original launcher/config and reconfigure. Leave cache claims and policy
in place until the last disposable pod is gone. Never resurrect a pod or reuse its home as a rollback shortcut.

## Checks

`(cd deploy/zuul && mise exec -- ./check.sh)` renders/schema-checks the inert templates, checks budgets and RBAC,
and runs the existing deployment tests. `node --test ci/zuul-node.test.mjs ci/zuul-gate.test.mjs` exercises the node
wrapper/cache setup and gate execution. With the pinned Zuul runtime installed:

```sh
python deploy/zuul/server/launcher/test_bounded.py
```

The Compose testbed runs the same lifecycle suite inside its pinned scheduler image before its ordinary scenarios.
Those scenarios still substitute static Compose nodes; they do not claim to prove Kubernetes admission or actual
pod termination. The coordinated rollout's gate/post and cancellation evidence is required separately.
