"""The test bed's checks (../README.md, "The test bed"), against a running Zuul that run.sh started.

Each check is an ordinary pull request in the test bed's copy of AIDepartment, made the way an agent makes one: a
branch, a commit and a pull request. The test bed then sends Zuul the event GitHub would send (Zuul has no route from
GitHub here), and reads the result from Zuul's API and from the commit status Zuul sets on the pull request.

  1. The layout loads: the copy of kristofk/aid-zuul-config as the config project, and AID's project, with no
     configuration error.
  2. A pull request that adds a job runs that job on itself, before it exists on main.
  3. A pull request that defines a base job in AID's project is refused, with a configuration error.
  4. A pull request that defines a pipeline in AID's project is refused, with a configuration error.
  5. A proposed job can read the fixture farm token in every phase of check, but none of its
     pre/run/post/cleanup hooks execute in check-external: the trusted base rejects it first.
  6. A job with a secret, defined in the config project, gets it in post on main; one whose secret may not go to post
     is failed by the trusted guard before its run playbook (deploy/zuul/job-secrets.md).
  7. A pull request cannot have that secret: Zuul refuses it in check, a job inheriting the job, and a job naming the
     secret.

Standard library only. Reads GITHUB_TOKEN and ZUUL_WEBHOOK_TOKEN from the environment.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

GITHUB_API = "https://api.github.com"


class Failure(Exception):
    pass


def log(message):
    print(message, flush=True)


def request(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            text = response.read().decode()
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as error:
        text = error.read().decode(errors="replace")
        try:
            return error.code, json.loads(text)
        except ValueError:
            return error.code, text


class GitHub:
    def __init__(self, repository, token):
        self.repository = repository
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        status, data = request(method, f"{GITHUB_API}{path}", body, self.headers)
        if status not in ok:
            raise Failure(f"GitHub {method} {path}: {status} {data}")
        return data

    def repo(self, path="", **kwargs):
        method = kwargs.pop("method", "GET")
        return self.call(method, f"/repos/{self.repository}{path}", **kwargs)


class Zuul:
    def __init__(self, url, tenant, webhook_token):
        self.url = url.rstrip("/")
        self.tenant = tenant
        self.webhook_token = webhook_token

    def get(self, path, ok=(200,)):
        status, data = request("GET", f"{self.url}/api/tenant/{self.tenant}{path}")
        if status not in ok:
            raise Failure(f"Zuul GET {path}: {status} {data}")
        return status, data

    def send(self, event_type, body):
        """Posts an event as GitHub's webhook would, signed with the connection's webhook token."""
        payload = json.dumps(body).encode()
        signature = "sha1=" + hmac.new(self.webhook_token.encode(), payload, hashlib.sha1).hexdigest()
        req = urllib.request.Request(
            f"{self.url}/api/connection/github/payload", data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-GitHub-Event": event_type,
                     "X-GitHub-Delivery": str(uuid.uuid4()), "X-Hub-Signature": signature})
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status != 200:
                raise Failure(f"Zuul refused the {event_type} event: {response.status}")


def wait(what, probe, timeout, interval=5):
    """Calls probe until it returns something true, and returns that."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            last = error
        time.sleep(interval)
    raise Failure(f"timed out after {timeout} s waiting for {what}" + (f" (last error: {last})" if last else ""))


class Testbed:
    def __init__(self, args):
        self.args = args
        self.github = GitHub(args.repository, os.environ["GITHUB_TOKEN"])
        self.zuul = Zuul(args.zuul, args.tenant, os.environ["ZUUL_WEBHOOK_TOKEN"])
        self.run = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
        self.opened = []
        self.branches = []
        self.worktree = tempfile.mkdtemp(prefix="testbed-")
        self.git("worktree", "add", "--detach", self.worktree, "origin/main", cwd=args.checkout)
        self.git("config", "user.name", "Zuul test bed")
        self.git("config", "user.email", "testbed@zuul.invalid")

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.worktree, capture_output=True, text=True)
        if result.returncode != 0:
            raise Failure(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def commit(self, files, message):
        """Writes the files (None deletes one) on top of origin/main, commits them, and returns the commit."""
        self.git("fetch", "--quiet", "origin", "main")
        self.git("checkout", "--quiet", "--detach", "origin/main")
        for path, content in files.items():
            full = os.path.join(self.worktree, path)
            if content is None:
                self.git("rm", "--quiet", path)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(content)
            self.git("add", path)
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def open_pull_request(self, name, files, title):
        branch = f"testbed/{self.run}/{name}"
        sha = self.commit(files, title)
        self.git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        self.branches.append(branch)
        pull = self.github.repo("/pulls", method="POST",
                                body={"title": title, "head": branch, "base": "main", "body": "Made by the Zuul test bed."})
        self.opened.append(pull["number"])
        if pull["head"]["sha"] != sha:
            raise Failure(f"pull request #{pull['number']} has head {pull['head']['sha']}, not {sha}")
        log(f"  opened #{pull['number']} ({branch} at {sha[:12]}): {title}")
        repository = self.github.repo()
        self.zuul.send("pull_request", {"action": "opened", "number": pull["number"], "pull_request": pull,
                                        "repository": repository, "sender": {"login": pull["user"]["login"]}})
        return pull

    def check_result(self, pull, timeout=1200, pipeline="check"):
        """Waits for the check pipeline's result on the pull request's head; returns the buildset and its builds."""
        number, sha = pull["number"], pull["head"]["sha"]
        query = urllib.parse.urlencode({"project": self.args.repository, "change": number, "patchset": sha,
                                        "pipeline": pipeline, "complete": "true"})

        def done():
            _, buildsets = self.zuul.get(f"/buildsets?{query}")
            return buildsets[0] if buildsets else None

        buildset = wait(f"{pipeline}'s result on #{number}", done, timeout)
        _, builds = self.zuul.get(f"/builds?{query}")
        self.last_builds = builds
        results = {build["job_name"]: build["result"] for build in builds}
        log(f"  #{number}: {pipeline} {buildset['result']}, builds {results}")
        return buildset, results

    def commit_status(self, pull, timeout=120, pipeline="check"):
        """The state of the commit status Zuul's check pipeline set on the pull request's head (tenant/pipeline)."""
        context = f"{self.args.tenant}/{pipeline}"

        def final():
            statuses = self.github.repo(f"/commits/{pull['head']['sha']}/statuses")
            mine = [status for status in statuses if status["context"] == context]
            return mine and mine[0]["state"] != "pending" and mine[0]

        status = wait(f"Zuul's {context} status on #{pull['number']}", final, timeout)
        log(f"  #{pull['number']}: GitHub shows {context} {status['state']}: {status['description']}")
        return status["state"]

    def external_comment(self, pull):
        comment = self.github.repo(f"/issues/{pull['number']}/comments", method="POST", body={"body": "check-external"})
        issue = self.github.repo(f"/issues/{pull['number']}")
        self.zuul.send("issue_comment", {"action": "created", "issue": issue, "comment": comment,
                                       "repository": self.github.repo(), "sender": comment["user"]})

    def comments(self, pull):
        return [comment["body"] for comment in self.github.repo(f"/issues/{pull['number']}/comments")]

    def job_on_main(self, name):
        status, _ = self.zuul.get(f"/job/{name}", ok=(200, 404))
        return status == 200

    def cleanup(self):
        for number in self.opened:
            try:
                self.github.repo(f"/pulls/{number}", method="PATCH", body={"state": "closed"})
            except Failure as error:
                log(f"  could not close #{number}: {error}")
        for branch in self.branches:
            try:
                self.github.repo(f"/git/refs/heads/{branch}", method="DELETE")
            except Failure as error:
                log(f"  could not delete {branch}: {error}")
        subprocess.run(["git", "worktree", "remove", "--force", self.worktree], cwd=self.args.checkout)


def expect(condition, message):
    if not condition:
        raise Failure(message)
    log(f"  ok: {message}")


def layout_loads(bed):
    log("1. The layout loads")
    names = {"check", "check-external", "gate"}

    def loaded():
        _, pipelines = bed.zuul.get("/pipelines", ok=(200, 404))
        return isinstance(pipelines, list) and names <= {pipeline["name"] for pipeline in pipelines}

    wait("the tenant's pipelines", loaded, 600)
    _, errors = bed.zuul.get("/config-errors")
    expect(errors == [], f"the tenant has no configuration error {[error.get('error', '')[:300] for error in errors]}")
    _, projects = bed.zuul.get("/projects")
    found = {(project["canonical_name"], project["type"]) for project in projects}
    expect((f"github.com/{bed.args.config_repository}", "config") in found,
           f"{bed.args.config_repository} is the config project")
    expect((f"github.com/{bed.args.repository}", "untrusted") in found, f"{bed.args.repository} is AID's untrusted project")
    _, jobs = bed.zuul.get("/jobs")
    expect({"base", "aid-bazel", "aid-script", "aid-seam-lint"} <= {job["name"] for job in jobs},
           "the base job and AID's job library are loaded")
    if bed.args.secret:
        expect({"testbed-secret-probe", "testbed-secret-guard-probe"} <= {job["name"] for job in jobs},
               "the test bed's jobs with a secret are loaded: Zuul decrypted the fixture with the imported key")
        status, key = request("GET", f"{bed.zuul.url}/api/tenant/{bed.zuul.tenant}/key/{bed.args.config_repository}.pub")
        fixture = subprocess.run(["openssl", "pkey", "-in", bed.args.project_key, "-pubout"],
                                 capture_output=True, text=True, check=True).stdout
        expect(status == 200 and der(key) == der(fixture), "the config copy's key in Zuul is the test bed's own")


NEW_JOB = """\
# Added by the test bed's pull request: a job of AID's, from the library, and the check pipeline running it.
- job:
    name: aid-testbed-hello
    parent: aid-bazel
    description: Builds the test bed's one target.
    vars:
      bazel_command: build
      bazel_targets: ["//testbed/hello:hello"]

- project:
    check:
      jobs:
        - aid-testbed-hello
"""

NEW_TARGET = """\
genrule(
    name = "hello",
    outs = ["hello.txt"],
    cmd = "echo hello > $@",
)
"""


def new_job_runs_speculatively(bed):
    log("2. A pull request that adds a job runs it on itself")
    expect(not bed.job_on_main("aid-testbed-hello"), "main has no job aid-testbed-hello")
    pull = bed.open_pull_request("new-job", {
        "ci/zuul/zuul.d/testbed-hello.yaml": NEW_JOB,
        "testbed/hello/BUILD.bazel": NEW_TARGET,
    }, "A new job: aid-testbed-hello")
    buildset, builds = bed.check_result(pull)
    expect(builds.get("aid-testbed-hello") == "SUCCESS", "the new job ran on its own pull request, and passed")
    expect(builds.get("aid-seam-lint") == "SUCCESS", "AID's existing job ran beside it, and passed")
    expect(buildset["result"] == "SUCCESS", "check passed")
    expect(bed.commit_status(pull) == "success", "Zuul reported success on the pull request")
    expect(not bed.job_on_main("aid-testbed-hello"), "main still has no job aid-testbed-hello: it ran speculatively")


BASE_JOB = """\
- job:
    name: testbed-base
    parent: null
    description: A base job of AID's own, which would replace the config project's checkout.
"""


def untrusted_base_job_is_refused(bed):
    log("3. A pull request that defines a base job in AID's project is refused")
    pull = bed.open_pull_request("untrusted-base-job", {"ci/zuul/zuul.d/testbed-base.yaml": BASE_JOB},
                                 "A base job in AID's untrusted project")
    expect(bed.commit_status(pull, timeout=600) == "failure", "Zuul reported failure on the pull request")
    comments = wait(f"Zuul's comment on #{pull['number']}", lambda: bed.comments(pull), 120)
    # Zuul's own words for it (zuul/exceptions.py, BaseJobNotPermittedError).
    expect(any("Base jobs may only be defined in config projects" in " ".join(body.split()) for body in comments),
           f"its comment says why: {comments[-1][:300]!r}")


PIPELINE = """\
- pipeline:
    name: testbed-untrusted
    manager: independent
    trigger:
      github:
        - event: pull_request
          action: comment
          comment: ^never$
"""


def untrusted_pipeline_is_refused(bed):
    log("4. A pull request that defines a pipeline in AID's project is refused")
    pull = bed.open_pull_request("untrusted-pipeline", {"ci/zuul/zuul.d/testbed-pipeline.yaml": PIPELINE},
                                 "A pipeline in AID's untrusted project")
    expect(bed.commit_status(pull, timeout=600) == "failure", "Zuul reported failure on the pull request")
    comments = wait(f"Zuul's comment on #{pull['number']}", lambda: bed.comments(pull), 120)
    # Zuul's own words for it (zuul/exceptions.py, PipelineNotPermittedError).
    expect(any("Pipelines may not be defined in untrusted repos" in " ".join(body.split()) for body in comments),
           f"its comment says why: {comments[-1][:300]!r}")


def container_text(container, path):
    result = subprocess.run(["docker", "exec", os.environ[container], "cat", path],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise Failure(f"cannot read test fixture {path}: {result.stderr[:200]}")
    return result.stdout


def external_job_cannot_read_farm(bed):
    log("5. check-external is rejected before every untrusted hook, including post and cleanup")
    job = "aid-testbed-farm-probe"
    files = {"ci/zuul/zuul.d/testbed-farm.yaml": f"""\
- job:
    name: {job}
    parent: aid-script
    pre-run: ci/zuul/playbooks/farm-pre.yaml
    run: ci/zuul/playbooks/farm-run.yaml
    post-run: ci/zuul/playbooks/farm-post.yaml
    cleanup-run: ci/zuul/playbooks/farm-cleanup.yaml
- project:
    check:
      jobs: [{job}]
    check-external:
      jobs: [{job}]
"""}
    for phase in ("pre", "run", "post", "cleanup"):
        files[f"ci/zuul/playbooks/farm-{phase}.yaml"] = """\
- hosts: all
  gather_facts: false
  tasks:
    - name: Read the raw fixture credential from proposed code
      ansible.builtin.shell: |
        printf '{{ zuul.pipeline }}:PHASE:' >> /tmp/farm-hook-probe
        cat /var/run/farm/token >> /tmp/farm-hook-probe
""".replace("PHASE", phase)
    pull = bed.open_pull_request("external-farm-probe", files, "Probe the external-job farm boundary in every hook")
    _, builds = bed.check_result(pull)
    expect(builds.get(job) == "SUCCESS", "the proposed probe actually executes in check")
    before = container_text("TESTBED_NODE_ID", "/tmp/farm-hook-probe")
    expected = {f"check:{phase}:testbed-farm-credential" for phase in ("pre", "run", "post", "cleanup")}
    expect(set(before.splitlines()) == expected and len(before.splitlines()) == 4,
           "all four untrusted hooks can read the raw token fixture in check")
    bed.external_comment(pull)
    buildset, builds = bed.check_result(pull, pipeline="check-external")
    expect(builds.get(job) == "FAILURE" and buildset["result"] == "FAILURE",
           "the external probe fails without retries")
    build = next(build for build in bed.last_builds if build["job_name"] == job)
    build_uuid = uuid.UUID(build["uuid"]).hex
    output = container_text("TESTBED_EXECUTOR_ID", f"/var/lib/zuul/builds/{build_uuid}/work/logs/job-output.txt")
    expect("check-external is blocked until a credential-free node is available" in output,
           "the failure is the trusted pipeline guard, not a broken job or node")
    expect(container_text("TESTBED_NODE_ID", "/tmp/farm-hook-probe") == before,
           "no external pre/run/post/cleanup hook executed or read the token")
    expect(bed.commit_status(pull, pipeline="check-external") == "failure", "the external failure is reported")


def der(pem):
    """A public key in PEM, as its DER bytes: the same key, whatever the line breaks."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    return base64.b64decode(body)


def post_job_gets_secret(bed):
    log("6. A job with a secret gets it in post, on main; the guard fails one whose secret may not go there")
    main = bed.github.repo("/commits/main")
    repository = bed.github.repo()
    before = main["parents"][0]["sha"] if main["parents"] else "0" * 40
    bed.zuul.send("push", {"ref": "refs/heads/main", "before": before, "after": main["sha"], "commits": [],
                           "repository": repository, "sender": {"login": repository["owner"]["login"]}})
    query = urllib.parse.urlencode({"project": bed.args.repository, "pipeline": "post", "complete": "true"})
    names = {"testbed-secret-probe", "testbed-secret-guard-probe"}

    def done():
        _, builds = bed.zuul.get(f"/builds?{query}")
        mine = [build for build in builds if build.get("newrev") == main["sha"]]
        return names <= {build["job_name"] for build in mine} and mine

    builds = {build["job_name"]: build for build in wait("post's builds on main", done, 900)}
    log(f"  post on main at {main['sha'][:12]}: " + str({name: build["result"] for name, build in builds.items()}))
    expect(all(build["ref"] == "refs/heads/main" for build in builds.values()), "post ran on main, a ref, not a change")

    def output(name):
        build_uuid = uuid.UUID(builds[name]["uuid"]).hex
        return container_text("TESTBED_EXECUTOR_ID", f"/var/lib/zuul/builds/{build_uuid}/work/logs/job-output.txt")

    probe = output("testbed-secret-probe")
    digest = hashlib.sha256(bed.args.secret.encode()).hexdigest()
    expect(builds["testbed-secret-probe"]["result"] == "SUCCESS", "the job that may have its secret passed")
    expect(f"testbed secret sha256 {digest}" in probe, "the value that reached it is the fixture's, by its SHA-256")
    _, found = bed.zuul.get(f"/build/{builds['testbed-secret-probe']['uuid']}")
    artifacts = {artifact["name"]: artifact.get("metadata", {}) for artifact in found.get("artifacts", [])}
    expect(artifacts.get("testbed-secret", {}).get("sha256") == digest,
           "the builds API shows the same, as the build's artifact: how ThunderBox's check is read")
    expect(bed.args.secret not in probe, "the value itself is not in its log")
    guarded = output("testbed-secret-guard-probe")
    expect(builds["testbed-secret-guard-probe"]["result"] == "FAILURE", "the job whose secret may not go to post failed")
    expect("has a secret, and runs only on a merged ref" in " ".join(guarded.split()),
           "the guard failed it, not anything else")
    expect("testbed secret sha256" not in guarded, "its run playbook, which uses the secret, never ran")


SECRET_IN_CHECK = """\
- project:
    check:
      jobs:
        - testbed-secret-probe
"""

SECRET_CHILD = """\
- job:
    name: testbed-secret-child
    parent: testbed-secret-probe
    run: ci/zuul/playbooks/script.yaml
- project:
    check:
      jobs:
        - testbed-secret-child
"""

SECRET_NAMED = """\
- job:
    name: testbed-secret-taker
    parent: aid-script
    secrets:
      - name: testbed_secret
        secret: testbed-secret
    vars:
      script: scripts/seam-lint.sh
      script_args: [--project, aid]
- project:
    check:
      jobs:
        - testbed-secret-taker
"""


def pull_request_cannot_have_secret(bed):
    log("7. A pull request cannot have the secret")
    cases = [
        ("secret-in-check", SECRET_IN_CHECK, "The job with the secret in check",
         # Zuul's words (model.py, the job graph's pending errors).
         "Pre-review pipeline check does not allow post-review job testbed-secret-probe"),
        ("secret-child", SECRET_CHILD, "A job of AID's inheriting the job with the secret",
         'The parent of job "testbed-secret-child", "testbed-secret-probe" is final and can not act as a parent'),
        ("secret-named", SECRET_NAMED, "A job of AID's naming the config project's secret",
         "Unable to use secret testbed-secret. Secrets must be defined in the same project in which they are used"),
    ]
    for name, text, title, words in cases:
        pull = bed.open_pull_request(name, {f"ci/zuul/zuul.d/testbed-{name}.yaml": text}, title)
        expect(bed.commit_status(pull, timeout=600) == "failure", f"{title}: Zuul reported failure")
        comments = wait(f"Zuul's comment on #{pull['number']}", lambda: bed.comments(pull), 120)
        expect(any(words in " ".join(body.split()) for body in comments), f"{title}: refused, {words!r}")
        _, builds = bed.zuul.get(f"/builds?project={urllib.parse.quote(bed.args.repository)}&change={pull['number']}")
        expect(not [build for build in builds if build["job_name"].startswith("testbed-secret")
                    and build["result"] == "SUCCESS"], f"{title}: no job of the pull request's with the secret ran")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zuul", required=True, help="zuul-web's URL")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repository", required=True, help="the test bed's copy of AIDepartment, owner/name")
    parser.add_argument("--config-repository", required=True, help="its copy of kristofk/aid-zuul-config, owner/name")
    parser.add_argument("--checkout", required=True, help="a checkout of it, that can push")
    parser.add_argument("--secret", default="", help="the fixture secret sync.sh encrypted; none skips checks 6 and 7")
    parser.add_argument("--project-key", help="the test bed's project key for the config copy, sync.sh's")
    args = parser.parse_args()
    bed = Testbed(args)
    try:
        checks = [layout_loads, new_job_runs_speculatively, untrusted_base_job_is_refused,
                  untrusted_pipeline_is_refused, external_job_cannot_read_farm]
        if args.secret:
            checks += [post_job_gets_secret, pull_request_cannot_have_secret]
        else:
            log("Checks 6 and 7 are left out: the aid-zuul-config checkout has no secrets guard (sync.sh).")
        for check in checks:
            check(bed)
    except Failure as error:
        log(f"FAILED: {error}")
        return 1
    finally:
        bed.cleanup()
    log(f"All {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
