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
| [visit_type_triage.yaml](scenarios/visit_type_triage.yaml) | Asking the visit type for an ambiguous eye exam and searching the triaged type. |
| [slot_identity.yaml](scenarios/slot_identity.yaml) | Selecting the confirmed date when adjacent dates share a provider and time. |
| [office_facts.yaml](scenarios/office_facts.yaml) | Office facts (address, office, open now, hours, staff, services) stated only from retrieved knowledge (ACU-96). |
| [ai_disclosure.yaml](scenarios/ai_disclosure.yaml) | Saying it is an AI assistant when a caller asks, in English and Spanish. |

## Before running

Use a configured LiveKit Cloud project, an already-running `abita-s2s` worker,
and sandbox patient middleware. The worker's simulation path disables Product
writes and live SIP transfer setup; configured office knowledge reads remain
available. Configuration is documented in [.env.example](../.env.example).

Read the prerequisite comments in each YAML. The appointment scenario books,
reschedules, and cancels the same visit in one call so the shared chart ends where
it started. The slot-identity regression requires 11 AM slots on two consecutive weekdays,
seeded 7-20 days after the run date. Its userdata neither creates those slots nor sets the clock.
Missing fixtures or failed middleware leave the scenario unproven.

## Run a suite

From the repository root, with the intended LiveKit project selected:

```sh
lk agent simulate text --agent-name abita-s2s --scenarios evals/scenarios/scenarios.yaml
lk agent simulate audio --agent-name abita-s2s --scenarios evals/scenarios/slot_identity.yaml
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
