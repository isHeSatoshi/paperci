# Run PaperCI on your own machine, with your own LLM

The public server ([paperci.vercel.app](https://paperci.vercel.app)) is one 2-CPU cloud box, so runs queue up
when many people try at once. You can run the same agent locally: the same sandbox, egress allowlist, sealed
claims, independent verifier and dashboard. Only the LLM changes, and you pick it.

## What you need

- **Docker** with Linux containers (Docker Desktop on Windows/macOS, Docker Engine on Linux).
- **Python 3.11+**.
- **An LLM server with an OpenAI-compatible API**: Ollama, LM Studio, vLLM, a llama.cpp server, or a local
  LiteLLM proxy (which can also front your own API keys). Hosted options still work too (see the end).
- Disk and RAM: the sandbox image is about 2.5 GB, and each paper build gets 4 CPUs and 6 GB by default
  (`PAPERCI_SANDBOX_CPUS`, `PAPERCI_SANDBOX_MEM`). Leave room for the model if it runs on the same machine.

## Quick start

```bash
git clone https://github.com/isHeSatoshi/paperci && cd paperci
bash scripts/run-local.sh                                         # macOS / Linux
powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1    # Windows
```

The script:

1. creates `.venv` and installs `requirements.txt`;
2. copies [`deploy/local/local.env.example`](../deploy/local/local.env.example) to `.env` if you have none
   (Ollama preset; edit it to choose your models);
3. builds the two sandbox images (`paperci/proxy`, `paperci/sandbox:base`) the first time;
4. runs the preflight `python -m paperci.localrun check`;
5. starts the agent and dashboard at **http://127.0.0.1:8000**.

Paste an arXiv link (or upload a paper) exactly as on the public site. Use `-Check` / `--check` to stop
after step 4.

## Pick your LLM

Every role takes a model name prefixed with `local:`, sent to `PAPERCI_LOCAL_BASE_URL`.
`PAPERCI_MODEL_ALL` sets every role at once, and `PAPERCI_MODEL_<ROLE>` overrides one role
(`BUILDER`, `VERIFIER`, `CLAIMS`, `TRIAGE`, `SOLVER`, `CONCIERGE`).

| Server | `PAPERCI_LOCAL_BASE_URL` | Start it with | Model names |
| --- | --- | --- | --- |
| Ollama | `http://localhost:11434/v1` | `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`, then `ollama pull qwen2.5-coder:14b` | `local:qwen2.5-coder:14b` |
| LM Studio | `http://localhost:1234/v1` | `lms server start` and `lms load <model> -c 32768` | `local:<id from lms ls>` |
| vLLM | `http://localhost:8001/v1` | `vllm serve <model> --port 8001 --max-model-len 32768 --enable-auto-tool-choice --tool-call-parser hermes` | `local:<model>` |
| llama.cpp | `http://localhost:8080/v1` | `llama-server -m model.gguf -c 32768 --jinja` | `local:<anything>` |
| LiteLLM proxy | `http://localhost:4000/v1` | `pip install "litellm[proxy]"`, then `litellm --config deploy/local/litellm.yaml --port 4000` | `local:paperci-builder`, `local:paperci-verifier` |

vLLM listens on port 8000 by default, which is PaperCI's port. Move one of them (`--port 8001` above, or
`PAPERCI_WEB_PORT`).

**Local LiteLLM proxy.** [`deploy/local/litellm.yaml`](../deploy/local/litellm.yaml) defines two aliases, a
builder and a verifier from different model families. Point each alias at whatever you like: Ollama, vLLM,
or a hosted provider using your own key in your environment. Then set `PAPERCI_LOCAL_API_KEY` to the proxy's
`master_key`. If an alias goes to a paid API, set `PAPERCI_LOCAL_PRICE_IN` / `PAPERCI_LOCAL_PRICE_OUT`
(USD per million tokens) so PaperCI's per-paper dollar caps apply. Local models are free by default, and runs
are bounded by turn and time caps instead.

### Model advice

- **Context: 32k tokens.** Paper prompts run 10 to 20k tokens. LM Studio's default 8k rejects them, and
  Ollama's default silently drops the start of the prompt. The preflight sends a 16k-token probe and fails
  loudly when the window is too small.
- **Size: 7B or larger, instruction-tuned, with tool calling.** For example Qwen2.5-Coder 14B as the builder
  and Llama 3.1 8B as the verifier. A 1.5B model runs the pipeline, but its claim extraction rarely survives
  PaperCI's grounding check (every claim must quote the paper verbatim), so it stops before the build.
- **Use a different model for the verifier.** Independence is the point of the verifier. The preflight warns
  when the builder and verifier are the same model.
- **Tool calls.** The builder works the sandbox through tool calls. If your server ignores the `tools` field,
  PaperCI switches to its JSON tool protocol on its own (or force it: `PAPERCI_LOCAL_TOOLS=json`). The
  preflight shows which one your model uses.
- **Time.** Local models are slower than hosted ones. The preset raises `PAPERCI_LOCAL_TIMEOUT` (per call)
  and `PAPERCI_PAPER_WALL` (per paper).

## The preflight

```
python -m paperci.localrun check [--build]
```

It checks:

- Docker is running Linux containers, and both sandbox images exist (`--build` builds them).
- The LLM server answers and serves every configured `local:` model.
- Each model makes a real tool call (native or JSON).
- Each model holds a 16k-token prompt.
- The builder and verifier are different models.

Example from our own test machine (LM Studio, model loaded with the default context, then reloaded with `-c 32768`):

```
[ok  ] Docker 29.6.2 (linux containers)
[ok  ] LLM server at http://localhost:1234/v1 serves 2 model(s)
[ok  ] local:qwen2.5-1.5b-instruct: made a tool call via native tool calls (shell {"cmd":"ls"})
[FAIL] local:qwen2.5-1.5b-instruct: context window is smaller than a paper prompt (~16000 tokens); load the model with a larger context ...
...
[ok  ] local:qwen2.5-1.5b-instruct: context holds a 16000-token prompt (16037 tokens seen)
```

## Local run vs the cloud server

| | Cloud ([paperci.vercel.app](https://paperci.vercel.app)) | Local |
| --- | --- | --- |
| Agent, sandbox, allowlist proxy, sealed claims, verifier, dashboard | yes | yes, same code |
| Process | `systemd` unit running `python -m paperci.web` | `scripts/run-local.*` running the same command |
| LLM | Factory API (DeepSeek V4.1 Flash builder, GLM-5.3-Flash verifier) | yours (`local:`), or the same hosted keys |
| Autopilot (watch arXiv, nightly evolution) | on | off by default (`PAPERCI_AUTOPILOT=1` turns it on) |
| Watchdog, Caddy, auto power-off | yes | not needed |
| Tracing | neatlogs | optional: set `NEATLOGS_API_KEY`, or leave it empty |

Your paper and the model stay on your machine. The sandbox still downloads the paper's code and dependencies
through the allowlist proxy (GitHub, PyPI, Hugging Face, Zenodo, and the like), and the scored rerun has no
network at all.

## Command line instead of the dashboard

```
python -m paperci review 2507.12843     # one paper end to end, writes a review page
python tests/test_offline.py            # offline checks, no keys or LLM needed
```

## Hosted LLMs instead

Leave out the `local:` settings and set `LITELLM_API_KEY` + `LITELLM_BASE_URL` (any LiteLLM proxy, model names
without a prefix), or `FACTORY_API_KEY` with `PAPERCI_MODEL_ALL=factory:glm-5.3-flash` (see `.env.example`).
