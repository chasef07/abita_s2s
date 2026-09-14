# Abita S2S

Python LiveKit worker using OpenAI GPT-Live. One job owns one call.
Incoming calls preload private patient evidence from middleware. Patient
verification tools, scheduling, Product, and transfers are not migrated yet.

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

- `speaker.md`: voice persona and when to delegate. Greetings live in office profiles.
- `thinker.md`: instructions for delegated reasoning and future tools.

These are migration drafts; some tools described in them are not connected yet.
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

Patient verification and Product registration remain future migration work.
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
not activate a patient. `identity.py` owns current-read tokens and patient changes:
older, cross-call, or replayed lookup results are rejected, and clearing the active
patient invalidates outstanding reads. The caller phone is not the patient phone;
missing caller ID remains unknown. Console sessions have unique local call IDs
and no fabricated SIP metadata. `session_started_at` records worker entry time;
Product call-start timestamp parity will be handled with Product integration.

Pre-call middleware lookup is connected. Patient matching, registration,
and model-facing tools are not implemented yet. `activate_verified_patient`
expects identity evidence already checked by the future resolution workflow.
Chart creation and other writes require separate receipt/commit handling.
Care and action records will be added with insurance and scheduling tools.

## Middleware and pre-call lookup

`middleware.py` owns the HTTP patient read and validates the current middleware
response contract. `runtime/precall_lookup.py` stores that evidence in private
state; it does not activate a patient or expose records to the models. Hydrated
records retain appointments, insurance, and private tokens without extra DTO copies.
A phone candidate remains different from an identity-verified active patient.

Set `SANDBOX_AMD_API_URL` and `SANDBOX_AMD_API_TOKEN` for incoming test calls.
`ABITA_MIDDLEWARE_ENV` defaults to `sandbox`, with the existing sandbox backend's
`spring_hill` office identifier. Sandbox configuration cannot reuse the configured
production origin or token. To explicitly select production reads, set
`ABITA_MIDDLEWARE_ENV=production`, `AMD_API_URL`, and `AMD_API_TOKEN`. Named LiveKit
deployments cannot select production. Tokens are sent as the existing middleware
Authorization value, without an added Bearer prefix. HTTPS is required.

Each production office specifies its canonical middleware number, separate from
incoming trunk aliases. Console sessions have no caller number, so they skip lookup
and need no middleware credentials. Calls with no caller ID also skip lookup.
Missing required middleware configuration fails startup rather than choosing a
fallback environment.

Lookup runs alongside voice startup. Each HTTP attempt has a 10-second deadline;
there is at most one retry for network/timeouts, 408/429/5xx, or a middleware error
response. Invalid data and rejected requests are not retried. Failed reads remain
failures, never empty patient matches. Request diagnostics contain IDs, timing,
status, and outcome, not patient data, response bodies, or auth tokens.

Caller disconnect or session closure cancels outstanding lookup immediately.
Startup failure and job shutdown close the HTTP client. Stale results are rejected
by the patient-state read token. The client only implements phone resolution;
name/DOB verification and the model-facing resolve_patient tool are next.

Validation uses HTTP fixtures and mocked LiveKit lifecycle events. No live
middleware or GPT-Live/SIP test has been run for this slice.
