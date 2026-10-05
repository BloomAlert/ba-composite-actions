# ETL reusable workflows

`etl-build.yaml` and `etl-deploy.yaml` build and run the `etl-*` fleet's Cloud Run jobs (decisions: etl-template #47 CI, #50 RUN). The composite actions in this repo are a separate thing, and their `@main` callers are unaffected.

Pin them by full SHA with a `# vX.Y.Z` comment (CI-04/05). Examples are in [`examples/`](examples/): ews (1 instance) and stormglass (11 instances).

## Inputs

| | `etl-build` | `etl-deploy` |
|---|---|---|
| `environment` (required) | `staging`/`prod`, must equal `github.ref_name` | same in `mode: deploy`; any branch in `mode: drift` |
| `declaration` | default `deploy/runtime.yaml` | same |
| `mode` | – | `deploy` (default) or `drift` |
| `dry_run` | – | `true` prints every gcloud command and the orphan list, and executes nothing |
| outputs | `image` (`…:<sha>`), `digest` | – |

- **Secrets:** callers pass `secrets: inherit`.
  - build needs `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_ARTIFACT_REGISTRY_WRITER_SA`.
  - deploy needs `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_WORKFLOW_SERVICE_ACCOUNT` and `GCP_SCHEDULER_SERVICE_ACCOUNT`.
- **Vars:** `GCP_PROJECT_ID` and `GCP_REGION`.
- **Auth and permissions:** WIF only. The calling job grants `contents: read` and `id-token: write`, and nothing else.
- **Environment:** both jobs set `environment: <env>`, so environment-level vars and secrets resolve inside the called workflow.
- **Concurrency:** `<mode>-<env>-<declaration>` (CI-03).
  - It is keyed per flow, so one push to a 3-flow repo doesn't cancel a queued flow.
  - Drift runs never block a deploy.

## Declaration (RUN-03)

There is one file per flow (image). A single-flow repo omits `flow`.

```yaml
flow: maps                    # optional; part of every name
dockerfile: Dockerfile.maps
image: map                    # AR image name; also the buildx cache scope
job:
  service_account_var: GCP_MAP_JOB_SERVICE_ACCOUNT_EMAIL   # var/secret holding the job SA
  cpu: 2
  memory: 4Gi
  timeout: 1800s
env:                          # set on the job; each value is looked up in vars, then secrets
  - AUTH0_DOMAIN              # NAME  = value of var/secret NAME
  - GCP_BUCKET_NAME=GCP_MAPS_BUCKET   # NAME=SOURCE
  - SFTP_BANNER_TIMEOUT?      # optional: unset -> omitted (otherwise the deploy fails)
time_zone: America/Santiago   # default for instances
instances:                    # one Scheduler each; name = [a-z0-9-]
  - {name: chl-01, schedule: "0 6 * * *", args: [--ts-id, CHL-01, --source, nrt]}
legacy:                       # globs of the per-instance resources this flow replaces ({env} substituted)
  - etl-map-ingestion-*-{env}
  - workflow-map-ingestion-*-{env}
  - schedule-map-ingestion-*-{env}
```

`ENVIRONMENT` and `GCP_PROJECT_ID` are always set on the job. Each run is a full set: `run jobs deploy` replaces the env vars.

## Naming (RUN-02)

`repo-short` is the repo name without a trailing `-gcp`. Everything lives in `ba-basic`/us-west2.

| resource | name | ews example |
|---|---|---|
| Cloud Run job, Workflow | `<repo-short>[-<flow>]-<env>` | `etl-ingestion-ews-staging` |
| Scheduler | `<repo-short>[-<flow>]-<instance>-<env>` | `etl-ingestion-ews-main-staging` |
| AR repo (unchanged) | `<repo>-<env>` | `etl-ingestion-ews-gcp-staging` |

Job and workflow names are capped at 63 characters.

## Execution path (RUN-01)

```
Scheduler (per instance) ── POST …/workflows/<name>/executions
   body {"argument": "{\"args\":[…],\"labels\":{\"instance\":\"wh-01\"[,\"flow\":…]}}",
         "labels": {"instance": "wh-01"[, "flow": …]}}
Workflow etl/workflow.yaml (one per flow, generic) ── jobs.run(JOB_NAME, overrides.containerOverrides[0].args = args)
   on error: http.post the legacy body to notify-slack-etl-error-<env> (ba-ops-notify), then re-raise
```

- **Labels** are native Workflows *execution* labels, so `gcloud workflows executions list --filter=labels.instance=wh-01` works.
  - Cloud Run's `jobs.run` has no per-execution labels (its overrides are only `containerOverrides{args,env}`, `taskCount` and `timeout`).
  - On the job side, the instance shows up in its args.
- **Manual runs:**
  - `gcloud workflows run <name> --data='{"args":["--start-dt","…"]}' --labels=instance=manual`
  - The `argument` must be a map.

## Deploy steps (idempotent; `dry_run` prints them only)

1. `artifacts docker images describe <image>:<sha>`: the image must exist.
2. `artifacts repositories set-cleanup-policies <repo>-<env>` with [`etl/ar-cleanup-policy.json`](../etl/ar-cleanup-policy.json) (**RUN-05**):
   - keep the 10 most recent versions
   - delete untagged versions older than 7 days
3. `run jobs deploy <name>`
4. `workflows deploy <name> --source=etl/workflow.yaml`: the generic definition, at the pinned SHA. `etl-deploy` checks this repo out at its own commit (OIDC `job_workflow_sha`).
5. Per instance: `scheduler jobs describe`, then `update http` or `create http`.
6. **Orphans (RUN-04):** live resources that match `legacy` globs or `<prefix>-*-<env>` but aren't declared.
   - ENABLED orphan schedulers are **paused** in the same deploy, so two schedulers never fire.
   - All orphans are printed.
   - Nothing is ever deleted. Delete them by hand after prod is verified.

The plan is rendered by [`etl/render.py`](../etl/render.py) (stdlib Python) and run with `bash`. Its self-check is `python3 etl/test_render.py` (needs `yq`).

## Drift check (RUN-06)

`mode: drift` runs on a schedule (example: [`examples/ews/drift.yaml`](examples/ews/drift.yaml)) and reports two things for each env:

- `MISSING`: declared resources that aren't live, such as transform-influx's phantom scheduler.
- `UNDECLARED`: live resources matching the flow's globs that aren't declared, i.e. orphans that weren't deleted.

It exits 1 when either list is non-empty, so a red run is the alert.

Scope is per flow. Resources that match no repo's globs need a fleet-wide sweep, which is not covered here.
