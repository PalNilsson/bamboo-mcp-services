# Container image

One image runs every Bamboo MCP Services agent under `bamboo-supervisor`.
Deployment happens in two stages, decided in S-12:

- **Stage A:** run the image on aipanda033 itself, with the host's
  `/data/bamboo` and `/tmp/bamboo` mounted at the same paths. Bamboo MCP keeps
  reading the same files, so the image is the only thing that changes.
- **Stage B:** run it in Kubernetes, in one pod with Bamboo MCP. This stage is
  not built yet; the [Kubernetes notes](#stage-b-kubernetes-notes) below
  record what it must satisfy.

The decisions are S-12 – S-24 in the containerisation handover. The parts that
explain the image's shape are summarised here.

---

## What is in the image

| | |
|---|---|
| Base | `python:3.12-slim-bookworm`, venv at `/opt/venv` (S-14). No conda. |
| Dependencies | Exactly `requirements/runtime-py312.txt` (S-21), installed `--no-deps` and then checked with `pip check`. torch is the **CPU** build of the locked version, so no CUDA libraries are installed. |
| Project | Installed from its wheel, which carries the bundled configs (S-22). |
| Model | `/opt/bamboo/models/sentence-transformers/all-MiniLM-L6-v2`: the files listed in `docker/model-manifest.sha256`, downloaded at commit `1110a243fdf4…` and checked by SHA-256 (S-15). |
| PID 1 | `tini -s` (S-14). |
| User | UID 10001 by default. Runs under any UID in group 0 (S-17). |
| Entry point | `bamboo-supervisor --config /data/bamboo/supervisor/supervisor-agent.yaml --log-file /data/bamboo/supervisor/logs/supervisor-agent.log --heartbeat-file /data/bamboo/supervisor/heartbeat.json` |
| Health | `HEALTHCHECK` runs `bamboo-supervisor --status` against that heartbeat file (S-18). |
| Provenance | `/opt/bamboo/build-info.json` (version, commit, lock hash, torch build, model revision, manifest hash) and OCI labels. |
| Environment | `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, so nothing reaches the Hub at run time. `ANONYMIZED_TELEMETRY=False` turns off ChromaDB's telemetry. `HOME=/home/bamboo`. |

The build itself checks three things as its last step, running as the
runtime user. First, the model loads offline as Transformer → Pooling →
Normalize and gives unit-norm 384-dimensional vectors
(`scripts/check_embedding_model.py`). Second, torch has no CUDA. Third, every
entry point and `git` is on `PATH`.

## Before the first build: the model manifest

The image must carry the *same* model files that built the ChromaDB store. A
different snapshot gives different vectors, and nothing fails loudly. The
manifest records those files. Generate it on aipanda033 from the directory that
`--model-path` points to in the reference configuration. That is the directory
`check_chroma_store.py` verified at cosine 1.000000:

```bash
cd /data/bamboo/models/sentence-transformers/all-MiniLM-L6-v2
find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /tmp/model-manifest.sha256
cat /tmp/model-manifest.sha256
```

Copy the file to `docker/model-manifest.sha256` and commit it. The build does
three things with it:

- it downloads exactly those files at the pinned commit;
- it fails if any hash differs, or if any file is missing or extra;
- it refuses to start without the manifest.

The pinned commit is `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, the snapshot
name under the `.hfcache` copy on aipanda033.

## Build

```bash
docker/build.sh                      # linux/amd64, loaded into the local Docker
IMAGE=registry.cern.ch/<project>/bamboo-mcp-services docker/build.sh --push
```

`docker/build.sh` always builds for **linux/amd64**, because aipanda033 and the
cluster nodes are x86_64. A plain `docker build` on an Apple Silicon Mac
produces arm64, which those hosts cannot run (S-24). The amd64 build runs under
emulation on the Mac. It is slower, and the model check at the end of the build
takes longer than usual, but the result is the same.

The tag is `<version>-<12-character commit>`. The script refuses to build when
tracked files have uncommitted changes, so a tag always names a real commit.
`ALLOW_DIRTY=1` overrides this and appends `-dirty` to the tag.

The script needs three things:

- `docker/model-manifest.sha256` must exist;
- the build needs network access to PyPI, `download.pytorch.org` and
  `huggingface.co`;
- pushing needs a project on registry.cern.ch (still to be decided).

Without a registry, copy the image directly:

```bash
docker save bamboo-mcp-services:<tag> | gzip | ssh aipanda033 'gunzip | sudo docker load'
```

### Refreshing the lock

The lock is generated, not edited. After the reference environment changes
on aipanda033:

```bash
pip freeze > services-freeze.txt                     # on aipanda033, in the env
python scripts/make_runtime_lock.py --freeze services-freeze.txt \
    --source-label "aipanda033, conda env bamboo-mcp-services, pip freeze of <date>"
```

Before committing a new lock, confirm that duckdb and chromadb still equal the
versions in bamboo-mcp's environment. Both write files that bamboo-mcp reads.

---

## Stage A: aipanda033

### Prerequisites

- **Docker access.** `atlpan` is not in the `docker` group, so the commands
  below use `sudo`. The container processes still run as atlpan
  (`--user 29817:1307`). Adding atlpan to the `docker` group would remove the
  need for `sudo`. Membership of that group is equivalent to root on the host,
  so decide that deliberately (on a Puppet-managed host it goes through
  hiera).
- **One supervisor at a time.** Two supervisors would run two cric and two
  ingestion writers against the same DuckDB files. **Stop the conda-based
  supervisor before starting the container**, and stop the container before
  starting the conda-based one.

### Run

```bash
IMAGE=registry.cern.ch/<project>/bamboo-mcp-services:<tag>
COMMON=(
  --user 29817:1307 --group-add 0       # atlpan; group 0 for $HOME (S-17)
  --pid=host                            # core-reaper sees Bamboo MCP's PIDs (S-23)
  -v /data/bamboo:/data/bamboo          # same paths as on the host (S-12)
  -v /tmp/bamboo:/tmp/bamboo            # core-analysis root, shared with Bamboo MCP
  --mount type=bind,source=/cvmfs,target=/cvmfs,readonly,bind-propagation=rslave
)

# 1. Smoke test: start everything, check it, run each scheduled agent once, stop.
sudo docker run --rm "${COMMON[@]}" "$IMAGE" \
    --config /data/bamboo/supervisor/supervisor-agent.yaml \
    --log-file /data/bamboo/supervisor/logs/supervisor-once.log --once
echo "exit: $?"                          # 0 or 1

# 2. The long-running container.
sudo docker run -d --name bamboo-services "${COMMON[@]}" \
    --restart unless-stopped \
    --stop-timeout 45 \
    --log-opt max-size=50m --log-opt max-file=3 \
    "$IMAGE"
```

The `/cvmfs` mount uses `rslave` propagation. CVMFS repositories are
automounted and can be unmounted when idle; with propagation, a repository
mounted on the host after the container started is still visible inside it.

`--stop-timeout 45` gives the supervisor its 30 s `stop_timeout_s` plus a
margin before Docker sends SIGKILL.

`--log-opt` bounds Docker's own copy of the logs, because every agent's
console output reaches the container's stdout. The agents also write their own
rotating files under `/data/bamboo/supervisor/logs/`, so each line is stored
twice. Reducing what the agents print is planned for the next round of code
changes.

### Verify

```bash
sudo docker ps --filter name=bamboo-services       # STATUS shows (healthy) after ~1 min
sudo docker exec bamboo-services bamboo-supervisor --status \
    --heartbeat-file /data/bamboo/supervisor/heartbeat.json
sudo docker exec bamboo-services cat /opt/bamboo/build-info.json
sudo docker exec bamboo-services ls -l /cvmfs/atlas.cern.ch/repo/sw/local/etc/cric_pandaqueues.json
sudo docker logs --tail 50 bamboo-services
```

After the first document-monitor cycle, run `check_chroma_store.py` as before.
It must still report **ALL HEALTHY**.

### Switch to the baked-in model

The first run keeps the reference configuration unchanged, so document-monitor
still reads the host's model through the `/data/bamboo` mount. Moving to the
model baked into the image (S-15) is a separate, checked step:

1. In `/data/bamboo/supervisor/supervisor-agent.yaml`, change
   document-monitor's `--model-path` to
   `/opt/bamboo/models/sentence-transformers/all-MiniLM-L6-v2`.
2. Restart the container: `sudo docker restart bamboo-services`.
3. Run `check_chroma_store.py` inside the container, with `--model-path`
   pointing at the baked model:
   `sudo docker exec bamboo-services python /data/bamboo/supervisor/check_chroma_store.py …`.
   The minimum cosine must be **> 0.999**; 1.000000 is expected, because the
   files are identical by hash. A lower value means stop and restore the old
   path.

### Roll back

```bash
sudo docker stop bamboo-services && sudo docker rm bamboo-services
# then start the conda-based supervisor as before
```

Nothing on `/data/bamboo` is in a different format: duckdb and chromadb are the
same versions inside and outside the image.

---

## Stage B: Kubernetes notes

These are not deployed yet. They are the constraints the decisions set:

- **One pod, two containers** (S-12): Bamboo MCP and this image, with
  `shareProcessNamespace: true` (S-23, core-reaper liveness). Before choosing a
  volume type, run `tests/test_duckdb_cross_process.py` in a pod on it.
- **Volumes** (S-13):
  - one PVC at `/data/bamboo` holds `db/`, `document_monitor-v2/`, `rag/`,
    `tmp/` and `supervisor/logs/`. About 2.5 GB today, mostly `db/`.
    Back it up only as a whole-volume snapshot, because the ChromaDB store and
    its checkpoints are one unit.
  - a shared volume at `/tmp/bamboo` for core dumps. If it is an `emptyDir`,
    its `sizeLimit` must exceed `BAMBOO_CORE_ANALYSIS_QUOTA_BYTES`, or the pod
    is evicted. The reaper deletes nothing.
  - the configs come from a ConfigMap, and the model is in the image.
- **CVMFS:** the CVMFS CSI driver, mounting `atlas.cern.ch` read-only.
- **Probes:** a liveness `exec` of `bamboo-supervisor --status --heartbeat-file
  … --max-age 120`. Use no readiness probe, since the container serves
  nothing.
- **Stop:** `terminationGracePeriodSeconds: 45` or more.
- **Identity:** an arbitrary UID in group 0 works as is. Set `fsGroup` so the
  PVC is writable.
- **CPU:** set `OMP_NUM_THREADS` to the container's CPU limit. Otherwise torch
  sizes its thread pool from the node's cores and is throttled.
- **Root filesystem:** with `readOnlyRootFilesystem: true`, mount an
  `emptyDir` at `/tmp`. github-sync clones repositories there, ROOT among them.
- **Secrets:** `GITHUB_TOKEN` comes from a Secret once S-10 is signed off.
  First measure the shared egress IP's GitHub rate limit from a pod.
- **Egress:** `bigpanda.cern.ch`, `api.github.com`,
  `raw.githubusercontent.com`, `github.com` and `gitlab.cern.ch`.
