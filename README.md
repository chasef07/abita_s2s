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
[jev.py](src/abita_s2s/jev.py) evaluates outcome against the full recorded text
conversation, instructions, and tool results; clarity uses the agent's recorded
instructions and user messages. Reaction requires post-call feedback and is
currently recorded as unavailable by the live hook.

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

