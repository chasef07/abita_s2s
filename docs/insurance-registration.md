# Insurance and registration migration

`check_insurance`, `add_patient`, and `update_insurance` are registered local
LiveKit tools. `InsuranceRegistration` owns participation checks, registration
validation, write coordination, and retained receipts for one call. The existing
managed Responses configuration and nullable `resolve_patient(firstName, dob)`
interface are unchanged.

## Source and contract choices

Inspected clean source checkouts:

- `abita_agent` at `0e77f0f6f38e12346ca3ed66b36eae910e769b55`:
  `src/tools/check-insurance.ts`, `add-patient.ts`, `update-insurance.ts`,
  `src/insurance-rules.ts`, `src/identity/patient-identity.ts`, office profiles,
  insurance rule tests, and `src/clients/owned-middleware.ts`.
- `abita_middleware` at `ae4636621848ee36e338914668e551eae4f11875`:
  `internal/http/handlers.go` and `internal/patient/patient.go`.

The four bundled `insurance_data/INSURANCE_*.json` files are unchanged copies of
that Agent snapshot. The deterministic matcher preserves phrase, exact-match,
required-word, rejection, clarification, and prior-authorization precedence.
Medical and routine-vision support follows the five existing production office
profiles. Provider restrictions and caller notices remain in responses. No vector
service is implemented or called; earlier vector work was design only. These
reference files require an explicit refresh when office policies change.

Writes use the current authenticated HTTP contracts:

- `POST /api/add-patient`: full identity, confirmed callback, address, insurance,
  policyholder/member ID, optional email and optional insured routine-vision SSN
  last four. Routine vision sends `coverageType`; medical uses the backend default.
- `POST /api/patient/update-insurance`: verified patient ID/DOB and backend
  insurance/responsible-party references, old/new coverage, visit type, member ID.

Creation must return `created` or `partial`, a nonempty backend ID, matching full
name and DOB. Update must return `updated`, the requested patient ID and plan.
The current creation HTTP response does not expose plan/responsible-party IDs;
none are invented. The confirmed requested plan supplies on-file coverage only
on full creation. Updates clear the old plan ID and preserve returned routing,
provider restrictions, ambiguity, and prior-authorization facts.

## Consumer contract and merge order

This branch starts from main `d651920` (merged PR #2). It has no unmerged PR
dependency. Merge this insurance/registration work before appointment work that
uses its contract. Shared changes are the additive agent constructor/tools,
`CallState.insurance`, startup construction/cleanup, and the narrow
`PatientResolver.activate_created` and `refresh_insurance` methods.

Appointment consumers import:

```python
from abita_s2s.insurance_state import accepted_insurance, insurance_ready

checked = accepted_insurance(state, coverage_type)
ready = insurance_ready(state, coverage_type)
registration_status = state.insurance.registrations.get(patient_id)
```

`AcceptedInsurance` contains `office_key`, `patient_revision`, `patient_id`,
private resolver `absence`, canonical `plan`, and `coverage_type`. Read through
`accepted_insurance`, not the raw stored value. A check without an active patient
or complete resolver absence can answer participation questions but cannot
license registration. Replacement absence evidence, patient/office switches,
plan corrections and visit-type changes make old acceptance unusable.

`insurance_ready` adds pending/uncertain-write, partial-registration, routing
ambiguity and prior-authorization guards. It is a participation/context guard;
it does not prove active benefits, coverage for a diagnosis, or that a newly
checked proposal has been written to the chart. Scheduling must still use the
verified on-file receipt and its own patient/visit/provider/backend policy.

`state.insurance.registrations` is the sole per-call created/partial status map,
keyed by verified backend patient ID. It replaces the initial contract commit's
single `registration_patient_id`/`registration_status` fields so late partial
receipts remain blocked across switches. Successful writes advance patient
revision, invalidating inventory held against the previous revision. Consumers
must also invalidate any selected inventory when their accepted plan/visit changes.

## Mutation and identity invariants

- Only the resolver's complete first-name/DOB absence evidence permits a new
  registration. Failed, partial, phone-only or unattempted lookup never does.
- First-registration, callback and full read-back confirmations remain explicit.
  Caller phone and patient callback stay separate. Self-pay substitutes the member
  value and omits SSN; insured routine vision allows declining SSN.
- A per-call pending write blocks additional writes. Caller cancellation or session
  interruption does not cancel the underlying write. Shutdown drains it before
  closing HTTP; the transport has a bounded deadline.
- There are no automatic write retries. Full/partial creation and duplicate update
  receipts are retained privately. A creation attempt is retained by office,
  first name and DOB so a later surname correction cannot silently create a second
  chart. Staff must resolve such corrections.
- Only validated, still-current creation evidence activates a patient. Late
  receipts remain retained but never replace another patient's active chart.
- Timeout, transport/HTTP failure, invalid receipt and indeterminate writes block
  further writes and scheduling for the call, requiring staff reconciliation.
  An insurance replacement failure may follow end-dating the old plan, so it is
  treated as uncertain even when attaching the replacement was explicitly rejected.
  Explicit creation failure stays visible and is not silently retried.
- Existing active insurance is not overwritten by a late result after a patient
  switch. A newer plan check is not overwritten by an older completed write. The identity
  owner also fences patient reads started before a successful insurance update.

## Offline proof and limits

The baseline had no registered insurance/registration tools. Tests now exercise
all three through actual `AgentSession` execution using deterministic model and
HTTP substitutes: confirmations, creation, update, duplicates, patient correction,
partial/uncertain receipts, and forced session interruption. Owner/HTTP tests add
failed and incomplete lookup, stale writes, corrected surname, self-pay, optional
SSN, verified references, timeouts, explicit failures, cancellation, and changing
coverage back to a previously used plan. Startup tests prove HTTP cleanup waits
for the pending write.

A differential run against the referenced TypeScript matcher compared **3,384**
queries (each canonical/display/alias/required-word term, caller-phrase variants,
unknown plans and unsupported visit types across the five offices). There were
**zero status or canonical-plan mismatches**. Focused rule regressions remain in
`tests/test_insurance_rules.py`. Wheel inspection confirms all four JSON references
are packaged.

Run:

```sh
uv run python -m unittest discover -s tests
uv build
```

This is offline implementation proof. It is not live backend, SIP/audio, deployment,
or production acceptance proof. No live charts, insurance changes, staff tasks or
calls were created. Receipt retention is per call, not a durable cross-process
idempotency store. Backend reconciliation remains necessary after process loss or
uncertain writes. Staff delivery and appointment operations belong to other tasks.
