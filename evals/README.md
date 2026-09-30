# LiveKit simulation scenarios

Scenario YAML lives in [scenarios/](scenarios/). These files describe simulated
callers, expected behavior, and office/caller context; they do not seed patients,
insurance, appointments, or availability.

| Suite | Exercises |
| --- | --- |
| [scenarios.yaml](scenarios/scenarios.yaml) | New-patient intake and existing-patient identity resolution. |
| [availability.yaml](scenarios/availability.yaml) | Existing-patient availability searches. |
| [appointments.yaml](scenarios/appointments.yaml) | Booking, rescheduling, and cancelling one visit in a single call. |
| [knowledge.yaml](scenarios/knowledge.yaml) | Office and provider knowledge from Product. |
| [ai_disclosure.yaml](scenarios/ai_disclosure.yaml) | Saying it is an AI assistant when a caller asks, in English and Spanish. |

## Before running

Use a configured LiveKit Cloud project, an already-running `abita-s2s` worker,
and sandbox patient middleware. The worker's simulation path disables Product
writes and live SIP transfer setup; configured office knowledge reads remain
available. Configuration is documented in [.env.example](../.env.example).

Read the prerequisite comments in each YAML. Every scenario must pass on any
date and in any order. The appointment scenario books, reschedules, and cancels
the same visit in one call so the shared chart ends where it started. Missing
fixtures or failed middleware leave the scenario unproven.

## Run a suite

From the repository root, with the intended LiveKit project selected:

```sh
lk agent simulate text --agent-name abita-s2s --scenarios evals/scenarios/scenarios.yaml
lk agent simulate audio --agent-name abita-s2s --scenarios evals/scenarios/appointments.yaml
lk agent simulate list
```

`--agent-name` targets an already-running registered agent. Check the selected
project and worker configuration before running. These command forms are verified
against `lk agent simulate --help`, `text --help`, and `audio --help`; this guide
does not claim a simulation was executed.

Use `lk agent simulate view --help` and `lk agent simulate export --help` for
inspecting and exporting a completed run. Preserve tool arguments, tool results,
and receipts with the run's conversation evidence. Audio behavior requires audio
simulation evidence; actual SIP behavior and provider writes require their own
observed records. A spoken success claim or a passing text simulation is not
provider confirmation.

## Prompt-change CI

[Prompt scenario evals](../.github/workflows/evals.yml) runs only when a pull
request or a push to `main` changes `prompts/speaker.md` or `prompts/thinker.md`
under `src/abita_s2s/`. It runs every YAML suite under `evals/scenarios/` in audio
mode against a local worker launched from the checkout. Tool-only, scenario-only,
and documentation-only changes do not trigger it.

Repository secrets must supply `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, `OPENAI_API_KEY`, `SANDBOX_AMD_API_URL`,
`SANDBOX_AMD_API_TOKEN`, `ACUITY_PRODUCT_KNOWLEDGE_URL`, and
`ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET`. Missing secrets fail before any
simulation starts. Fork pull requests do not receive repository secrets.

Runs and suites execute serially with simulation concurrency set to one. Every
suite is attempted even after a failure, and any failed suite fails the job.
Logs are saved as a seven-day Actions artifact. CI does not seed fixture state or
freeze the clock, so do not add scenarios that depend on the run date or on writes
from another scenario. Do not configure this path-filtered workflow as a required
merge check; GitHub leaves its check pending on PRs without prompt changes.

## Releases and offline checks

Release discovery includes `.yaml` and `.yml` files recursively under `evals/`.
Checksums and release archives retain relative paths such as
`scenarios/appointments.yaml`. Moving a scenario changes the bundle identity even
when its contents stay identical. This README and non-YAML run outputs are not
part of the eval checksum set.

Release tests verify recursive discovery, content-based versions, archive paths,
and reproducible packaging:

```sh
uv run --no-sync python -m unittest discover -s tests -p test_release_deploy.py -q
```

Packaging scenarios does not execute them. Reusable offline Python fixtures remain
with the tests that consume them in `tests/`; no separate eval fixture runner or
fixture directory is provided.
