# LiveKit simulation findings

## This agent needs audio

Installed `GPTLiveModel` extends `llm.DuplexModel` at `.venv/lib/python3.13/site-packages/livekit/plugins/openai/realtime/gpt_live_model.py:154`. Official upstream explicitly rejects this adapter during text simulation because it produces replies through audio; it instructs users to run `lk agent simulate audio`. The installed 1.8 SDK lacks that new fail-fast guard, so text could hang instead. A substitute text model would exercise different model behavior. [Official source](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/voice/agent_activity.py)

## Scenario format and integration

Requirements: Python Agents >=1.6.6, CLI >=2.16.4 for text or >=2.18.3 for audio, authenticated LiveKit Cloud project. The local worker executes actual tools. Read `ctx.simulation_context()` to detect simulations; `sim.userdata()` supplies per-scenario fixtures. This is separate from SIP metadata. Use session-scoped mocks or a fake backend; final-state callback `on_simulation_end` can call `ctx.fail(reason=...)` to reject wrong state. It cannot override an LLM judge failure. Scenario dates should be absolute with the application's clock pinned. Schema is beta. [Simulation guide](https://docs.livekit.io/agents/start/testing/simulations/)

```yaml
name: Abita appointment scenarios
scenarios:
  - label: Existing patient declines cancellation
    instructions: >
      You are a synthetic existing patient, Alex, born 1990-04-12.
      Ask to cancel your upcoming appointment. When asked to confirm,
      change your mind and say to keep the appointment.
    agent_expectations: >
      The agent verifies identity, asks for explicit cancellation
      confirmation, and leaves the appointment unchanged when declined.
    tags:
      feature: cancellation
    userdata:
      fixture: existing_patient_one_appointment
      expected_state:
        appointment_status: booked
```

`fixture` and `expected_state` are application-defined data: YAML alone does not seed a backend or enforce assertions.

## Commands after simulation startup and fixtures are wired

```sh
lk agent simulate audio --scenarios scenarios.yaml --concurrency 1 src/abita_s2s/main.py
lk agent simulate audio --scenarios scenarios.yaml --concurrency 1 --background-noise --packet-loss src/abita_s2s/main.py
lk agent simulate --export RUN_ID > run.json
```

By default CLI starts local worker under temporary dispatch name; simulator runs in Cloud. Explicit entrypoint is needed for this repository layout. `--agent-name NAME` instead targets an existing worker and requires scenario file. Auth/model credentials and local dependencies remain necessary. `--scenarios` avoids source-generated scenarios/source upload. `-n` generates scenarios, not repeats of a file; use repeated runs for trials. Exports require a finished run. `--view RUN_ID` retrieves prior runs. [CLI reference](https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/)

Simulation audio is a WebRTC participant, not a SIP call; verify telephony routing separately. Audio covers actual timing and turn-taking. Start with one scenario, inspect transcript/tool results/final state and audio, then expand the suite. Overall pass should require both conversational expectations and deterministic application outcome.
