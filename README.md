# Claude Proxy Bundle

A lightweight Flask + Gunicorn proxy that exposes OpenAI-compatible `/v1/chat/completions` and `/v1/models` endpoints while forwarding requests to an upstream Claude-compatible vendor (default: fizzlycode). The bundle also includes Docker/Compose packaging and a smoke-test script so the project can be cloned and deployed on any machine in a few commands.

## Features
- Translate OpenAI Chat Completions payloads (messages, tools, tool_choice, streaming) into Anthropic/Fizzlycode format.
- Enforce per-client API keys and dynamic, per-model `max_tokens` caps to avoid upstream 5xx responses.
- Auto-regenerate Anthropic-style `user_id`s and forward system prompts required by the upstream.
- Optional proxy autodetection plus configurable upstream headers to interoperate with custom vendors.
- Production-ready Dockerfile + docker-compose.yml with health checks, and a `.env.example` for zero-guess configuration.

## Repository Layout
| Path | Purpose |
| --- | --- |
| `claude_proxy.py` | Core Flask application that adapts OpenAI requests to the upstream API. |
| `entrypoint.sh` | Gunicorn bootstrap used by Docker images. |
| `Dockerfile` | Multi-stage container definition with health check and sane defaults. |
| `docker-compose.yml` | Compose service exposing the proxy and loading `.env`. |
| `.env.example` | Copy to `.env` and fill in credentials/settings. |
| `remote_gen_test.py` | Minimal smoke test that calls the upstream vendor directly; handy for troubleshooting credentials. |
| `requirements.txt` | Python runtime dependencies. |
| `README.md` | You are reading it. |

## Requirements
- Python 3.10+ and `pip` (for local/dev usage)
- Docker 24+ (optional, for containerized deployments)
- Valid upstream vendor credentials and a list of client-side API keys

## Quick Start (local Python)
1. Clone this repo and enter the folder:
   ```bash
   git clone <your-fork-url> && cd claude_proxy_bundle
   ```
2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. Copy the sample env file and edit your values:
   ```bash
   cp .env.example .env
   # edit .env (UPSTREAM_API_URL, UPSTREAM_API_KEY, ALLOWED_API_KEYS, etc.)
   ```
4. Export the variables for the current shell (or rely on a process manager that reads `.env`):
   ```bash
   export $(grep -v '^#' .env | xargs)
   ```
5. Start the proxy (development):
   ```bash
   python claude_proxy.py
   ```
   or run production-style with Gunicorn:
   ```bash
   gunicorn -w ${GUNICORN_WORKERS:-2} -b 0.0.0.0:${PORT:-5000} --timeout ${GUNICORN_TIMEOUT:-300} claude_proxy:app
   ```
6. Call the API:
   ```bash
   curl -sS -H "Authorization: Bearer sk-demo1" -H "Content-Type: application/json" \
     -d '{
       "model": "claude-3-5-sonnet-latest",
       "messages": [{"role": "user", "content": [{"type":"text","text":"ping"}]}],
       "stream": false,
       "max_tokens": 64
     }' http://localhost:5000/v1/chat/completions
   ```

## Quick Start (Docker)
```bash
cd claude_proxy_bundle
cp .env.example .env && edit it
# Build image
docker build -t claude-proxy:latest .
# Run container
docker run -d --name claude-proxy \
  --env-file .env -p 5000:5000 \
  claude-proxy:latest
```

### Using docker-compose
```bash
cp .env.example .env
PORT=5000 docker compose up -d --build
```
The compose file wires the health check to `/health` and restarts the service automatically on failure.

## Configuration
Environment variables can be supplied via `.env`, shell exports, or your orchestration platform.

### Required
| Variable | Description |
| --- | --- |
| `UPSTREAM_API_URL` | Claude-compatible messages endpoint (includes query params if required). |
| `UPSTREAM_API_KEY` | Bearer token for the upstream vendor. The code now ships with a placeholder that **must** be overridden. |
| `ALLOWED_API_KEYS` | Comma-separated list of client API keys allowed to call your proxy (each request must use `Authorization: Bearer <key>`). |

### Common optional knobs
| Variable | Default | Notes |
| --- | --- | --- |
| `DEFAULT_MODEL` | `claude-3-5-sonnet-latest` | Used when the client omits `model`. |
| `MODEL_ALIASES` | _empty_ | Map inbound model IDs to upstream-supported ones (format `from:to,foo:bar`). |
| `DEFAULT_SYSTEM_PROMPT` | Claude CLI prompt | Prepended to every upstream request. |
| `DEFAULT_MAX_TOKENS` | `4096` | Fallback when clients omit `max_tokens`. |
| `MAX_TOKENS_HARD_LIMIT` | `16384` | Upper bound forwarded upstream. |
| `MAX_TOKENS_DYNAMIC` | `false` | When `true`, estimate prompt tokens and squeeze max tokens to stay within per-model context. Requires `MODEL_CONTEXT_LIMITS_JSON`. |
| `MODEL_CONTEXT_LIMITS_JSON` | _empty_ | JSON map of `model -> context_tokens`. |
| `TOKEN_EST_CHARS_PER_TOKEN` | `4.0` | Heuristic used for dynamic budgeting. |
| `DYNAMIC_SAFETY_MARGIN` | `1024` | Reserve tokens to avoid hard limits. |
| `IMAGE_TOKEN_EQUIV` | `256` | Approximate image cost in tokens. |
| `CORS_ORIGINS` | `*` | Comma-separated origins or `*`. |
| `DEFAULT_PROXY_URL` / `UPSTREAM_PROXY_URL` | _auto detect_ | HTTP(S) proxy for outbound requests (explicit wins). |
| `PORT`, `GUNICORN_WORKERS`, `GUNICORN_TIMEOUT` | `5000`, `2`, `300` | Gunicorn tuning knobs (Docker entrypoint honors them). |
| `UPSTREAM_*` headers | see `.env.example` | Override Anthropic-specific header values when your vendor diverges. |

## Endpoints
| Method | Path | Description |
| --- | --- | --- |
| `POST /v1/chat/completions` | Accepts OpenAI-style payloads. Supports JSON body or `?max_tokens=` override, streaming SSE responses, tool calls, and error passthrough from upstream. Requires `Authorization: Bearer <client-key>`. |
| `GET /v1/models` | Returns a list containing the configured default model; useful for quick capability checks. |
| `GET /health` | Health probe used by Docker. Includes upstream URL, alias map, allowed key count, and current cached `user_id`. |

## Smoke Tests & Troubleshooting
- **Direct upstream test**: `remote_gen_test.py` picks up `UPSTREAM_API_URL`, `UPSTREAM_API_KEY`, and `DEFAULT_MODEL` from your environment and performs a single `ping` request. Run it before exposing the proxy:
  ```bash
  UPSTREAM_API_KEY=cr_real_key python3 remote_gen_test.py
  ```
- **Proxy contract test**: Use the `curl` command shown above or point an OpenAI-compatible SDK at `http://<host>:<port>`. Remember to inject one of the keys from `ALLOWED_API_KEYS`.
- **Health check**: `curl http://localhost:5000/health` should return `{ "status": "ok", ... }`. Docker uses this endpoint automatically.

## Development Notes
- `.gitignore` already excludes logs, `.env`, caches, and virtual environments. Keep credentials outside of version control.
- When running locally, prefer `python3` and `pip` from a virtualenv; Python 2 is only present for compatibility with older systems.
- To lint quickly, run `python3 -m py_compile claude_proxy.py remote_gen_test.py`.
- Logs from Gunicorn go to stdout/stderr; pipe them into your logging stack (Docker default) or set `--access-logfile` as needed.

## Security Checklist
1. Always replace the default upstream key placeholder _before_ deploying.
2. Rotate `ALLOWED_API_KEYS` regularly and store them outside of the repo (e.g., env vars, secret manager).
3. Consider running the container behind an HTTPS terminator or reverse proxy; this bundle intentionally exposes plain HTTP to stay simple.
4. If using shared hosts, set `CORS_ORIGINS` to the minimal set of domains that need browser access.

Happy hacking! Clone, configure, and deploy wherever you like.
