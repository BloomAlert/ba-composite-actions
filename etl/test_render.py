"""Self-check for render.py against the docs/examples declarations. Run: python3 etl/test_render.py (needs yq)."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIVE = os.path.join(HERE, "testdata", "live.json")
FAKE = {
    "VARS_JSON": json.dumps({"GCP_PROJECT_ID": "proj", "GCP_REGION": "us-west2",
                             "GCP_JOB_SERVICE_ACCOUNT_EMAIL": "job@sa", "AUTH0_DOMAIN": "a,b"}),
    "SECRETS_JSON": json.dumps({"GCP_WORKFLOW_SERVICE_ACCOUNT": "wf@sa", "GCP_SCHEDULER_SERVICE_ACCOUNT": "sch@sa"}),
}


def render(example, mode="deploy", env="staging", fill_env=True):
    decl = subprocess.run(["yq", "-o=json", ".", "-"], check=True, capture_output=True, text=True,
                          stdin=open(os.path.join(ROOT, "docs/examples", example, "runtime.yaml"))).stdout
    vars_ = json.loads(FAKE["VARS_JSON"])
    if fill_env:  # every non-optional declared env name resolves
        for item in json.loads(decl).get("env", []):
            name = item.split("=")[-1]
            if not name.endswith("?"):
                vars_.setdefault(name, f"v-{name.lower()}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(decl)
    repo = {"ews": "etl-ingestion-ews-gcp", "stormglass": "etl-ingestion-stormglass-gcp"}[example]
    e = dict(os.environ, SECRETS_JSON=FAKE["SECRETS_JSON"], VARS_JSON=json.dumps(vars_), ENVIRONMENT=env, REPO=repo,
             SHA="abc123", CI_DIR="/ci")
    return subprocess.run([sys.executable, os.path.join(HERE, "render.py"), mode, f.name, LIVE],
                          env=e, capture_output=True, text=True)


def count(out, needle):
    return sum(1 for line in out.splitlines() if line.startswith(needle))


sg = render("stormglass").stdout
assert count(sg, "gcloud run jobs deploy etl-ingestion-stormglass-staging ") == 1, sg
assert count(sg, "gcloud workflows deploy etl-ingestion-stormglass-staging ") == 1, sg
assert count(sg, "if gcloud scheduler jobs describe etl-ingestion-stormglass-") == 11, sg
assert count(sg, "gcloud scheduler jobs pause schedule-stormglass-ingestion-") == 11, sg
assert count(sg, "echo '  jobs/etl-stormglass-ingestion-") == 11, sg
assert count(sg, "echo '  workflows/workflow-stormglass-ingestion-") == 11, sg
assert "-prod" not in sg.replace("--project", ""), "staging plan must not touch prod"
assert "'--set-env-vars=^@^AUTH0_DOMAIN=a,b'" in sg  # comma-safe
assert '\\"args\\": [\\"--ts-id\\", \\"WH-01\\"]' in sg and '"labels": {"instance": "wh-01"}' in sg

ews = render("ews").stdout
assert count(ews, "if gcloud scheduler jobs describe etl-ingestion-ews-main-staging ") == 1, ews
assert "SFTP_BANNER_TIMEOUT" not in ews  # optional + unset -> omitted, not ""
assert "well-level" not in ews and "s3-map" not in ews  # neighbours with 'ews' in the name aren't orphans
assert "gcloud scheduler jobs pause schedule-ews-ingestion-staging " in ews

missing = render("ews", fill_env=False)
assert missing.returncode != 0 and "INGESTION_SERVICE" in missing.stderr
assert render("ews", env="dev").returncode != 0

drift = render("stormglass", mode="drift")
assert drift.returncode == 1 and count(drift.stdout, "MISSING") == 13 and count(drift.stdout, "UNDECLARED") == 33
print("ok")
