# Call reporting

LiveKit owns the session transcript, tool executions, events, and model usage. One
`CallReporter` owns Product delivery and the small set of application mutation
receipts. No model-generated summary or separate metrics collector is required.

## Lifecycle

- A routed SIP call queues `START` without blocking voice startup.
- Scheduling checkpoints each actual booking/cancellation response. A reschedule
  first records the booking with cancellation `not_attempted`, then records the
  actual cancellation result. Partial and uncertain changes retain their meaning.
- The native `on_session_end` hook drains accepted writes, calls
  `ctx.make_session_report().to_dict()`, and delivers `CLOSEOUT`.
- Closeout includes the unmodified native report under `transcript`, package
  version, close reason, transfer status, and private `domainOutcomes`. Receipts
  use LiveKit tool-call IDs and the original patient captured by the write.
  Signed middleware tokens are excluded from application receipts.
- Startup failure after call identity is established sends a minimal `FAILED`
  closeout without inventing a transcript. Before a supported office and caller
  are known, there is no valid Product call identity to ingest.

`COMPLETED` describes a normally ended session; business receipts separately say
whether a mutation succeeded, was partial, failed, or is uncertain. `ESCALATED`
means the SIP provider accepted the transfer, not that a human answered. Session
errors and failed mutation drains produce `FAILED`.

## Configuration and delivery

Set `ACUITY_PRODUCT_INTERACTION_URL` to the Product `/v1/ai/interactions` endpoint
and `ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET` to a credential with
`INGEST_AI_INTERACTION`. URLs must use HTTPS, except loopback HTTP. Console calls
and named `LIVEKIT_AGENT_DEPLOYMENT` environments never ingest Product calls.
Without the endpoint, Product reporting is disabled and native LiveKit telemetry
continues normally. Missing SIP caller contact is logged; no fake phone is used.

Delivery is ordered and uses Product's existing idempotent lifecycle ingestion.
Each envelope has at most two attempts, each bounded to five seconds; permanent
HTTP rejection is not retried. Acknowledgement requires `created`/`updated` and a
valid interaction ID. Accepted Product receipts have a durable backend projection
path. Failure before acknowledgement is logged; final delivery failure raises an
explicit error. There is no new persistent agent-side outbox: process loss or an
outage beyond these retries can leave Product evidence incomplete. Native LiveKit
telemetry still flushes independently. Session finalization has a 180-second cap
within LiveKit's default 300-second session-end allowance.

## Analytics boundary and validation

Product already accepts native `chat_history.items` and uses `domainOutcomes`
to select its native tool execution parser. Tool execution success and business
mutation success remain separate. Product retains the latest appointment outcome
at the top level, with all observed mutation receipts in `domainOutcomes`.

The report preserves native GPT-Live usage. Product's current cost calculation
prices the older Gemma/AssemblyAI/Rime pipeline; GPT-Live cost pricing is a
separate Product change. Realtime sessions do not produce the pipeline-only
`llm_node_ttft`/`tts_node_ttfb` fields. These must remain unavailable, not fabricated
as zero. Cloud recording/retention settings continue to control native insights.

Offline tests cover the actual SDK report serializer, native final hook, ordered
Product envelopes, late mutation receipts, cancelled waiters, startup failure,
partial reschedules, patient switches, private staff receipts, transient retries,
permanent rejection, malformed acknowledgement, and sandbox exclusion. They do
not establish deployed credentials, Product persistence, or live audio/SIP proof.

Sources: [LiveKit data hooks](https://docs.livekit.io/deploy/observability/data/),
installed LiveKit Agents 1.8.1 lifecycle/report implementation, and Product's
`AIInteractionIngestRequest`, interaction validation, and analytics readers.
