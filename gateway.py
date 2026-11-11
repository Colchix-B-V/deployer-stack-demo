"""A tiny model gateway that plays the role of a hosted AI provider.

Your app asks for a model by an alias ("assistant"), the way you'd ask a
provider for "gpt-4o". The gateway decides which real model answers, and
reports back:
  - response.model:      the model that actually answered
  - system_fingerprint:  a hash of that model's weights (its Ollama digest)

That lets you see, on your own laptop, what a provider can do behind an
alias, and how to catch it.

    python gateway.py                    # serve on http://127.0.0.1:8000/v1
    python gateway.py swap qwen2.5:3b    # change what "assistant" points to

It speaks just enough of the OpenAI API for this demo (chat completions and
embeddings) and forwards to Ollama's native API.
"""

import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "gateway.json"
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
PORT = int(os.getenv("GATEWAY_PORT", "8000"))
CONTEXT = int(os.getenv("GATEWAY_CONTEXT", "4096"))
if not OLLAMA.startswith(("http://", "https://")):  # urlopen would also take file://
    sys.exit("OLLAMA_URL must be an http(s) URL")


def load_aliases():
    # Read on every request, so a swap takes effect without a restart:
    # exactly the "silent" change this demo is about.
    return json.loads(CONFIG.read_text(encoding="utf-8"))["aliases"]


def ollama(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def fingerprint(model):
    """A short hash of the model's weights, as Ollama stores them."""
    for m in ollama("/api/tags")["models"]:
        if m["name"] == model:
            return "fp_" + m["digest"][:10]
    return "fp_unknown"


def chat(body):
    asked = body["model"]
    model = load_aliases().get(asked, asked)
    options = {"num_ctx": CONTEXT}
    if body.get("temperature") is not None:
        options["temperature"] = body["temperature"]
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    if limit:
        options["num_predict"] = limit
    out = ollama(
        "/api/chat",
        {
            "model": model,
            "messages": body["messages"],
            "stream": False,
            "options": options,
        },
    )
    print(f"  {asked} -> {model}", flush=True)
    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": fingerprint(model),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": out["message"]["content"],
                },
                "finish_reason": out.get("done_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": out.get("prompt_eval_count", 0),
            "completion_tokens": out.get("eval_count", 0),
            "total_tokens": out.get("prompt_eval_count", 0)
            + out.get("eval_count", 0),
        },
    }


def embeddings(body):
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    out = ollama(
        "/api/embed",
        {"model": body["model"], "input": texts, "options": {"num_ctx": CONTEXT}},
    )
    return {
        "object": "list",
        "model": body["model"],
        "data": [
            {"object": "embedding", "index": i, "embedding": e}
            for i, e in enumerate(out["embeddings"])
        ],
        "usage": {"prompt_tokens": out.get("prompt_eval_count", 0), "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    routes = {"/v1/chat/completions": chat, "/v1/embeddings": embeddings}

    def do_POST(self):
        route = self.routes.get(self.path)
        if route is None:
            return self.reply(404, {"error": {"message": f"no route {self.path}"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.reply(200, route(body))
        except Exception as exc:  # report, don't crash the gateway
            self.reply(502, {"error": {"message": str(exc), "type": "gateway_error"}})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the console for the alias lines
        pass


def swap(model):
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    old = config["aliases"]["assistant"]
    config["aliases"]["assistant"] = model
    CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f'"assistant" now points to {model} (was {old}). Your app changed nothing.')


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "swap":
        swap(sys.argv[2])
    else:
        aliases = load_aliases()
        print(f"Gateway on http://127.0.0.1:{PORT}/v1  ->  Ollama at {OLLAMA}")
        for alias, model in aliases.items():
            print(f'  "{alias}" -> {model}')
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
