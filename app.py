"""Demo 1: a customer-support reply drafter, traced end to end.

The AI drafts a reply from the shop's policy documents; a human agent
approves, edits or rejects it. Each request produces:
  - a trace (retrieval -> model call -> human review), sent to the Collector
  - one audit record, appended to the hash-chained log (audit.py)

    python app.py "Can I return my tent after 20 days?"
    python app.py --loop                # steady traffic for the drift demo
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Use the latest GenAI semantic conventions (gen_ai.provider.name and friends).
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")

from openai import OpenAI  # noqa: E402
from opentelemetry import trace  # noqa: E402

import audit  # noqa: E402
import telemetry  # noqa: E402

HERE = Path(__file__).parent
MODEL = os.getenv("MODEL", "assistant")
SYSTEM = "support-reply-drafter"
PURPOSE = "Draft replies to customer questions; a human agent sends them"

provider = telemetry.setup(SYSTEM)
tracer = trace.get_tracer(SYSTEM)
client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "not-needed-for-the-local-gateway"),
)
TEMPLATE = (HERE / "prompt.txt").read_text(encoding="utf-8")


def load_chunks():
    """Split each policy file into sections: one chunk per '## ' heading."""
    chunks = []
    for path in sorted((HERE / "docs").glob("*.md")):
        for i, part in enumerate(path.read_text(encoding="utf-8").split("\n## ")[1:], 1):
            title, _, body = part.partition("\n")
            chunks.append({"id": f"{path.stem}#{i}", "title": title.strip(), "text": body.strip()})
    return chunks


CHUNKS = load_chunks()


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}


def retrieve(question, k=3):
    with tracer.start_as_current_span("retrieval") as span:
        q = words(question)
        ranked = sorted(CHUNKS, key=lambda c: -len(q & words(c["title"] + " " + c["text"])))
        found = ranked[:k]
        span.set_attribute("gen_ai.operation.name", "retrieval")
        span.set_attribute("gen_ai.retrieval.top_k", k)
        span.set_attribute("app.retrieval.document_ids", [c["id"] for c in found])
        return found


def review(draft, reviewer, simulated):
    with tracer.start_as_current_span("human_review") as span:
        started = time.monotonic()
        if simulated:
            # Load-generator traffic: a simulated agent who approves everything
            # within a few seconds. This is the rubber stamp slide 13 warns about.
            decision, final, seconds = "approved", draft, round(random.uniform(2, 4), 1)
        else:
            print("\n" + draft + "\n")
            answer = input("Send this reply? [a]pprove  [e]dit  [r]eject: ").strip().lower()
            final = draft
            if answer.startswith("e"):
                final = input("Your version: ").strip() or draft
                decision = "edited"
            elif answer.startswith("r"):
                decision, final = "rejected", None
            else:
                decision = "approved"
            seconds = round(time.monotonic() - started, 1)
        span.set_attribute("app.review.reviewer", reviewer)
        span.set_attribute("app.review.decision", decision)
        span.set_attribute("app.review.seconds", seconds)
        return decision, final, seconds


def draft_reply(question, reviewer, simulated=False):
    with tracer.start_as_current_span("draft_reply") as root:
        root.set_attribute("app.system", SYSTEM)
        found = retrieve(question)
        context = "\n\n".join(f"[{c['id']}] {c['title']}\n{c['text']}" for c in found)
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            max_tokens=150,
            messages=[
                {"role": "system", "content": TEMPLATE.format(context=context)},
                {"role": "user", "content": question},
            ],
        )
        choice = response.choices[0]
        draft = choice.message.content.strip()
        decision, final, seconds = review(draft, reviewer, simulated)

        record = audit.append({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "system": SYSTEM,
            "purpose": PURPOSE,
            "model_requested": MODEL,
            "model": response.model,
            "fingerprint": response.system_fingerprint,
            "prompt_template": audit.sha256(TEMPLATE),
            "context": [f"{c['id']} {audit.sha256(c['text'])}" for c in found],
            "input": audit.store_content(question),
            "output": audit.store_content(draft),
            "sent": audit.store_content(final) if final else None,
            "finish_reason": choice.finish_reason,
            "tokens": {"in": response.usage.prompt_tokens, "out": response.usage.completion_tokens},
            "reviewer": reviewer,
            "decision": decision,
            "review_seconds": seconds,
            "trace_id": format(root.get_span_context().trace_id, "032x"),
        })
        return draft, record


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("question", nargs="?")
    parser.add_argument("--loop", action="store_true", help="send the questions in questions.json")
    parser.add_argument("--rounds", type=int, default=0, help="stop after N rounds (0 = forever)")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args()
    reviewer = os.getenv("REVIEWER", "agent.weber")

    try:
        if args.loop:
            questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
            n = 0
            while not args.rounds or n < args.rounds:
                for q in questions:
                    draft, rec = draft_reply(q, "simulated.agent", simulated=True)
                    print(f"#{rec['seq']:<4} {rec['model']:<14} {rec['tokens']['out']:>4} tokens  {q[:50]}")
                    time.sleep(args.pause)
                n += 1
        elif args.question:
            draft, rec = draft_reply(args.question, reviewer)
            print(f"\nAudit record #{rec['seq']} written ({rec['decision']}).")
            print(f"Trace: http://localhost:16686/trace/{rec['trace_id']}")
        else:
            parser.print_help()
    except KeyboardInterrupt:
        pass
    finally:
        provider.force_flush()
        provider.shutdown()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
