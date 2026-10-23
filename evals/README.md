# Evals

This folder holds two checks on call behavior. The LiveKit simulation scenarios in
[scenarios/](scenarios/) drive a local worker through LiveKit Cloud with simulated
callers. The offline replay in [replay/](replay/) drives the agent in this process
with scripted callers and needs only the model API key.

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

## Replay scripted calls offline

`python -m evals.replay` plays ten scripted calls against the real speaker and
thinker prompts and the real tools. GPT-Live and the thinker run on the OpenAI API.
Every other request goes to the seeded backend in [replay/world.py](replay/world.py),
which answers in memory and records any request it has no route for. The replay
starts no LiveKit server, room, or worker, and reads no secret except
`OPENAI_API_KEY`.

From the repository root:

```sh
OPENAI_API_KEY=... uv run --no-sync python -m evals.replay
OPENAI_API_KEY=... uv run --no-sync python -m evals.replay --only person_request named_staff
```

Each call runs in four steps:

1. `gpt-4o-mini-tts` speaks each distinct caller line once.
2. The lines play as caller audio over continuous silence. GPT-Live does not speak
   without input audio, and it reads typed input as a note rather than as the
   caller, so typed input does not reproduce call behavior.
3. After each turn, the replay waits until GPT-Live has been quiet for 3 seconds
   with no thinker work, then plays the next turn.
4. The checks in [replay/checks.py](replay/checks.py) judge the transcript. They
   flag an action the agent says it is doing with no tool behind it (#153), a fact
   stated before the tool that grounds it, a wrong office fact the caller suggested
   and the agent confirmed, a stall after the caller answered, a call that ends
   early or never ends, and the visit type of the first insurance check (#173).

The run prints every transcript, a Markdown verdict table, and the model spend.
`--out` writes the results as JSON, and `--summary` appends the table to a file.
The exit code is 0 when every call passes, 1 when a check fails, and 2 when the key
is missing or the run reaches `--max-calls`, which defaults to 200.

`--max-calls` counts each speech request, each GPT-Live session, and each thinker
response. The replay asks before every speech request and before every GPT-Live
session starts, and stops without making the call once the count reaches the limit.
Thinker responses start inside GPT-Live, so the replay can count one only after it
has started, and the run stops once those push the count past the limit. GPT-Live
audio is billed by the second and is reported in the summary, not counted as calls.

Nine full local runs on 2026-10-23 each made 70 to 74 model calls and used 420 to
490 seconds of GPT-Live audio, 250,000 to 290,000 thinker input tokens (about 72%
cached), and 3,600 to 5,400 thinker output tokens. Each took 70 to 99 seconds.

Verdicts vary between runs because the models do. In three runs at one commit, 9
of the 10 calls got the same verdict each time with the current checks. The tenth,
`wrong_facts_suggested`, confirmed the caller's wrong street ("And yes, we're on
Main Street.") in two of the three runs and passed in the third. Read a failed
call's transcript before you act on it.

To add a scenario, add a `Scenario` to [replay/scenarios.py](replay/scenarios.py).
Give the caller everything the agent needs in the first turn, because a scripted
caller does not adapt. Put any backend data it needs in `replay/world.py`. A
transfer returns "No active SIP caller", because the replay has no room.

The `Offline replay` workflow in `.github/workflows/replay.yml` runs on pull
requests from branches in this repository and on pushes to `main` that change
`src/abita_s2s/prompts/`, `src/abita_s2s/tools/`, `src/abita_s2s/agent.py`,
`src/abita_s2s/model_config.py`, `src/abita_s2s/prompt.py`,
`src/abita_s2s/runtime/silence.py`, `src/abita_s2s/runtime/delegation_watchdog.py`,
or `evals/replay/`. It is separate from `Verify`, so a model outage or a failed
replay never holds back a release. It reads the `REPLAY_OPENAI_API_KEY` secret and
skips with a summary line when the secret is not set. Pull requests from forks
never run it, and the job stops after 10 minutes. Anyone who can push a branch to
this repository can run code that reads the key, so use a dedicated key with a
spending limit.

## Releases and offline checks

Release packaging hashes every tracked file under `evals/`, including this README
and the replay. Checksums and release archives retain relative paths such as
`scenarios/appointments.yaml`. Moving a file changes the bundle identity even when
its contents stay identical. Untracked output, such as `evals/results/`, is not
part of the bundle.

Release tests verify recursive discovery, content-based versions, archive paths,
and reproducible packaging:

```sh
uv run --no-sync python -m unittest discover -s tests -p test_release_deploy.py -q
```

Packaging scenarios does not execute them. The unit tests in
`tests/test_replay.py` run the replay's checks and backend offline, with no model
calls.
