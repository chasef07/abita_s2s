# Call state audit and Python migration map

Audit of the current local `abita_agent` source (HEAD `d29e60d`). This is a design
proposal, not an implemented refactor or proof of production behavior. Source and
relevant test scenarios were inspected; tests were not executed for this audit.

## Conclusion

Preserve the existing correctness rules. Simplify where state lives and what it
is called. Do not translate the large TypeScript CallState definition wholesale,
and do not build a generic workflow engine or event-sourced system.

Call state should answer five questions: which call is this, which patient is
being served, what care context is current, what actions happened, and how does
the call end? Conversation history belongs to LiveKit. Backend records remain
authoritative; call state holds verified snapshots and evidence of actions.

## Current map

| Current state | Meaning and owner | Migration decision |
| --- | --- | --- |
| `runtime` call/phone/SIP fields | Immutable incoming-call identity; startup | Group as `context`. Preserve actual called number, even when several numbers map to one office. |
| `office.activeKey` | Mutable office used by tools; office selection | Call it `care.office_key`. It is distinct from the called office. |
| `identity.privateCandidates`, `nameSearch`, `pendingIdentity` | Identity evidence, lookup cache, pending caller identity | Keep under the identity module; never expose private candidates as model context. |
| `identity.activePatient` | Verified or created patient snapshot, chart references, loaded appointments | Group patient-bound insurance here too. |
| `identity.registration`, `unregisteredPatientReceipt` | Draft identity plus evidence permitting registration | Keep distinct from an active patient; a lookup failure is not proof of a new patient. |
| `identity.operationVersion`, `transitionVersion` | Stale-read and patient-transition guards | Preserve semantics initially; hide inside the identity owner. |
| `insurance.onFile` | Insurance stored for the patient | Move with the active patient snapshot. |
| `insurance.lastEligibilityCheck` | Acceptance check for a proposed plan/visit, potentially before registration | Keep in care context, explicitly scoped to its subject, office, and visit type. |
| `workflow.visitType`, `workflow.routing` | Visit purpose, provider routing and prior-authorization requirement | Keep under care. |
| `availability` | Offered slots, private booking tokens, search selection | One availability owner under care; keep model-facing references separate from private token data. |
| `identity.completed*`, `runtime.staffTasks` | Receipts used to prevent repeat mutations | Move under actions, retaining typed booking/cancellation/reschedule/staff receipts. |
| `runtime.outcomeReceipts` | Per-tool-call outcome evidence for Product/observability | Separate diagnostics from operational receipt storage; derive output where sufficient. |
| `runtime.transferState`, `endedReason` | Transfer/hangup lifecycle | Group as lifecycle; preserve ambiguous transfer state. |
| `runtime.voiceLanguage` | STT/TTS language and voice machinery | Do not port the old provider machinery to GPT-Live. Add language state only if a concrete workflow needs it. |

## Proposed logical structure

```text
CallState
├── context                     immutable for this call
│   ├── call_id, started_at
│   ├── caller_phone, called_number, called_office_key
│   └── room_name, sip_participant_identity, sip_call_id
├── identity                    identity module owns transitions
│   ├── lookup evidence and private candidates
│   ├── pending identity / registration attempt
│   ├── active patient or none
│   │   ├── chart ID, verified identity, backend references
│   │   ├── appointment snapshot with load status
│   │   └── insurance on file
│   └── internal operation and patient-context revisions
├── care                        current work, invalidated when its subject changes
│   ├── office_key, visit_type, routing, preauthorization
│   ├── proposed insurance check with explicit scope
│   └── availability snapshot and private inventory
├── actions                     survive patient changes
│   ├── booking, cancellation, reschedule receipts
│   └── staff request receipts and idempotency keys
└── lifecycle                   transfer and call termination
    ├── transfer: idle / pending / accepted / ambiguous
    └── ended_reason
```

This is a logical map, not a request for a file per branch. Start with `state.py`
for small records, `identity.py` for identity transitions, and the existing startup
module for composition. Add scheduling and action modules with their workflows.
Avoid setters for every field; use operations such as resolving a patient or
selecting an office that own all required invalidation.

Async tasks, locks, abort signals, and short-lived caches are implementation state
of their owning modules. They should not appear in a serialized CallState. The
current TypeScript implementation already has private WeakMaps for identity
operations, office cancellation, and availability runtime. The Python equivalent
can use private fields on per-call owners rather than module-global side tables.

## Simplifications with evidence

1. **Move completed actions out of identity.** Appointment replay guards currently
   live under `identity.completed*` and are read by scheduling workflows. They
   survive patient switches and represent effects, not identity. Relocate first;
   unify receipt formats only after comparing every consumer's required fields.
2. **Rename overloaded IDs.** `runtime.callId` is the phone call ID, but
   `DomainOutcomeReceipt.callId` is supplied a LiveKit tool-call ID and used to
   replace that tool call's outcome. Use `call_id` and `tool_call_id` explicitly.
3. **Co-locate availability data.** Selected slots, tokens, and routing are public
   CallState fields; cache deadlines, token expiries, generations, failures, and
   in-flight reads live in a private WeakMap. One availability owner should manage
   them. A private slot entry can hold token and expiry together while projecting
   only safe references and labels to the model. Preserve expired-token rejection,
   stable reference rules, retry bounds, and request coalescing.
4. **Make patient-bound insurance atomic.** Patient promotion currently writes the
   patient, insurance, and routing separately. Expose one identity transition that
   installs verified patient data and resets dependent work together.
5. **Reduce invalid combinations rather than adding status flags.** The current
   pending identity, registration draft, active patient, and absence receipt have
   ordering rules. Explicit attempt variants may simplify them, but retain any
   combinations used during re-verification. Do not impose a generic linear
   `identified -> insured -> booked` state machine: callers can ask unrelated
   questions, switch patients, or cancel an already-loaded appointment.

## Distinctions that must not be collapsed

- **Caller versus patient:** shared numbers and calls on behalf of another person
  are supported. Pre-call lookup currently stores private candidates only;
  `applyPreCallLookup` does not activate a patient.
- **Called office versus care office:** `selectAvailabilityOffice` can change the
  active office; patient switching resets it to the trunk office. Office context
  also affects insurance and knowledge, so it is broader than a scheduling filter.
  Preserve the raw trunk for Product's special Sweetwater optical routing.
- **On-file versus checked insurance:** a checked plan is not a persisted update.
  Checks can exist before an active patient. Name these acceptance checks, not
  coverage guarantees, in the Python domain model.
- **Not loaded, empty, failed appointments:** an empty list cannot represent all
  three. Use a snapshot with an explicit load status; add loading only if needed.
- **Read revision versus patient revision:** a later lookup can supersede a read
  without changing the patient. Creation commit currently tests both revisions;
  replacing them with one counter without parity tests could lose valid writes.
- **Speech interruption versus mutation cancellation:** stopping audio cannot undo
  a successful backend write. Record late successful writes against their original
  subject even if they must not become the active patient's state.
- **Operational receipts versus diagnostics:** a log entry is not an idempotency
  guard. Existing outcome evidence is generic and may be replaced by tool-call ID;
  it cannot simply replace typed completion records.

## Transition map and required proof

| Event | Required state behavior | Existing evidence to preserve |
| --- | --- | --- |
| Phone lookup completes | Add private evidence; preserve active runtime work | `initial-call-state.test.ts` |
| Identity remains ambiguous or fails | Do not promote a patient or assume new registration | `patient-identity.test.ts`, `patient-resolution-flow.test.ts` |
| Caller switches patient | Clear active patient immediately while unresolved; reset patient-scoped care, insurance and inventory; retain action receipts | `patient-identity.test.ts` |
| Patient activates | Commit verified snapshot, insurance and routing together | `patient-identity.test.ts` |
| Chart creation returns after context changes | Preserve write evidence; apply the current commit guards before activation; never blindly retry | `patient-identity.test.ts`, creation implementation |
| Office, visit, route, or search changes | Invalidate applicable inventory; prevent old results replacing current selection | `appointment-inventory.test.ts`, `scheduling/state.ts` |
| Slot expires | Require fresh availability before booking | Availability/token implementation and inventory tests |
| Reschedule partially succeeds | Retain replacement booking and unresolved old cancellation; do not rebook | `scheduling/workflow.ts` |
| Transfer outcome is uncertain | Keep ambiguity visible; do not treat it as idle or confirmed success | `transfer-call.ts`, `call-lifecycle.ts` |

These are inspected contracts/test scenarios, not new executed proof. Run their
Python equivalents against the new interfaces during implementation.

## Implementation order

1. Add call context and identity state only. Port pre-call candidates, resolution,
   and identity-switch behavior with the existing test scenarios.
2. Introduce care and acceptance checks when migrating insurance and availability.
3. Add typed action receipts with the first mutation and Product outcome delivery.
4. Add lifecycle state when implementing transfers and deadlines.

Keep canonical state private to application tools. Return minimal verified facts
and the next required input to the thinker; pass only relevant outcomes to the
speaker. Session userdata is not automatically visible to either model. Do not
inject the whole state or create a second authoritative copy in conversation.

## Source map

- `abita_agent/src/state/call-state.ts`: current types and initialization.
- `abita_agent/src/runtime/initial-call-state.ts`: pre-call evidence application.
- `abita_agent/src/identity/patient-identity.ts`: resolution, promotion, resets, operation guards.
- `abita_agent/src/scheduling/state.ts`: insurance and routing invalidation.
- `abita_agent/src/scheduling/availability.ts`: inventory, tokens, cache and read coordination.
- `abita_agent/src/scheduling/routing.ts`: called-office versus selected-office behavior.
- `abita_agent/src/state/appointments.ts`: loaded appointments and completion receipts.
- `abita_agent/src/scheduling/workflow.ts`: replay protection and partial rescheduling.
- `abita_agent/src/state/observability.ts`: outcome projection and staff receipt storage.
- `abita_agent/src/state/call-lifecycle.ts`: active office and transfer states.
