# Abita S2S 

A Python LiveKit worker for Abita's voice receptionist. One job owns one call: one
`AgentSession`, one `CallState`, and one shared HTTP client. GPT-Live handles speech and
turn-taking; its delegated thinker calls the application tools.

## Code layout

Tools in `tools/` are what the model calls. Each tool hands off to an owner module that
holds one slice of the call's state, and owners send HTTP requests through
`integrations/`. `runtime/` starts the call, reports it, and shuts it down.
[tests/test_layers.py](tests/test_layers.py) assigns every module a layer and lists the
imports each layer may make.

Before you change code, read [AGENTS.md](AGENTS.md). It lists the commands CI runs and
pairs each code rule with the test or check that enforces it.

## Local development

Use Python 3.13 and uv. Configure `.env.local` (see [.env.example](.env.example));
exported variables take precedence. Console audio needs live credentials and an office.
Named LiveKit deployments are sandboxes: no human transfers, no Product writes.

```sh
uv sync --locked
ABITA_CONSOLE_OFFICE=spring-hill uv run abita-s2s console
uv run abita-s2s dev
```

Offline tests and [eval scenarios](evals/README.md) do not prove live audio, SIP, or
provider writes.

## Post-call evaluation

After accepted writes drain, calls with caller messages are scored by a jury of three
decision models through OpenRouter with zero data retention (`OPENROUTER_API_KEY`).
[observability/judges](src/abita_s2s/observability/judges) holds the seated `JURORS`
and each judge's definition. Each question needs a majority of jurors to answer; a
yes/no verdict is the majority vote. Caller sentiment is the mean juror score from
0 (very negative) to 4 (very positive), independent of failure flags. It is sent as
`results.expressed_sentiment.score` with per-juror votes and errors. Results go into Product's
closeout as `closeoutPayload.evaluation`, marked `complete`, `incomplete`, or `skipped`.
Evaluation failure never blocks closeout or changes the call outcome.

## Releases

The deployed agent has one version and Git commit. Each folder in
[component_folders.json](src/abita_s2s/component_folders.json) (`prompts/`, `tools/`,
`evals/`, `observability/`) also gets its own version and SHA-256 digest; an unchanged
folder keeps its previous version.
