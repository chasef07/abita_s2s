# Abita S2S

Python LiveKit worker using OpenAI GPT-Live. One job owns one call.
Office questions use Product knowledge search. Patient resolution uses the existing
middleware contract. Insurance, registration, and appointment scheduling have local
implementations and offline tests; transfers are not migrated yet.

## Releases and LiveKit deployment

Releases follow the same approval point as `abita_agent`: merge the generated
release PR, then GitHub Actions verifies, publishes, and deploys automatically.

1. A successful `Verify` push run on `main` triggers Release Please to create or
   update a release PR with the version, changelog, and matching `uv.lock` version.
2. Merging that PR creates a draft release and immutable tag. The exact release
   commit passes the full verification workflow before publishing the
   agent, prompt, and eval assets and checksums.
3. The published release deploys automatically to the separately provisioned
   `abita-s2s` LiveKit agent. Deployment verifies the target dispatch name, release
   checksums, running replicas, and version attributes. Missing configuration or
   failed health fails the workflow.

### Agent, prompt, and eval versions

Each release records `agent_version`, `prompts_version`, and `evals_version` in
`release.json`, startup identity logs, and LiveKit deployment version attributes.
Release Please advances the agent version. Prompt and eval versions advance only
when their content differs from their previous component release. A changed bundle
uses the new agent release number; an unchanged bundle keeps its previous version.
For example, agent `0.4.0` can reference prompts `0.3.2` and evals `0.3.5`.
The `prompts_sha256` and `evals_sha256` hashes identify exact content. Deployment
verifies every version and hash.

Edit scenario YAML files under `evals/` and commit them normally. Changed suites publish
an immutable `evals-v<VERSION>.tar.gz` containing the YAML files and manifest,
alongside the prompt bundle and agent package. The `evals-v<VERSION>` GitHub release
also provides the eval bundle independently. Unchanged bundles remain on their
existing component releases; the new agent manifest references them, and publishing
verifies their content without creating another bundle release. Results are not
part of this bundle.
The running agent records the associated eval suite's identity; it does not run
the suite during deployment or claim that the scenarios passed.

Inspect a deployed release with `lk agent versions --json` or the startup
`release_identity` log. No separate manual version files are needed. Release builds require full Git
history and component tags (`git fetch --tags`); shallow checkouts are rejected.

### Deployment configuration

One-time GitHub configuration:

- Install the existing Release Please GitHub App on this repository. Set variable
  `RELEASE_PLEASE_APP_CLIENT_ID` and secret `RELEASE_PLEASE_APP_PRIVATE_KEY`.
  Its contents, issues, and pull requests permissions must allow writes. The app
  token ensures generated release PRs trigger ordinary CI.
- Set secrets `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`.
- Provision the Python agent with its runtime secrets. Set variables
  `ABITA_S2S_AGENT_ID` to that new agent ID and `ABITA_S2S_PROJECT_SUBDOMAIN` to its
  project subdomain. The deploy workflow checks the `production` environment;
  repository secrets and variables may also be configured there.

`Publish release` can resume artifact publication for an exact commit after a
failed run. `Deploy exact release` can retry a published release, stage it, promote
a tested staging version, or roll back to an explicitly selected version. All
deployment operations share one lock. These workflows do not provision agents,
upload runtime secrets, or change SIP routing.

Release Please creates drafts with tags immediately so the existing immutable
asset publisher can finish them safely; see its
[draft and tag options](https://github.com/googleapis/release-please/blob/main/docs/cli.md).

## Setup

Use Python 3.13 and uv:

```sh
uv sync --locked
```

Runtime variable names are in `.env.example`. The worker loads `.env.local`
from the working directory without overriding exported environment variables.
GPT-Live requires an OpenAI account with GPT-Live access. LiveKit room modes also
require `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`; the CLI validates
those settings. No credentials are included.

```sh
uv run abita-s2s --help
ABITA_CONSOLE_OFFICE=spring-hill uv run abita-s2s console  # Local microphone/speaker test
uv run abita-s2s dev      # LiveKit worker; dispatch name: abita-s2s
```

## Source ownership

- `src/abita_s2s/main.py`: worker registration and environment loading.
- `src/abita_s2s/config.py`: application credential validation.
- `src/abita_s2s/model_config.py`: GPT-Live and backend model configuration.
- `src/abita_s2s/agent.py`: voice instructions and greeting.
- `src/abita_s2s/runtime/session_startup.py`: session and room lifecycle.
- `src/abita_s2s/knowledge.py`: Product knowledge search and response validation.

The voice model is `gpt-live-1`, with `gleam` and Responses delegation to
`gpt-5.6-luna`. Voice and backend instructions are separate. GPT-Live owns
turn-taking; no separate STT, TTS, or VAD is configured. The greeting is a request
to speak, not an exact script. A refused greeting is logged.

Use a dedicated test room: the session closes when the linked participant leaves
and deletes the room on close. LiveKit owns connection and shutdown callbacks.

## Verification boundary

CLI help and Python construction checks do not require credentials. Local audio,
model access, SIP dispatch, interruptions, and hangup still require live
verification. No test number has been configured.

Reference: [LiveKit GPT-Live plugin](https://docs.livekit.io/agents/models/realtime/plugins/gpt-live/).

## Prompts

Edit the Markdown files in `src/abita_s2s/prompts/`:

- `speaker.md`: voice persona, opening greeting, and when to delegate.
- `thinker.md`: instructions for delegated reasoning and future tools.

The prompts describe the intended Abita workflows; office knowledge and patient
resolution have model-facing tools. Unavailable actions must not be claimed as completed.
`prompt.py` loads them relative to the package, independent of the working
directory. They ship in the built wheel. Missing or empty files fail visibly.
New agents load the files again; restart the worker after editing prompts.
Existing GPT-Live sessions retain their startup instructions.

## Trying voices

The installed plugin lists `aster`, `beacon`, `cinder`, `marin`, `stone`, and
`vesper`. It also accepts other voice names supported by your OpenAI account.
Set `GPT_LIVE_VOICE` in `.env.local` or override it for a new console session:

```sh
GPT_LIVE_VOICE=vesper ABITA_CONSOLE_OFFICE=spring-hill uv run abita-s2s console
```

Stop the session before trying another voice. Voice selection is fixed for each
GPT-Live session. Console tests need credentials and make live API calls.

## Office routing

`offices.py` owns office identity, trunk aliases, and greetings.
Supported production offices match `abita_agent`: Spring Hill, Crystal River
(Eye Radiance), Hollywood, Sweetwater, and North Miami Beach Optical, including
all 11 trunk numbers. Sweetwater optical remains a Sweetwater routing alias. Other trunks fail explicitly; they do not select a default office.
Real jobs wait for a SIP participant and bind session audio to that participant.
Console jobs require `ABITA_CONSOLE_OFFICE` set to `spring-hill`, `crystal-river`,
`hollywood`, `sweetwater`, or `north-miami-beach-optical`; this setting is ignored
for real calls. The agent receives the resolved immutable office profile.

Product call registration remains future migration work.
No SIP trunk or dispatch configuration has been changed.

```sh
uv run python -m unittest discover -s tests
```

## Call state

`state.py` defines customer-independent call metadata and patient snapshots.
Startup creates one `CallState` per session and attaches it as typed LiveKit
`userdata`. Future tools use `RunContext[CallState].userdata` to access it; state
is not automatically added to either model's prompt.

The first slice contains only `call` and `patient`. Private lookup candidates do
not activate a patient. `PatientResolver` privately owns current-read tokens and patient changes:
older, cross-call, or replayed lookup results are rejected, and clearing the active
patient invalidates outstanding reads. The caller phone is not the patient phone;
missing caller ID remains unknown. Console sessions have unique local call IDs
and no fabricated SIP metadata. `session_started_at` records worker entry time;
Product call-start timestamp parity will be handled with Product integration.

`PatientResolver` validates and activates one canonical patient receipt, including
its backend references and loaded facts. Call state holds no read coordination;
tokens, tasks, and pending identity remain private to the resolver.
Chart creation and other writes require separate receipt/commit handling.
Care and action records will be added with insurance and scheduling tools.

## Office knowledge

Set `ACUITY_PRODUCT_KNOWLEDGE_URL` to the full Product
`/v1/agent/knowledge/search` endpoint and
`ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET` to a service credential authorized for
the office with `READ_KNOWLEDGE`. The URL requires the secret, which can also
enable staff delivery. Without knowledge configuration, the tool
reports unavailable; partial or insecure configuration fails at startup.

The thinker calls `search_office_knowledge(query)` through LiveKit's existing
Responses delegation. The tool derives `X-Office-Key` from call state and sends
only the non-patient question. One HTTP client is reused for the call and closed
at job shutdown. Requests have a four-second total deadline, do not follow
redirects, and propagate cancellation. The called office is fixed for now;
cross-office searches are not supported.

Results distinguish found facts, no relevant information, and temporary failure.
Found passages must belong to one revision. The thinker receives plain text
starting with `success:`, `no_results:`, `blocked:`, or `needs_input:`, without
corpus IDs or legacy status markers. Simulations retain the configured read-only
knowledge endpoint; patient operations use sandbox middleware and Product writes
remain disabled.
There is no local-file fallback.

Offline tests cover the Product contract and real `AgentSession` tool execution
using deterministic model/HTTP substitutes. They do not prove live Product
retrieval, GPT-Live audio, or spoken correction handling. With credentials set,
use the console command above to ask about office hours, then correct the question
to Saturday hours while lookup is running; verify the final answer matches the
latest question and the returned facts.

## Patient resolution

Set both `AMD_API_URL` (middleware base URL) and `AMD_API_TOKEN` to enable patient
reads. Without them, resolution reports unavailable. The client sends the token
in `Authorization`, matching the middleware contract, and routes through the
called office's canonical phone number. Neither office nor chart IDs are model inputs.

One per-call `PatientResolver` owns private phone lookup, pending first name/DOB,
matching, read freshness, and patient activation. Startup begins phone lookup in
parallel with session startup; it never activates a chart. If that read is still
pending when interactive resolution starts, it is cancelled and its late result
cannot replace current evidence. Missing caller ID stays unknown.

The registered `resolve_patient(firstName, dob)` tool accepts only these two nullable
fields. It tries a supplied first name immediately, using a unique qualifying
phone candidate without demanding DOB. Phone matching preserves the existing
whole-name Damerau similarity and Double Metaphone thresholds. Otherwise it asks
for DOB. Supplied DOB needs no separate confirmation. A complete first-name/DOB
search must select one chart, then its hydrated receipt must match both the
selected chart and supplied identity. Ambiguity stays unresolved and asks for
clarification or staff help. A same-name patient switch requires a different DOB;
this interface cannot distinguish two people with the same first name and DOB.

A conflicting name or DOB clears the old active patient before another read.
DOB-only followups retain the pending first name; a changed name does not inherit
an earlier DOB. Duplicate in-flight requests share one read. Superseded, cancelled,
cross-call, and replayed reads cannot activate a patient. The HTTP owner permits
one retry for eligible read failures within a ten-second total deadline and does
not follow redirects. The LiveKit tool is cancellable; it does not block interruptions.

Callers who say they are new proceed directly through full intake and `add_patient`;
no existing-chart lookup is required. Registration requires caller confirmation,
complete details, accepted insurance, and a confirmed read-back. Existing-patient
work still uses `resolve_patient`.

Verified state retains backend references, on-file insurance, routing, appointments,
and appointment-load status. Appointment-load failure remains visible and allows
reloading. The model receives only the verified name, insurance carrier, DOB-on-file
indicator, appointment-load status, outcome, and next input; private references and
candidate details never enter tool output. Dedicated owners perform registration
and appointment mutations.

Offline tests exercise the HTTP contract, identity cases, cancellation and late
responses, and the actual registered tool across multiple `AgentSession` turns.
They substitute the model and HTTP transport: they do not prove live middleware,
GPT-Live Responses delegation, audio interruptions, SIP, or deployment behavior.

## Appointment scheduling

`Scheduling` owns availability, booking, cancellation and rescheduling. It consumes
the insurance/registration guard and updates the canonical active patient receipt.
Only returned private references can select slots or loaded appointments. Writes
are never automatically retried; uncertain results and partial moves require staff
reconciliation. Offline tests cover the four registered tools through AgentSession,
patient switches, cancellation, duplicate writes, and partial rescheduling. Live
backend, audio and SIP behavior still require verification.

## Staff tasks

Caller-approved staff delivery uses the existing Product service credential and
optional `ACUITY_PRODUCT_HANDOFF_URL`.
