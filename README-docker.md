## Claude Proxy - Docker Packaging

This container wraps the Flask+Gunicorn proxy that converts OpenAI-style `/v1/chat/completions` calls to the upstream vendor (default: fizzlycode Claude-compatible endpoint).

### Build

```
cd claude_proxy_bundle
docker build -t claude-proxy:latest .
```

### Run (docker)

```
# copy and edit envs
cp .env.example .env
# then run
docker run -d --name claude-proxy \
  --env-file .env -p 5000:5000 \
  claude-proxy:latest
```

### Run (docker-compose)

```
cd claude_proxy_bundle
cp .env.example .env
# edit .env values (UPSTREAM_API_URL, UPSTREAM_API_KEY, ALLOWED_API_KEYS, etc.)
docker compose up -d --build
```

### Required environment
- `UPSTREAM_API_URL` – upstream endpoint (e.g. `https://.../api/v1/messages?beta=true`)
- `UPSTREAM_API_KEY` – upstream credential
- `ALLOWED_API_KEYS` – comma separated list of client keys allowed to call this proxy

### Optional environment
- `DEFAULT_MODEL` – default model id used when the client omits `model`
- `MODEL_ALIASES` – `from:to` pairs (comma separated) to map inbound model IDs; leave unset to preserve client model
- `DEFAULT_MAX_TOKENS` – fallback `max_tokens` when client omits it (default 4096)
- `MAX_TOKENS_HARD_LIMIT` – hard ceiling for `max_tokens` forwarded upstream (default 16384)
- Dynamic max tokens (optional):
  - `MAX_TOKENS_DYNAMIC` – when `true`, auto-adjust `max_tokens` using prompt estimate + per-model context window
  - `MODEL_CONTEXT_LIMITS_JSON` – JSON map of model -> context tokens (e.g. `{ "claude-3-5-sonnet-latest": 200000 }`)
  - `TOKEN_EST_CHARS_PER_TOKEN` – heuristic char-per-token ratio (default 4.0)
  - `DYNAMIC_SAFETY_MARGIN` – reserved tokens to avoid hitting the exact window (default 1024)
  - `IMAGE_TOKEN_EQUIV` – token equivalent per image block when estimating (default 256)
- `UPSTREAM_ANTHROPIC_VERSION`, `UPSTREAM_ANTHROPIC_BETA`, `UPSTREAM_USER_AGENT`, `UPSTREAM_X_APP`, `UPSTREAM_ANTHROPIC_DANGEROUS`
- `UPSTREAM_EXTRA_HEADERS_JSON` – JSON object to append/override/remove headers
- `CORS_ORIGINS` – `*` or comma separated origins
- `DEFAULT_PROXY_URL` / `UPSTREAM_PROXY_URL` – HTTP(S) proxy to reach the upstream
- `PORT`, `GUNICORN_WORKERS`, `GUNICORN_TIMEOUT`

### Healthcheck
The container exposes `/health`. Docker healthcheck uses it by default.

### Example request
```
curl -sS -H "Authorization: Bearer sk-demo1" -H "Content-Type: application/json" \
  -d '{
    "model":"claude-3-5-sonnet-latest",
    "messages":[{"role":"user","content":[{"type":"text","text":"ping"}]}],
    "stream":false,
    "max_tokens":64
  }' http://localhost:5000/v1/chat/completions
```

### Notes
- If your upstream requires custom headers, set `UPSTREAM_EXTRA_HEADERS_JSON` (e.g. `{"x-vendor":"abc"}`). Use empty string to remove a header.
- If you integrate non-Claude vendors that still accept the Anthropic Messages schema, only the URL/key/headers usually need adjustment. If the schema differs, extend the adapter logic in `claude_proxy.py` accordingly.
