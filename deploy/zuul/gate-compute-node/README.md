# Disposable gate compute slot

Additive prerequisites for #1867; routing remains unchanged until the orchestrator
activates this pool from merged main, installs the trusted companion provider and
proves a canary. See `ci/zuul/gate-compute-design.md` for staged activation/rollback.

One pod uses the checked **PR** node image (same image as `../node`, never the post
image). Apply only a checked sha256 through `../apply-disposable.sh gate-compute-node
<image>@sha256:<digest>`. There is no controller or long-lived job pod.

**Its share of the node.** One pod requests at most 384 MiB of memory, reserved even while idle.

Step 3 leaves 416 MiB reserved memory headroom. The orchestrator's 2026-11-19
23:29Z baseline was 7.87 GiB host MemAvailable, memory PSI some/full avg60 0,
node usage 3566 MiB, and live requests 12160 MiB / 5270m CPU / 34512 MiB storage.
These are idle baselines, not load peaks. The orchestrator directed sizing from
allocations without waiting for peaks. Reserve 384 MiB / 100m. After the orchestrator-recorded BuildKit retirement,
the complete request envelope is 17520/18048 MiB and 8335/9000m, leaving 528 MiB including the flake tracker reservation.
The 6 GiB memory limit preserves the 3 GiB JVM heap plus native overhead; the
384 MiB request is an admission reservation, not a measured working set. Bursting
and shared farm contention still require host-pressure proof at activation.

The initial activation gate evicted full Bazel three times because home exceeded
5 GiB. The orchestrator's revised capacity decision is **11 GiB home / 12 GiB
ephemeral request and limit**, with tmp unchanged at 512 MiB. A 16 GiB proposal
still exceeded allocation after releasing both idle BuildKits; this decision
saves 4096 MiB without reducing any other tenant. CPU and memory stay unchanged.
The orchestrator deletes the retired live namespaces when applying the resized
template. The next eviction, if any, requires a larger kubelet dataset rather
than reducing other tenants or the JVM heap.

Scratch budget: **116256/116736 MiB**, **480 MiB spare**.
Home (11 GiB), tmp (512 MiB), logs and writable layers share the 12 GiB ceiling.
The downloads PV remains a read-only claim on the existing cache, not another
scratch copy. The capacity-only prerequisite PR merges first with existing routing unchanged.
Keep routing PR #1926 draft and held pending the orchestrator's live PodTemplate
apply and explicit activation-gate release. A static budget pass
is not a full-build storage proof.

Create only namespace-local SSH host material using the existing login public key:

```sh
aid-keychain get zuul-node-ssh | deploy/zuul/gate-compute-node/secrets.sh | aid-keychain set zuul-gate-compute-node-ssh
```

Save and reapply that Secret. Never regenerate the executor login key. The farm
identity is exactly `system:serviceaccount:zuul-gate-compute:compute`, client
`zuul-gate-compute`, dev only. No BuildKit, registry, publishing credentials or
scratch-image access are mounted. Executor and launcher existing egress policies
already admit pod CIDRs; the namespace's ingress restricts SSH/console to those
callers, and its egress allows only DNS, proxy and farm.

Before enabling routing, audit live quotas/requests, prove SSH, dev access and denied
main, success/abort deletion, full Bazel at the unchanged heap within the 12 GiB scratch ceiling,
and concurrent check admission. Keep MemAvailable >=3 GiB, stop on memory full PSI
>5, OOM/eviction or disk pressure. Keep image slot, scratch semaphore and gate
window at one. Report gate wall-clock/check latency before and after. No live apply
is part of this PR.
