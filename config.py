import os
import shutil
from pathlib import Path

# Server Network Settings
# STRICT SECURITY POLICY: Bind only to 127.0.0.1 (localhost)
HOST = os.getenv("AGY_API_HOST", "127.0.0.1")
PORT = int(os.getenv("AGY_API_PORT", "8000"))

# Security / Authentication (optional API key)
# If empty or None, requests from localhost are allowed without an API key
API_KEY = os.getenv("AGY_API_KEY", "")

_detected_agy = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
AGY_BIN = os.getenv("AGY_BIN_PATH", _detected_agy)

# Working directory for agy agent execution
WORKSPACE_DIR = os.getenv("AGY_WORKSPACE_DIR", str(Path.home()))

# AGY Application State Directory (where brain/ and sqlite databases reside)
AGY_DATA_DIR = os.getenv("AGY_DATA_DIR", str(Path.home() / ".gemini" / "antigravity-cli"))

# ==============================================================================
# MULTI-WORKER AGY GATEWAY POOL
# Connects multiple independent agy CLI instances (separate Google accounts / IDs)
# for 4x parallel concurrency, zero queuing delay, and multiplied quota headroom.
# ==============================================================================
_home = Path.home()
DEFAULT_WORKERS = [
    {
        "id": "agy1",
        "name": "AGY Primary",
        "aliases": ["agy", "default", "agy1", "worker1"],
        "bin": os.getenv("AGY1_BIN", str(_home / ".local" / "bin" / "agy")),
        "data_dir": os.getenv("AGY1_DATA_DIR", str(_home / ".gemini" / "antigravity-cli")),
    },
    {
        "id": "agy2",
        "name": "AGY Worker 2",
        "aliases": ["agy2", "worker2"],
        "bin": os.getenv("AGY2_BIN", str(_home / ".local" / "bin" / "agy2")),
        "data_dir": os.getenv("AGY2_DATA_DIR", str(_home / ".gemini-profiles" / "agy2" / ".gemini" / "antigravity-cli")),
    },
    {
        "id": "agy3",
        "name": "AGY Worker 3",
        "aliases": ["agy3", "worker3"],
        "bin": os.getenv("AGY3_BIN", str(_home / ".local" / "bin" / "agy3")),
        "data_dir": os.getenv("AGY3_DATA_DIR", str(_home / ".gemini-profiles" / "agy3" / ".gemini" / "antigravity-cli")),
    },
    {
        "id": "agy4",
        "name": "AGY Worker 4",
        "aliases": ["agy4", "worker4"],
        "bin": os.getenv("AGY4_BIN", str(_home / ".local" / "bin" / "agy4")),
        "data_dir": os.getenv("AGY4_DATA_DIR", str(_home / ".gemini-profiles" / "agy4" / ".gemini" / "antigravity-cli")),
    },
]

# Worker pool dispatch mode: "least_busy" (default) or "round_robin"
POOL_DISPATCH_MODE = os.getenv("AGY_POOL_DISPATCH_MODE", "least_busy").lower()

# Default Model & Reason Effort
DEFAULT_MODEL = os.getenv("AGY_DEFAULT_MODEL", "gemini-3.8-flash-high")
DEFAULT_EFFORT = os.getenv("AGY_DEFAULT_EFFORT", "high")  # low, medium, high, max

# Auto-approve tool permissions in headless/API mode
AUTO_APPROVE = os.getenv("AGY_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")

# Process timeouts in seconds
DEFAULT_TIMEOUT = int(os.getenv("AGY_TIMEOUT", "180"))

# ==============================================================================
# CONVERSATION RETENTION & ANTI-BLOAT POLICY
# ==============================================================================
# Mode options:
#  - "ephemeral": Cleans up temporary conversations after every request so AGY's
#                 history and database are never bloated. Since OpenClaw sends
#                 full conversation history in 'messages', each turn is self-contained.
#  - "session":   Reuses a single persistent AGY conversation per OpenClaw user/session
#                 via `agy --conversation <id>` so turns are appended rather than creating
#                 new conversations.
#  - "persist":   Keeps all conversations in AGY history without cleanup.
CONVERSATION_MODE = os.getenv("AGY_CONVERSATION_MODE", "ephemeral").lower()

# Automatically clean up throwaway conversation artifacts if ephemeral
CLEANUP_EPHEMERAL = os.getenv("AGY_CLEANUP_EPHEMERAL", "true").lower() in ("true", "1", "yes")

# Default session name for OpenClaw when session mode is active
DEFAULT_SESSION_NAME = os.getenv("AGY_DEFAULT_SESSION_NAME", "openclaw_main")

# Static list of supported models (fallback if dynamic listing is slow)
KNOWN_MODELS = [
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "context_window": 1000000},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "context_window": 1000000},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)", "context_window": 1000000},
    {"id": "gemini-3.7-flash-high", "name": "Gemini 3.7 Flash (High)", "context_window": 1000000},
    {"id": "gemini-3.7-flash-medium", "name": "Gemini 3.7 Flash (Medium)", "context_window": 1000000},
    {"id": "gemini-3.7-flash-low", "name": "Gemini 3.7 Flash (Low)", "context_window": 1000000},
    {"id": "gemini-3.6-flash-high", "name": "Gemini 3.6 Flash (High)", "context_window": 1000000},
    {"id": "gemini-3.6-flash-medium", "name": "Gemini 3.6 Flash (Medium)", "context_window": 1000000},
    {"id": "gemini-3.6-flash-low", "name": "Gemini 3.6 Flash (Low)", "context_window": 1000000},
    {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro (High)", "context_window": 1000000},
    {"id": "gemini-3.1-pro-low", "name": "Gemini 3.1 Pro (Low)", "context_window": 1000000},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)", "context_window": 200000},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)", "context_window": 200000},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "context_window": 128000},
    {"id": "agy-default", "name": "Antigravity Default", "context_window": 1000000},
    {"id": "default", "name": "Antigravity Default", "context_window": 1000000},
]
