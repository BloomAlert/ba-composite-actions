"""Self-check for render.py / ar_cleanup.py against docs/examples. Run: python3 etl/test_render.py [LIVE_JSON] (needs yq).

Without LIVE_JSON it uses a synthetic snapshot; with one (from etl/live.sh) it prints the plans instead.
"""
import json
import os
import re
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
    live["metrics"].append({"name": f"etl-heartbeat-etl-ews-ingestion-{env}"})  # metric live, policy not (e.g. deleted by hand)
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


def yq(expr, path):
    return json.loads(subprocess.run(["yq", "-o=json", expr, path], check=True, capture_output=True, text=True).stdout)


def render(example, decl="runtime.yaml", mode="deploy", env="staging", live=None, drop=(), patch=None,
           secrets=SECRETS):
    d = yq(".", os.path.join(EXAMPLES, example, decl))
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
    assert "gcloud monitoring policies create" in ews  # #24: policy missing -> create + first-window snooze
    assert "--end-time=\"$(date -u -d '+3600 seconds' +%FT%TZ)\"" in ews and "|| echo \"::warning::" in ews
    assert "well-level" not in ews  # neighbours aren't orphans
    job_env = json.load(open(os.path.join(r.out, "job-env.yaml")))
    assert job_env["AUTH0_DOMAIN"] == NASTY and "SFTP_BANNER_TIMEOUT" not in job_env  # (1) value intact, optional omitted
    wf_env = json.load(open(os.path.join(r.out, "workflow-env.yaml")))
    assert wf_env["JOB_TIMEOUT_SECONDS"] == "600" and wf_env["NOTIFY_URL"] == "https://notify.example"
    assert '"alignmentPeriod": "3600s"' in open(os.path.join(r.out, "heartbeat-policy.json")).read()  # */15 -> 1h
    img = f"{R}-docker.pkg.dev/{P}/etl-ingestion-ews-gcp-staging/main"  # RUN-05: right after the job deploy
    assert lines(ews, "gcloud artifacts docker tags add ") == [i + 1 for i in lines(ews, "gcloud run jobs deploy ")]
    assert f"tags add {img}:abc123 {img}:deployed --quiet" in ews

    r = render("stormglass")
    sg = r.stdout
    assert "# plan: runtime 13 create, 0 update; heartbeat 2 create, 0 update; 11 pause, 34 orphans" in sg, sg
    # (7) #24: new metric -> policy in the same run, then a snooze for its first window (daily -> 25h)
    hb = [lines(sg, p)[0] for p in ("gcloud logging metrics create", "  if POLICY=$(gcloud monitoring policies create",
                                    "gcloud monitoring snoozes create")]
    assert hb == sorted(hb) and "+90000 seconds" in sg, sg
    assert '"alignmentPeriod": "90000s"' in open(os.path.join(r.out, "heartbeat-policy.json")).read()
    # 9 waits x 60s covers GCP's "up to 10 min" metric lag and stays inside etl-deploy's 20-min job timeout
    assert "for try in 1 2 3 4 5 6 7 8 9 10;" in sg and "sleep 60" in sg
    loop = sg[sg.index("for try in"):sg.index("done\n", sg.index("for try in")) + 5].replace("sleep 60", "sleep 0")
    with tempfile.TemporaryDirectory() as d:  # run the rendered retry loop against a stub gcloud
        stub = os.path.join(d, "gcloud")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\nn=$(cat "$ETL_OUT/n" 2>/dev/null || echo 0); echo $((n+1)) > "$ETL_OUT/n"\n'
                    '[ "$n" -ge "$FAILS" ] && { echo projects/p/alertPolicies/1; exit 0; }; echo "$ERR" >&2; exit 1\n')
        os.chmod(stub, 0o755)
        lag = ('ERROR: (gcloud.monitoring.policies.create) NOT_FOUND: Cannot find metric(s) that match type ='
               ' "logging.googleapis.com/user/x". If a metric was created recently, it could take up to 10 minutes')
        for fails, err, rc in ((2, lag, 0), (1, "PERMISSION_DENIED: no", 1), (10, lag, 1)):
            for n in os.listdir(d):
                n != "gcloud" and os.remove(os.path.join(d, n))
            env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "ETL_OUT": d, "FAILS": str(fails), "ERR": err}
            got = subprocess.run(["bash", "-c", loop], env=env, capture_output=True).returncode
            assert got == rc, (fails, err, got)
    for secret in [*SECRETS.values(), NASTY]:  # (2) no value from env files/secrets in the plan
        assert secret not in sg, secret
    assert '--service-account="$S_GCP_WORKFLOW_SERVICE_ACCOUNT"' in sg
    pauses, metric = lines(sg, "gcloud scheduler jobs pause "), lines(sg, "gcloud logging metrics")
    creates, wf = lines(sg, "gcloud scheduler jobs create http "), lines(sg, "gcloud workflows deploy ")
    # (4) #23: pause before any create, so a failed create (set -e) never leaves old + new both ENABLED
    assert len(pauses) == 11 and wf[0] < min(pauses) and max(pauses) < min(creates + metric)
    assert "echo '  schedulers/etl-ingestion-stormglass-old-01-staging'" in sg  # owned by target
    assert "-prod" not in sg.replace("--project", ""), "staging plan must not touch prod"
    assert '\\"args\\": [\\"--ts-id\\", \\"WH-01\\"]' in sg and '"labels": {"instance": "wh-01"}' in sg

    assert "--clear-secrets" in sg  # no `secrets:` -> the job keeps no stale secret refs
    assert all(" --args='' --command='' " in ln for out in (ews, sg)
               for ln in out.splitlines() if ln.startswith("gcloud run jobs deploy "))  # #27: no stale args/command survive

    # env reads vars only: a name that exists only as a GitHub secret fails, value never rendered
    leak = render("stormglass", drop=("STORMGLASS_SECRET_KEY",),
                  secrets={**SECRETS, "STORMGLASS_SECRET_KEY": "s3cr3t-value"})
    assert leak.returncode != 0 and "is a GitHub secret" in leak.stderr, leak.stderr
    assert not os.path.exists(os.path.join(leak.out, "job-env.yaml"))
    optional = render("stormglass", drop=("STORMGLASS_SECRET_KEY",), secrets={**SECRETS, "STORMGLASS_SECRET_KEY": "x"},
                      patch=lambda d: d.update(env=[e + "?" if e == "STORMGLASS_SECRET_KEY" else e for e in d["env"]]))
    assert optional.returncode != 0 and "is a GitHub secret" in optional.stderr  # optional doesn't drop it silently

    # `secrets:` -> --set-secrets refs to existing Secret Manager secrets, value never in env files
    sm = render("stormglass", patch=lambda d: d.update(secrets={"SLACK_BOT_TOKEN": "slack-bot-token",
                                                                "PINNED": "slack-bot-token:3"}))
    assert "--set-secrets=SLACK_BOT_TOKEN=slack-bot-token:latest,PINNED=slack-bot-token:3" in sm.stdout, sm.stderr
    assert "--clear-secrets" not in sm.stdout
    assert "SLACK_BOT_TOKEN" not in json.load(open(os.path.join(sm.out, "job-env.yaml")))
    per_env = render("stormglass", patch=lambda d: d.update(secrets={"T": "slack-bot-{env}"}),
                     live={**synthetic_live(), "secrets": ["slack-bot-staging"]})
    assert "--set-secrets=T=slack-bot-staging:latest" in per_env.stdout, per_env.stderr
    assert "not a valid env var name" in render("stormglass", patch=lambda d: d.update(
        secrets={"A=x:1,B": "slack-bot-token"})).stderr
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

    # #23 re-run after a partial failure (pauses done, one create done): nothing left to pause, existing -> update
    live = synthetic_live()
    for sc in live["schedulers"]:
        if sc["name"].startswith("schedule-stormglass-"):
            sc["state"] = "PAUSED"
    live["jobs"].append({"name": "etl-ingestion-stormglass-staging"})
    live["schedulers"].append({"name": "etl-ingestion-stormglass-wh-01-staging", "state": "ENABLED",
                               "uri": wf_uri("etl-ingestion-stormglass-staging")})
    rerun = render("stormglass", live=live).stdout
    assert not lines(rerun, "gcloud scheduler jobs pause "), rerun
    assert lines(rerun, "gcloud scheduler jobs update http etl-ingestion-stormglass-wh-01-staging ")

    off = render("ews", patch=lambda d: d.update(heartbeat=False))  # opt-out lists the leftover metric
    assert "echo '  metrics/etl-heartbeat-etl-ews-ingestion-staging'" in off.stdout, off.stdout + off.stderr
    assert "monitoring policies" not in off.stdout and "snoozes" not in off.stdout  # opt-out: no policy, no snooze
    live = synthetic_live()  # policy already live: update in place, never a second snooze
    live["policies"].append({"displayName": "etl-heartbeat-etl-ews-ingestion-staging",
                             "name": f"projects/{P}/alertPolicies/9"})
    upd = render("ews", live=live).stdout
    assert f"gcloud monitoring policies update projects/{P}/alertPolicies/9 " in upd and "snoozes" not in upd, upd
    assert "heartbeat 0 create, 2 update" in upd

    def weekly(d):  # (6) sparser than daily needs an explicit window
        d["instances"][0]["schedule"] = "0 13 * * MON"
    assert "heartbeat_window" in render("stormglass", patch=weekly).stderr
    assert render("stormglass", patch=lambda d: (weekly(d), d.update(heartbeat=False))).returncode == 0
    assert render("stormglass", patch=lambda d: (weekly(d), d.update(heartbeat_window="25h"))).returncode == 0
    assert render("stormglass", patch=lambda d: d["instances"][0].update(schedule="0 */6 * * *")).returncode == 0

    # instances: [] (event-driven / chain-only) + flow-level replaces, orphan-detected like instance-level
    def event(d, **kw):
        d.update(instances=[], replaces=["etl-well-level-ews-{env}", "workflow-etl-well-level-ews-{env}",
                                         "schedule-etl-well-level-ews-{env}"], **kw)
    ev = render("stormglass", patch=lambda d: event(d, heartbeat_window="6h"))
    assert "# plan: runtime 2 create, 0 update; heartbeat 2 create, 0 update; 1 pause, 4 orphans" in ev.stdout, \
        ev.stdout + ev.stderr
    assert not lines(ev.stdout, "gcloud scheduler jobs create") and "workflow-etl-well-level-ews-staging'" in ev.stdout
    assert lines(ev.stdout, "gcloud scheduler jobs pause schedule-etl-well-level-ews-staging ")
    nowin = render("stormglass", patch=event)
    assert nowin.returncode != 0 and "no instances" in nowin.stderr, nowin.stderr
    assert "flow: replaces schedule-does-not-exist-staging, which doesn't exist" in render(
        "stormglass", patch=lambda d: d.update(replaces=["schedule-does-not-exist-{env}"])).stderr
    nosa = {k: v for k, v in SECRETS.items() if k != "GCP_SCHEDULER_SERVICE_ACCOUNT"}
    assert render("stormglass", patch=lambda d: event(d, heartbeat=False), secrets=nosa).returncode == 0
    assert "instances is required" in render("stormglass", patch=lambda d: d.pop("instances")).stderr
    assert render("stormglass", patch=lambda d: d.update(replaces=None)).returncode == 0
    evd = render("stormglass", mode="drift", patch=lambda d: event(d, heartbeat=False))
    assert "MISSING    schedulers/" not in evd.stdout, evd.stdout
    assert "UNDECLARED schedulers/schedule-etl-well-level-ews-staging ENABLED" in evd.stdout, evd.stdout
    assert "targets workflow workflow-etl-well-level-ews-staging" in render(
        "stormglass", patch=lambda d: d.update(replaces=["schedule-etl-well-level-ews-{env}"])).stderr

    lg = render("cmems", "maps-large.yaml", live={})
    assert "doesn't exist" in lg.stderr  # replaces is checked against live
    lg = render("cmems", "maps-large.yaml", patch=lambda d: [i.pop("replaces") for i in d["instances"]])
    wf_env = json.load(open(os.path.join(lg.out, "workflow-env.yaml")))
    assert wf_env["NEXT_WORKFLOW"] == "etl-ingestion-cmems-timeseries-staging" and wf_env["RETRY_COUNT"] == "16"
    assert "--memory=8Gi" in lg.stdout and len(lines(lg.stdout, "gcloud scheduler jobs create http")) == 4
    # #23: ownership is by target workflow, not name prefix: maps never pauses maps-large's scheduler
    live = synthetic_live()
    live["schedulers"].append({"name": "etl-ingestion-cmems-maps-large-chl-02-staging", "state": "ENABLED",
                               "uri": wf_uri("etl-ingestion-cmems-maps-large-staging")})
    mp = render("cmems", "maps.yaml", live=live, patch=lambda d: [i.pop("replaces") for i in d["instances"]])
    assert mp.returncode == 0 and " 0 pause, 0 orphans" in mp.stdout and "maps-large" not in mp.stdout, \
        mp.stdout + mp.stderr

    assert render("ews", drop=("INGESTION_SERVICE",)).returncode != 0
    assert render("ews", env="dev").returncode != 0

    drift = render("stormglass", mode="drift")
    assert drift.returncode == 1 and len(lines(drift.stdout, "MISSING")) == 14, drift.stdout
    assert len(lines(drift.stdout, "UNDECLARED")) == 34
    assert lines(render("ews", mode="drift").stdout, "MISSING") == [0]  # only the hand-deleted policy

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
    # Workflows expressions have no {} map literal (gcloud deploy rejects it; found 2026-10-06 on ews staging).
    wf_path = os.path.join(HERE, "workflow.yaml")
    wf = open(wf_path).read()
    assert not re.search(r"\$\{[^}]*\{\}", wf), "map literal {} inside ${...} in workflow.yaml"
    # ALERT-01: job_failed carries entrypoint.py's ERROR line, looked up fail-soft (a lookup error never escapes)
    has_except, summary = yq('[.. | select(tag == "!!map" and has("cause_lookup")) | .cause_lookup | has("except")]'
                             ' + [.. | select(tag == "!!map" and has("post")) | .post.try.args.body.event.summary]',
                             wf_path)
    assert has_except and summary == "${default(cause, summary)}", (has_except, summary)
    # next: per-run opt-out "chain": false (non-bool fails fast); the next run gets the input minus "chain"
    check, cond, argument = yq('[.. | select(tag == "!!map" and has("check_chain")) | .check_chain.switch[0].condition]'
                               ' + [.. | select(tag == "!!map" and has("start_next")) | .start_next.switch[0]'
                               ' | (.condition, .steps[0].create_next_execution.args.body.argument)]', wf_path)
    assert "get_type(chain)" in check and "chain" in cond and 'map.delete(map.delete(input, "chain"), "retry")' in argument, \
        (check, cond, argument)
    conds, clamp, nxt = yq('[[.. | select(tag == "!!map" and has("override_retry")) | .override_retry.switch[].condition]]'
                           ' + [.. | select(tag == "!!map" and has("clamp_retry")) | .clamp_retry'
                           ' | (.assign[0].max_attempts, .next)]', wf_path)
    assert conds == ['${retry == null}', '${get_type(retry) != "integer"}', '${retry < 0}'] \
        and clamp == "${math.min(retry, max_attempts - 1) + 1}" and nxt == "attempts", (conds, clamp, nxt)
    # one job_failed per run: only give_up notifies, after the loop; failed attempts before the last only log and sleep
    loop, give_up = yq('[[.. | select(tag == "!!map" and has("attempts")) | .attempts.for.steps[] | keys[0]]]'
                       ' + [[.. | select(tag == "!!map" and has("give_up")) | .give_up.switch[0].steps[] | keys[0]]]', wf_path)
    assert "notify_failure" not in loop and give_up == ["notify_failure", "reraise"], (loop, give_up)
    print("ok")


if __name__ == "__main__":
    main()
