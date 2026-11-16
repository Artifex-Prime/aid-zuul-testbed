# Publishing from main on Zuul

What GitHub Actions does for `main` today, outside pull requests, and how Zuul does it (#1350): the app images, AID's
release, the release index and the nightly. The jobs are in kristofk/aid-zuul-config's `zuul.d/publish.yaml`, since
each holds a secret ([`deploy/zuul/job-secrets.md`](../../deploy/zuul/job-secrets.md)); what each runs is a script here,
under [`publish/`](publish), which runs the same commands as the Actions job it stands beside.

Until Zuul's switch (#1355), both CIs carry these jobs, and [`ci/publisher`](../publisher) says which one publishes
([Which CI publishes](#which-ci-publishes)). It names `zuul`, since step 6 of [Turning it on](#turning-it-on).

## The pipelines, and who starts them

| Pipeline | Starts on | Who can start it | Post-review | Its jobs |
| --- | --- | --- | --- | --- |
| `post` | a push to `main` | whoever merges: Mergify or Zuul's `gate`, or an administrator's push. `main` is protected, so nobody else | yes | `aid-release-credentials` (#1351), `aid-publish-apps`, `aid-release` |
| `periodic-0200`, `periodic-0300` | a timer, at 02:00 UTC and at 03:00 UTC | nobody: Zuul's clock, on `main`'s head | yes | `aid-nightly` (this repository's), `aid-nightly-report` |

Both are aid-zuul-config's (`zuul.d/pipelines.yaml`), and post-review, so the only pipelines where a job with a secret
may run. Their items are refs, never a pull request, which the secrets' guard checks again.

**No tag pipeline.** AID's release starts from `VERSION` on `main`, in `post`, and makes its own tag `aid-v<version>`
when it creates the GitHub Release; a pipeline on tags would start again for that tag. The app workflows' tag job, which
attaches a version's declaration to a `<app>-v<version>` tag someone pushes, has never run: no such tag exists
(`git ls-remote --tags origin`, 2026-11-15, lists `aid-v*` only). It stays on GitHub Actions until #1355, which removes
it or brings it over. A tag can name any commit, and a job on it would run that commit's scripts with a token: one
taken over would check first that `main` contains the tag's commit.

**No reporter.** Neither pipeline has a pull request to report on. A failed release opens its issue (`aid-release`'s
post-run, as `aid-release.yml`'s report job does). The nightly's result goes to its `ci-nightly` issue
(`aid-nightly-report`). Every build is in Zuul's API and web UI (`/api/tenant/aid/builds?pipeline=post`). AID's events
from Zuul are #1526's.

## The jobs

Each publishing job runs on the **post node** ([`deploy/zuul/post-node/`](../../deploy/zuul/post-node/README.md)),
which only these jobs name: no pull request's job runs there, and nothing there is the PR node's. Its stages are
playbooks of their own, and each stage gets only the credentials it uses, made as it starts
(aid-zuul-config's `playbooks/publish/stage.yaml`):

| Stage | App images (`aid-publish-apps`, [`publish/apps.sh`](publish/apps.sh)) | AID's release (`aid-release`, [`publish/release.sh`](publish/release.sh)) | Credentials |
| --- | --- | --- | --- |
| `build` | for every app whose payload the push changed (`ci/image-policy.mjs`): the image built with BuildKit, checked as AID starts it, its declaration written | the plan (`VERSION` changed, unreleased, the changelog dated); the release folder built clean (`deploy/release-folder.sh`, #1298); the three images built and checked; the keychain asset and the entry written | the App's token, `contents: read`, for the push's base and the tags |
| `push` | each image through ThunderBox's pusher | the version still unreleased, then the three images through the pusher | Zuul's token for the pusher; the release also the App's, `contents: read` |
| `record` | each pushed image's declaration into the release index | the GitHub Release `aid-v<version>`, then its entry into the index | the App's token, `contents: write` |
| `report` | | after a failure only: the release's issue | the App's token, `issues: write` |

What a stage hands to the next is in `ZUUL_STATE` on the node: the plan, the images by digest, the declarations and
the entry, as files. Nothing goes through a forge's artifacts (#1286). Zuul runs a job's next playbook only after one
that passed, so an app whose push fails does not fail `push`: `record` indexes every app that did publish, then fails
the job for it. A failure anywhere in `aid-release` opens the release's issue, a refusal by the plan included, unless
the plan ran to its end and released nothing.

**Not carried over from Actions:** the merge queue's checked images (#466). On Zuul the gate builds no image yet
(#1521), so `post` builds and checks each image once itself, as Actions does without a queue image.

[`publish/job-env.sh`](publish/job-env.sh) is the one place that maps Zuul's variables onto those the release scripts
read, which GitHub Actions names: the push's range (`GITHUB_SHA`, the event's `before`), the run (`GITHUB_RUN_ID` is the
build's UUID, which names the scratch images and labels the image-check pods), `CI_SYSTEM=zuul`, and `origin` with the
App's token, read by git from its file.

The job's timeout is the tenant's maximum, two hours, for all its stages: each copy through the pusher may take 15
minutes (`CI_PUBLISH_TIMEOUT_SECONDS`), where Actions allows an hour.

### The nightly

`aid-nightly` is this repository's job ([`zuul.d/jobs.yaml`](zuul.d/jobs.yaml)), untrusted and with no secret, on the
PR node with the farm's `dev` credential: [`publish/nightly.sh`](publish/nightly.sh) builds and tests everything with
a BUILD file with no cached test result, on `main`'s head. Zuul's timer has no time zone, so it fires at 02:00 UTC
(`periodic-0200`) and at 03:00 UTC (`periodic-0300`), and `ci/nightly.mjs`'s clock lets through only the one that is
04:00 in Madrid (the owner's decision of 2026-10-20, #225), and only while `ci/publisher` names Zuul. Otherwise it
passes having run nothing. A pipeline per time because Zuul tells a job its pipeline, not when its timer fired: an
item that waits for the PR node past the other time keeps the decision of the timer that made it, in both jobs.

That is the Bazel part of Actions' nightly. The rest of CI's jobs (the Node suites, the images, staging) come to Zuul
with #1520 and #1521, and the nightly takes them then.

`aid-nightly-report` (aid-zuul-config's `playbooks/nightly/`) runs beside it on the executor, with no node. It decides
by the same pipeline's time and `ci/publisher` first, with no credential; only then does it make a token with `issues: write`,
wait for `aid-nightly`'s result in the same buildset from Zuul's API, and close, comment on or open the `ci-nightly`
issue, as `ci/nightly.mjs report` does on Actions. A nightly with no result within the job's two hours is reported as
one.

## The credentials

- **The App `aid-release`** ([`deploy/zuul/job-secrets.md`](../../deploy/zuul/job-secrets.md)): its key is the secret
  `aid-release-github`, and never leaves the executor. Each stage gets a token made for it with only its permissions,
  for AIDepartment alone, revoked when the stage ends. The App needs **Contents: read and write** (it has it) and
  **Issues: read and write**, for the release's and the nightly's issues ([Turning it on](#turning-it-on), step 1).
- **The pusher**: Zuul's own OIDC token, the secret `aid-pusher` (no stored value: Zuul signs a token as each stage
  starts, for three hours at most, the tenant's maximum). Only the `push` stage hands it to the script.

### The pusher

ThunderBox's pusher, the registry's one writer, accepts GitHub's OIDC token today. For Zuul it accepts one more issuer,
and only this token of it (`ci/publish-image.mjs` sends it as the bearer, from `CI_PUBLISH_TOKEN_FILE`):

| Claim | Value |
| --- | --- |
| `iss` | `http://web.zuul.svc.cluster.local:9000`, zuul-web in the cluster, with its keys at `<iss>/oidc/jwks`: set on the secret (`iss`), and allowed by the tenant (`allowed-oidc-issuers`, #1637). Zuul's default would be `[web] root`, AID's Zuul UI's address, which the pusher neither trusts nor reaches |
| `aud` | `thunderbox-registry`, as GitHub's tokens have it (`CI_PUBLISH_AUDIENCE`) |
| `sub` | `secret:aid/github.com/kristofk/aid-zuul-config/aid-pusher`: the tenant, the config project and the secret. No pull request can name that secret (Zuul: "Secrets must be defined in the same project in which they are used") |
| `pipeline` | `post` |
| `job-name` | `aid-publish-apps` or `aid-release` |
| `exp` | at most three hours after `iat` |

The test bed's check 11 has Zuul 14.3.0 sign such a token in `post` and checks each claim, and its signature against
Zuul's published keys ([`README.md`](README.md), "The test bed"). The change to the pusher is ThunderBox's (its
repository holds the pusher).

**Where it fetches the keys.** In the cluster, from Zuul's web: `http://web.zuul.svc.cluster.local:9000/oidc/jwks`.
The namespace `zuul` lets in only what its policies name ([`deploy/zuul/namespace/networkpolicy.yaml`](../../deploy/zuul/namespace/networkpolicy.yaml)),
so AIDepartment's policy `web-pusher` lets the pusher, the pod `pusher` in `arc-release`, reach `web`'s port 9000, and
no other pod of another namespace (#1601). The pusher's egress to it is ThunderBox's. Without both, every `push` stage
is refused with 401: the pusher cannot fetch the keys, so the token's key is not one of the issuer's. ThunderBox also lets the post node reach the API for the image check and the pusher
([`deploy/zuul/post-node/README.md`](../../deploy/zuul/post-node/README.md), "What ThunderBox grants"). Its BuildKit is
its own, which only it reaches ([`deploy/zuul/post-buildkit/`](../../deploy/zuul/post-buildkit/README.md), #1566).

## The release index

The index branch `release-index` now holds the whole Pages site in each commit: `index.json`, the page that asks
crawlers not to list it, `robots.txt`, and `.nojekyll` (`ci/release-index.mjs`, `siteFiles`). So GitHub Pages can serve
the branch as it is, and no job deploys it: GitHub's Pages deployment API takes only an Actions artifact and an Actions
OIDC token, which no other CI has. The address stays `https://kristofk.github.io/AIDepartment/index.json`.

Pages then builds the branch itself, with its own workflow (`pages-build-deployment`) running from `release-index`,
in the `github-pages` environment, whose branch rule must allow `release-index` (step 5). What starts that build:

- **A record by Zuul's jobs**: their push, with the App's token, starts it.
- **A record by GitHub Actions**: its push, with Actions' own `GITHUB_TOKEN`, starts none (GitHub's documentation,
  "Troubleshooting publishing from a branch"). The index jobs already dispatch `release-index.yml` after each record;
  once Pages serves the branch, that workflow asks Pages for a build (`node ci/release-index.mjs pages`, `POST
  /repos/<repository>/pages/builds`) instead of deploying. So the index stays published while Actions still
  publishes (until step 6) and after a return to Actions.

Until Pages is set to serve the branch, `release-index.yml` deploys the same files from Actions. Pages from a branch is
GitHub's own build of what is on it, the one way to publish Pages without Actions; on another forge, the site's files
are on the branch to serve from anywhere.

One thing changes: a merge that changes how the site is written (`ci/release-index.mjs`) shows on Pages at the next
record, not at once (#972's re-deploy).

## Which CI publishes

[`ci/publisher`](../publisher) names `actions` or `zuul`. Each CI's publishing reads it, and the other's runs publish
nothing:

- **Actions:** the app workflows' publish (`ci/image-policy.mjs`), the release's plan (`ci/aid-release.mjs`), and the
  nightly's clock (`ci/nightly.mjs`), so its report too.
- **Zuul:** the same three, run by its jobs with `CI_SYSTEM=zuul`, and `aid-nightly-report`.

A pull request that changes it moves publishing from one CI to the other at its merge. That merge itself is published
by the CI it names, since each reads the file as merged. It builds no image anywhere: it is in no app workflow's
`paths` ([`ci/workflow-policy.test.mjs`](../workflow-policy.test.mjs), `notTriggers`). #1355 removes the file with
Actions' workflows.

## Turning it on

In order, each step only once the one before is done. Each says whose it is.

0. **The post node's memory** (Kristóf's decision): ThunderBox's documented node budget is full (14336 of 14336 MiB),
   and the post node requests 128 MiB more. Raise the node's cap by that, or give it up elsewhere
   ([`deploy/zuul/post-node/README.md`](../../deploy/zuul/post-node/README.md), "Its share of the node").
1. **The App's permission** (Kristóf): in `aid-release`'s settings, add **Issues: read and write**, and accept it on
   the installation. Check: aid-zuul-config's `aid-release-credentials` still passes on the next merge.
2. **The post node** (the admin, after a heads-up to the orchestrator): [`deploy/zuul/post-node/README.md`](../../deploy/zuul/post-node/README.md),
   "Applying it", with `deploy/build-farm/images/admission.yaml` applied first. Then its BuildKit, with its dataset,
   the node's mount and the node's memory cap raised by its 2560 MiB (Kristóf's decision on #1566):
   [`deploy/zuul/post-buildkit/README.md`](../../deploy/zuul/post-buildkit/README.md), "Applying it".
3. **ThunderBox's grants** (ThunderBox's session): the post node's reach and its binding in the image-check namespace,
   the scratch registry admitting the post node's BuildKit, the pusher's acceptance of Zuul's token
   ([The pusher](#the-pusher)), and the endpoints' values. `BUILDKIT_HOST` is the post node's own BuildKit's,
   `tcp://buildkitd.zuul-post-buildkit.svc:1234`.

   With these, the admin applies `zuul`'s namespace for its policy `web-pusher` (#1601), from a checkout of `main` that
   has it: `kubectl kustomize deploy/zuul/namespace | ssh thunderbox 'sudo docker exec -i ci-k3s-1 kubectl apply -f -'`.
   It must be live before step 6, not before step 4: while `ci/publisher` names Actions, the jobs push nothing. Check:
   from the pusher's pod, `wget -qO- http://web.zuul.svc.cluster.local:9000/oidc/jwks` lists Zuul's keys, and from a
   pod in another namespace, `zuul-nodes` for one, the same `wget -T 5` cannot connect.
4. **aid-zuul-config** (the orchestrator merges, after the other bot's review): the post node in `providers.yaml`'s
   provider with its host key, `publish_endpoints` and `zuul_web_root` set in `zuul.d/publish.yaml`, and the project
   stanza that runs its jobs, which that file gives in a comment: until this step no project runs them. Its `check`
   loads them; after the merge `/api/tenant/aid/config-errors` is `[]`, and the next merge into `main` runs
   `aid-publish-apps` and `aid-release`, which publish nothing while `ci/publisher` names Actions. Check: both pass,
   and their `build` stage's log says so.
5. **Pages from the branch** (decided by Kristóf on 2026-11-15, `docs/decisions.md`, with `contracts/application.md`,
   "The release index", saying so), Kristóf's, in this order:
   1. Let the `github-pages` environment deploy from `release-index`, keeping `main`, which `release-index.yml` still
      runs from: `gh api -X POST repos/kristofk/AIDepartment/environments/github-pages/deployment-branch-policies -f
      name=release-index -f type=branch`. Check: `gh api
      repos/kristofk/AIDepartment/environments/github-pages/deployment-branch-policies --jq '[.branch_policies[].name]'`
      lists `main` and `release-index`.
   2. Settings › Pages › Build and deployment: **Deploy from a branch**, `release-index`, `/ (root)`.
   3. Check, before step 6: `gh workflow run release-index.yml --ref main`. Its run says "Pages is asked to build it",
      a `pages-build-deployment` run from `release-index` follows and succeeds (`gh run list --workflow
      pages-build-deployment`), and `curl -sf https://kristofk.github.io/AIDepartment/index.json` gives the branch's
      `index.json`. The branch holds the whole site from its first record after #1550; before that it has
      `index.json` alone, which Pages serves as it is.

   Undo: Settings › Pages back to **GitHub Actions**, then `gh workflow run release-index.yml --ref main`, which
   deploys again.
6. **The switch**: a pull request that sets `ci/publisher` to `zuul`. From its merge, Zuul publishes, and Actions'
   publishing jobs do nothing.
7. **Seen to work** (#1350's "Done when"): an app's change merged, and a patch release (`VERSION` and `CHANGELOG.md`),
   published by Zuul's jobs; the index lists both with the digests the pusher returned, and Pages serves it.

Undo: a pull request setting `ci/publisher` back to `actions`; Actions publishes again from its merge, and its
records reach Pages through `release-index.yml`'s build request ([The release index](#the-release-index)).

## Running a publishing job again

A failed `post` item runs again as an operator enqueues its push once more, through zuul-web's admin API
([`deploy/zuul/web.md`](../../deploy/zuul/web.md), "The admin API", #1616). The orchestrator decides it, and runs it or
has a bot run it; either needs to run a command in web's pod, the tenant's `~/.kube/devinfra.yaml` or the admin's
`ssh thunderbox`. `<before>` and `<after>` are the push's, both full SHAs: the failed buildset's `refs[0].oldrev` and
`refs[0].newrev`, which a box reads with `curl -s 'http://zuul-web:9000/api/tenant/aid/buildsets?pipeline=post&limit=5'
| jq '.[] | {uuid, result, refs}'`.

```sh
export KUBECONFIG=~/.kube/devinfra.yaml
kubectl -n zuul exec -i deploy/web -- sh -s <who> enqueue-ref --tenant aid --pipeline post \
  --project kristofk/AIDepartment --ref refs/heads/main --oldrev <before> --newrev <after> < deploy/zuul/admin-api.sh
```

It prints nothing but zuul-admin's deprecation warning, and exits 0, once Zuul has the event; a new `post` buildset for
`<after>` then shows in `buildsets?pipeline=post`. It runs every `post` job again for that push, not only the failed
one: the credentials, the apps and the release. Each starts from `build`:

- **An app not published** (its `push` failed): the run builds it again and publishes it. An app published and not
  recorded is published again too, under the same version, which the pusher replaces, and recorded.
- **A release not published**: the run builds anew. Once its GitHub Release exists, the plan refuses the version, a tag
  never moving. If only the index lacks it, record its entry by hand, from the release's `aid-release.json`: `node
  ci/aid-index.mjs record aid-release.json <aid digest> <aid-broker digest> <aid-ingress digest>`, each digest the one
  after `@` in the entry's `images`, from a checkout of `main` that can push.
