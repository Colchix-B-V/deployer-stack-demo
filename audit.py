"""Demo 2: an append-only, hash-chained audit log.

Every record carries the hash of the record before it. Change one byte in an
old record and verification fails from that record on.

    python audit.py show 3          # print one record
    python audit.py verify          # check every hash and every link
    python audit.py tamper 3        # quietly "fix" record 3 (for the demo)
    python audit.py tamper 3 --rehash   # a smarter edit: recompute its hash too
    python audit.py tamper 3 --rewrite  # the full cover-up: rebuild the chain
    python audit.py head            # the latest hash: anchor it somewhere else
    python audit.py verify --anchor sha256:...   # is the anchored hash still there?
    python audit.py reviewers       # oversight per reviewer (slide 13)

Content (questions, answers) never goes in the log itself: only its hash. The
text sits in a separate store (data/content/) with its own access rules and
retention, so you can delete it under GDPR without breaking the chain.
"""

import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

DATA = Path(__file__).parent / "data"
LOG = DATA / "audit.jsonl"
CONTENT = DATA / "content"
GENESIS = "sha256:" + "0" * 64


def sha256(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(record):
    """One exact byte form per record, so the hash is reproducible."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def store_content(text):
    """Keep the text apart from the log; the log only gets the hash."""
    digest = sha256(text)
    CONTENT.mkdir(parents=True, exist_ok=True)
    (CONTENT / (digest.removeprefix("sha256:") + ".txt")).write_text(text, encoding="utf-8")
    return digest


def read_content(digest):
    hexpart = (digest or "").removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", hexpart):  # never build a path from anything else
        return None
    path = CONTENT / (hexpart + ".txt")
    return path.read_text(encoding="utf-8") if path.exists() else None


def records():
    if not LOG.exists():
        return []
    lines = LOG.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def append(record):
    existing = records()
    record = dict(record)
    record["seq"] = len(existing) + 1
    record["prev"] = existing[-1]["hash"] if existing else GENESIS
    record["hash"] = sha256(canonical(record))
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def verify():
    prev = GENESIS
    rows = records()
    for r in rows:
        n = r["seq"]
        if r["prev"] != prev:
            print(f"record {n:>3}  BROKEN LINK  expects prev {r['prev'][7:19]}...,"
                  f" but record {n - 1} hashes to {prev[7:19]}...")
            print(f"\nFAILED: chain broken at record {n}.")
            return False
        if sha256(canonical(r)) != r["hash"]:
            print(f"record {n:>3}  CHANGED      stored hash {r['hash'][7:19]}...,"
                  f" content now hashes to {sha256(canonical(r))[7:19]}...")
            print(f"\nFAILED: chain broken at record {n}.")
            return False
        print(f"record {n:>3}  ok  {r['hash'][7:19]}...")
        prev = r["hash"]
    print(f"\nOK: {len(rows)} records, chain intact. Head: {prev[7:23]}...")
    return True


def tamper(n, rehash=False, rewrite=False):
    rows = records()
    r = rows[n - 1]
    old = r.get("decision")
    r["decision"] = "approved" if old != "approved" else "rejected"
    if rehash or rewrite:
        r["hash"] = sha256(canonical(r))
    if rewrite:  # the full cover-up: re-link and re-hash every later record
        for prev, later in zip(rows[n - 1:], rows[n:]):
            later["prev"] = prev["hash"]
            later["hash"] = sha256(canonical(later))
    LOG.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                   encoding="utf-8")
    how = {(False, False): "", (True, False): " and recomputed its hash"}.get(
        (rehash, rewrite), f" and rewrote records {n} to {len(rows)}")
    print(f'Record {n}: decision "{old}" -> "{r["decision"]}"{how}.')


def check_anchor(anchor):
    """A hash you copied elsewhere earlier must still be in the chain."""
    if any(r["hash"] == anchor for r in records()):
        print(f"Anchor {anchor[7:19]}... found: history up to it is unchanged.")
        return True
    print(f"FAILED: anchor {anchor[7:19]}... is no longer in the log."
          " History was rewritten.")
    return False


def show(n):
    r = records()[n - 1]
    print(json.dumps(r, indent=2, ensure_ascii=False))


def reviewers():
    by = {}
    for r in records():
        if r.get("reviewer"):
            by.setdefault(r["reviewer"], []).append(r)
    print(f"{'reviewer':<16}{'reviews':>8}{'approved':>10}{'overrode':>10}{'median time':>13}")
    for name, rows in by.items():
        approved = sum(r["decision"] == "approved" for r in rows)
        times = [r["review_seconds"] for r in rows if r.get("review_seconds") is not None]
        median = f"{statistics.median(times):.0f} s" if times else "-"
        print(f"{name:<16}{len(rows):>8}{approved:>10}{len(rows) - approved:>10}{median:>13}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "verify":
        ok = verify()
        if ok and "--anchor" in sys.argv:
            ok = check_anchor(sys.argv[sys.argv.index("--anchor") + 1])
        sys.exit(0 if ok else 1)
    elif cmd == "tamper":
        tamper(int(sys.argv[2]), rehash="--rehash" in sys.argv,
               rewrite="--rewrite" in sys.argv)
    elif cmd == "show":
        show(int(sys.argv[2]))
    elif cmd == "head":
        print(records()[-1]["hash"])
    elif cmd == "reviewers":
        reviewers()
    else:
        print(__doc__)
