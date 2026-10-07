# Call review

Blind human review of real calls against the scorecard (ACU-93, ACU-99). Reviewers answer the same yes/no questions the judges answer; their answers become the golden set the judges are tested against.

## Files

- `batch_2026_10_06.json`: the 20 calls in a batch, with code-check results and Jev answers (v1, v2 and production). Call IDs only, no transcripts.
- `scorecard_questions.py`: the judge questions in Jev's format. `QUESTIONS` is v1; `QUESTIONS_V2` holds the revised `need_understood` and `clear_and_responsive`.
- `template.html`: the review page.
- `build_review_page.py`: pulls each call's transcript from the portal database (read-only transaction through a local Cloud SQL proxy) and writes the page to `out/`.

## Review a batch

Needs `gcloud` (logged in with access to `acuity-health-prod`), `cloud-sql-proxy` and `psql`.

```bash
python evals/call_review/build_review_page.py batch_2026_10_06.json
open evals/call_review/out/call-review-2026-10-06.html
```

1. Enter your name at the top of the scorecard.
2. For each call, answer yes/no before clicking **Reveal Jev**. Add a one-line note for every no.
3. Click **Export CSV** and send the file to Kyle.

Answers save in your browser as you go. Don't look at anyone else's answers first.

**The built page contains patient transcripts.** `out/` is gitignored. Never commit, upload, or post the page.
