# ETL reusable workflows

`etl-build.yaml` and `etl-deploy.yaml` build and run the `etl-*` fleet's Cloud Run jobs (etl-template #47 CI, #50 RUN, #52 ALERT). The composite actions in this repo are a separate thing, and their `@main` callers are unaffected.

- Pin both workflows by full SHA with a `# vX.Y.Z` comment (CI-04/05).
- Examples are in [`examples/`](examples/):
  - ews: in-place adoption
  - stormglass: collapses 11 instances
  - cmems maps: two resource tiers plus `next`
- This repo is public, so nothing environment-specific lives in it.
- **Unsupported:** private package indexes at build time, e.g. bloompy from a GAR `index_url`. No fleet ETL uses one.

## Inputs

| | `etl-build` | `etl-deploy` |
|---|---|---|
| `environment` (required) | `staging`/`prod`, must equal `github.ref_name` | same for `mode: deploy`; any branch for `mode: drift` |
| `declaration` | default `deploy/runtime.yaml` | same |
| `mode` | – | `deploy` (default) or `drift` |
| `dry_run` | – | `true` prints the plan and the orphans and executes nothing |
| outputs | `image` (`…:<sha>`), `digest` | – |

Callers pass `secrets: inherit`. Each name below is looked up in vars, then in secrets.

| name | used by |
|---|---|
| `GCP_PROJECT_ID`, `GCP_REGION` | all (repo- or org-level vars) |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | all (WIF only) |
| `GCP_ARTIFACT_REGISTRY_WRITER_SA` | build |
| `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_WORKFLOW_SERVICE_ACCOUNT`, `GCP_SCHEDULER_SERVICE_ACCOUNT` | deploy |
| `GCP_DRIFT_SERVICE_ACCOUNT` | drift: a **read-only** SA |
| `ETL_OPS_NOTIFY_URL` | deploy: the env's ba-ops-notify URL, set as the Workflow's `NOTIFY_URL` |
| `ETL_ALERT_CHANNEL` | deploy: the notification channel of `#etl-errors-<env>` (`projects/…/notificationChannels/…`) |
| the declaration's `job.service_account_var` and `env` names | deploy |

**Jobs and environments:**
- **Deploy** runs with `environment: <env>`, so environment-level values resolve.
- **Drift** has no environment, uses only repo/org vars and the drift SA, and never writes.

**Hardening:**
- **Permissions:** the calling job grants `contents: read` and `id-token: write`, and nothing else.
- **Concurrency:** `deploy-<env>-<declaration>` (CI-03), keyed per flow.
- **No silent rollback:** Apply aborts unless the env branch head equals `github.sha`, so re-running an old commit can't overwrite a newer deploy.
- **No values in output:** `render.py` writes values to files in `$ETL_OUT`, which are never printed:
  - `job-env.yaml` and `workflow-env.yaml`, passed with `--env-vars-file`
  - `heartbeat-policy.json`
  - `secrets.env`
  
  The plan holds names, flags and `"$S_<NAME>"` references only, in dry runs too.

## Declaration (RUN-03)

One file per flow (one job, one Workflow). Flows may share an image, e.g. CPU/memory tiers such as `maps` and `maps-large`.

```yaml
flow: maps-large              # optional; part of the RUN-02 default name
name: …                       # optional base name, default RUN-02 (below); job = <name>-<env>
workflow_name: …              # optional, default <name>;               workflow = <workflow_name>-<env>
scheduler_name: …             # optional, default "<name>-{instance}"; scheduler = <scheduler_name>-<env>
dockerfile: Dockerfile.maps
image: map                    # AR image name and buildx cache scope
job:
  service_account_var: GCP_MAP_JOB_SERVICE_ACCOUNT_EMAIL
  cpu: 2
  memory: 8Gi
  timeout: 1200s              # Ns/Nm/Nh; the Workflow waits timeout + 600s for jobs.run
env:                          # each value is looked up in vars, then secrets
  - AUTH0_DOMAIN              # NAME           = var/secret NAME
  - GCP_BUCKET_NAME=GCP_MAPS_BUCKET   # NAME=SOURCE
  - SFTP_BANNER_TIMEOUT?      # optional: unset -> omitted (otherwise the deploy fails)
time_zone: America/Santiago   # required here or per instance
retry: {count: 16, backoff_seconds: 1800}   # optional
next: etl-ingestion-cmems-timeseries        # optional: Workflow (minus -<env>) started on success, same input
heartbeat_window: 25h         # optional, 1h..25h; required when it can't be derived (below)
heartbeat: false              # optional opt-out
instances:                    # one Scheduler each; name = [a-z0-9-], also the `instance` label
  - name: chl-02
    schedule: "15 8 * * *"
    args: [--ts-id, CHL-02, --publish-arrival, --source, nrt]
    replaces:                 # RUN-04: exact old resources this instance supersedes ({env} substituted)
      - etl-map-ingestion-chl-02-{env}
      - workflow-map-ingestion-chl-02-{env}
      - schedule-map-ingestion-chl-02-{env}
```

`ENVIRONMENT` and `GCP_PROJECT_ID` are always set on the job. Each deploy sets the full env (`--env-vars-file` replaces it).

## Naming (RUN-02)

- **Repos that collapse per-instance resources** take the defaults:
  - job and workflow: `<repo-short>[-<flow>]-<env>`
  - scheduler: `<repo-short>[-<flow>]-<instance>-<env>`
  - `repo-short` is the repo name without a trailing `-gcp`.
- **Existing single-job repos** declare their current names with `name`, `workflow_name` and `scheduler_name`. Adoption is then an in-place update with no cutover.
- The AR repo is `<repo>-<env>`, unchanged.
- Job and workflow names are capped at 63 characters.

## Execution path (RUN-01, ALERT-01/02)

```
Scheduler (per instance) ── POST …/workflows/<wf>/executions
   {"argument": "{\"args\":[…],\"labels\":{\"instance\":…[,\"flow\":…]}}", "labels": {"instance": …}}
Workflow etl/workflow.yaml (generic, one per flow)
   for attempt in 1..RETRY_COUNT+1:  jobs.run(JOB_NAME, containerOverrides[0].args = args)
      failure → ba-ops-notify job_failed (then sleep RETRY_BACKOFF_SECONDS)
   all attempts failed → raise
   success → ba-ops-notify job_succeeded; then, if NEXT_WORKFLOW, executions.create(same argument, same labels)
```

- **Labels** are native Workflows *execution* labels. Cloud Run's `jobs.run` has no per-execution labels.
- **Events** use ba-ops-notify's typed v2 format: `{schema_version: 2, source: {job_name, gcp_project_id, gcp_region, environment}, event: {...}}`. The `event` has:
  - `event_type`: `job_failed` or `job_succeeded`
  - `severity`: `ERROR` or `INFO`
  - `title`, and `summary` (the error message)
  - `log_uri`: the execution's console logs
  - `context`: the instance labels, plus:
    - `exit_code`: on failure, the first task's `lastAttemptResult.exitCode`; `0` on success
    - `exit_class`: APP-05 classes (0 ok, 1 bug, 2 config, 3 transient source, else unknown)
    - `execution`, `attempt`, `args`
  - Notifying is **fail-soft**: a notifier error is logged and never fails the workflow.
- **Manual run:** `gcloud workflows run <wf> --data='{"args":["--start-dt","…"]}' --labels=instance=manual`

## Deploy steps (`dry_run` prints them only)

`etl/live.sh` takes a read-only snapshot of jobs, workflows, schedulers, `etl-heartbeat-*` metrics and policies. `etl/render.py` turns the declaration plus that snapshot into a bash plan, where each resource is a `create` or an `update`:

1. `run jobs deploy <job> --image=<image>:<sha> --env-vars-file` (fails before anything changes if the image wasn't built)
2. `workflows deploy <wf> --source=etl/workflow.yaml --env-vars-file`. `etl-deploy` checks this repo out at its own commit (OIDC `job_workflow_sha`).
3. `scheduler jobs create|update http`, once per instance.
4. **Orphans (RUN-04),** right after the schedulers:
   - What counts as an orphan:
     - everything named in `replaces`
     - schedulers that target this flow's workflow but aren't declared (removed instances)
     - the flow's own heartbeat metric/policy when it sets `heartbeat: false`
   - ENABLED orphan schedulers are **paused**, so two schedulers never fire.
   - All orphans are printed. Nothing is deleted: delete them by hand after prod is verified, then drop the `replaces` entries.
   - The deploy **fails** if a `replaces` name doesn't exist, if a replaced scheduler targets a workflow this flow neither owns nor replaces, or if a `replaces` name is also declared by the flow (in-place adoption needs no `replaces`). That keeps flows independent of deploy order.
5. **Heartbeat (ALERT-03):**
   - log metric `etl-heartbeat-<job>`: the job's `Container called exit(0).` lines
   - alert policy of the same name, which fires when the sum is < 1 over the window
   - missing data counts as a breach (the ba-infra pattern)
   - it notifies `ETL_ALERT_CHANNEL`
   - **The window:**
     - every N minutes or hours, or hourly: 3× the interval, at least 1h
     - daily: 25h, the alerting maximum
     - anything else (day-of-week/month, names, ranges) needs `heartbeat_window` or `heartbeat: false`, otherwise the deploy fails
     - with several instances, the smallest window wins
   - The policy is created only once the metric exists, i.e. from the second deploy on, so a metric with no data yet can't raise a false alarm.

**RUN-05 (Artifact Registry):**
- `etl-build` sets the cleanup policy once, when it creates `<repo>-<env>` ([`etl/ar-cleanup-policy.json`](../etl/ar-cleanup-policy.json)):
  - keep the 30 most recent versions of each image (~10 builds: each buildx push is an index + platform + attestation manifests)
  - delete all other tagged versions
  - delete untagged versions older than 7 days
  - AR never deletes manifests that a kept image index references.
- Existing repos get it from the one-off `etl/ar_cleanup.py PROJECT REGION live.json [--apply]`.
  - It is a **dry run by default**: it lists, per repo, what the policy would delete.
  - With `--apply` it also adds a Keep rule for every image a live Cloud Run job or service runs.

Self-check: `python3 etl/test_render.py` (needs `yq`). Proof mode: `python3 etl/test_render.py live.json`.

## Drift check (RUN-06)

`mode: drift` runs on a schedule (see [`examples/ews/drift.yaml`](examples/ews/drift.yaml)). It prints two lists and exits 1 when either is non-empty:

- `MISSING`: declared resources that aren't live, including the heartbeat.
- `UNDECLARED`: orphans that haven't been deleted.

It needs names only, not values.

## IAM (verify in phase 2)

| SA | roles |
|---|---|
| deploy | `roles/run.developer`, `roles/workflows.editor`, `roles/cloudscheduler.admin` (pause included), `roles/logging.configWriter`, `roles/monitoring.alertPolicyEditor`, `roles/iam.serviceAccountUser` on the job, workflow and scheduler SAs. Possibly also `roles/monitoring.notificationChannelViewer`, if attaching the channel needs `notificationChannels.get` |
| drift (read-only) | `roles/run.viewer`, `roles/workflows.viewer`, `roles/cloudscheduler.viewer`, `roles/logging.viewer` (metrics list), `roles/monitoring.viewer` |
| workflow | what it has today (`run.jobs.runWithOverrides`, `run.invoker` on ba-ops-notify), plus `roles/run.viewer` (`tasks.list` for the exit code) and `roles/workflows.invoker` (`next`) |
| build | `roles/artifactregistry.writer`, plus `artifactregistry.repositories.create` and `.update` (set the policy at creation; `repoAdmin` lacks `update`) |
| `ar_cleanup.py` operator | `roles/artifactregistry.reader` + `roles/run.viewer` for the dry run; `roles/artifactregistry.admin` to `--apply` |
