"""Demo 3: watch for drift as records arrive.

Three signals, all from data you already have:
  1. Model identity: did response.model or the fingerprint change?
  2. Performance: are answers suddenly longer or shorter than usual?
  3. Meaning: do answers to the same question still say the same thing?
     Each answer is embedded and compared with the baseline answers to the
     same question (matched by the question's hash in the audit log).

    python drift.py baseline      # learn "normal" from the records so far
    python drift.py watch         # follow the audit log live
    python drift.py watch --from 14   # replay from record 14, then keep watching
"""

import json
import math
import os
import statistics
import sys
import time
from collections import deque
from pathlib import Path

from openai import OpenAI

import audit

BASELINE = Path(__file__).parent / "data" / "baseline.json"
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "not-needed-for-the-local-gateway"),
)

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def embed(text):
    return client.embeddings.create(model=EMBED_MODEL, input=text).data[0].embedding


def mean_vector(vectors):
    return [sum(xs) / len(xs) for xs in zip(*vectors)]


def distance(a, b):
    """Cosine distance: 0 = same meaning, higher = further apart."""
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 1 - dot / norm


def baseline():
    groups, tokens, fingerprints = {}, [], set()
    for r in audit.records():
        text = audit.read_content(r["output"])
        if text is None:
            continue
        groups.setdefault(r["input"], []).append(embed(text))
        tokens.append(r["tokens"]["out"])
        fingerprints.add(r["fingerprint"])
    centres = {q: mean_vector(vs) for q, vs in groups.items()}
    noise = [distance(v, centres[q]) for q, vs in groups.items() for v in vs]
    # Alert when answers sit clearly further from normal than normal noise.
    threshold = max(0.05, statistics.mean(noise) + 3 * statistics.pstdev(noise))
    BASELINE.write_text(json.dumps({
        "centres": centres,
        "threshold": round(threshold, 4),
        "tokens_mean": statistics.mean(tokens),
        "fingerprints": sorted(fingerprints),
    }), encoding="utf-8")
    print(f"Baseline from {len(noise)} answers to {len(groups)} questions.")
    print(f"  fingerprints: {', '.join(sorted(fingerprints))}")
    print(f"  answer length: {statistics.mean(tokens):.0f} tokens on average")
    print(f"  semantic drift threshold: {threshold:.3f}")


def bar(value, threshold, width=24):
    filled = min(width, round(value / (threshold * 2) * width))
    colour = RED if value > threshold else GREEN
    return colour + "█" * filled + RESET + DIM + "·" * (width - filled) + RESET


def watch(start=None):
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    threshold, known = base["threshold"], set(base["fingerprints"])
    recent = deque(maxlen=3)  # rolling window: the last three answers
    alerted = set()
    # By default only new records; --from N replays from record N on.
    seen = len(audit.records()) if start is None else start - 1
    print(f"Watching {audit.LOG.name} (threshold {threshold:.3f}). Ctrl+C to stop.\n")
    print(f"{DIM}{'#':>4}  {'model':<15}{'fingerprint':<15}{'tokens':>6}  "
          f"{'meaning vs baseline':<30}{RESET}")
    while True:
        rows = audit.records()
        for r in rows[seen:]:
            fp, model = r["fingerprint"], r["model"]
            centre = base["centres"].get(r["input"])
            text = audit.read_content(r["output"])
            d = max(0.0, distance(embed(text), centre)) if centre and text else None
            if d is not None:
                recent.append(d)
            shown = f"{bar(d, threshold)} {d:.3f}" if d is not None else "new question"
            fp_col = (RED if fp not in known else "") + f"{fp[:13]:<15}" + RESET
            print(f"{r['seq']:>4}  {model:<15}{fp_col}{r['tokens']['out']:>6}  {shown}")

            if fp not in known and fp not in alerted:
                alerted.add(fp)
                print(f"\n  {RED}{BOLD}ALERT  model changed:{RESET} asked for "
                      f"'{r['model_requested']}', got '{model}' ({fp}).")
                print(f"         Nothing in the app changed. Owner: decide, and log it.\n")
            rolling = statistics.mean(recent) if recent else 0
            if len(recent) >= 3 and rolling > threshold and "semantic" not in alerted:
                alerted.add("semantic")
                print(f"\n  {RED}{BOLD}ALERT  semantic drift:{RESET} answers moved "
                      f"{rolling:.3f} from baseline (threshold {threshold:.3f}).\n")
            ratio = r["tokens"]["out"] / base["tokens_mean"]
            if (ratio > 1.5 or ratio < 0.5) and "length" not in alerted:
                alerted.add("length")
                print(f"\n  {RED}{BOLD}ALERT  answer length:{RESET} {r['tokens']['out']} tokens,"
                      f" usual is {base['tokens_mean']:.0f}.\n")
        seen = len(rows)
        time.sleep(0.5)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # turn on colour codes in the Windows console
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    try:
        start = int(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else None
        baseline() if cmd == "baseline" else watch(start)
    except KeyboardInterrupt:
        pass
