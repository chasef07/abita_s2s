# Abita S2S

Python LiveKit worker using OpenAI GPT-Live. One job owns one call.
Office questions use Product knowledge search. Patient, scheduling, and transfer
workflows are not migrated yet.

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

The prompts describe the intended Abita workflows; only office knowledge has a
model-facing tool so far. Unavailable actions must not be claimed as completed.
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

Product call registration and patient lookup remain future migration work.
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

This is state infrastructure only. Patient matching, registration, backend lookup,
and model-facing tools are not implemented yet. `activate_verified_patient`
expects identity evidence already checked by the future resolution workflow.
Chart creation and other writes require separate receipt/commit handling.
Care and action records will be added with insurance and scheduling tools.

## Office knowledge

Set `ACUITY_PRODUCT_KNOWLEDGE_URL` to the full Product
`/v1/agent/knowledge/search` endpoint and
`ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET` to a service credential authorized for
the office with `READ_KNOWLEDGE`. Set both or neither. Without them, the tool
reports unavailable; partial or insecure configuration fails at startup.

The thinker calls `search_office_knowledge(query)` through LiveKit's existing
Responses delegation. The tool derives `X-Office-Key` from call state and sends
only the non-patient question. One HTTP client is reused for the call and closed
at job shutdown. Requests have a four-second total deadline, do not follow
redirects, and propagate cancellation. The called office is fixed for now;
cross-office searches are not supported.

Results distinguish found facts, no relevant information, and temporary failure.
Found passages must belong to one revision. The thinker receives complete answer
text with its question and office, without corpus IDs or legacy status markers.
There is no local-file fallback.

Offline tests cover the Product contract and real `AgentSession` tool execution
using deterministic model/HTTP substitutes. They do not prove live Product
retrieval, GPT-Live audio, or spoken correction handling. With credentials set,
use the console command above to ask about office hours, then correct the question
to Saturday hours while lookup is running; verify the final answer matches the
latest question and the returned facts.
