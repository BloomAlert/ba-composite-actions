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


def die(msg):
    sys.exit(f"render.py: {msg}")


def plan(decl, env, repo, sha, ci_dir, vars_, secrets):
    def lookup(name):
        return vars_.get(name) or secrets.get(name)

    def need(name):
        return lookup(name) or die(f"no var/secret named {name}")

    project, region = need("GCP_PROJECT_ID"), need("GCP_REGION")
    flow = decl.get("flow", "")
    prefix = re.sub(r"-gcp$", "", repo) + (f"-{flow}" if flow else "")  # RUN-02
    name = f"{prefix}-{env}"
    if len(name) > 63 or not NAME_RE.match(name):
        die(f"bad job/workflow name {name!r} (RUN-02, max 63)")

    job = decl["job"]
    ar_repo = f"{repo}-{env}"
    image = f"{region}-docker.pkg.dev/{project}/{ar_repo}/{decl['image']}:{sha}"

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
    sa = need(job["service_account_var"])

    q = shlex.quote
    loc = f"--project={project} --region={region}"
    cmds = [
        f"gcloud artifacts docker images describe {q(image)} --project={project} >/dev/null",
        # RUN-05; the policy file ships next to this script
        f"gcloud artifacts repositories set-cleanup-policies {ar_repo} --project={project}"
        f" --location={region} --policy={q(ci_dir + '/etl/ar-cleanup-policy.json')} --no-dry-run",
        f"gcloud run jobs deploy {name} {loc} --image={q(image)}"
        f" --service-account={q(sa)} --cpu={job['cpu']} --memory={job['memory']}"
        f" --task-timeout={job['timeout']} --max-retries=0 --quiet "
        # ^@^ = gcloud's alternate delimiter, so commas inside values survive
        + " ".join(q(f"--set-env-vars=^@^{k}={v}") for k, v in env_vars.items()),
        f"gcloud workflows deploy {name} --project={project} --location={region}"
        f" --source={q(ci_dir + '/etl/workflow.yaml')}"
        f" --service-account={q(need('GCP_WORKFLOW_SERVICE_ACCOUNT'))}"
        f" --set-env-vars=GCP_PROJECT_ID={project},GCP_REGION={region},ENVIRONMENT={env},JOB_NAME={name} --quiet",
    ]

    url = (f"https://workflowexecutions.googleapis.com/v1/projects/{project}"
           f"/locations/{region}/workflows/{name}/executions")
    sched_sa = need("GCP_SCHEDULER_SERVICE_ACCOUNT")
    declared = {name}
    for inst in decl["instances"]:
        if not NAME_RE.match(inst["name"]):
            die(f"instance name {inst['name']!r} must be lowercase [a-z0-9-] (label + RUN-02)")
        sched = f"{prefix}-{inst['name']}-{env}"
        declared.add(sched)
        labels = {"instance": inst["name"], **({"flow": flow} if flow else {})}
        body = json.dumps({
            "argument": json.dumps({"args": [str(a) for a in inst.get("args", [])], "labels": labels}),
            "labels": labels,
        })
        common = (f"{sched} --location={region} --project={project} --schedule={q(inst['schedule'])}"
                  f" --time-zone={q(inst.get('time_zone', decl.get('time_zone', 'America/Santiago')))}"
                  f" --uri={q(url)} --http-method=POST --message-body={q(body)} --attempt-deadline=10m"
                  f" --oauth-service-account-email={q(sched_sa)}")
        cmds.append(
            f"if gcloud scheduler jobs describe {sched} --location={region} --project={project} >/dev/null 2>&1;"
            f" then gcloud scheduler jobs update http {common} --update-headers=Content-Type=application/json;"
            f" else gcloud scheduler jobs create http {common} --headers=Content-Type=application/json; fi")

    # RUN-04/06: what this flow owns = its RUN-02 names + its legacy globs
    owned = [f"{prefix}-*-{env}"] + [g.replace("{env}", env) for g in decl.get("legacy", [])]
    return dict(project=project, region=region, name=name, declared=declared, owned=owned, cmds=cmds)


def owned_live(p, live):
    out = []
    for kind in ("jobs", "workflows", "schedulers"):
        for r in live.get(kind, []):
            if r["name"] in p["declared"]:
                continue
            if any(fnmatch.fnmatchcase(r["name"], g) for g in p["owned"]):
                out.append((kind, r["name"], r.get("state", "")))
    return out


def main():
    mode, decl_path, live_path = sys.argv[1:4]
    decl = json.load(open(decl_path))
    live = json.load(open(live_path))
    e = os.environ
    if e["ENVIRONMENT"] not in ("staging", "prod"):
        die(f"environment must be staging|prod, got {e['ENVIRONMENT']!r}")
    p = plan(decl, e["ENVIRONMENT"], e["REPO"], e["SHA"], e["CI_DIR"],
             json.loads(e.get("VARS_JSON") or "{}"), json.loads(e.get("SECRETS_JSON") or "{}"))
    orphans = owned_live(p, live)

    if mode == "drift":
        names = {k: {r["name"] for r in live.get(k, [])} for k in ("jobs", "workflows", "schedulers")}
        missing = [f"{k}/{p['name']}" for k in ("jobs", "workflows") if p["name"] not in names[k]]
        missing += [f"schedulers/{s}" for s in sorted(p["declared"] - {p["name"]}) if s not in names["schedulers"]]
        for m in missing:
            print(f"MISSING    {m}")
        for kind, n, state in orphans:
            print(f"UNDECLARED {kind}/{n} {state}".rstrip())
        sys.exit(1 if missing or orphans else 0)

    print("set -euo pipefail")
    print("\n".join(p["cmds"]))
    # RUN-04: pause old schedulers in the same deploy; deleting is manual, after prod is verified
    for kind, n, state in orphans:
        if kind == "schedulers" and state == "ENABLED":
            print(f"gcloud scheduler jobs pause {n} --location={p['region']} --project={p['project']}")
    print(f"echo 'ORPHANS ({len(orphans)}), delete manually after prod is verified (RUN-04):'")
    for kind, n, _ in orphans:
        print(f"echo '  {kind}/{n}'")


if __name__ == "__main__":
    main()
