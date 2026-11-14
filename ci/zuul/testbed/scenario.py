"""The test bed's checks (../README.md, "The test bed"), against a running Zuul that run.sh started.

Each check is an ordinary pull request in the test bed's copy of AIDepartment, made the way an agent makes one: a
branch, a commit and a pull request. The test bed then sends Zuul the event GitHub would send (Zuul has no route from
GitHub here), and reads the result from Zuul's API and from the commit status Zuul sets on the pull request.

  1. The layout loads: the copy of kristofk/aid-zuul-config as the config project, and AID's project, with no
     configuration error.
  2. A pull request that adds a job runs that job on itself, before it exists on main.
  3. A pull request that defines a base job in AID's project is refused, with a configuration error.
  4. A pull request that defines a pipeline in AID's project is refused, with a configuration error.

Then the gate's rows (../gate.md, "From Mergify to Zuul"), each seen on the test bed's copies:

  5. A job runs no longer than its timeout.
  6. Ready means queued: a draft whose check passed stays out of gate; once ready, it enters, gate runs the full jobs
     (and not check's cheap ones), and Zuul merges it by rebase, so main stays linear.
  7. The hold label keeps a ready pull request out of gate, and takes one out that is in it; without it, it comes back.
  8. The speculative queue: two pull requests are tested at once, the second on top of the first, in a window of two.
     The first fails (a flaky test), which starts the second again without it, and the second merges. A "regate"
     comment enqueues the first again, and it merges.
  9. A ready pull request without hold enters gate when check passes on its head, with no other event. A push takes
     it out, and it comes back once check passes on the new head.

Standard library only. Reads GITHUB_TOKEN and ZUUL_WEBHOOK_TOKEN from the environment.
"""

import argparse
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

    def commit(self, files, message, base="main"):
        """Writes the files (None deletes one; a .sh is executable) on top of origin's base branch, commits them, and
        returns the commit."""
        self.git("fetch", "--quiet", "origin", base)
        self.git("checkout", "--quiet", "--detach", f"origin/{base}")
        for path, content in files.items():
            full = os.path.join(self.worktree, path)
            if content is None:
                self.git("rm", "--quiet", path)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(content)
            # A script a job runs as ./<script> must be executable, in the commit too.
            os.chmod(full, 0o755 if path.endswith(".sh") else 0o644)
            self.git("add", path)
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def open_pull_request(self, name, files, title, draft=False):
        branch = f"testbed/{self.run}/{name}"
        sha = self.commit(files, title)
        self.git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        self.branches.append(branch)
        pull = self.github.repo("/pulls", method="POST", body={
            "title": title, "head": branch, "base": "main", "body": "Made by the Zuul test bed.", "draft": draft})
        self.opened.append(pull["number"])
        if pull["head"]["sha"] != sha:
            raise Failure(f"pull request #{pull['number']} has head {pull['head']['sha']}, not {sha}")
        log(f"  opened #{pull['number']}{' as a draft' if draft else ''} ({branch} at {sha[:12]}): {title}")
        self.send_pull_request("opened", pull)
        return pull

    def push(self, pull, files, message):
        """Pushes a commit onto the pull request's branch, sends Zuul the event GitHub would, and returns the pull
        request with its new head."""
        branch = pull["head"]["ref"]
        sha = self.commit(files, message, base=branch)
        self.git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        wait(f"#{pull['number']}'s new head on GitHub", lambda: self.pull(pull)["head"]["sha"] == sha, 120, interval=2)
        log(f"  pushed {sha[:12]} to #{pull['number']}: {message}")
        return self.send_pull_request("synchronize", pull)

    def pull(self, pull):
        """The pull request as GitHub has it now."""
        return self.github.repo(f"/pulls/{pull['number']}")

    def send_pull_request(self, action, pull, **extra):
        """Sends Zuul the pull_request event GitHub would, with the pull request as it is now."""
        body = self.pull(pull)
        self.zuul.send("pull_request", {"action": action, "number": body["number"], "pull_request": body,
                                        "repository": self.github.repo(), "sender": {"login": body["user"]["login"]},
                                        **extra})
        return body

    def mark_ready(self, pull):
        self.github.call("POST", "/graphql", body={
            "query": "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
                     "{ pullRequest { isDraft } } }",
            "variables": {"id": pull["node_id"]}})
        body = self.send_pull_request("ready_for_review", pull)
        if body["draft"]:
            raise Failure(f"#{pull['number']} is still a draft")
        log(f"  #{pull['number']}: ready for review")

    def add_label(self, pull, label):
        self.github.repo(f"/issues/{pull['number']}/labels", method="POST", body={"labels": [label]})
        self.send_pull_request("labeled", pull, label={"name": label})
        log(f"  #{pull['number']}: labelled {label}")

    def remove_label(self, pull, label):
        self.github.repo(f"/issues/{pull['number']}/labels/{urllib.parse.quote(label)}", method="DELETE")
        self.send_pull_request("unlabeled", pull, label={"name": label})
        log(f"  #{pull['number']}: {label} removed")

    def comment(self, pull, text):
        comment = self.github.repo(f"/issues/{pull['number']}/comments", method="POST", body={"body": text})
        self.zuul.send("issue_comment", {"action": "created", "issue": self.github.repo(f"/issues/{pull['number']}"),
                                         "comment": comment, "repository": self.github.repo(),
                                         "sender": {"login": comment["user"]["login"]}})
        log(f"  #{pull['number']}: commented {text!r}")

    def send_check_passed(self, pull):
        """Waits for check's success on the head, then sends Zuul the status event GitHub would for it: gate's trigger
        on check's result (on ThunderBox, its check run; on the test bed, its status, sync.sh)."""
        expect(self.commit_status(pull) == "success", f"check passed on #{pull['number']}")
        sha = pull["head"]["sha"]
        status = next(status for status in self.github.repo(f"/commits/{sha}/statuses")
                      if status["context"] == f"{self.args.tenant}/check")
        self.zuul.send("status", {"sha": sha, "name": self.args.repository, "context": status["context"],
                                  "state": status["state"], "description": status["description"],
                                  "repository": self.github.repo(), "sender": status["creator"]})

    def buildsets(self, pull, pipeline):
        """The pipeline's complete buildsets of the pull request's head, newest first."""
        query = urllib.parse.urlencode({"project": self.args.repository, "change": pull["number"],
                                        "patchset": pull["head"]["sha"], "pipeline": pipeline, "complete": "true"})
        return self.zuul.get(f"/buildsets?{query}")[1]

    def builds(self, pull, pipeline):
        """The pipeline's builds of the pull request's head, finished or not, newest first."""
        query = urllib.parse.urlencode({"project": self.args.repository, "change": pull["number"],
                                        "patchset": pull["head"]["sha"], "pipeline": pipeline})
        return self.zuul.get(f"/builds?{query}")[1]

    def result(self, pull, pipeline="check", timeout=1200, after=0):
        """Waits for a pipeline's result on the pull request's head, its buildset after the first `after` of them.
        Returns the buildset and its builds' results."""
        number = pull["number"]

        def done():
            # In gate, a pull request's buildset is started again when one ahead of it fails: its result is the one
            # it has once it has left gate.
            if pipeline == "gate" and self.in_gate(pull):
                return None
            buildsets = self.buildsets(pull, pipeline)
            return len(buildsets) > after and buildsets[0]

        buildset = wait(f"{pipeline}'s result on #{number}", done, timeout)
        _, detail = self.zuul.get(f"/buildset/{buildset['uuid']}")
        results = {build["job_name"]: build["result"] for build in detail.get("builds", [])}
        log(f"  #{number}: {pipeline} {buildset['result']}, builds {results}")
        return buildset, results

    def check_result(self, pull, timeout=1200):
        """Waits for the check pipeline's result on the pull request's head; returns the buildset and its builds."""
        return self.result(pull, "check", timeout)

    def gate(self):
        """gate's one queue, as Zuul's status shows it: its window, and its items, each with its pull request's
        number, the item ahead of it and its jobs."""
        _, status = self.zuul.get("/status")
        pipeline = next(pipeline for pipeline in status["pipelines"] if pipeline["name"] == "gate")
        # Zuul makes a dependent pipeline's queue when a change enters it, and removes it once it is empty.
        queues = pipeline["change_queues"]
        if not queues:
            return None, []
        if len(queues) != 1:
            raise Failure(f"gate has {len(queues)} queues, not one: {[queue['name'] for queue in queues]}")
        items = [item for head in queues[0]["heads"] for item in head]
        for item in items:
            item["number"] = int(item["refs"][0]["id"].split(",")[0])
        return queues[0]["window"], items

    def in_gate(self, pull):
        return next((item for item in self.gate()[1] if item["number"] == pull["number"]), None)

    def stays_out_of_gate(self, pull, why, seconds=30):
        """Zuul has had the events: the pull request does not enter gate for a while, and gate has no buildset of it."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.in_gate(pull):
                raise Failure(f"#{pull['number']} entered gate, though {why}")
            time.sleep(3)
        expect(not self.buildsets(pull, "gate"), f"#{pull['number']} stays out of gate: {why}")

    def running_in_gate(self, pull, timeout=600):
        """Waits until the pull request is in gate with a build running, and returns its item."""
        def running():
            item = self.in_gate(pull)
            return item and running_build(item) and item
        item = wait(f"#{pull['number']}'s build in gate", running, timeout, interval=2)
        log(f"  ok: #{pull['number']} is in gate, its build running")
        return item

    def merged(self, pull, timeout=300):
        """Waits until GitHub shows the pull request merged; returns it."""
        return wait(f"#{pull['number']} merged", lambda: (body := self.pull(pull))["merged"] and body, timeout)

    def main_head(self):
        return self.github.repo("/commits/main")["sha"]

    def commit_status(self, pull, timeout=120, pipeline="check"):
        """The state of the commit status a Zuul pipeline set on the pull request's head (tenant/pipeline)."""
        context = f"{self.args.tenant}/{pipeline}"

        def final():
            statuses = self.github.repo(f"/commits/{pull['head']['sha']}/statuses")
            mine = [status for status in statuses if status["context"] == context]
            return mine and mine[0]["state"] != "pending" and mine[0]

        status = wait(f"Zuul's {context} status on #{pull['number']}", final, timeout)
        log(f"  #{pull['number']}: GitHub shows {context} {status['state']}: {status['description']}")
        return status["state"]

    def comments(self, pull):
        return [comment["body"] for comment in self.github.repo(f"/issues/{pull['number']}/comments")]

    def job_on_main(self, name):
        status, _ = self.zuul.get(f"/job/{name}", ok=(200, 404))
        return status == 200

    def cleanup(self):
        for number in self.opened:
            try:
                if self.github.repo(f"/pulls/{number}")["state"] == "closed":
                    continue
                self.github.repo(f"/pulls/{number}", method="PATCH", body={"state": "closed"})
            except Failure as error:
                log(f"  could not close #{number}: {error}")
        for branch in self.branches:
            try:
                self.github.repo(f"/git/refs/heads/{branch}", method="DELETE")
            except Failure as error:
                log(f"  could not delete {branch}: {error}")
        subprocess.run(["git", "worktree", "remove", "--force", self.worktree], cwd=self.args.checkout)


def running_build(item):
    """Whether a build of an item in Zuul's status has started and not ended."""
    return any(job["start_time"] and not job["end_time"] for job in item["jobs"])


def expect(condition, message):
    if not condition:
        raise Failure(message)
    log(f"  ok: {message}")


def layout_loads(bed):
    log("1. The layout loads")
    names = {"check", "gate"}

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
    expect(builds == {**{job: "SUCCESS" for job in CHEAP}, "aid-testbed-hello": "SUCCESS"},
           f"check ran the cheap jobs {CHEAP} beside it, and they passed")
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


# What check and gate run (ci/zuul/zuul.d/project.yaml): the cheap jobs, and the full ones.
CHEAP = ["aid-seam-lint", "aid-seam-lint-test", "aid-apps-lint", "aid-apps-lint-test"]
FULL = ["aid-bazel-everything"]

TIMEOUT_JOB = """\
# Added by the test bed's pull request: a job that runs longer than its timeout.
- job:
    name: aid-testbed-timeout
    parent: aid-script
    timeout: 30
    vars:
      script: ci/testbed-sleep.sh

- project:
    check:
      jobs:
        - aid-testbed-timeout
"""

SLEEP = """\
#!/usr/bin/env bash
sleep 600
"""


def job_timeout(bed):
    log("5. A job runs no longer than its timeout")
    pull = bed.open_pull_request("timeout", {"ci/zuul/zuul.d/testbed-timeout.yaml": TIMEOUT_JOB,
                                             "ci/testbed-sleep.sh": SLEEP}, "A job that outlives its timeout")
    started = time.monotonic()
    buildset, builds = bed.check_result(pull)
    expect(builds.get("aid-testbed-timeout") == "TIMED_OUT", "the job was stopped at its timeout")
    expect(time.monotonic() - started < 600, "well before its script would have ended")
    expect(buildset["result"] == "FAILURE", "and check failed")


def behaviour(name, lines):
    """A pull request's testbed/gate/<name>.txt, which gate's stand-in test follows (workspace/gate_test.sh)."""
    return {f"testbed/gate/{name}.txt": "".join(f"{line}\n" for line in lines)}


def linear(bed, old, new, pull):
    """main went from old to new by the pull request's commits, rebased: one parent each, the first on old."""
    compare = bed.github.repo(f"/compare/{old}...{new}")
    commits = compare["commits"]
    expect(compare["status"] == "ahead" and compare["behind_by"] == 0,
           f"main moved forward from {old[:12]} to {new[:12]}")
    expect(all(len(commit["parents"]) == 1 for commit in commits), "with no merge commit: main stays linear")
    expect(commits[0]["parents"][0]["sha"] == old, "its first new commit sits on main as it was")
    messages = [commit["commit"]["message"].split("\n")[0] for commit in commits]
    expect(pull["title"] in messages, f"and carries the pull request's commit: {messages}")


def ready_means_queued(bed):
    log("6. Ready means queued, gate runs the full jobs, and Zuul merges by rebase")
    pull = bed.open_pull_request("ready", behaviour("ready", ["# ready means queued"]),
                                 "Gate: ready means queued", draft=True)
    bed.send_check_passed(pull)
    bed.stays_out_of_gate(pull, "it is a draft")
    old = bed.main_head()
    bed.mark_ready(pull)
    buildset, builds = bed.result(pull, "gate")
    expect(builds == {job: "SUCCESS" for job in FULL}, f"gate ran the full jobs {FULL}, not check's, and they passed")
    expect(buildset["result"] == "SUCCESS", "gate passed")
    expect(bed.commit_status(pull, pipeline="gate") == "success", "Zuul reported gate's success on the pull request")
    merged = bed.merged(pull)
    expect(merged["merged_by"] is not None, f"Zuul merged it (as {merged['merged_by']['login']})")
    linear(bed, old, bed.main_head(), pull)


def hold(bed):
    log("7. The hold label keeps a pull request out of gate, and takes it out")
    until = int(time.time()) + 900
    pull = bed.open_pull_request("hold", behaviour("hold", [f"{until} sleep 60"]), "Gate: hold", draft=True)
    bed.add_label(pull, "hold")
    bed.mark_ready(pull)
    bed.send_check_passed(pull)
    bed.stays_out_of_gate(pull, "it has the hold label")
    bed.remove_label(pull, "hold")
    bed.running_in_gate(pull)
    bed.add_label(pull, "hold")
    wait(f"#{pull['number']} out of gate", lambda: not bed.in_gate(pull), 120, interval=2)
    log(f"  ok: labelling hold took #{pull['number']} out of gate")
    held = wait("gate's record of it", lambda: bed.buildsets(pull, "gate"), 60)
    expect(held[0]["result"] != "SUCCESS", f"its buildset there ended {held[0]['result']}, not SUCCESS")
    statuses = [status for status in bed.github.repo(f"/commits/{pull['head']['sha']}/statuses")
                if status["context"] == f"{bed.args.tenant}/gate"]
    expect(not bed.pull(pull)["merged"], "it did not merge")
    log(f"  #{pull['number']}: GitHub shows aid/gate {statuses[0]['state'] if statuses else 'nothing'} after the hold")
    bed.remove_label(pull, "hold")
    buildset, _ = bed.result(pull, "gate", after=len(held))
    expect(buildset["result"] == "SUCCESS", "without hold it entered gate again, and passed")
    bed.merged(pull)
    log(f"  ok: and #{pull['number']} merged")


def speculative_queue(bed):
    log("8. Two pull requests at once, a flaky failure, and regate")
    # The first's test fails until flaky_until (a flaky test), long enough for its first gate run and short enough
    # for the regate after it; both sleep, so that their builds are seen running at once.
    sleep_until = int(time.time()) + 900
    flaky_until = int(time.time()) + 300
    first = bed.open_pull_request("first", behaviour("first", [f"{sleep_until} sleep 30", f"{flaky_until} fail"]),
                                  "Gate: the first, flaky", draft=True)
    second = bed.open_pull_request("second", behaviour("second", [f"{sleep_until} sleep 30"]),
                                   "Gate: the second", draft=True)
    for pull in (first, second):
        bed.send_check_passed(pull)
    bed.mark_ready(first)
    bed.mark_ready(second)

    def both_running():
        window, items = bed.gate()
        mine = {item["number"]: item for item in items}
        running = [pull["number"] in mine and running_build(mine[pull["number"]]) for pull in (first, second)]
        return all(running) and (window, mine)

    window, mine = wait("both pull requests' builds running in gate at once", both_running, 600, interval=2)
    expect(window == 2, "gate's window is two")
    expect(mine[second["number"]]["item_ahead"] == mine[first["number"]]["id"],
           f"#{second['number']} is tested on top of #{first['number']}, whose build runs at the same time")
    buildset, _ = bed.result(first, "gate")
    expect(buildset["result"] == "FAILURE", f"#{first['number']}'s flaky test failed it in gate")
    expect(not bed.pull(first)["merged"], f"#{first['number']} did not merge")
    expect(bed.commit_status(first, pipeline="gate") == "failure", "Zuul reported gate's failure on it")
    comments = wait(f"Zuul's comment on #{first['number']}", lambda: bed.comments(first), 120)
    expect(any("aid-bazel-everything" in body for body in comments), "with a comment naming the job that failed")
    buildset, _ = bed.result(second, "gate")
    expect(buildset["result"] == "SUCCESS", f"#{second['number']} was tested again without it, and passed")
    builds = [build for build in bed.builds(second, "gate") if build["job_name"] == FULL[0]]
    expect(len(builds) >= 2, f"#{second['number']} ran {FULL[0]} again: {[build['result'] for build in builds]}")
    bed.merged(second)
    log(f"  ok: #{second['number']} merged")
    if time.time() < flaky_until:
        time.sleep(flaky_until - time.time() + 5)
    bed.comment(first, "regate")
    buildset, _ = bed.result(first, "gate", after=1)
    expect(buildset["result"] == "SUCCESS", f"regate enqueued #{first['number']} again, and it passed")
    bed.merged(first)
    log(f"  ok: #{first['number']} merged")


def new_head_comes_back(bed):
    log("9. A ready pull request enters gate when check passes on it, and again after a push")
    until = int(time.time()) + 900
    pull = bed.open_pull_request("new-head", behaviour("new-head", [f"{until} sleep 60"]), "Gate: a new head")
    bed.stays_out_of_gate(pull, "check has not passed on it yet", seconds=10)
    bed.send_check_passed(pull)
    bed.running_in_gate(pull)
    pull = bed.push(pull, behaviour("new-head-2", ["# a second commit"]), "Gate: a new head, pushed while in gate")
    wait(f"#{pull['number']} out of gate", lambda: not bed.in_gate(pull), 120, interval=2)
    log(f"  ok: the push took #{pull['number']} out of gate")
    expect(bed.commit_status(pull) == "success", f"check passed on #{pull['number']}'s new head")
    bed.stays_out_of_gate(pull, "Zuul has not yet heard that check passed on its new head", seconds=20)
    bed.send_check_passed(pull)
    buildset, builds = bed.result(pull, "gate")
    expect(buildset["result"] == "SUCCESS" and builds == {job: "SUCCESS" for job in FULL},
           "check's success alone brought the new head into gate, with no ready, label or comment event, and it passed")
    bed.merged(pull)
    log(f"  ok: #{pull['number']} merged")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zuul", required=True, help="zuul-web's URL")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repository", required=True, help="the test bed's copy of AIDepartment, owner/name")
    parser.add_argument("--config-repository", required=True, help="its copy of kristofk/aid-zuul-config, owner/name")
    parser.add_argument("--checkout", required=True, help="a checkout of it, that can push")
    args = parser.parse_args()
    bed = Testbed(args)
    try:
        for check in (layout_loads, new_job_runs_speculatively, untrusted_base_job_is_refused,
                      untrusted_pipeline_is_refused, job_timeout, ready_means_queued, hold, speculative_queue,
                      new_head_comes_back):
            check(bed)
    except Failure as error:
        log(f"FAILED: {error}")
        return 1
    finally:
        bed.cleanup()
    log("All nine checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
