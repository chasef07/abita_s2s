# GPT-Live call timeline

The model created by `model_config.create_model` attaches an observer before its
connection task runs. It uses the LiveKit public raw-event hooks (validated against the locked 1.8.3 dependencies);
no SDK fork or dependency upgrade is needed. This supplies backend output tracing
locally rather than installing upstream PR #7408.

In a call's LiveKit traces, look for:

- `openai.received.session.delegation.created`: delegation ID, target and provider
  offset. This says help was requested, not that a backend response started.
- `openai.backend_response`: response ID, model, delegation ID, start/end time,
  token totals and terminal reason. Overlapping delegations have separate spans.
- `openai.received.response.output_item.done`: completed backend text/refusals or
  proposed function calls. Actual execution remains in LiveKit `function_tool`
  spans. A proposed call does not prove it was validated or executed.
- `openai.queued.response.item.create`: tool result submitted to the SDK transport,
  correlated by tool call ID. There is no separate tool-result success receipt.
- `openai.queued.response.create`: request to continue backend work. Follow it to
  a received `response.created` event; queued does not prove delivery.
- `openai.queued.session.*.append` and corresponding received `*.appended` events:
  context updates and receipts correlated by event ID/client event ID. A receipt
  indicates estimated context injection, not completed speech.
- Input/output transcript events: provider start/end offsets plus local arrival
  timestamps. Fragments are not complete conversational turns.
- `openai.audio_activity`: one-second activity windows containing frame counts and
  first/last local timestamps, never audio payloads. Continuous audio can contain
  silence; activity is not evidence of speech. The final partial window flushes
  on close or reconnect.
- Existing LiveKit `agent_speaking` and `realtime_inference` spans: compare these
  with output transcripts to locate generation versus local playback gaps.
  These do not prove what the caller heard on their phone.

Lifecycle events also produce structured `openai_timeline` INFO logs. Transcript
and audio content never enter those logs. Backend spans left open are marked with
`connection_replaced`, `session_closed`, or `session_disposed` on cleanup. These are
observed cleanup boundaries, not invented provider completion/failure timestamps.
An abrupt process crash can still lose unfinished spans.

Content capture follows LiveKit's `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`
setting. Payload attributes use `lk.pii.*` or the standard GenAI content attributes
recognized by LiveKit's PII filter. Raw audio and private reasoning are never
recorded. Startup configuration captures only instructions, initial context and
delegation configuration, not credentials or transport headers. Backend token
stream deltas are omitted in favor of completed output items.

## Reading a silence incident

1. Find the caller transcript and latest delegation.
2. Check whether a backend response started and how long it ran.
3. Check completed output, tool execution, queued result and queued continuation.
4. Look for the next received response and its completed answer.
5. Compare output transcript/audio activity with LiveKit playback spans.

No backend response can mean no delegation, a missing start event, or a provider
problem; do not infer the model's internal reasoning from absence alone. Managed
backend inputs and the voice model's internal consumption of returned context are
not exposed. This instrumentation cannot reconstruct them or recover past calls.

Validation is synthetic protocol replay and local unit tests. Verify one deployed
call and its downloaded Cloud traces before treating Cloud rendering/export as
proven. No behavior, recovery, timeout or automatic retry policy is introduced.
