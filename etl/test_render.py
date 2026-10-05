"""Self-check for render.py against docs/examples. Run: python3 etl/test_render.py [LIVE_JSON] (needs yq).

Without LIVE_JSON it uses a synthetic snapshot; with one (from etl/live.sh) it prints the plans instead.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "docs", "examples")
REPOS = {"ews": "etl-ingestion-ews-gcp", "stormglass": "etl-ingestion-stormglass-gcp",
         "cmems": "etl-ingestion-cmems-gcp"}
P, R = os.environ.get("PROOF_PROJECT", "proj"), "us-west2"
VARS = {"GCP_PROJECT_ID": P, "GCP_REGION": R, "AUTH0_DOMAIN": "a,b",
        "ETL_ALERT_CHANNEL": f"projects/{P}/notificationChannels/1"}
SECRETS = {"GCP_WORKFLOW_SERVICE_ACCOUNT": "wf@sa", "GCP_SCHEDULER_SERVICE_ACCOUNT": "sch@sa",
           "ETL_OPS_NOTIFY_URL": "https://notify.example"}


def wf_uri(wf):
    return f"https://workflowexecutions.googleapis.com/v1/projects/{P}/locations/{R}/workflows/{wf}/executions"


def synthetic_live(env="staging"):
    live = {"jobs": [], "workflows": [], "schedulers": [], "metrics": [], "policies": []}

    def legacy(job, wf, sched, repo, tag="old"):
        live["jobs"].append({"name": job, "image": f"{R}-docker.pkg.dev/{P}/{repo}-{env}/main:{tag}"})
        live["workflows"].append({"name": wf})
        live["schedulers"].append({"name": sched, "state": "ENABLED", "uri": wf_uri(wf)})

    legacy(f"etl-ews-ingestion-{env}", f"workflow-ews-ingestion-{env}", f"schedule-ews-ingestion-{env}",
           REPOS["ews"], tag="deployed-ews")
    legacy(f"etl-well-level-ews-{env}", f"workflow-etl-well-level-ews-{env}", f"schedule-etl-well-level-ews-{env}",
           "etl-ingestion-well-level-ews-gcp")
    for i in "wh-01 wh-02 wad-01 wid-01 wis-01 wap-01 swd-01 swh-01 swp-01 wid-03 wis-03".split():
        legacy(f"etl-stormglass-ingestion-{i}-{env}", f"workflow-stormglass-ingestion-{i}-{env}",
               f"schedule-stormglass-ingestion-{i}-{env}", REPOS["stormglass"])
    # an instance removed from the declaration: owned because it targets the flow's workflow
    live["schedulers"].append({"name": f"etl-ingestion-stormglass-old-01-{env}", "state": "PAUSED",
                               "uri": wf_uri(f"etl-ingestion-stormglass-{env}")})
    return live


def render(example, decl="runtime.yaml", mode="deploy", env="staging", live=None, drop=()):
    decl_json = subprocess.run(["yq", "-o=json", ".", "-"], check=True, capture_output=True, text=True,
                               stdin=open(os.path.join(EXAMPLES, example, decl))).stdout
    vars_ = dict(VARS)
    for item in json.loads(decl_json).get("env", []):  # every non-optional declared name resolves
        name = item.split("=")[-1]
        if not name.endswith("?"):
            vars_.setdefault(name, f"v-{name.lower()}")
    vars_.setdefault(json.loads(decl_json)["job"]["service_account_var"], "job@sa")
    for d in drop:
        vars_.pop(d, None)
    files = []
    for content in (decl_json, json.dumps(live or synthetic_live(env))):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(content)
        files.append(f.name)
    e = dict(os.environ, VARS_JSON=json.dumps(vars_), SECRETS_JSON=json.dumps(SECRETS), ENVIRONMENT=env,
             REPO=REPOS[example], SHA="abc123", CI_DIR="/ci")
    return subprocess.run([sys.executable, os.path.join(HERE, "render.py"), mode, *files],
                          env=e, capture_output=True, text=True)


def count(out, prefix):
    return sum(1 for line in out.splitlines() if line.startswith(prefix))


def main():
    if len(sys.argv) > 1:  # proof mode: real read-only snapshot
        live = json.load(open(sys.argv[1]))
        for ex, decl in (("ews", "runtime.yaml"), ("stormglass", "runtime.yaml"),
                         ("cmems", "maps-large.yaml"), ("cmems", "maps.yaml")):
            r = render(ex, decl, live=live)
            print(f"===== {ex}/{decl}\n{r.stdout}{r.stderr}")
        return

    ews = render("ews").stdout
    assert "# plan: runtime 0 create, 3 update; heartbeat 2 create, 0 update; 0 pause, 0 orphans" in ews, ews
    assert count(ews, "gcloud scheduler jobs update http schedule-ews-ingestion-staging ") == 1, ews
    assert "gcloud workflows deploy workflow-ews-ingestion-staging " in ews
    assert "gcloud run jobs deploy etl-ews-ingestion-staging " in ews
    assert "SFTP_BANNER_TIMEOUT" not in ews  # optional + unset -> omitted, not ""
    assert "well-level" not in ews  # neighbours aren't orphans
    assert '"tagPrefixes": ["deployed-ews"]' in ews  # RUN-05 keeps what's running
    assert '"alignmentPeriod": "3600s"' in ews  # */15 -> 45 min -> 1h floor

    sg = render("stormglass").stdout
    assert "# plan: runtime 13 create, 0 update; heartbeat 2 create, 0 update; 11 pause, 34 orphans" in sg, sg
    assert count(sg, "gcloud scheduler jobs create http etl-ingestion-stormglass-") == 11
    assert count(sg, "gcloud scheduler jobs pause schedule-stormglass-ingestion-") == 11
    assert "echo '  schedulers/etl-ingestion-stormglass-old-01-staging'" in sg  # owned by target
    assert "-prod" not in sg.replace("--project", ""), "staging plan must not touch prod"
    assert "'--set-env-vars=^@^AUTH0_DOMAIN=a,b'" in sg  # comma-safe
    assert '\\"args\\": [\\"--ts-id\\", \\"WH-01\\"]' in sg and '"labels": {"instance": "wh-01"}' in sg
    assert '"alignmentPeriod": "90000s"' in sg  # daily -> 72h, capped at 25h

    lg = render("cmems", "maps-large.yaml").stdout
    assert "gcloud run jobs deploy etl-ingestion-cmems-maps-large-staging " in lg and "--memory=8Gi" in lg
    assert "^@^NEXT_WORKFLOW=etl-ingestion-cmems-timeseries-staging" in lg and "^@^RETRY_COUNT=16" in lg
    assert count(lg, "gcloud scheduler jobs create http etl-ingestion-cmems-maps-large-") == 4

    assert render("ews", drop=("INGESTION_SERVICE",)).returncode != 0
    assert render("ews", drop=("ETL_ALERT_CHANNEL",)).returncode != 0
    assert render("ews", env="dev").returncode != 0

    drift = render("stormglass", mode="drift")
    assert drift.returncode == 1 and count(drift.stdout, "MISSING") == 15, drift.stdout
    assert count(drift.stdout, "UNDECLARED") == 34
    assert render("ews", mode="drift").stdout.count("MISSING") == 2  # only the new heartbeat
    print("ok")


if __name__ == "__main__":
    main()
