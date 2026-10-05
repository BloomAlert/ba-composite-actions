#!/usr/bin/env python3
"""Render the gcloud plan for one ETL flow (docs/etl-workflows.md).

    render.py deploy|drift DECL_JSON LIVE_JSON

Env: ENVIRONMENT, REPO, SHA, CI_DIR, ETL_OUT, VARS_JSON, SECRETS_JSON (GitHub `vars`/`secrets`).
deploy -> prints a bash script and writes its value files to $ETL_OUT (nothing runs here; no secret
value is ever printed). drift -> prints drift, exit 1 if any (names only, no values needed).
"""
import json
import os
import re
import shlex
import sys
from collections import Counter

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
KINDS = ("jobs", "workflows", "schedulers")
q = shlex.quote


def die(msg):
    sys.exit(f"render.py: {msg}")


def seconds(text):
    m = re.fullmatch(r"(\d+)([smh])", str(text))
    if not m:
        die(f"bad duration {text!r} (use e.g. 600s, 90m, 25h)")
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600}[m[2]]


def heartbeat_window(decl):
    """ALERT-03 window in seconds, or None when the flow opts out with `heartbeat: false`."""
    if decl.get("heartbeat") is False:
        return None
    if "heartbeat_window" in decl:
        w = seconds(decl["heartbeat_window"])
        if not 3600 <= w <= 90000:
            die("heartbeat_window must be 1h..25h (the Cloud Monitoring alignment limit)")
        return w
    windows = []
    for inst in decl["instances"]:
        f = inst["schedule"].split()
        mi, h = (f + ["", ""])[:2]
        if len(f) == 5 and f[2:] == ["*", "*", "*"]:
            if re.fullmatch(r"\d+", h) and re.fullmatch(r"[\d,]+", mi):
                windows.append(25 * 3600)  # daily
                continue
            if h == "*" and (n := re.fullmatch(r"\*/(\d+)", mi)):
                windows.append(3 * 60 * int(n[1]))  # every n minutes
                continue
            if h == "*" and re.fullmatch(r"\d+", mi):
                windows.append(3 * 3600)  # hourly
                continue
            if (n := re.fullmatch(r"\*/(\d+)", h)) and re.fullmatch(r"\d+", mi):
                windows.append(3 * 3600 * int(n[1]))  # every n hours
                continue
        die(f"instance {inst['name']}: can't derive a heartbeat from {inst['schedule']!r};"
            " set `heartbeat_window: <1h..25h>` or `heartbeat: false` in the declaration")
    return min(max(min(windows), 3600), 90000)


def plan(decl, env, repo, sha, ci_dir, vars_, secrets, live, mode):
    secret_env = {}  # values that come from `secrets`: the plan only references them as $S_<NAME>

    def need(name):
        if vars_.get(name):
            return vars_[name]
        if secrets.get(name):
            return secrets[name]
        die(f"no var/secret named {name}")

    def token(name):
        """Shell token for a value: vars inline, secrets via $S_<NAME> from secrets.env."""
        if vars_.get(name):
            return q(vars_[name])
        secret_env[f"S_{name}"] = need(name)
        return f'"$S_{name}"'

    project, region = need("GCP_PROJECT_ID"), need("GCP_REGION")
    flow = decl.get("flow", "")
    base = decl.get("name") or re.sub(r"-gcp$", "", repo) + (f"-{flow}" if flow else "")  # RUN-02
    job_name = f"{base}-{env}"
    wf_name = f"{decl.get('workflow_name', base)}-{env}"
    sched_tpl = decl.get("scheduler_name", base + "-{instance}")
    for n in (job_name, wf_name):
        if len(n) > 63 or not NAME_RE.match(n):
            die(f"bad job/workflow name {n!r} (RUN-02, max 63)")

    live_names = {k: {r["name"]: r for r in live.get(k, [])} for k in (*KINDS, "metrics")}
    live_names["policies"] = {p["displayName"]: p for p in live.get("policies", [])}
    verb = {}  # "kind/name" -> create|update

    def upsert(kind, name):
        verb[f"{kind}/{name}"] = "update" if name in live_names[kind] else "create"
        return verb[f"{kind}/{name}"]

    url = (f"https://workflowexecutions.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/workflows/{wf_name}/executions")

    def target(sched):
        m = re.search(r"/workflows/([^/]+)/executions$", sched.get("uri") or "")
        return m[1] if m else None

    # ---- instances, RUN-04 `replaces`
    declared = {"jobs": {job_name}, "workflows": {wf_name}, "schedulers": set()}
    replaced = {k: set() for k in KINDS}
    schedulers = []
    for inst in decl["instances"]:
        if not NAME_RE.match(inst["name"]):
            die(f"instance name {inst['name']!r} must be lowercase [a-z0-9-] (label + RUN-02)")
        sched = sched_tpl.replace("{instance}", inst["name"]) + f"-{env}"
        if sched in declared["schedulers"]:
            die(f"scheduler {sched} declared twice (scheduler_name needs {{instance}} with >1 instance)")
        declared["schedulers"].add(sched)
        for r in inst.get("replaces", []):
            n = r.replace("{env}", env)
            kinds = [k for k in KINDS if n in live_names[k]]
            if not kinds:
                die(f"instance {inst['name']}: replaces {n}, which doesn't exist (already deleted? drop it)")
            for k in kinds:
                replaced[k].add(n)
        schedulers.append((inst, sched))
    for n in replaced["schedulers"]:
        t = target(live_names["schedulers"][n])
        if t not in replaced["workflows"] | {wf_name}:
            die(f"replaces {n}, but it targets workflow {t}, which this flow neither owns nor replaces")

    orphans = [(k, n, live_names[k][n].get("state", "")) for k in KINDS for n in sorted(replaced[k])]
    orphans += [("schedulers", n, r.get("state", "")) for n, r in sorted(live_names["schedulers"].items())
                if target(r) == wf_name and n not in declared["schedulers"] and n not in replaced["schedulers"]]

    window = heartbeat_window(decl)
    metric = f"etl-heartbeat-{job_name}"

    if mode == "drift":
        missing = [f"{k}/{n}" for k in KINDS for n in sorted(declared[k]) if n not in live_names[k]]
        if window and metric not in live_names["metrics"]:
            missing.append(f"metrics/{metric}")
        elif window and metric not in live_names["policies"]:
            missing.append(f"policies/{metric}")
        return dict(missing=missing, orphans=orphans)

    # ---- deploy: values go to files in $ETL_OUT, never into the plan
    job = decl["job"]
    timeout = seconds(job["timeout"])
    image = f"{region}-docker.pkg.dev/{project}/{repo}-{env}/{decl['image']}:{sha}"
    job_env = {"ENVIRONMENT": env, "GCP_PROJECT_ID": project}
    for item in decl.get("env", []):
        target_name, _, source = item.partition("=")
        optional = target_name.endswith("?")
        target_name = target_name.rstrip("?")
        value = vars_.get(source or target_name) or secrets.get(source or target_name)
        if value:
            job_env[target_name] = value
        elif not optional:
            die(f"env {target_name}: no var/secret named {source or target_name}")
    retry, nxt = decl.get("retry", {}), decl.get("next")
    wf_env = {"GCP_PROJECT_ID": project, "GCP_REGION": region, "ENVIRONMENT": env, "JOB_NAME": job_name,
              "JOB_TIMEOUT_SECONDS": timeout, "NOTIFY_URL": need("ETL_OPS_NOTIFY_URL"),
              "RETRY_COUNT": retry.get("count", 0), "RETRY_BACKOFF_SECONDS": retry.get("backoff_seconds", 0),
              "NEXT_WORKFLOW": f"{nxt}-{env}" if nxt else "none"}
    files = {"job-env.yaml": {k: str(v) for k, v in job_env.items()},
             "workflow-env.yaml": {k: str(v) for k, v in wf_env.items()}}

    cmds = [
        'source "$ETL_OUT/secrets.env"',
        f"gcloud artifacts docker images describe {q(image)} --project={project} >/dev/null",
        (f"# {upsert('jobs', job_name)}\ngcloud run jobs deploy {job_name} --project={project} --region={region}"
        f" --image={q(image)} --service-account={token(job['service_account_var'])} --cpu={job['cpu']}"
        f" --memory={job['memory']} --task-timeout={timeout}s --max-retries=0 --quiet"
        ' --env-vars-file="$ETL_OUT/job-env.yaml"'),
        (f"# {upsert('workflows', wf_name)}\ngcloud workflows deploy {wf_name} --project={project}"
        f" --location={region} --source={q(ci_dir + '/etl/workflow.yaml')}"
        f" --service-account={token('GCP_WORKFLOW_SERVICE_ACCOUNT')} --quiet"
        ' --env-vars-file="$ETL_OUT/workflow-env.yaml"'),
    ]
    sched_sa = token("GCP_SCHEDULER_SERVICE_ACCOUNT")
    for inst, sched in schedulers:
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
            f" --message-body={q(body)} --attempt-deadline=10m --oauth-service-account-email={sched_sa}"
            f" --{'update-' if action == 'update' else ''}headers=Content-Type=application/json")
    # RUN-04: pause old schedulers right after the new ones exist; deleting is manual, after prod is verified
    pauses = [n for k, n, state in orphans if k == "schedulers" and state == "ENABLED"]
    cmds += [f"gcloud scheduler jobs pause {n} --location={region} --project={project}" for n in pauses]

    # ALERT-03 heartbeat: successful executions of this job; the policy only once the metric exists,
    # so a brand-new metric with no data yet can't fire a false alert
    if window:
        log_filter = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job_name}"'
                      f' AND logName="projects/{project}/logs/run.googleapis.com%2Fvarlog%2Fsystem"'
                      f' AND textPayload="Container called exit(0)."')
        cmds.append(f"# {upsert('metrics', metric)}\ngcloud logging metrics {verb['metrics/' + metric]} {metric}"
                    f" --project={project} --description={q('etl-deploy heartbeat (ALERT-03)')}"
                    f" --log-filter={q(log_filter)}")
        if metric in live_names["metrics"]:
            files["heartbeat-policy.json"] = {
                "displayName": metric,
                "combiner": "OR",
                "userLabels": {"managed-by": "etl-deploy"},
                "notificationChannels": [need("ETL_ALERT_CHANNEL")],
                "documentation": {"mimeType": "text/markdown",
                                  "content": f"No successful execution of `{job_name}` in {window // 60} min."},
                "conditions": [{
                    "displayName": f"no success in {window // 60} min",
                    "conditionThreshold": {
                        "filter": f'metric.type="logging.googleapis.com/user/{metric}"'
                                  ' AND resource.type="cloud_run_job"',
                        "aggregations": [{"alignmentPeriod": f"{window}s", "perSeriesAligner": "ALIGN_SUM",
                                          "crossSeriesReducer": "REDUCE_SUM"}],
                        "comparison": "COMPARISON_LT", "thresholdValue": 1, "duration": "300s",
                        # same shape as ba-infra app-alerts: silence counts as a breach
                        "evaluationMissingData": "EVALUATION_MISSING_DATA_ACTIVE",
                        "trigger": {"count": 1},
                    },
                }],
            }
            if upsert("policies", metric) == "update":
                cmds.append(f"# update\ngcloud monitoring policies update {live_names['policies'][metric]['name']}"
                            f' --project={project} --policy-from-file="$ETL_OUT/heartbeat-policy.json"')
            else:
                cmds.append(f"# create\ngcloud monitoring policies create --project={project}"
                            ' --policy-from-file="$ETL_OUT/heartbeat-policy.json"')
        else:
            cmds.append("# heartbeat policy deferred: its metric is new, so it's created on the next deploy")
    else:
        cmds.append("# heartbeat: disabled in the declaration (heartbeat: false)")

    files["secrets.env"] = "".join(f"export {k}={q(v)}\n" for k, v in secret_env.items())
    return dict(cmds=cmds, verb=verb, orphans=orphans, pauses=pauses, files=files)


def main():
    mode, decl_path, live_path = sys.argv[1:4]
    decl = json.load(open(decl_path))
    live = json.load(open(live_path))
    e = os.environ
    if e["ENVIRONMENT"] not in ("staging", "prod"):
        die(f"environment must be staging|prod, got {e['ENVIRONMENT']!r}")
    p = plan(decl, e["ENVIRONMENT"], e["REPO"], e.get("SHA", ""), e.get("CI_DIR", ""),
             json.loads(e.get("VARS_JSON") or "{}"), json.loads(e.get("SECRETS_JSON") or "{}"), live, mode)

    if mode == "drift":
        for m in p["missing"]:
            print(f"MISSING    {m}")
        for kind, n, state in p["orphans"]:
            print(f"UNDECLARED {kind}/{n} {state}".rstrip())
        sys.exit(1 if p["missing"] or p["orphans"] else 0)

    out = e["ETL_OUT"]
    os.makedirs(out, exist_ok=True)
    for name, content in p["files"].items():
        with open(os.path.join(out, name), "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    c = Counter((k.startswith(("metrics/", "policies/")), v) for k, v in p["verb"].items())
    print("set -euo pipefail")
    print(f"# plan: runtime {c[False, 'create']} create, {c[False, 'update']} update;"
          f" heartbeat {c[True, 'create']} create, {c[True, 'update']} update;"
          f" {len(p['pauses'])} pause, {len(p['orphans'])} orphans")
    print("\n".join(p["cmds"]))
    print(f"echo 'ORPHANS ({len(p['orphans'])}), delete manually after prod is verified (RUN-04):'")
    for kind, n, _ in p["orphans"]:
        print(f"echo '  {kind}/{n}'")


if __name__ == "__main__":
    main()
