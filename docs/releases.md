# Release and startup operations

This change prepares offline tooling. It does not provision an agent, publish a
release, configure credentials, change dispatch, or deploy anything.

## One release version

`pyproject.toml` owns the version. Bump it and run `uv lock` for every deployable
change, including prompt-only changes. The paired tags are `v<version>` and
`prompts-v<version>`; both bind to the same exact commit. This intentionally gives
unchanged prompts a new paired version when the agent changes. No runtime fetch,
registry, PyPI publishing, or moving `latest` reference is involved.

From a clean checkout of the intended commit:

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync python -m unittest discover -s tests -q
uv run --no-sync python scripts/release.py "$(git rev-parse HEAD)"
docker build -t abita-s2s:verify .
docker run --rm --network none --entrypoint /app/.venv/bin/python abita-s2s:verify -m abita_s2s.release
```

The generated ignored `src/abita_s2s/release.json` is included in the wheel and
source distribution, and is required by the production Docker build. It contains
agent/prompt versions, exact commit, individual prompt checksums, a canonical
prompt-checksum-map digest, lockfile checksum, and model configuration. The prompt
archive contains the actual untouched Markdown files and the same manifest. The
agent GitHub release also includes the prompt archive, wheel, sdist, lockfile,
manifest, and `SHA256SUMS`. Startup verifies installed prompt bytes and package
version before running the worker, and emits release identity without caller data.
Development imports without a generated manifest identify themselves as development.

Release builds use the commit timestamp and deterministic archive metadata. Reusing
an output directory for another commit fails; use a fresh directory. Publishing
is a separate, manually dispatched **Publish release** workflow. It requires a
reviewed commit reachable from main and a protected `release` environment. It first
runs offline tests and container smoke. Tags cannot move; existing assets must be
byte-identical. Partial draft uploads can resume; published assets are never
clobbered. Protect both tag patterns with repository rules and enable GitHub
immutable releases to enforce the policy outside this workflow too. Pair publishing
is sequential: if the agent upload fails after the prompt release is published,
rerun the same commit. A prompt release alone never changes a running agent.

## Provisioning gate — required before any cloud action

An operator must separately provision a **new Python agent**, dispatch name
`abita-s2s`, in the intended LiveKit project. Do not reuse the existing TypeScript
agent or copy its `livekit.toml`. This repository deliberately has no deployable
agent ID. Initial creation also creates production, so provision sandbox backend
credentials first and decide how that initial worker is isolated before creating it.
Do not connect production SIP dispatch during provisioning.

Configure these repository variables only after provisioning:

- `ABITA_S2S_AGENT_ID`: the new Python agent ID.
- `ABITA_S2S_PROJECT_SUBDOMAIN`: its explicit project subdomain.
- `ABITA_S2S_CLOUD_ENABLED=true`: explicit enablement; otherwise the workflow skips.

Create protected GitHub environments `release`, `staging`, and `production`, with
required reviewers for production and release. Supply `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` to the deployment environments. Give
cloud mutation access only to this serialized workflow. The scripts verify the
allowlisted ID and the observed `abita-s2s` dispatch name before any mutation.
They never create an agent or upload secrets.

LiveKit deployments share secret keys. Provision the existing production variables
and these five **distinct sandbox values** in LiveKit before staging:

- `STAGING_AMD_API_URL`
- `STAGING_AMD_API_TOKEN`
- `STAGING_ACUITY_PRODUCT_KNOWLEDGE_URL`
- `STAGING_ACUITY_PRODUCT_HANDOFF_URL` (ends in `/v1/handoffs`; tasks use `/v1/tasks`)
- `STAGING_ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET`

All five are required in staging; none falls back to production. Exact reuse of
production values fails. Operators must also verify sandbox URLs do not alias
production and sandbox tokens cannot write production data; string validation
cannot prove backend isolation. OpenAI credentials/voice remain shared. The
existing transfer tool blocks non-production SIP transfers. Unknown non-production
deployment names fail configuration validation. Production's existing backend
variable behavior is preserved. Cloud sets `LIVEKIT_AGENT_DEPLOYMENT=staging` for
staging, and an empty value for production.

## Stage, test, promote and rollback

Use **Deploy exact release**, with an exact published tag such as `v0.2.0` and
`action=stage`. It checks out the tag's commit, checks all downloaded asset hashes,
validates the version/lock/prompts against source, restores the packaged manifest,
and invokes pinned `lk` 2.18.6 with explicit staging and four release attributes.
It uses LiveKit's normal Dockerfile/source build; prebuilt image upload is
[Enterprise-only](https://docs.livekit.io/deploy/agents/builds/).

The workflow requires the exact observed version and release attributes in every
region, `Running` status and at least one replica. `Sleeping`, absent, mixed, failed,
or unknown status does not count as health. Staging can sleep: an authorized
operator may need to wake it with a sandbox Agent Console session during the
five-minute check. Failure to observe health fails the workflow and prevents a
successful deployment record. It does not automatically roll back or retry writes.

Keep the uploaded deployment record's `livekit_version` with the sandbox test
results. After validating the call behavior below, explicitly dispatch the workflow
with `action=promote`, the same release tag, and that **tested exact LiveKit version**.
The protected production environment is the approval gate. Immediately before
promotion the script rechecks staging identity and health. `lk agent promote
--deployment staging` promotes the tested image without building it again, then
production health and version are checked.

A workflow-level concurrency group serializes stage, promote, and rollback,
including environment approval. A later stage can run between completed workflows;
the expected-version check rejects promotion if staging changed. Do not mutate
staging via other workflows, the CLI, or the console while promoting: LiveKit's CLI
has no atomic compare-and-promote argument and an external writer could race the
last read. Restrict writers accordingly.

For rollback, explicitly select a known prior agent release tag and its recorded
LiveKit version with `action=rollback`. The version's four attributes must match
that release before `lk agent rollback --version ...` runs. Only production supports
rollback. Failed post-operation health requires operator investigation; do not
interpret it as a confirmed rollback or promotion.

## Lifecycle and evidence boundaries

One AgentServer and one per-call startup owner remain. SIP admission waits at most
20 seconds and aborts on disconnect. Session startup has a 30-second bound and
cancels on room/caller departure. Configuration and startup failures remain visible;
logs include cause classes and session close reasons, not patient details.

Application shutdown first closes admission for every owner, cancels private reads,
and concurrently drains accepted writes before closing HTTP and SIP transports.
Repeated/concurrent callback invocations share the same cleanup task. SDK callbacks
run concurrently; this ordering never depends on their registration order. Accepted
transfers are shielded from cancellation of the tool caller and have a 40-second
whole-operation deadline. Rescheduling can use two sequential 20-second HTTP
requests. The application drain budget is 50 seconds, and the SDK process shutdown
budget is 60 seconds (default was 10), including a separate 5-second transport
close limit. A cleanup deadline violation is a visible
failure; the transport still closes. The SDK's separate one-hour worker drain,
health endpoint, process pool, signal handling, and GPT-Live turn handling stay
SDK-owned. No VAD, model download, prewarm or TS recovery layer was added.

Offline tests prove lifecycle ordering with concurrent shutdown callbacks and
HTTP/CLI substitutes, failure gates, immutable reruns and package contents. They do
not prove Cloud entitlement, worker registration, GPT-Live connectivity, audible
greeting, SIP routing, office selection on a real trunk, hangup behavior, accepted
write behavior during real hangup, rolling drain, or interruption semantics. Those
need separately authorized staging validation. Full Product reporting/closeout is
outside this change and remains a rollout gap.

References checked against installed SDK 1.8.1 and CLI 2.18.6:
[deployments](https://docs.livekit.io/deploy/agents/deployments/),
[CLI commands and health statuses](https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/),
[CLI JSON output](https://github.com/livekit/livekit-cli/blob/v2.18.6/pkg/util/json.go),
[job lifecycle](https://docs.livekit.io/agents/server/job/).
