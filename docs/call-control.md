# Call control migration

`CallControl` owns transfer state and guards the inherited LiveKit `EndCallTool`.
The registered tools have no patient prerequisites. They remain available for
emergency and named-staff requests without requiring identity collection.

The transfer tool waits for prior speech, requests one noninterruptible announcement
in the caller's language, waits for its playout, then admits the handoff and sends
one REFER to the original SIP participant. GPT-Live supplies native speech; this
uses `generate_reply` because there is no standalone TTS configured for `say`.
The announcement text is instructed, not a guarantee of verbatim generated audio.

Destinations follow the current abita_agent office profile: Crystal River uses
`tel:+13527941244`; other supported offices use authenticated Product admission
when configured, otherwise the existing direct handoff endpoint. The original
called office and trunk remain authoritative. No model-supplied destination is
accepted. Product contact and idempotency payload freeze on the first admission
attempt, so patient corrections cannot change an already admitted handoff.

## Configuration

Product admission uses `ACUITY_PRODUCT_HANDOFF_URL`,
`ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID`, and
`ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET`. The shared Product secret can be used without enabling knowledge search;
knowledge search still requires its URL and this secret.
Without Product routing, direct admission requires `ACUITY_HANDOFF_URL` and
`ACUITY_HANDOFF_SECRET`. URLs must use HTTPS. Incomplete configuration fails
visibly. Named `LIVEKIT_AGENT_DEPLOYMENT` sandbox deployments block transfers.
Console and missing-SIP cases return explicit unavailable results without closing.

## Outcomes and cleanup

- `pending`: another request is already running; no second announcement or REFER.
- `accepted`: SDK reports successful transfer; this does not prove a human answered.
- `ambiguous`: transport cancellation/failure, ongoing/unknown SDK result, or uncertain
  admission write. Neither transfer retry nor end-call is allowed.
- `failed`: known pre-REFER failure or explicit SDK failure. A pre-REFER failure may
  offer one retry. An explicit SIP failure never offers a REFER retry.

Admission timeouts, conflicts, server errors and malformed successful responses
may reflect a committed write. These are deliberately more conservative than the
TypeScript admission retry behavior. There is no automatic mutation retry, and
LiveKit API failover is disabled. No uncertain admission is duplicated.

End-call delegates goodbye and shutdown ordering to LiveKit's built-in tool.
RoomIO remains the only room-deletion owner. Agent `on_exit` closes the existing
patient resolver, invalidating its token and canceling reads, including a backend
that returns late after cancellation. The job shutdown callback remains as cleanup
for startup failures.

## Evidence and dependencies

Offline tests run actual registered tools through `AgentSession` with deterministic
model, speech, HTTP, SIP, and job substitutes. Coverage includes announcement
ordering/failure, exact participant selection, absent caller, sandbox/console,
provider outcomes, duplicate suppression, bounded retries, patient changes,
partial admission writes, cancellation, disconnect, goodbye-before-close, and
late patient-read suppression at session close.

Source contracts inspected: abita_agent `tools/transfer-call.ts`, `tools/handoff.ts`,
`state/call-lifecycle.ts`, `runtime/tool-registry.ts`, office profiles and transfer
behavior tests; Product `httpapi/server.go` and `humancalling/handoff.go`. Call
control does not invoke Abita middleware. The installed LiveKit Agents 1.8.1/API
1.2.1 source defines the end-call lifecycle and structured SIP transfer result.

Based on main d651920 (merged PR #2). No sibling PR dependency. Other migrations
must preserve the additive `call_control` constructor argument, toolset registration,
and resolver cleanup when combining changes in agent/startup. There is no live
backend, GPT-Live audio, SIP/provider, human-answer, or deployment proof here.
