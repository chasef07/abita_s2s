# Abita S2S

A Python LiveKit worker for Abita's voice receptionist. One job owns one call:
one `AgentSession`, one `CallState`, and one shared HTTP client. GPT-Live handles
speech and turn-taking; its delegated thinker invokes the application tools.

## Start here

- [Eval scenarios](evals/README.md): sandbox prerequisites and LiveKit simulation usage.
- [Environment settings](.env.example): configuration names and examples.

## Local development

Use Python 3.13 and uv. Configure `.env.local`; exported variables take precedence.
Console audio requires live credentials and an explicit office.

```sh
uv sync --locked
uv run abita-s2s --help
ABITA_CONSOLE_OFFICE=spring-hill uv run abita-s2s console
uv run abita-s2s dev
uv run --no-sync python -m unittest discover -s tests -q
uv run --no-sync ruff check .
```

## Repository map

| Path | Purpose |
| --- | --- |
| `src/abita_s2s/tools/` | Model-facing tools, grouped by capability. |
| `src/abita_s2s/integrations/` | Patient, registration, and scheduling HTTP adapters. |
| `src/abita_s2s/observability/` | Post-call evaluations and their result contracts. |
| `src/abita_s2s/runtime/` | Call composition, reporting, and observability. |
| `src/abita_s2s/prompts/` | Speaker and thinker instructions. |
| `tests/` | Offline behavior, integration contracts, and release checks. |
| `evals/scenarios/` | LiveKit simulation scenario YAML. |
| `scripts/` | Release publishing and deployment operations. |

Deploy middleware contracts before their agent consumer. Offline tests and packaged
eval scenarios do not establish live audio, SIP, or provider-write correctness;
see the eval guide for verification boundaries.

## Post-call evaluation

After accepted writes drain, calls with caller messages are evaluated by
`typesafe-ai/jev` through Vercel AI Gateway using `AI_GATEWAY_API_KEY`.
[jev.py](src/abita_s2s/observability/jev.py) uses the
[TypeSafe-compatible API](https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe)
at `POST /typesafe/v1/systemone`. Evaluator version `typesafe-scorecard-v2`
includes six `noul` checks:

- `request_understood`: understood the caller's request and corrections.
- `appointment_datetime_correct`: tool results match the final agreed date/time
  in the office timezone, including both appointments for rescheduling.
- `office_rules_grounded`: policy answers come from recorded office rules and
  knowledge, without invented restrictions, requirements, or exceptions.
- `results_reported_truthfully`: action claims match tool evidence available
  when the claim was made, including failed or uncertain results.
- `resolved_or_handed_off`: every request is resolved or has a supported,
  appropriate handoff, honoring requests for a person and office escalation rules.
- `conversation_responsive`: caller attempts to regain attention or repeat unanswered
  information indicate a stall, even if the agent later recovers. Ordinary greetings,
  clarifications, and caller-requested pauses do not. This checks conversational
  evidence, not measured audio silence or its technical cause.

`expressed_sentiment` retains the existing five-level score from very negative to
very positive across the whole call. No clear sentiment is neutral or mixed.
Sentiment is independent of resolution or handoff and evaluates text, not vocal
tone. Every judge receives the full recorded conversation, instructions, and
tool calls/results. `noul` is TypeSafe's native yes/no primitive: its response
contains a `noul` value from 0 to 1. We preserve that value without inventing a
binary threshold, applicability decision, or aggregate grade. An absent matching
appointment result cannot establish appointment correctness.

Seven independent requests run concurrently under a shared 20-second deadline,
with a one-second outer cleanup allowance. Each transient HTTP or transport
failure gets at most one retry within that deadline; `Retry-After` is respected.
Successful judges survive other judges' HTTP errors, invalid answers, or timeouts.
The existing Product CLOSEOUT request includes `closeoutPayload.evaluation`,
stored in `ai_interactions.closeout_payload`. `results` is keyed by judge name and
retains each successful raw response and usage. `errors` is keyed by failed judge
and records only exception class, HTTP status when available, and attempt count;
response bodies, credentials, and exception messages are not logged.

`complete` means all seven judges returned valid answers, not that the call passed.
`incomplete` records judge errors, timeouts, or missing instructions; `skipped`
records no caller messages or an unconfigured Gateway key. Failed judges have no
answer rather than a fabricated pass/fail. Evaluation failure does not prevent
Product closeout or change the call outcome. Evidence remains available through
`/v1/ai/interactions/{id}/evidence`. No database migration is needed; older stored
evaluations keep their original evaluator version and shape. This change does
not rerun or overwrite past evaluations.

## Release versions

The complete deployed agent has one version and exact Git commit. Its release
manifest also records a version, file inventory, and SHA-256 digest for each
folder defined in [component_folders.json](src/abita_s2s/component_folders.json):

- `src/abita_s2s/prompts/`
- `src/abita_s2s/tools/`
- `evals/`
- `src/abita_s2s/observability/`

Each component covers every Git-tracked file recursively, including documentation
and non-code assets. Local caches and untracked output are excluded. An unchanged
folder retains its previous component version; a changed folder takes the new
agent release number. Component numbers can therefore skip agent releases.
Changes include additions, edits, renames, and deletions. Comparison is against
the latest component release in the selected commit's ancestry.

Changed components receive immutable `<component>-v<version>` releases containing
the full folder archive and a separate `release.json`. Unchanged releases are
verified and reused. All components still ship through one agent deployment;
LiveKit release attributes record all four component versions and digests.
Older prompt/eval bundles are reused only if their original limited file coverage
matches the complete folder. Jev's `evaluatorVersion` separately labels evaluation
semantics; the observability release version identifies the whole source folder.
