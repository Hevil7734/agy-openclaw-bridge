# 🚀 AGY CLI-to-API Bridge for OpenClaw

An OpenAI-compatible REST API wrapper for the **Google Antigravity CLI (`agy`)**, purpose-built to integrate seamlessly with **OpenClaw** (and any other OpenAI-compatible LLM agent or client).

---

## 🌟 Key Features

* **⚡ 4-Worker Multi-Instance Gateway Pool**:
  * Integrates all 4 configured AGY instances (`agy`, `agy2`, `agy3`, `agy4`), each authenticated with a separate Google account.
  * **4x Parallel Concurrency**: Up to 4 queries stream tokens concurrently with zero queuing delay.
  * **4x Rate-Limit & Quota Headroom**: Multiplies your Google Cloud quota across all 4 accounts.
  * **Dynamic Least-Busy Load Balancing**: Routes each new request to the currently idle or lowest-loaded worker.
  * **Targeted Worker Routing**: Pin specific subagents to specific workers via `model@agy<N>` or header `X-Agy-Worker`.
* **OpenAI-Compatible Endpoints**:
  * `POST /v1/chat/completions` (Streaming & Non-Streaming)
  * `GET  /v1/models`
  * `GET  /v1/models/{model_id}`
  * `GET  /health`
* **Real-time SSE Streaming**: Emits live tokens using Server-Sent Events (`text/event-stream`) directly from AGY's internal stream updates.
* **OpenClaw Ready**: Drop-in configuration for OpenClaw custom providers (`~/.openclaw/openclaw.json`).
* **Multi-Turn Conversation Awareness**: Translates OpenAI message chains (system, user, assistant) into clean agent prompts and tracks session continuation IDs.
* **Model Agnostic**: Supports all AGY models including Gemini 3.8 Flash, Gemini 3.7 Flash, Claude Sonnet 4.6, Claude Opus 4.6, and GPT-OSS 120B.
* **🔒 Strict Security Compliance**: Binds strictly to `127.0.0.1` (localhost only) in compliance with the local system firewall policy.

---

## 🛠️ Quick Start

### 1. Start the Bridge

You can run `agy-api` from anywhere:

```bash
# Start background daemon
agy-api start

# Check running status and health
agy-api status
```

To run in the foreground (useful for live debugging):

```bash
agy-api start --foreground
```

### 2. Run Built-In Self-Tests

Verify health check, model discovery, non-streaming completions, and streaming SSE tokens:

```bash
agy-api test
```

### 3. Stop or Restart

```bash
# Stop daemon
agy-api stop

# Restart daemon
agy-api restart
```

---

## 🤖 OpenClaw Integration Guide

OpenClaw connects to external models via the `"openai-completions"` API provider format.

### Step 1: Add Provider to OpenClaw Configuration

Open your OpenClaw configuration (e.g. `~/.openclaw/openclaw.json` or `~/.openclaw/agents/<agentId>/agent/models.json`):

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "agy": {
        "baseUrl": "http://127.0.0.1:8000/v1",
        "apiKey": "agy-local",
        "api": "openai-completions",
        "models": [
          {
            "id": "gemini-3.8-flash-high",
            "name": "Gemini 3.8 Flash High (Antigravity)",
            "contextWindow": 1000000,
            "maxTokens": 64000
          },
          {
            "id": "gemini-3.7-flash-high",
            "name": "Gemini 3.7 Flash High (Antigravity)",
            "contextWindow": 1000000,
            "maxTokens": 64000
          },
          {
            "id": "claude-sonnet-4-6",
            "name": "Claude Sonnet 4.6 (Antigravity)",
            "contextWindow": 200000,
            "maxTokens": 64000
          },
          {
            "id": "agy-default",
            "name": "Antigravity Default",
            "contextWindow": 1000000,
            "maxTokens": 64000
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "models": {
        "agy/gemini-3.8-flash-high": {
          "alias": "agy"
        }
      }
    }
  }
}
```

You can view this snippet anytime with:
```bash
agy-api config
```

### Step 2: Apply in OpenClaw

Restart OpenClaw or apply configuration:

```bash
openclaw gateway config.apply --file ~/.openclaw/openclaw.json
# or check models
openclaw models list
```

---

## 🧹 Anti-Bloat & Zero Conversation Clutter

By default, every non-interactive call to the raw `agy` CLI would register a new conversation inside `~/.gemini/antigravity-cli/brain/` and `conversation_summaries.db`. If OpenClaw or an API client sent dozens of queries, AGY's local history would become flooded with hundreds of throwaway conversations.

This bridge includes built-in **Anti-Bloat Protection**:

1. **`ephemeral` Mode (Default)**:
   * OpenClaw sends full conversation context in the `messages` array for every turn.
   * As soon as a turn completes (or the SSE stream terminates), the bridge immediately purges the temporary conversation folder and summary DB row.
   * **Result:** AGY's local history and disk space remain **100% clean**, leaving zero conversation clutter.

2. **`session` Mode**:
   * Set `AGY_CONVERSATION_MODE=session` to reuse a single persistent conversation slot per OpenClaw user/session.
   * Subsequent turns are appended to the existing conversation (`--conversation <id>`), ensuring only **1** conversation is created per session instead of a new one on every turn.

3. **Per-Request Controls**:
   * Header `X-Persist: true` or JSON `"persist": true` forces saving the conversation.
   * Header `X-Ephemeral: true` or JSON `"ephemeral": true` forces immediate cleanup.

---

## ⚙️ Configuration & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AGY_API_HOST` | `127.0.0.1` | Binding address (strictly localhost for security) |
| `AGY_API_PORT` | `8000` | Local port for API server |
| `AGY_CONVERSATION_MODE` | `ephemeral` | Conversation policy: `ephemeral` (zero clutter), `session` (reuse 1 conv), or `persist` |
| `AGY_CLEANUP_EPHEMERAL` | `true` | Purges temporary brain folders & summary records |
| `AGY_BIN_PATH` | `~/.local/bin/agy` | Path to `agy` CLI binary |
| `AGY_DEFAULT_MODEL`| `gemini-3.8-flash-high` | Default model used if unspecified |
| `AGY_DEFAULT_EFFORT`| `high` | Reasoning effort (`low`, `medium`, `high`, `max`) |
| `AGY_AUTO_APPROVE` | `true` | Runs with `--dangerously-skip-permissions` in API mode |
| `AGY_API_KEY` | *(None)* | Optional API token authentication |

---

## 🔄 Running as a Systemd Service

To keep the bridge running persistently in the background across restarts:

```bash
# Install the user service
agy-api install-service

# Start and enable on boot
systemctl --user start agy-api
systemctl --user enable agy-api

# Check service status
systemctl --user status agy-api
```
