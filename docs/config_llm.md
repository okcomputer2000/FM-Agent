# LLM Provider Configuration

FM-Agent reads these settings from `.env` (mapped to constants in `config.py`):

```dotenv
LLM_API_KEY=your-api-key                  # auth token for FM-Agent's direct calls
LLM_API_BASE_URL=https://openrouter.ai/api/v1    # endpoint for FM-Agent's direct reasoner calls
LLM_MODEL=anthropic/claude-sonnet-4.6            # a model key registered under the provider below
OPENCODE_MODEL_PROVIDER=openrouter               # an OpenCode provider id
```

It calls the model two ways:

- **OpenCode** (setup / spec / bug validation): `opencode run --model "$OPENCODE_MODEL_PROVIDER/$LLM_MODEL"`, so that string must resolve to a provider + model registered in OpenCode (below).
- **Direct** (reasoner): hits `$LLM_API_BASE_URL` itself, authenticating with `$LLM_API_KEY`.

## Register the OpenCode provider

`OPENCODE_MODEL_PROVIDER/LLM_MODEL` is only valid if the provider is registered in `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://openrouter.ai/api/v1",
        "apiKey": "{env:LLM_API_KEY}"
      },
      "models": { "anthropic/claude-sonnet-4.6": {} }
    }
  }
}
```

How it lines up with `.env`:

| `opencode.json` | `.env` |
|---|---|
| provider key (`openrouter`) | `OPENCODE_MODEL_PROVIDER` |
| `options.baseURL` | `LLM_API_BASE_URL` |
| `options.apiKey` | `LLM_API_KEY` (read from env, never hard-coded) |
| a key under `models` | `LLM_MODEL` |

To use another endpoint, copy the block, rename it, point `baseURL` at the endpoint, and update `.env` to match. Pick `npm` by API style: `@ai-sdk/openai-compatible` (OpenAI-style: OpenRouter, svip) or `@ai-sdk/anthropic` (Anthropic-style, native `/v1/messages`).

## Prompt caching on a multi-tenant relay (`opencode-svip-proxy`)

If your endpoint is a multi-tenant relay (e.g. svip) that fans requests across several upstream accounts, prompt caching only pays off when every request in a session lands on the *same* account — otherwise each account has a cold cache and the hit rate stays near zero.

The [`opencode-svip-proxy`](https://www.npmjs.com/package/opencode-svip-proxy) OpenCode plugin fixes this: it wraps `globalThis.fetch` in-process and injects a stable `metadata.user_id` into every Claude request body, so the relay pins the whole session to one account. Add it to the `plugin` array (OpenCode fetches it from npm on first use), and point the provider straight at the relay:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["opencode-svip-proxy"],
  "provider": {
    "svip": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://svip.xty.app/v1", "apiKey": "{env:LLM_API_KEY}" },
      "models": { "claude-sonnet-4-6": {}, "claude-opus-4-7": {} }
    }
  }
}
```

Optional knobs:

```bash
export OPENCODE_METADATA_USER_ID=your-stable-id   # default: a built-in constant
export OPENCODE_SVIP_HOST=svip.xty.app            # relay host to inject on
```

FM-Agent's direct reasoner calls (`src/llm_client.py`) bypass OpenCode and already inject their own stable `metadata.user_id`, so they stay sticky without the plugin.
