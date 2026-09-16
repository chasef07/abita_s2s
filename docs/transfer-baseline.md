# One confident offer baseline

The help/transfer and staff-task wording is frozen to the version used in LiveKit audio run
`SR_dwmecYiU9ZVN`, whose behavior Kyle accepted: one useful attempt, then a
respectful transfer is a win. Routine help before a staff request does not count
as that deliberate attempt. The prompt also preserves staff-task categories,
caller approval, truthful results, and mandatory transfer handling.

The retained prompt says to skip an approach that already failed. The accepted
example nevertheless asked one brief reason question after the caller described
previous failed calls. This is an observed interpretation, not proof that the
prompt guarantees the same response every time. Keep the wording fixed and use
reviewed calls to evaluate it rather than claiming a universal pass rate.

## Run locally

Requires the project dependencies, LiveKit CLI, and `.env.local` containing
`OPENAI_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
`SANDBOX_AMD_API_URL`, and `SANDBOX_AMD_API_TOKEN`. Do not commit credentials.

```sh
.venv/bin/python scripts/run_voice_scenarios.py --mock-transfer --concurrency 3
.venv/bin/python scripts/run_voice_scenarios.py --export RUN_ID > dist/voice-run.json
```

The 15 scenarios use native audio with the actual speaker and thinker. The
explicit `--mock-transfer` entrypoint installs call-control overrides inside each
simulation worker. Transfer announces, records an accepted simulated result, and
closes the session and room; end-call also closes the session. No phone number is
dialed. Production call control is unchanged. Other tools retain sandbox behavior,
including unavailable staff delivery. No chart-creation scenario is included.

Offline harness verification (no model calls):

```sh
.venv/bin/python -m unittest discover -s tests -p test_voice_baseline_harness.py -v
```

## Review evidence

- `SR_dwmecYiU9ZVN`: accepted reference call; transfer invocation recorded and
  session closed after one reason question and renewed staff request.
- `SR_GiAEsdVQAeFD`: one-call closure check; prior failed help transferred immediately.
- `SR_sgzTymn5dXSg`: remaining 14 scenarios, 43 audio turns and zero text turns.
  Includes registration withdrawal, appointment department requests, optical
  acceptance/refusal, and insurance help accepted after a staff request.

A correct tool invocation is an **action win**, regardless of sandbox result.
Judge the offer, timing, and repeated questions separately. Good transfers count
as success alongside retained useful help; low transfer rate alone is not success.
Production completion still requires actual successful results.

LiveKit can label a call failed for a 30-second caller silence timeout after the
agent closes. Its judge also sometimes overlooks tools present in the export.
Retain raw verdicts, but review the tool trace and recording before assigning an
outcome. Initial harness setup attempts are not policy evidence. Transcripts and
tool traces were inspected; recordings were not manually listened to.

Keep this baseline fixed for the initial review period. Compare useful completed
work, respectful transfers, repeated refusals/frustration, and unresolved endings
using real examples. These synthetic runs do not establish conversion improvement
or authorize deployment.

The PR retains newer scheduling and tool-result guidance from main. The referenced
voice runs predate that merge; their exact full checkout has not been retested
with paid simulations. The approved transfer and staff-task sections are unchanged.
