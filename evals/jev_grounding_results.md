# Jev office-grounding comparison

Live replay on September 27, 2026, using `typesafe-ai/jev` through the existing
TypeSafe Gateway endpoint. Only `office_rules_grounded` instructions and criteria
changed. Both variants used the same recorded call or synthetic conversation,
the same production serialization, and the same evaluator request code.

The production case is interaction `9ab490fa-3a25-4b3d-a116-418921d9aae6`:
the agent repeatedly claimed the office was open until five before looking up
office information. Its stored original grounding score was 0.68. The private
call report is retained outside the repository and is not a committed fixture.

## Scores

Higher values mean stronger estimated grounding. These are raw `noul` values,
not binary pass/fail labels or calibrated probabilities.

| Case | Repeats per variant | Original | Revised |
| --- | --- | --- | --- |
| Production unsupported hours | 3 | 0.64–0.65 | 0.14–0.16 |
| Invented hours corrected after lookup | 2 | 0.07 | 0.10 |
| Correct hours retrieved before answering | 2 | 0.94 | 0.92–0.93 |
| Caller suggests hours; assistant agrees without evidence | 2 | 0.38–0.39 | 0.17–0.18 |
| Assistant honestly says hours are unknown | 2 | 0.88 | 0.94 |
| Invented provider and specialty without evidence | 2 | 0.81 | 0.20 |

All 26 requests returned valid scores without evaluator errors. The revised
wording explicitly includes office facts such as hours and providers, requires
evidence available before each claim, and treats any unsupported claim as a
violation even when later answers are grounded. No transport, retry, model,
response-schema, or UI-threshold changes were made.

The focused sample supports the change for these failure cases, not a general
claim of reliable hallucination detection. The tests do not isolate whether the
explicit factual categories or the stronger temporal wording caused the change.
Past production evaluations were not overwritten. Eight existing evaluator tests,
Ruff checks, and `git diff --check` passed; those checks validate integration,
not model judgment accuracy. These replays did not deploy the change.
