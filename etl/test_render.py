"""Self-check for render.py / ar_cleanup.py against docs/examples. Run: python3 etl/test_render.py [LIVE_JSON] (needs yq).

Without LIVE_JSON it uses a synthetic snapshot; with one (from etl/live.sh) it prints the plans instead.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES = os.path.join(ROOT, "docs", "examples")
REPOS = {"ews": "etl-ingestion-ews-gcp", "stormglass": "etl-ingestion-stormglass-gcp",
         "cmems": "etl-ingestion-cmems-gcp"}
P, R = os.environ.get("PROOF_PROJECT", "proj"), "us-west2"
NASTY = "p@ss'w,o\"rd"
VARS = {"GCP_PROJECT_ID": P, "GCP_REGION": R, "AUTH0_DOMAIN": NASTY,
        "ETL_ALERT_CHANNEL": f"projects/{P}/notificationChannels/1"}
SECRETS = {"GCP_WORKFLOW_SERVICE_ACCOUNT": "wf@sa", "GCP_SCHEDULER_SERVICE_ACCOUNT": "sch@sa",
           "ETL_OPS_NOTIFY_URL": "https://notify.example"}


def wf_uri(wf):
    return f"https://workflowexecutions.googleapis.com/v1/projects/{P}/locations/{R}/workflows/{wf}/executions"


def synthetic_live(env="staging"):
    live = {"jobs": [], "workflows": [], "schedulers": [], "metrics": [], "policies": [],
            "secrets": ["slack-bot-token"]}  # Secret Manager secrets that exist (live.sh describes them)

    def legacy(job, wf, sched, repo):
        live["jobs"].append({"name": job, "image": f"{R}-docker.pkg.dev/{P}/{repo}-{env}/main:old"})
        live["workflows"].append({"name": wf})
        live["schedulers"].append({"name": sched, "state": "ENABLED", "uri": wf_uri(wf)})

    legacy(f"etl-ews-ingestion-{env}", f"workflow-ews-ingestion-{env}", f"schedule-ews-ingestion-{env}", REPOS["ews"])
    live["metrics"].append({"name": f"etl-heartbeat-etl-ews-ingestion-{env}"})  # 2nd deploy: policy now created
    legacy(f"etl-well-level-ews-{env}", f"workflow-etl-well-level-ews-{env}", f"schedule-etl-well-level-ews-{env}",
           "etl-ingestion-well-level-ews-gcp")
    for i in "wh-01 wh-02 wad-01 wid-01 wis-01 wap-01 swd-01 swh-01 swp-01 wid-03 wis-03".split():
        legacy(f"etl-stormglass-ingestion-{i}-{env}", f"workflow-stormglass-ingestion-{i}-{env}",
               f"schedule-stormglass-ingestion-{i}-{env}", REPOS["stormglass"])
    # an instance removed from the declaration: owned because it targets the flow's workflow
    live["schedulers"].append({"name": f"etl-ingestion-stormglass-old-01-{env}", "state": "PAUSED",
                               "uri": wf_uri(f"etl-ingestion-stormglass-{env}")})
    return live


def tmp(content):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(content)
    return f.name


def render(example, decl="runtime.yaml", mode="deploy", env="staging", live=None, drop=(), patch=None,
           secrets=SECRETS):
    d = json.loads(subprocess.run(["yq", "-o=json", ".", "-"], check=True, capture_output=True, text=True,
                                  stdin=open(os.path.join(EXAMPLES, example, decl))).stdout)
    if patch:
        patch(d)
    vars_ = dict(VARS)
    for item in d.get("env", []):  # every non-optional declared name resolves
        name = item.split("=")[-1]
        if not name.endswith("?") and name not in SECRETS:
            vars_.setdefault(name, f"v-{name.lower()}")
    vars_.setdefault(d["job"]["service_account_var"], "job@sa")
    for k in drop:
        vars_.pop(k, None)
    out = tempfile.mkdtemp()
    e = dict(os.environ, VARS_JSON=json.dumps(vars_), SECRETS_JSON=json.dumps(secrets), ENVIRONMENT=env,
             REPO=REPOS[example], SHA="abc123", CI_DIR="/ci", ETL_OUT=out)
    r = subprocess.run([sys.executable, os.path.join(HERE, "render.py"), mode, tmp(json.dumps(d)),
                        tmp(json.dumps(live or synthetic_live(env)))], env=e, capture_output=True, text=True)
    r.out = out
    return r


def lines(out, prefix):
    return [i for i, line in enumerate(out.splitlines()) if line.startswith(prefix)]


def main():
    if len(sys.argv) > 1:  # proof mode: real read-only snapshot
        live = json.load(open(sys.argv[1]))
        for ex, decl in (("ews", "runtime.yaml"), ("stormglass", "runtime.yaml"),
                         ("cmems", "maps-large.yaml"), ("cmems", "maps.yaml")):
            r = render(ex, decl, live=live)
            print(f"===== {ex}/{decl}\n{r.stdout}{r.stderr}")
        return

    r = render("ews")
    ews = r.stdout
    assert "# plan: runtime 0 create, 3 update; heartbeat 1 create, 1 update; 0 pause, 0 orphans" in ews, ews
    assert lines(ews, "gcloud scheduler jobs update http schedule-ews-ingestion-staging ")
    assert "gcloud workflows deploy workflow-ews-ingestion-staging " in ews
    assert "gcloud monitoring policies create" in ews  # metric already existed -> policy now
    assert "well-level" not in ews  # neighbours aren't orphans
    job_env = json.load(open(os.path.join(r.out, "job-env.yaml")))
    assert job_env["AUTH0_DOMAIN"] == NASTY and "SFTP_BANNER_TIMEOUT" not in job_env  # (1) value intact, optional omitted
    wf_env = json.load(open(os.path.join(r.out, "workflow-env.yaml")))
    assert wf_env["JOB_TIMEOUT_SECONDS"] == "600" and wf_env["NOTIFY_URL"] == "https://notify.example"
    assert '"alignmentPeriod": "3600s"' in open(os.path.join(r.out, "heartbeat-policy.json")).read()  # */15 -> 1h

    r = render("stormglass")
    sg = r.stdout
    assert "# plan: runtime 13 create, 0 update; heartbeat 1 create, 0 update; 11 pause, 34 orphans" in sg, sg
    assert "heartbeat policy deferred" in sg  # (7) no policy before the metric exists
    for secret in [*SECRETS.values(), NASTY]:  # (2) no value from env files/secrets in the plan
        assert secret not in sg, secret
    assert '--service-account="$S_GCP_WORKFLOW_SERVICE_ACCOUNT"' in sg
    pauses, metric = lines(sg, "gcloud scheduler jobs pause "), lines(sg, "gcloud logging metrics")
    creates = lines(sg, "gcloud scheduler jobs create http ")
    assert len(pauses) == 11 and max(creates) < min(pauses) and max(pauses) < min(metric)  # (4) ordering
    assert "echo '  schedulers/etl-ingestion-stormglass-old-01-staging'" in sg  # owned by target
    assert "-prod" not in sg.replace("--project", ""), "staging plan must not touch prod"
    assert '\\"args\\": [\\"--ts-id\\", \\"WH-01\\"]' in sg and '"labels": {"instance": "wh-01"}' in sg

    assert "--clear-secrets" in sg  # no `secrets:` -> the job keeps no stale secret refs

    # env reads vars only: a name that exists only as a GitHub secret fails, value never rendered
    leak = render("stormglass", drop=("STORMGLASS_SECRET_KEY",),
                  secrets={**SECRETS, "STORMGLASS_SECRET_KEY": "s3cr3t-value"})
    assert leak.returncode != 0 and "is a GitHub secret" in leak.stderr, leak.stderr
    assert not os.path.exists(os.path.join(leak.out, "job-env.yaml"))
    optional = render("stormglass", drop=("STORMGLASS_SECRET_KEY",), secrets={**SECRETS, "STORMGLASS_SECRET_KEY": "x"},
                      patch=lambda d: d.update(env=[e + "?" if e == "STORMGLASS_SECRET_KEY" else e for e in d["env"]]))
    assert "is a GitHub secret" in optional.stderr  # optional doesn't silently drop it either

    # `secrets:` -> --set-secrets refs to existing Secret Manager secrets, value never in env files
    sm = render("stormglass", patch=lambda d: d.update(secrets={"SLACK_BOT_TOKEN": "slack-bot-token",
                                                                "PINNED": "slack-bot-token:3"}))
    assert "--set-secrets=SLACK_BOT_TOKEN=slack-bot-token:latest,PINNED=slack-bot-token:3" in sm.stdout, sm.stderr
    assert "--clear-secrets" not in sm.stdout
    assert "SLACK_BOT_TOKEN" not in json.load(open(os.path.join(sm.out, "job-env.yaml")))
    missing = render("stormglass", patch=lambda d: d.update(secrets={"X": "not-in-sm"}))
    assert missing.returncode != 0 and "not-in-sm not found" in missing.stderr, missing.stderr
    assert "also set as a plain env" in render("stormglass", patch=lambda d: d.update(
        secrets={"AUTH0_DOMAIN": "slack-bot-token"})).stderr
    assert "bad reference" in render("stormglass", patch=lambda d: d.update(secrets={"X": "slack-bot-token:v1"})).stderr

    def foreign(d):  # (5) a replaced scheduler that targets someone else's workflow
        d["instances"][0]["replaces"] = ["schedule-etl-well-level-ews-{env}"]
    bad = render("stormglass", patch=foreign)
    assert bad.returncode != 0 and "targets workflow workflow-etl-well-level-ews-staging" in bad.stderr, bad.stderr

    def gone(d):
        d["instances"][0]["replaces"] = ["schedule-does-not-exist-{env}"]
    assert "doesn't exist" in render("stormglass", patch=gone).stderr

    def adopt(d):  # replacing a name the flow itself declares would update then pause it
        d["instances"][0]["replaces"] = ["etl-ingestion-stormglass-{env}"]
    live = synthetic_live()
    live["jobs"].append({"name": "etl-ingestion-stormglass-staging"})
    assert "also declares" in render("stormglass", patch=adopt, live=live).stderr

    off = render("ews", patch=lambda d: d.update(heartbeat=False))  # opt-out lists the leftover metric
    assert "echo '  metrics/etl-heartbeat-etl-ews-ingestion-staging'" in off.stdout, off.stdout + off.stderr

    def weekly(d):  # (6) sparser than daily needs an explicit window
        d["instances"][0]["schedule"] = "0 13 * * MON"
    assert "heartbeat_window" in render("stormglass", patch=weekly).stderr
    assert render("stormglass", patch=lambda d: (weekly(d), d.update(heartbeat=False))).returncode == 0
    assert render("stormglass", patch=lambda d: (weekly(d), d.update(heartbeat_window="25h"))).returncode == 0
    assert render("stormglass", patch=lambda d: d["instances"][0].update(schedule="0 */6 * * *")).returncode == 0

    lg = render("cmems", "maps-large.yaml", live={})
    assert "doesn't exist" in lg.stderr  # replaces is checked against live
    lg = render("cmems", "maps-large.yaml", patch=lambda d: [i.pop("replaces") for i in d["instances"]])
    wf_env = json.load(open(os.path.join(lg.out, "workflow-env.yaml")))
    assert wf_env["NEXT_WORKFLOW"] == "etl-ingestion-cmems-timeseries-staging" and wf_env["RETRY_COUNT"] == "16"
    assert "--memory=8Gi" in lg.stdout and len(lines(lg.stdout, "gcloud scheduler jobs create http")) == 4

    assert render("ews", drop=("INGESTION_SERVICE",)).returncode != 0
    assert render("ews", env="dev").returncode != 0

    drift = render("stormglass", mode="drift")
    assert drift.returncode == 1 and len(lines(drift.stdout, "MISSING")) == 14, drift.stdout
    assert len(lines(drift.stdout, "UNDECLARED")) == 34
    assert lines(render("ews", mode="drift").stdout, "MISSING") == [0]  # only the deferred policy

    # (10) + RUN-05: untagged image refs mean :latest; build's inline policy == etl/ar-cleanup-policy.json
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    from ar_cleanup import deployed
    ar = f"{R}-docker.pkg.dev/{P}/repo-staging/"
    assert deployed({"jobs": [{"image": ar + "main"}, {"image": ar + "main:abc"},
                              {"image": ar + "main@sha256:ff"}]}, ar) == ({"latest", "abc"}, {"sha256:ff"})
    build = open(os.path.join(ROOT, ".github", "workflows", "etl-build.yaml")).read()
    inline = build.split("<<'JSON'\n", 1)[1].split("\n          JSON", 1)[0]
    assert json.loads(inline) == json.load(open(os.path.join(HERE, "ar-cleanup-policy.json")))
    print("ok")


if __name__ == "__main__":
    main()
