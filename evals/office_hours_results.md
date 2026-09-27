# Sweetwater weekend hours audio evaluation

Run date: September 27, 2026, approximately 02:10–02:14 America/New_York
(Sunday). Scenarios: [office_hours.yaml](scenarios/office_hours.yaml).

## Result

The revised speaker prompt still produced an unsupported Saturday opening-hours
claim. The scenario catches the reported failure pattern; the prompt change is
not a verified fix.

The runs used a local worker, live Product knowledge reads, and the simulation
path's sandbox middleware and disabled Product writes. Exported agent instructions
confirm the revised office-grounding rule was loaded in every conversation.

## First run

[SR_m8AGSKfMTsL9](https://cloud.livekit.io/projects/p_3ix7gzvuwmc/simulations/runs/SR_m8AGSKfMTsL9)

| Case | Simulator grade | Exported evidence |
| --- | --- | --- |
| Are you open today? | Pass | Retrieved Sunday hours before the substantive hours answer, then said the office was closed. |
| Suggested five o'clock closing | Fail | Invented Saturday hours and an appointment restriction before lookup; later corrected them. |
| Saturday opening hours | Fail | Grader incorrectly claimed no lookup. The export shows successful retrieval before the hours answer. |

The confirmed hallucination is in job `SRJ_Wnd837aZh8BP`:

- 06:11:40 UTC: “Sí, el sábado tenemos horas más limitadas, de nueve a una,
  solo con cita. ¿Quieres que te ayude a buscar una?”
  (Saturday hours are nine to one, by appointment only.)
- 06:11:56 UTC: first `search_office_knowledge` call, asking for Sweetwater
  Saturday hours.
- 06:11:57 UTC: successful result explicitly says “Closed Saturday and Sunday.”
- 06:12:03 UTC: speaker acknowledges the error and says Saturday is closed.

The correction does not erase the initial false claim. The lookup and its result
arrived about 17 seconds after that claim.

For `SRJ_oSTUEX9UyPQo`, the search started at 06:13:08 UTC, returned the closure
at 06:13:09 UTC, and the speaker stated the closure at 06:13:15 UTC. This directly
contradicts the grader's claim that no search occurred.

## Additional run and limits

[SR_LtPT7foL3LsZ](https://cloud.livekit.io/projects/p_3ix7gzvuwmc/simulations/runs/SR_LtPT7foL3LsZ)
was intended as an old-prompt comparison, but the exported instructions show the
revised prompt loaded. Discard it as a baseline comparison.

In that run, the suggested-closing-time case retrieved hours and rejected the
suggested time. The Saturday case again received an incorrect no-lookup grade
despite a recorded successful lookup. The today case transcribed the Spanish
question as “Hello. Is Tana Virtos,” answered about a directory, and failed after
30 seconds of silence; it does not establish the hours behavior.

These six generated audio conversations do not estimate a production failure
rate or establish improvement over the previous prompt. Automatic grades must
be checked against tool calls, results, and spoken claims. No additional prompt
or runtime changes were made as part of this evaluation.

## Local checks

- `git diff --check` passed.
- `uv run --no-sync python -m unittest discover -s tests -p test_release_deploy.py -q`:
  18 tests passed. These verify packaging, not voice behavior.

## Follow-up: explicit knowledge limit at the top of the prompt

Local experiment on September 27, 2026, approximately 02:21–02:25 Eastern:
replace the bottom office-grounding sentence with this paragraph immediately
after the speaker's role:

> You do not know office hours, providers, locations, or policies until the backend
> returns them. Delegate these questions before answering. Never fill in missing facts.

[SR_etHgA6atRV74](https://cloud.livekit.io/projects/p_3ix7gzvuwmc/simulations/runs/SR_etHgA6atRV74)
ran each of the same three scenarios three times, with concurrency three.
All nine exported instruction snapshots contain the new paragraph and omit the
previous bottom sentence.

- Eight conversations reached the hours question. Every one retrieved a
  successful result containing “Closed Saturday and Sunday” before its hours
  answer. No invented opening hours were observed in those eight conversations.
- One conversation stalled after the greeting, before the caller's question
  appeared in the export (`SRJ_zmkJ6REkqTSB`); hours behavior is unproven there.
- Another conversation answered correctly after retrieval, but subsequently
  failed with a 30-second silence (`SRJ_tzhzA3C79vcH`). Its answer was in English
  after the Spanish input was transcribed poorly.
- Raw simulator grades were four passes and five failures. Three failures
  incorrectly claimed no lookup, contradicted by the exported calls and results
  (`SRJ_owfGUqrXBc4r`, `SRJ_z84dXxBUo3fA`, `SRJ_R87kD7rWCaUr`). The other two
  failures were the silences above. The suite did not fully pass.

Keep this replacement as a local candidate: the observed hours-grounding result
supports further use, but eight answered conversations do not establish a
production fix or a statistically reliable improvement. No runtime change or
deployment was performed. The obsolete temporary baseline wrapper was removed;
raw simulation exports and logs were retained locally.
