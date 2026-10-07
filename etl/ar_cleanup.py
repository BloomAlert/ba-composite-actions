#!/usr/bin/env python3
"""One-off RUN-05 rollout for existing `etl-*-{staging,prod}` AR repos. Dry-run by default.

    ar_cleanup.py PROJECT REGION LIVE_JSON [--apply]

Lists, per repo, what the policy would delete (count, GiB). --apply sets the policy:
etl/ar-cleanup-policy.json plus a Keep rule for every image a live job runs (LIVE_JSON from etl/live.sh).
New repos get the base policy from etl-build at creation.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def gcloud(*args):
    return json.loads(subprocess.run(["gcloud", *args, "--format=json"], check=True,
                                     capture_output=True, text=True).stdout or "[]")


def deployed(live, ar_path):
    """Tags and digests that live jobs (and services) run from this AR repo."""
    tags, digests = set(), set()
    for j in live.get("jobs", []):
        ref = j.get("image") or ""
        if ref.startswith(ar_path):
            rest = ref[len(ar_path):]  # <image>[:<tag>][@sha256:<hex>]
            if "@" in rest:
                digests.add(rest.split("@", 1)[1])
            else:
                tags.add(rest.partition(":")[2] or "latest")
    return tags, digests


def to_delete(images, tags_kept, digests_kept, now, keep_n):
    """Mirror of the policy: keep newest keep_n per package + deployed; delete other tagged, untagged > 7d."""
    out, by_pkg = [], {}
    for im in images:
        by_pkg.setdefault(im["package"], []).append(im)
    def ts(v):
        return datetime.fromisoformat(v["createTime"].replace("Z", "+00:00"))

    def tags_of(v):
        t = v.get("tags") or []
        return t.split(",") if isinstance(t, str) else t

    for versions in by_pkg.values():
        versions.sort(key=lambda v: v["createTime"], reverse=True)
        kept = {v["version"] for i, v in enumerate(versions)
                if i < keep_n or v["version"] in digests_kept or set(tags_of(v)) & tags_kept}
        kept_times = [ts(v) for v in versions if v["version"] in kept]
        for v in versions:
            if v["version"] in kept:
                continue
            # AR never deletes manifests a kept index references; buildx pushes those children
            # just before the index. ponytail: time heuristic (<=120s before a kept version), estimate only
            if not tags_of(v) and any(timedelta(0) <= k - ts(v) <= timedelta(seconds=120) for k in kept_times):
                continue
            if tags_of(v) or now - ts(v) > timedelta(days=7):
                out.append(v)
    return out


def main():
    project, region, live_path = sys.argv[1:4]
    apply = "--apply" in sys.argv
    live = json.load(open(live_path))
    # live.sh only lists jobs; an etl-* repo can also back a Cloud Run service, whose image must be kept too
    live.setdefault("jobs", []).extend(
        {"image": s["spec"]["template"]["spec"]["containers"][0]["image"]}
        for s in gcloud("run", "services", "list", f"--project={project}", f"--region={region}"))
    base = json.load(open(os.path.join(HERE, "ar-cleanup-policy.json")))
    keep_n = next(r["mostRecentVersions"]["keepCount"] for r in base if "mostRecentVersions" in r)
    # ponytail: exact match on the base Keep rules' tagPrefixes (only `deployed` exists); estimate only
    keep_tags = {t for r in base if r["action"]["type"] == "Keep" for t in r.get("condition", {}).get("tagPrefixes", [])}
    now, total = datetime.now(timezone.utc), 0
    for repo in gcloud("artifacts", "repositories", "list", f"--project={project}", f"--location={region}"):
        name = repo["name"].split("/")[-1]
        if not re.fullmatch(r"etl-.*-(staging|prod)", name):
            continue
        ar_path = f"{region}-docker.pkg.dev/{project}/{name}/"
        tags, digests = deployed(live, ar_path)
        images = gcloud("artifacts", "docker", "images", "list", ar_path.rstrip("/"), "--include-tags")
        doomed = to_delete(images, tags | keep_tags, digests, now, keep_n)
        # image indexes (buildx) report no size; only their per-platform manifests do
        size = sum(int(s) for v in doomed if str(s := v.get("metadata", {}).get("imageSizeBytes")).isdigit())
        total += size
        print(f"{name}: delete {len(doomed)}/{len(images)} versions, {size / 2**30:.1f} GiB;"
              f" keeping deployed {sorted(tags | digests)}")
        if apply:
            # AR ANDs conditions within one rule, so tags and digests need separate rules; prefixes max 64 chars
            policy = base + [{"name": f"keep-deployed-{n}", "action": {"type": "Keep"},
                              "condition": {k: sorted(x[:64] for x in v)}}
                             for n, k, v in (("tags", "tagPrefixes", tags), ("digests", "versionNamePrefixes", digests)) if v]
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(policy, f)
            subprocess.run(["gcloud", "artifacts", "repositories", "set-cleanup-policies", name,
                            f"--project={project}", f"--location={region}", f"--policy={f.name}",
                            "--no-dry-run"], check=True)
    print(f"TOTAL to delete: ~{total / 2**30:.1f} GiB (estimate: shared layers counted per manifest)"
          + ("" if apply else "; dry run, --apply sets the policies"))


if __name__ == "__main__":
    main()
