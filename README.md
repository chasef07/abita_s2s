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
[jev.py](src/abita_s2s/observability/jev.py) sends the full recorded text conversation,
instructions, and tool calls/results to each evaluator. Outcome judges achieved
results from evidence; clarity judges the caller's request in context. Reaction
scores the caller's expressed sentiment
across the whole recorded conversation, including changes during
the call. No clear sentiment is neutral or mixed; no separate feedback is required.
Sentiment measures expressed emotion independently of resolution or handoff;
`reports_unresolved` separately checks the caller's reported outcome.
`claims_supported` checks factual claims throughout the call against evidence
available when each claim was made. There is no `left_undone` question.
This evaluates transcript text, not vocal tone.

This adapts [TypeSafe's trace-observability example](https://evals.typesafe.ai/agent_trace_observability):
completion, request clarity, and caller reaction remain independent judgments.
Our evaluators all receive the whole call, and reaction uses in-call sentiment
instead of requiring separate post-call feedback. The example's permission gate
and automated triage actions are not implemented here.

Evaluation has a 20-second total deadline. The existing Product CLOSEOUT request
includes `closeoutPayload.evaluation`, stored on the AI Interaction in
`ai_interactions.closeout_payload`. It contains evaluator version, model,
timestamp, status, and raw grouped results with probabilities, scores, and usage.
Authorized evidence retrieval exposes it through the existing
`/v1/ai/interactions/{id}/evidence` endpoint. No database migration is needed.

Evaluation status `complete` means the applicable groups returned, not that the
call passed. `incomplete` records missing instructions, errors, or timeouts;
`skipped` records no caller messages or an unconfigured Gateway key. Evaluation
failure does not prevent Product closeout or change the call's outcome. Scores
do not independently verify backend state or audio quality. This first version
stores one evaluation with closeout; it does not overwrite it with later reruns.
