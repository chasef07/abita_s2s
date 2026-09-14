# Staff task delivery

`create_staff_task(category, urgency, summary, message)` submits one caller-approved
unresolved need. Approval, intake clarification, records policy lookup, speaking
restrictions, and clinical transfer remain tool instructions, matching the source
agent. There is no invented consent boolean or change to emergency prompts. Staff
owns fulfillment; the tool promises neither resolution nor callback timing.

## Contract and source choices

Inspected Abita Agent `0e77f0f`: `src/tools/create-staff-task.ts`,
`src/identity/patient-identity.ts`, `src/runtime/portal-auth.ts`, office profiles,
and `src/__tests__/staff-task-tool.test.ts`.

Inspected Product `456fafd`: `api/openapi.yaml` (`CreateStaffTaskRequest`,
`StaffTaskReceipt`), `backend/internal/httpapi/server.go` (`CreateStaffTask`), and
`backend/internal/work/work.go` (`CreateAITask`, normalization, validation,
fingerprint). Product authenticates the Bearer service credential and authorizes
CREATE_TASK for the office inside its transaction. Its insert explicitly sets
`work_tasks.call_id` to NULL and stores `source_call_id`: **no call record or START
interaction prerequisite exists**. Only `/v1/tasks` is called. No lifecycle pipeline
or fabricated call identity is needed.

Set `ACUITY_PRODUCT_HANDOFF_URL` ending in `/v1/handoffs`, as in Abita Agent; the
client derives `/v1/tasks`. Use `ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET`. The
endpoint must use HTTPS except loopback. Redirects are disabled. Crystal River
has no registered staff tool and the owner also blocks it. Other currently
supported offices retain source enablement; Product remains the final authorizer.

The existing nullable `resolve_patient` interface and resolution implementation
are unchanged. A read-only resolver accessor snapshots its canonical pending
caller-reported identity or verified receipt. No candidates are treated as a
verified patient, and a switched unresolved patient cannot inherit the old chart.
The middleware's patient-resolve specification confirms the existing receipt
fields; this work adds no middleware requests or registration behavior.

Patient ID/name/DOB are sent only through Product's documented optional patient
compatibility fields. Product does not persist ID or DOB and source-labels the
name as contact context, not verified identity. Caller phone comes exclusively
from call context, never the patient's on-file phone. Missing patient identity is
allowed; missing caller contact blocks delivery because Product requires it.
The tool has no new model-provided callback number input. Essential intake and
missing details stay in the message; backend limits reject, never truncate.

## Known Product category gap

Abita Agent exposes nine non-billing categories. Current Product supports only
six of those: appointments, documentation, medication, optical, referrals, other.
It rejects insurance, pre_op, and post_op. The tool preserves all nine model
categories but explicitly fails those three before sending and instructs the
model not to relabel them to bypass the restriction. No Product source was edited.
Full nine-category delivery requires a separate Product contract/schema change;
this PR can merge independently with the limitation visible.

## Mutation and receipt semantics

A private per-call owner retains in-flight deliveries and successful receipts.
Concurrent identical submissions share a write. Completed identical submissions
return duplicate without another request. Keys hash the complete normalized
payload (outer text whitespace trimmed), including urgency and patient identity.
This deliberately fixes the source agent's omission of urgency and its lossy
case/whitespace hash: Product fingerprints exact details, so those collisions
could reject changed requests or return a misleading local duplicate.

Changed details, urgency, or patient are distinct submissions, not edits or
cancellations of an existing task. Semantic deduplication is not guessed. When
recovering uncertain delivery, repeat the exact request instead of rewording it.
Product can also return a duplicate receipt through its own database deduplication.

Writes receive at most two attempts per invocation, each bounded to ten seconds,
with identical payload/key. Timeout, network failure, retryable HTTP errors, or
invalid success receipts are uncertain; they do not prove absence of a write.
A receipt must have created/duplicate status, UUID task ID, and matching category
and urgency. Explicit rejection is failed unless an earlier attempt was already
uncertain. Uncertainty survives a later invocation's rejection; an exact retry
can recover a duplicate receipt. No transport payloads or secrets are logged.

The registered tool disallows interruptions. The owner additionally shields the
in-flight write from waiter cancellation and drains it during shutdown. Patient
switches never cancel mutations. Late results retain the original patient name
and a patientChanged flag; private patient IDs/DOB are not emitted to the model.
The delivery receipt is evidence of submission, not of staff completion.

## Offline proof and integration

`uv run python -m unittest discover -s tests -q` exercises the existing suite and
registered staff tools through actual AgentSession execution with deterministic
model and httpx transports. Coverage includes distinct needs, duplicates, changed
urgency/details, verified and unresolved patient switches through resolve_patient,
missing identity/contact, office filtering, unsupported categories, validation,
invalid receipts, explicit rejection, timeout recovery, uncertain writes, late
results, concurrent duplicates, and cancellation of a mutation waiter.

No real Product requests, staff messages, live GPT-Live delegation, voice/SIP
calls, deployment, or durable database proof were performed. Consent-following by
the live model and voice interruption behavior remain unverified.

Base is main `d651920` (merged PR #2). No dependency on other migration PRs.
Additive integration touches `agent.py`, `config.py`, `offices.py`, startup, and the
resolver's read-only accessor. When combining sibling workstreams, retain each
owner's constructor argument and startup wiring; do not replace their tool lists.
`state.py`, middleware, dependencies, and prompts are unchanged.
