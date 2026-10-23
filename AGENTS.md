# Agent guide

Read this file before you change code in this repository. It lists the commands CI
runs, each rule with the check that enforces it, and the few rules that nothing checks.

## Run the same commands as CI

The `Verify` workflow in `.github/workflows/ci.yml` runs these commands in this order.
Run them from the repository root:

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync basedpyright
uv run --no-sync python -m unittest discover -s tests -q
uv run --no-sync python scripts/release.py "$(git rev-parse HEAD)"
```

`basedpyright` fails on any type error that `.basedpyright/baseline.json` does not
list. When a change removes a listed error, the run rewrites the baseline. Commit the
rewritten file, because CI fails when it differs from the committed one. Never add a
new error to the baseline.

To run one test file, or one test class, use these forms. Many test modules import a
sibling such as `test_patient_resolution`, so the second form needs `PYTHONPATH=tests`:

```sh
uv run --no-sync python -m unittest discover -s tests -p test_budgets.py -q
PYTHONPATH=tests uv run --no-sync python -m unittest test_budgets.BudgetTests
```

To simulate calls through LiveKit Cloud, follow `evals/README.md`. Pass a copy of the
suite, because the simulator writes `id:` fields into it. Never pass `--agent-name`,
and never start a worker named `abita-s2s`, the name the deployed agent takes
production calls under in the same LiveKit project:

```sh
cp evals/scenarios/ai_disclosure.yaml /tmp/ai_disclosure.yaml
lk agent simulate audio --scenarios /tmp/ai_disclosure.yaml -k 3 -y src/abita_s2s/main.py
```

To replay the scripted calls offline, which needs only the OpenAI key:

```sh
OPENAI_API_KEY=... uv run --no-sync python -m evals.replay
```

## Rules and what enforces them

| Rule | Enforced by |
| --- | --- |
| Each module imports only the layers its layer may use. Owners and tools never import `httpx`. They reach HTTP through `integrations/`. | `tests/test_layers.py` |
| Each `CallState`, `PatientState`, and `InsuranceState` field has one writer module. Other modules read the field or call its owner. | `tests/test_state_ownership.py` |
| Appointment, registration, and insurance writes record in `WriteLedger`, and `write_effect` maps each `WriteOutcome` to one effect. A new outcome fails type checking until `write_effect` handles it. | `src/abita_s2s/writes.py`, `.github/workflows/ci.yml`, `tests/test_scheduling.py` |
| Owners send middleware and Product requests as `Request` models through `transport.post`. An unknown field fails before send, and each adapter parses every reply. | `src/abita_s2s/integrations/transport.py`, `tests/test_integration_boundary.py` |
| `name_matcher.same_patient` decides whether a caller is a chart's patient. The `SAME_PATIENT` table pins each call site's name rule. | `tests/test_patient_resolution.py` |
| Each model-facing tool is one `Entry` in `TOOLS`. Prompts and tool descriptions name only registered tools and real headings, keep each rule sentence in one place, and list each `Capability` once. | `src/abita_s2s/tools/registry.py`, `tests/test_prompt_contract.py` |
| An action tool calls its owner before it speaks or waits on speech. | `tests/test_action_speech.py` |
| Each time budget in `budgets.py` fits inside the budget it runs in, up to the process kill. | `tests/test_budgets.py` |
| The uv, Python, and LiveKit CLI pins agree. No tracked file matches `.gitignore`. Each file this table names exists. | `tests/test_repo_consistency.py` |
| A local worker never registers as `abita-s2s`. `config.agent_name` returns that name only when the image sets `ABITA_S2S_DEPLOYED=1`. | `src/abita_s2s/config.py`, `tests/test_agent_name.py`, `.github/workflows/ci.yml` |
| Each private LiveKit member that the runtime overrides or reads still exists with the same parameters. | `tests/test_livekit_surface.py` |
| Judges read recorded write outcomes, not answer wording. Changed judge definitions need a `PUBLISHED_VERSIONS` entry. | `tests/test_evaluation.py` |
| A change to prompts, tools, or the agent replays the scripted calls. The replay runs apart from `Verify` and never blocks a release. | `.github/workflows/replay.yml`, `tests/test_replay.py` |
| Python files carry no comments except shebangs and ruff `noqa` directives. | `tests/test_no_comments.py` |
| The number of type errors only goes down. | `.basedpyright/baseline.json`, `.github/workflows/ci.yml` |

## Rules that nothing checks

- Fix spoken behavior in runtime code or tool results before you change prompt text.
- Answer every Codex P1 finding on a pull request with a fix or a reason.
- Run LiveKit simulations for trunk and head from separate git worktrees, and compare
  their pass counts, not absolute scores. Office knowledge is not configured locally,
  and each call reads the speaker and thinker prompts from disk.
- Deploy a middleware contract change before the agent change that depends on it.
- Add every owner that admits writes to `Owners.writers()`, which shutdown drains.
- Under `src/`, read environment variables only in `config.py` and `runtime/`.
