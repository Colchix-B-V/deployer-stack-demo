# The deployer stack: OpenTelemetry, audit & drift

A small, runnable demo of the records an organisation needs when it
*deploys* AI:

- **Trace:** what happened in one request?
- **Audit:** can we prove it, months later?
- **Drift:** is it still the system we approved?

It accompanies the talk "The Deployer Stack: OpenTelemetry, Audit & Drift"
at ARIC's Brown Bag Session on 19 November 2026.

Everything runs on your laptop. You need no API key and no account.

## What's inside

The demo is a support team's reply drafter for a made-up shop. An AI drafts
an answer from the policy documents, and a human agent approves, edits or
rejects it.

| File | Teaches |
|---|---|
| `telemetry.py` | The ten lines that turn every model call into an OpenTelemetry span |
| `app.py` | One traced request: retrieval, model call, human review, audit record |
| `audit.py` | An append-only, hash-chained audit log: verify it, tamper with it, anchor it |
| `drift.py` | Live drift checks: model identity, answer length, meaning against a baseline |
| `gateway.py` | Plays the hosted provider: serves the alias `assistant` and can swap the model behind it |
| `docker-compose.yml` | The OpenTelemetry Collector and Jaeger, for viewing traces |

```
app.py ──spans──▶ OTel Collector ──▶ Jaeger          (traces: what happened?)
   │
   └──records──▶ data/audit.jsonl, hash-chained      (audit: can we prove it?)
                       │
                       └──▶ drift.py, live           (drift: is it still true?)
```

## Setup

You need Python 3.10+, Docker and [Ollama](https://ollama.com).

```bash
ollama pull llama3.2:3b
ollama pull qwen2.5:1.5b
ollama pull nomic-embed-text

python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip

docker compose up -d          # Collector + Jaeger UI on http://localhost:16686
python gateway.py             # keep this running in its own terminal
```

## 1. Trace: one request, end to end

```bash
python app.py "Can I still return my tent? It arrived 20 days ago."
```

Read the draft, then approve, edit or reject it. The command prints a link to
the trace in Jaeger. Open the `chat assistant` span and look at:

- `gen_ai.request.model`: what you asked for (`assistant`)
- `gen_ai.response.model`: what actually answered
- `openai.response.system_fingerprint`: which exact weights answered
- `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`, and
  `gen_ai.response.finish_reasons`

The prompt and the answer are **not** in the trace. Content capture is off
by default (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`), and that
is a good default: prompts carry personal data.

`gen_ai.provider.name` says `openai` because the app uses the OpenAI client;
the gateway speaks the same API.

## 2. Audit: a log nobody can quietly edit

```bash
python audit.py show 1            # one record: hashes, model, reviewer, trace id
python audit.py verify            # every hash, every link
python audit.py tamper 3          # change one decision in record 3
python audit.py verify            # FAILED: chain broken at record 3
```

Each record stores the hash of the record before it. A record holds hashes
of the question, the answer, the prompt template and every retrieved
chunk. The text itself sits in `data/content/`, so it can have its own
access rules and be deleted under GDPR without breaking the chain.

What if someone rewrites the whole chain after the edit? That's what
anchoring is for:

```bash
python audit.py head              # copy this hash somewhere you don't control
python audit.py tamper 3 --rewrite
python audit.py verify --anchor sha256:...   # FAILED: history was rewritten
```

`python audit.py reviewers` shows human oversight per reviewer. A reviewer
with hundreds of approvals, no changes and three seconds each isn't
overseeing anything.

## 3. Drift: catch the change you didn't make

```bash
python app.py --loop --rounds 2   # some normal traffic
python drift.py baseline          # learn "normal" from it
python drift.py watch             # terminal 1: watch live
python app.py --loop              # terminal 2: steady traffic
python gateway.py swap qwen2.5:1.5b   # terminal 3: the provider swaps models
```

Your app changed nothing, yet `drift.py` flags it:

- **Model changed:** `response.model` and the fingerprint no longer match
  the baseline.
- **Semantic drift:** each answer is embedded and compared with the
  baseline answers to the same question. Questions are matched by their
  hash in the audit log.
- **Answer length:** answers became much longer or shorter than usual.

At `temperature=0` repeat answers are identical, so the threshold falls back
to a floor of 0.05. At higher temperatures the baseline measures the real
noise and sets the threshold from it.

Swap back with `python gateway.py swap llama3.2:3b`.

`python drift.py watch --from 14` replays the log from record 14 and then
keeps watching, so you can review a run after the fact.

## Using a hosted provider instead

Copy `.env.example` to `.env` and fill it in, or set the variables directly:
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `MODEL` (e.g. `gpt-4o-mini`) and
`EMBED_MODEL`. Ask for an alias and look at `response.model`: you'll get
the dated version that actually answered.

## Notes

- **This is a teaching demo, not a product.** A real audit log needs
  write-once storage, access control and a retention policy. A real anchor
  is an external timestamp service (RFC 3161), object-locked storage or a
  third party.
- **Ollama on a small machine:** the gateway asks for a 4k context. Without
  that, some models reserve memory for their full 128k window.
- **The GenAI semantic conventions are still marked "Development".**
  Versions are pinned in `requirements.txt`.

Made by [Colchix](https://colchix.com). MIT licence.
