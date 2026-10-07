"""Build the local call-review page for one batch.

Reads the batch's call IDs, code checks and Jev answers from the committed batch file,
pulls each call's transcript from the portal database inside a read-only transaction,
and writes a self-contained HTML page to evals/call_review/out/ (gitignored).

The built page embeds patient transcripts: never commit, upload, or post it.

Usage:
    python evals/call_review/build_review_page.py batch_2026_10_06.json
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).parent
PROJECT = "acuity-health-prod"
INSTANCE = "acuity-health-prod:us-east1:acuity-production"
SECRET = "acuity-product-east1-portal-database-url"
THRESHOLD = 0.4

# Scorecard question -> (Jev version, Jev question) shown after "Reveal Jev".
JEV_SOURCE = {
    "B1": ("v1", "booking_requested"),
    "B5": ("v1", "time_offered"),
    "H1": ("v2", "need_understood"),
    "H2": ("v1", "right_help"),
    "H4": ("v2", "clear_and_responsive"),
    "A1": ("production", "office_rules_grounded"),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch_transcripts(call_ids: list[str]) -> dict[str, list]:
    """Return chat_history items per source_call_id, read-only, credentials kept in memory."""
    url = urlparse(
        subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={SECRET}",
                f"--project={PROJECT}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    port = free_port()
    proxy = subprocess.Popen(
        ["cloud-sql-proxy", "--gcloud-auth", "--port", str(port), INSTANCE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.5)
        ids = ",".join("'" + i.replace("'", "") + "'" for i in call_ids)
        sql = (
            "BEGIN TRANSACTION READ ONLY;\n"
            "SELECT coalesce(json_object_agg(source_call_id, transcript->'chat_history'->'items'), '{}') "
            f"FROM public.ai_interactions WHERE source_call_id IN ({ids});\n"
            "ROLLBACK;\n"
        )
        env = {
            **os.environ,
            "PGHOST": "127.0.0.1",
            "PGPORT": str(port),
            "PGSSLMODE": "disable",
            "PGUSER": unquote(url.username or ""),
            "PGPASSWORD": unquote(url.password or ""),
            "PGDATABASE": url.path.lstrip("/"),
            "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=60000",
        }
        out = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At"],
            input=sql,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return json.loads(out)
    finally:
        proxy.terminate()


def text(content) -> str:
    if isinstance(content, list):
        return " ".join(c for c in content if isinstance(c, str)).strip()
    return str(content or "").strip()


def events(items: list) -> list[dict]:
    """Caller and agent turns plus tool calls with their results, in seconds from the first turn."""
    out, start = [], None
    for item in items:
        ts = float(item.get("created_at") or 0)
        if item["type"] == "message" and item.get("role") in ("user", "assistant"):
            start = start or ts
            out.append(
                {
                    "k": "caller" if item["role"] == "user" else "agent",
                    "t": ts,
                    "text": text(item.get("content")),
                    "int": bool(item.get("interrupted")),
                }
            )
        elif item["type"] == "function_call":
            out.append(
                {
                    "k": "tool",
                    "t": ts,
                    "name": item["name"],
                    "args": item.get("arguments") or "",
                    "id": item.get("call_id"),
                }
            )
        elif item["type"] == "function_call_output":
            for e in reversed(out):
                if e["k"] == "tool" and e.get("id") == item.get("call_id"):
                    e["out"] = item.get("output") or ""
                    break
    start = start or (out[0]["t"] if out else 0)
    for e in out:
        e["t"] = round(e["t"] - start, 1)
        e.pop("id", None)
    return sorted(out, key=lambda e: e["t"])


def yes_no(p) -> str:
    if isinstance(p, (int, float)):
        return f"{'yes' if p > THRESHOLD else 'NO'} ({p:.2f})"
    return p or ""


def main() -> None:
    batch_path = HERE / (sys.argv[1] if len(sys.argv) > 1 else "batch_2026_10_06.json")
    batch = json.loads(batch_path.read_text())
    transcripts = fetch_transcripts([c["source_call_id"] for c in batch["calls"]])
    missing = [
        c["source_call_id"]
        for c in batch["calls"]
        if not transcripts.get(c["source_call_id"])
    ]
    if missing:
        sys.exit(f"No transcript for: {', '.join(missing)}")
    calls = []
    for c in batch["calls"]:
        checks = c["code_checks"]
        prod = c["jev"]["production"]
        calls.append(
            {
                "n": c["n"],
                "room": c["room"],
                "scl": c["source_call_id"],
                "office": c["office"],
                "time": c["time_et"],
                "secs": c["secs"],
                "status": c["sample"],
                "d": {
                    "Availability searched": checks["availability_searched"],
                    "Booking tool called": checks["booking_tool_called"],
                    "Staff task created": checks["staff_task_created"],
                    "Transferred": checks["transferred"],
                    "Callback within 1h": checks["callback_within_1h"],
                    "Insurance verified": checks["insurance_verified"],
                },
                "jev": {
                    **{
                        q: yes_no(c["jev"][v][name])
                        for q, (v, name) in JEV_SOURCE.items()
                    },
                    "prod_responsive": yes_no(prod["conversation_responsive"]),
                    "prod_appt": yes_no(prod["appointment_datetime_correct"]),
                    "sentiment": f"{prod['expressed_sentiment']:.2f}"
                    if isinstance(prod["expressed_sentiment"], (int, float))
                    else (prod["expressed_sentiment"] or ""),
                },
                "events": events(transcripts[c["source_call_id"]]),
            }
        )
    label = date.fromisoformat(batch["date"]).strftime("%b %-d")
    page = (
        (HERE / "template.html")
        .read_text()
        .replace(
            "__DATA__", json.dumps(calls, ensure_ascii=False).replace("</", "<\\/")
        )
        .replace("__BATCH_DATE__", json.dumps(batch["date"]))
        .replace("__BATCH_LABEL__", label)
    )
    out = HERE / "out" / f"call-review-{batch['date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page)
    print(
        f"Wrote {out.relative_to(HERE.parent.parent)} ({len(calls)} calls). Open it in a browser; don't share it."
    )


if __name__ == "__main__":
    main()
