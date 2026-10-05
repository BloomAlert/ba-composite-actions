#!/usr/bin/env python3
"""Render the gcloud plan for one ETL flow (docs/etl-workflows.md).

    render.py deploy|drift DECL_JSON LIVE_JSON

Env: ENVIRONMENT, REPO, SHA, CI_DIR, VARS_JSON, SECRETS_JSON (GitHub `vars`/`secrets`).
deploy -> prints a bash script (nothing runs here). drift -> prints drift, exit 1 if any.
"""
import fnmatch
import json
import os
import re
import shlex
import sys

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
q = shlex.quote


def die(msg):
    sys.exit(f"render.py: {msg}")


def cron_field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else map(int, (rng.split("-") + [rng])[:2])
        out |= set(range(a, b + 1, int(step or 1)))
    return out


def max_gap_minutes(crons):
    """Largest gap between consecutive firings of the union of 5-field crons, over a 4-week cycle.

    ponytail: ignores time zones (all instances of a flow share one in practice); no L/W/# syntax.
    """
    fire = set()
    for c in crons:
        mi, h, dom, mon, dow = c.split()
        mi, h = cron_field(mi, 0, 59), cron_field(h, 0, 23)
        dom_s, dow_s = cron_field(dom, 1, 31), {d % 7 for d in cron_field(dow, 0, 7)}
        for day in range(28):  # day 0 = a Monday, the 1st
            d_dom, d_dow = day + 1, (day + 1) % 7
            if dom != "*" and dow != "*":
                ok = d_dom in dom_s or d_dow in dow_s
            else:
                ok = d_dom in dom_s and d_dow in dow_s
            fire |= {day * 1440 + hh * 60 + m for hh in h for m in mi} if ok else set()
    t = sorted(fire)
    if not t:
        die(f"crons {crons} never fire")
    return max([b - a for a, b in zip(t, t[1:])] + [t[0] + 28 * 1440 - t[-1]])


def plan(decl, env, repo, sha, ci_dir, vars_, secrets, live):
    def lookup(name):
        return vars_.get(name) or secrets.get(name)

    def need(name):
        return lookup(name) or die(f"no var/secret named {name}")

    project, region = need("GCP_PROJECT_ID"), need("GCP_REGION")
    flow = decl.get("flow", "")
    base = decl.get("name") or re.sub(r"-gcp$", "", repo) + (f"-{flow}" if flow else "")  # RUN-02
    job_name = f"{base}-{env}"
    wf_name = f"{decl.get('workflow_name', base)}-{env}"
    sched_tpl = decl.get("scheduler_name", base + "-{instance}")
    for n in (job_name, wf_name):
        if len(n) > 63 or not NAME_RE.match(n):
            die(f"bad job/workflow name {n!r} (RUN-02, max 63)")

    job = decl["job"]
    ar_repo = f"{repo}-{env}"
    ar_path = f"{region}-docker.pkg.dev/{project}/{ar_repo}/"
    image = f"{ar_path}{decl['image']}:{sha}"

    env_vars = {"ENVIRONMENT": env, "GCP_PROJECT_ID": project}
    for item in decl.get("env", []):
        target, _, source = item.partition("=")
        optional = target.endswith("?")
        target = target.rstrip("?")
        value = lookup(source or target)
        if value:
            env_vars[target] = value
        elif not optional:
            die(f"env {target}: no var/secret named {source or target}")

    names = {k: {r["name"]: r for r in live.get(k, [])} for k in ("jobs", "workflows", "schedulers", "metrics")}
    policies = {p["displayName"]: p["name"] for p in live.get("policies", [])}
    verb = {}  # resource -> create|update, for the summary

    def upsert(kind, name):
        verb[f"{kind}/{name}"] = "update" if name in names.get(kind, {}) else "create"
        return verb[f"{kind}/{name}"]

    # RUN-05: newest 10 per image kept; older tagged + untagged>7d deleted; whatever a live job runs is kept
    tags, digests = set(), set()
    for j in live.get("jobs", []):
        ref = j.get("image", "")
        if ref.startswith(ar_path):  # <image>:<tag> or <image>@sha256:<hex>
            rest = ref[len(ar_path):]
            if "@" in rest:
                digests.add(rest.split("@", 1)[1])
            else:
                tags.add(rest.split(":", 1)[1])
    keep_deployed = {k: sorted(v) for k, v in (("tagPrefixes", tags), ("versionNamePrefixes", digests)) if v}
    ar_policy = [
        {"name": "keep-last-10", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 10}},
        *([{"name": "keep-deployed", "action": {"type": "Keep"}, "condition": keep_deployed}] if keep_deployed else []),
        {"name": "delete-tagged", "action": {"type": "Delete"}, "condition": {"tagState": "tagged"}},
        {"name": "delete-untagged-7d", "action": {"type": "Delete"},
         "condition": {"tagState": "untagged", "olderThan": "7d"}},
    ]

    retry = decl.get("retry", {})
    nxt = decl.get("next")
    loc = f"--project={project} --region={region}"
    cmds = [
        f"gcloud artifacts docker images describe {q(image)} --project={project} >/dev/null",
        f"AR_POLICY=$(mktemp) && printf %s {q(json.dumps(ar_policy))} > \"$AR_POLICY\"",
        f"gcloud artifacts repositories set-cleanup-policies {ar_repo} --project={project}"
        f" --location={region} --policy=\"$AR_POLICY\" --no-dry-run",
        f"# {upsert('jobs', job_name)}\ngcloud run jobs deploy {job_name} {loc} --image={q(image)}"
        f" --service-account={q(need(job['service_account_var']))} --cpu={job['cpu']} --memory={job['memory']}"
        f" --task-timeout={job['timeout']} --max-retries=0 --quiet "
        # ^@^ = gcloud's alternate delimiter, so commas inside values survive
        + " ".join(q(f"--set-env-vars=^@^{k}={v}") for k, v in env_vars.items()),
        f"# {upsert('workflows', wf_name)}\ngcloud workflows deploy {wf_name} --project={project} --location={region}"
        f" --source={q(ci_dir + '/etl/workflow.yaml')}"
        f" --service-account={q(need('GCP_WORKFLOW_SERVICE_ACCOUNT'))} --quiet "
        + " ".join(q(f"--set-env-vars=^@^{k}={v}") for k, v in {
            "GCP_PROJECT_ID": project, "GCP_REGION": region, "ENVIRONMENT": env, "JOB_NAME": job_name,
            "NOTIFY_URL": need("ETL_OPS_NOTIFY_URL"),
            "RETRY_COUNT": retry.get("count", 0), "RETRY_BACKOFF_SECONDS": retry.get("backoff_seconds", 0),
            "NEXT_WORKFLOW": f"{nxt}-{env}" if nxt else "none"}.items()),
    ]

    url = (f"https://workflowexecutions.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/workflows/{wf_name}/executions")
    sched_sa = need("GCP_SCHEDULER_SERVICE_ACCOUNT")
    declared_scheds = set()
    for inst in decl["instances"]:
        if not NAME_RE.match(inst["name"]):
            die(f"instance name {inst['name']!r} must be lowercase [a-z0-9-] (label + RUN-02)")
        sched = sched_tpl.replace("{instance}", inst["name"]) + f"-{env}"
        if sched in declared_scheds:
            die(f"scheduler {sched} declared twice (scheduler_name needs {{instance}} with >1 instance)")
        declared_scheds.add(sched)
        labels = {"instance": inst["name"], **({"flow": flow} if flow else {})}
        body = json.dumps({
            "argument": json.dumps({"args": [str(a) for a in inst.get("args", [])], "labels": labels}),
            "labels": labels,
        })
        tz = inst.get("time_zone") or decl.get("time_zone") or die("time_zone is required")
        action = upsert("schedulers", sched)
        cmds.append(
            f"gcloud scheduler jobs {action} http {sched} --location={region} --project={project}"
            f" --schedule={q(inst['schedule'])} --time-zone={q(tz)} --uri={q(url)} --http-method=POST"
            f" --message-body={q(body)} --attempt-deadline=10m --oauth-service-account-email={q(sched_sa)}"
            f" --{'update-' if action == 'update' else ''}headers=Content-Type=application/json")

    # ALERT-03: heartbeat = successful executions of this job; alert if none for 3x the schedule gap (1h..25h)
    metric = f"etl-heartbeat-{job_name}"
    window = min(max(3 * 60 * max_gap_minutes([i["schedule"] for i in decl["instances"]]), 3600), 90000)
    log_filter = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job_name}"'
                  f' AND logName="projects/{project}/logs/run.googleapis.com%2Fvarlog%2Fsystem"'
                  f' AND textPayload="Container called exit(0)."')
    cmds.append(f"# {upsert('metrics', metric)}\ngcloud logging metrics {verb['metrics/' + metric]} {metric}"
                f" --project={project} --description={q('etl-deploy heartbeat (ALERT-03)')}"
                f" --log-filter={q(log_filter)}")
    policy = {
        "displayName": metric,
        "combiner": "OR",
        "userLabels": {"managed-by": "etl-deploy"},
        "notificationChannels": [need("ETL_ALERT_CHANNEL")],
        "documentation": {"mimeType": "text/markdown",
                          "content": f"No successful execution of `{job_name}` in {window // 60} min."},
        "conditions": [{
            "displayName": f"no success in {window // 60} min",
            "conditionThreshold": {
                "filter": f'metric.type="logging.googleapis.com/user/{metric}" AND resource.type="cloud_run_job"',
                "aggregations": [{"alignmentPeriod": f"{window}s", "perSeriesAligner": "ALIGN_SUM",
                                  "crossSeriesReducer": "REDUCE_SUM"}],
                "comparison": "COMPARISON_LT", "thresholdValue": 1, "duration": "300s",
                # same shape as ba-infra app-alerts: silence counts as a breach
                "evaluationMissingData": "EVALUATION_MISSING_DATA_ACTIVE",
                "trigger": {"count": 1},
            },
        }],
    }
    if metric in policies:
        verb[f"policies/{metric}"] = "update"
        cmds.append(f"# update\ngcloud monitoring policies update {policies[metric]} --project={project}"
                    f" --policy={q(json.dumps(policy))}")
    else:
        verb[f"policies/{metric}"] = "create"
        cmds.append(f"# create\ngcloud monitoring policies create --project={project} --policy={q(json.dumps(policy))}")

    # RUN-04/06 orphans: legacy globs, plus schedulers that target this workflow but aren't declared
    globs = [g.replace("{env}", env) for g in decl.get("legacy", [])]
    declared = {"jobs": {job_name}, "workflows": {wf_name}, "schedulers": declared_scheds}
    orphans = []
    for kind in ("jobs", "workflows", "schedulers"):
        for n, r in names[kind].items():
            if n in declared[kind]:
                continue
            targets_us = kind == "schedulers" and r.get("uri") == url
            if targets_us or any(fnmatch.fnmatchcase(n, g) for g in globs):
                orphans.append((kind, n, r.get("state", "")))

    missing = [k for k, v in verb.items() if v == "create"]
    return dict(project=project, region=region, cmds=cmds, verb=verb, orphans=orphans, missing=missing)


def main():
    mode, decl_path, live_path = sys.argv[1:4]
    decl = json.load(open(decl_path))
    live = json.load(open(live_path))
    e = os.environ
    if e["ENVIRONMENT"] not in ("staging", "prod"):
        die(f"environment must be staging|prod, got {e['ENVIRONMENT']!r}")
    p = plan(decl, e["ENVIRONMENT"], e["REPO"], e["SHA"], e["CI_DIR"],
             json.loads(e.get("VARS_JSON") or "{}"), json.loads(e.get("SECRETS_JSON") or "{}"), live)

    if mode == "drift":
        for m in p["missing"]:
            print(f"MISSING    {m}")
        for kind, n, state in p["orphans"]:
            print(f"UNDECLARED {kind}/{n} {state}".rstrip())
        sys.exit(1 if p["missing"] or p["orphans"] else 0)

    pauses = [n for kind, n, state in p["orphans"] if kind == "schedulers" and state == "ENABLED"]
    def n(verb, heartbeat):
        return sum(1 for k, v in p["verb"].items() if v == verb and k.startswith(("metrics/", "policies/")) == heartbeat)

    print("set -euo pipefail")
    print(f"# plan: runtime {n('create', False)} create, {n('update', False)} update;"
          f" heartbeat {n('create', True)} create, {n('update', True)} update;"
          f" {len(pauses)} pause, {len(p['orphans'])} orphans")
    print("\n".join(p["cmds"]))
    # RUN-04: pause old schedulers in the same deploy; deleting is manual, after prod is verified
    for n in pauses:
        print(f"gcloud scheduler jobs pause {n} --location={p['region']} --project={p['project']}")
    print(f"echo 'ORPHANS ({len(p['orphans'])}), delete manually after prod is verified (RUN-04):'")
    for kind, n, _ in p["orphans"]:
        print(f"echo '  {kind}/{n}'")


if __name__ == "__main__":
    main()
