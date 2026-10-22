# LiveKit simulation scenarios

Scenario YAML lives in [scenarios/](scenarios/), one suite per file. These files
describe simulated callers, expected behavior, and office/caller context; they do
not seed patients, insurance, appointments, or availability.

## Before running

Use a configured LiveKit Cloud project and sandbox patient middleware. The
worker's simulation path disables Product writes and live SIP transfer setup;
configured office knowledge reads remain
available. Configuration is documented in [.env.example](../.env.example).

Read the prerequisite comments in each YAML. The appointment scenario books,
reschedules, and cancels the same visit in one call so the shared chart ends where
it started. The slot-identity regression requires 11 AM slots on two consecutive weekdays,
seeded 7-20 days after the run date. Its userdata neither creates those slots nor sets the clock.
Missing fixtures or failed middleware leave the scenario unproven.

## Run a suite

From the repository root, with the intended LiveKit project selected, pass
`src/abita_s2s/main.py` as the entrypoint. The simulator writes `id:` fields into
the scenario file, so pass a copy:

```sh
cp evals/scenarios/ai_disclosure.yaml /tmp/ai_disclosure.yaml
lk agent simulate audio --scenarios /tmp/ai_disclosure.yaml -k 3 -y src/abita_s2s/main.py
lk agent simulate list
```

The CLI spawns a uniquely named local agent. Do not pass `--agent-name`, because
the project also hosts the deployed `abita-s2s` agent. Only the deployed image
sets `ABITA_S2S_DEPLOYED`, so a local `abita-s2s dev` worker registers as
`abita-s2s-local-<user>` and never takes production calls.

Without `ACUITY_PRODUCT_KNOWLEDGE_URL` and the product secret, office
knowledge fails locally; compare those suites before and after, not absolutely.
Judges can mark a spoken result as premature because GPT-Live adds speech to the
history before the tool output; check the order in the agent log before trusting
such a failure. These command forms are verified
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
