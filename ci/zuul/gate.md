# Zuul's check and gate: what Mergify does today

How Zuul's pipelines do what CI's two layers and Mergify's merge queue do for `main` today (#1347, umbrella #1341).
Today's rules are in [`.mergify.yml`](../../.mergify.yml), [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
and [`docs/development.md`](../../docs/development.md), "Merging: the merge queue". Zuul's are in two places
([README.md](README.md), "Trusted and untrusted"):

- **the pipelines** `check`, `gate` and `hold`, in kristofk/aid-zuul-config's `zuul.d/pipelines.yaml` (trusted);
- **which jobs each runs, and the merge mode**, in [`zuul.d/project.yaml`](zuul.d/project.yaml), and the jobs in
  [`zuul.d/jobs.yaml`](zuul.d/jobs.yaml) (AID's own, untrusted).

Until the switch (#1355) Mergify still merges, and Zuul's gate takes no pull request of AIDepartment's: `main`'s
branch protection requires `Every package passed`, which only Mergify's queue draft carries, and Zuul enqueues a pull
request into a dependent pipeline only when GitHub would let it merge (its GitHub driver's `canMerge`: "required status
checks are missing"). Every row below was seen on the test bed ([Seen working](#seen-working)).

## From Mergify to Zuul

| Today (Mergify / Actions) | On Zuul | Where |
|---|---|---|
| `Cheap checks passed` required to enter the queue | `check`'s cheap jobs, and gate's requirement `status: aid-zuul:aid/check:success`: `check` passed on the pull request's head. gate is triggered when check's check run completes successfully, so a ready pull request enters by itself once check passes on a new head | `project.yaml` `check`; gate's `require` and `trigger` |
| `Every package passed` required to merge | gate's full jobs, on the pull request merged onto `main` and onto those ahead of it; Zuul merges only when every one passed | `project.yaml` `gate` |
| serial queue, batches of 2 | gate's speculative queue: a window of two, so two pull requests are tested at once, the second on top of the first ([Speculative depth](#speculative-depth)) | gate's `window` |
| fast-forward merges | `merge-mode: rebase`: Zuul tests the pull request's commits rebased onto the state ahead of them, and merges with GitHub's rebase. `main` stays linear ([Merging](#merging)) | `project.yaml` |
| the `hold` label blocks queueing | gate's `reject: label: hold`; labelling `hold`, or turning the pull request back into a draft, takes it out of gate (the pipeline `hold`); removing the label enqueues it again | gate's `reject` and `trigger`, the pipeline `hold` |
| ready means queued; a draft is never queued | gate's `require: draft: false`, and its trigger `ready_for_review` | gate's `require` and `trigger` |
| 4 h checks timeout (2 h a job) | each job's `timeout`: the base job's 2 hours, less where a job sets it ([Timeouts](#timeouts)) | aid-zuul-config's `base`, `jobs.yaml` |
| dequeue and `@mergifyio requeue` | a comment `regate` by an account with write access enqueues a pull request again; a push dequeues it (`dequeue-on-new-patchset`), and it comes back once check passes on the new head | gate's `trigger` |
| queued, dequeued labels; the "Merge Queue Status" comment | gate's check run `aid/gate`: queued, in progress, success, failure (with a comment naming the failed job and its log), cancelled | gate's reporters |

### What changes

Each of these is a difference from today, kept on purpose or open for a later issue:

- **A draft runs `check`.** Actions runs nothing on a draft; Zuul's `check` runs on every push by an account with
  write access (#1344's trust model, which keeps `ready_for_review` out of `check`'s triggers: a maintainer marking a
  stranger's pull request ready would run it with the farm's credential). check holds only the cheap jobs.
- **The commit on `main` is not the tested commit.** Mergify fast-forwards `main` onto the commit its run tested. Zuul
  tests its own rebase of the pull request's commits, then asks GitHub to rebase and merge them: new commits, with the
  tree Zuul tested whenever `main` moved only through the gate. GitHub's rebase merge cannot fast-forward, and no merge
  mode of Zuul's GitHub driver does.
- **A push to `main` outside the gate does not restart it.** Mergify restarts the check in front
  (`reset_on_external_merge`). Zuul's builds go on against the `main` they started from, and GitHub's rebase then puts
  their commits on the newer one. From #1355 only Zuul moves `main` but for the orchestrator's `internal/`-only state
  commits, which no job tests; #1348 decides whether those wait for an empty gate.
- **`hold` leaves `aid/gate` as it was.** Zuul removes a superseded item without a dequeue report, so a pull request
  taken out by `hold` keeps its `aid/gate` check run queued or in progress until it enters gate again, or a push
  starts a new one. It never shows success, so branch protection still refuses it (#1355).
- **No labels.** `queued` and `dequeued` were Mergify's labels; `aid/gate`'s state shows the same in lists.
- **The gate's own check.** Zuul runs `check`'s cheap jobs once per head and gate's full ones once per speculative
  state. A failure that a rebase onto newer pull requests caused shows in gate, as it shows in Mergify's queue draft.

## Merging

`merge-mode: rebase` is a property of AID's project ([`zuul.d/project.yaml`](zuul.d/project.yaml)). Zuul's merger
builds each pull request's state by rebasing its commits onto `main` and onto the pull requests ahead of it, and its
GitHub reporter merges with GitHub's `rebase` method. The repository allows rebase merges (`allow_rebase_merge`), which
Zuul reads with the App's Contents permission ([`deploy/zuul/github.md`](../../deploy/zuul/github.md)). A pull request
with a merge commit in it cannot be rebased by GitHub; push it rebased instead.

## Timeouts

Mergify's `checks_timeout` bounds the whole queue run: 4 hours since 2026-11-13, for a run that builds every app image
one at a time, with each job of it limited to 2 hours. Zuul has no timeout for a buildset, only for each job: the base
job's `timeout: 7200` (aid-zuul-config's `zuul.d/jobs.yaml`), and less where a job of AID's sets it (the lint jobs, 10
minutes). Waiting for a build node counts against no timeout. A job that runs out is `TIMED_OUT`, which fails its
buildset. The app images become jobs of their own (#1350, #1353), each with its own timeout, so no limit has to cover
them all one after another.

## Speculative depth

Zuul's gate tests the pull requests at the head of its queue at once, each on top of those ahead of it: the window.
The window is two, its floor one and its ceiling two. A failure halves it, to one, and each pull request that merges
adds one again, back to two.

- **Two, as today's throughput.** A Mergify check takes a batch of two pull requests in one run. Zuul's window of two
  tests two at once, each as its own version, and merges each as it passes, so the first never waits for the second.
- **What bounds it on ThunderBox.** Each pull request in the window runs gate's full jobs: Bazel's build and test of
  everything on the farm. The farm is shared with CI's runners until #1355, and the build nodes come out of the node
  budget in [`deploy/thunderbox/zuul.md`](../../deploy/thunderbox/zuul.md) (#1345). Two keeps the gate's load on the
  farm near today's, one queue run of two pull requests. A window above the number of nodes gains nothing: the jobs
  behind wait for a node. Raise the ceiling with the nodes, once #1353 has measured the times.
- **What a deeper window costs.** When a pull request fails, every build behind it is thrown away and run again
  without it. With a window of two that is one pull request's builds, as a failed batch of two is today.

## Flakes

A test that fails by chance in gate fails the pull request it ran for, and throws away the builds of every pull request
behind it in the window. Those start again without the failed one, and merge if they pass. Nothing is retried:
Zuul runs a job again only when its node or its pre-run failed (`attempts`, `RETRY_LIMIT`), never for a failed test,
which keeps #225's rule that a test runs once per version.

The failed pull request leaves gate with `aid/gate` failed and a comment. A `regate` comment enqueues it again, as
`@mergifyio requeue` does today. That is for a failure that was not its code: the flake's issue is filed first, and a
test that keeps failing goes into quarantine ([`docs/development.md`](../../docs/development.md), "Flaky tests"). The
quarantine works the same on Zuul: Bazel's BUILD files and `build/quarantine.bzl` leave a quarantined test out of
`bazel test //...`, so gate does not run it, and the quarantine runs are no gate job (they come with #1350 and #1353).
A flake hurts more in a speculative queue than in Mergify's serial one, since it resets the pull requests behind it
too, which is one more reason the window stays at two until the flake handling milestone has the open flakes down.

## Seen working

The test bed ([README.md](README.md), "The test bed") runs each row on Zuul 14.3.0, with copies of this folder and of
kristofk/aid-zuul-config ([`testbed/scenario.py`](testbed/scenario.py), checks 5 to 8; check 2 for check's cheap jobs).
The gate's full job runs a stand-in test there ([`testbed/workspace/`](testbed/workspace)) that each pull request of
the scenario tells to sleep or to fail. On the test bed, check reports a commit status instead of a check run, so gate's
requirement and trigger read that status ([`testbed/sync.sh`](testbed/sync.sh)).

| Row | Check | Seen |
|---|---|---|
| `Cheap checks passed` | 2, 6 | check runs the cheap jobs; a draft whose check passed stays out of gate until it is ready |
| `Every package passed` | 6 | gate runs `aid-bazel-everything`, and not check's jobs |
| batches of 2 | 8 | two pull requests' builds run at once, the second on top of the first, in a window of two |
| fast-forward | 6 | Zuul merges by rebase: `main` moves forward with no merge commit |
| `hold` | 7 | a ready pull request with `hold` stays out; labelled `hold` in gate, it leaves; without it, it comes back and merges |
| checks timeout | 5 | a job that runs past its `timeout` is `TIMED_OUT`, and check fails |
| requeue | 8 | a failed pull request enters gate again on `regate`, and merges |
| labels, the status comment | 6, 8 | `aid/gate` success and failure on the pull request, and a comment naming the failed job |
| flakes | 8 | the first fails, the second is tested again without it and merges |
