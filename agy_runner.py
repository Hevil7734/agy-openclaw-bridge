import asyncio
import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import AsyncGenerator, Dict, Any, List, Optional, Tuple

import base64
from contextlib import asynccontextmanager

import config

logger = logging.getLogger("agy_runner")

def cleanup_conversation(conv_id: str, data_dir: Optional[str] = None) -> bool:
    """
    Remove conversation from agy's SQLite summaries, conversations folder,
    and brain directory so temporary API calls leave zero disk/database footprint.
    Can clean a specific worker directory or all configured worker directories.
    """
    if not conv_id or len(conv_id) < 8:
        return False
    
    target_dirs = [data_dir] if data_dir else [w["data_dir"] for w in config.DEFAULT_WORKERS]
    cleaned = False
    
    for d in target_dirs:
        base = Path(d)
        if not base.exists():
            continue
        try:
            # 1. Delete brain/<conv_id> directory
            brain_path = base / "brain" / conv_id
            if brain_path.is_dir():
                shutil.rmtree(brain_path, ignore_errors=True)
                cleaned = True
                logger.info("Purged ephemeral brain directory: %s (in %s)", conv_id, d)

            # 2. Delete conversations/<conv_id>.db*
            conv_dir = base / "conversations"
            for suffix in ("", "-shm", "-wal"):
                db_file = conv_dir / f"{conv_id}.db{suffix}"
                if db_file.exists():
                    try:
                        db_file.unlink()
                        cleaned = True
                    except OSError:
                        pass

            # 3. Delete from conversation_summaries.db
            summaries_db = base / "conversation_summaries.db"
            if summaries_db.exists():
                try:
                    conn = sqlite3.connect(str(summaries_db), timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?;", (conv_id,))
                    conn.commit()
                    conn.close()
                    cleaned = True
                    logger.info("Purged ephemeral conversation row from summaries DB: %s (in %s)", conv_id, d)
                except Exception as e:
                    logger.warning("Could not delete summary DB entry for %s in %s: %s", conv_id, d, e)

        except Exception as e:
            logger.warning("Error during conversation cleanup for %s in %s: %s", conv_id, d, e)

    return cleaned

class WorkerPool:
    """
    Manages a pool of AGY CLI workers (each backed by a separate Google account / ID).
    Provides least-busy dispatching, round-robin load distribution, and tracking.
    """
    def __init__(self, worker_configs: List[Dict[str, Any]]):
        self.workers = worker_configs
        self._active_counts = {w["id"]: 0 for w in self.workers}
        self._total_served = {w["id"]: 0 for w in self.workers}
        self._rr_index = 0
        self._lock = asyncio.Lock()

    def get_worker_identity(self, worker: Dict[str, Any]) -> str:
        token_file = Path(worker["data_dir"]) / "antigravity-oauth-token"
        if not token_file.exists():
            return "Not logged in"
        try:
            with open(token_file, "r") as f:
                d = json.load(f)
            token_str = d.get("id_token") or ""
            parts = token_str.split(".")
            if len(parts) >= 2:
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                info = json.loads(base64.urlsafe_b64decode(payload))
                return info.get("email", "Logged in")
        except Exception:
            pass
        return "Logged in"

    def get_status(self) -> Dict[str, Any]:
        return {
            "total_workers": len(self.workers),
            "dispatch_mode": config.POOL_DISPATCH_MODE,
            "workers": [
                {
                    "id": w["id"],
                    "name": w.get("name", w["id"]),
                    "bin": w["bin"],
                    "active_requests": self._active_counts.get(w["id"], 0),
                    "total_served": self._total_served.get(w["id"], 0),
                    "account": self.get_worker_identity(w),
                }
                for w in self.workers
            ]
        }

    def _resolve_worker(self, name: str) -> Optional[Dict[str, Any]]:
        target = name.strip().lower()
        for w in self.workers:
            if w["id"].lower() == target:
                return w
            if target in [a.lower() for a in w.get("aliases", [])]:
                return w
        return None

    @asynccontextmanager
    async def lease_worker(self, preferred_worker: Optional[str] = None):
        async with self._lock:
            selected_worker = None
            if preferred_worker:
                selected_worker = self._resolve_worker(preferred_worker)
                if not selected_worker:
                    logger.warning("Preferred worker '%s' not recognized, falling back to dynamic pool", preferred_worker)

            if not selected_worker:
                if config.POOL_DISPATCH_MODE == "round_robin":
                    selected_worker = self.workers[self._rr_index % len(self.workers)]
                    self._rr_index = (self._rr_index + 1) % 1_000_000
                else:
                    # Least-busy worker selection with round-robin tie breaker
                    min_active = min(self._active_counts[w["id"]] for w in self.workers)
                    candidates = [w for w in self.workers if self._active_counts[w["id"]] == min_active]
                    selected_worker = candidates[self._rr_index % len(candidates)]
                    self._rr_index = (self._rr_index + 1) % 1_000_000

            worker_id = selected_worker["id"]
            self._active_counts[worker_id] += 1
            self._total_served[worker_id] += 1

        logger.info(
            "Leased worker '%s' (active: %d, total served: %d)",
            worker_id, self._active_counts[worker_id], self._total_served[worker_id]
        )

        try:
            yield selected_worker
        finally:
            async with self._lock:
                self._active_counts[worker_id] = max(0, self._active_counts[worker_id] - 1)
            logger.info("Released worker '%s' (active: %d)", worker_id, self._active_counts[worker_id])

GLOBAL_POOL = WorkerPool(config.DEFAULT_WORKERS)

def extract_text_from_content(content: Any) -> str:
    """Extract plain text from message content (string or multipart array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    parts.append("[Attached Image]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content or "")

def format_messages_to_prompt(messages: List[Dict[str, Any]], continuation_only: bool = False) -> str:
    """
    Format standard OpenAI messages into a clean prompt string for agy CLI.
    If continuation_only is True (e.g. continuing an existing agy conversation),
    only formats the latest user turn so history isn't duplicated in agy.
    """
    if not messages:
        return ""
    
    # If resuming an existing conversation, agy already has previous turns.
    # We only need the latest user query.
    if continuation_only:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return extract_text_from_content(msg.get("content", ""))
        return extract_text_from_content(messages[-1].get("content", ""))

    # If single user message, pass directly
    if len(messages) == 1 and messages[0].get("role") == "user":
        return extract_text_from_content(messages[0].get("content", ""))

    system_instructions: List[str] = []
    dialogue: List[Tuple[str, str]] = []

    for msg in messages:
        role = msg.get("role", "user").lower()
        content = extract_text_from_content(msg.get("content", ""))
        if not content.strip():
            continue

        if role == "system":
            system_instructions.append(content)
        elif role == "user":
            dialogue.append(("User", content))
        elif role == "assistant":
            dialogue.append(("Assistant", content))
        elif role in ("tool", "function"):
            tool_name = msg.get("name", "tool")
            dialogue.append((f"Tool Result ({tool_name})", content))
        else:
            dialogue.append((role.capitalize(), content))

    MAX_PROMPT_CHARS = 85_000  # Safe headroom below Linux MAX_ARG_STRLEN (128KB)

    current_request_text = ""
    history_text = ""
    system_text = "\n\n".join(system_instructions)

    if dialogue:
        if len(dialogue) > 1:
            history = dialogue[:-1]
            last_turn = dialogue[-1]
            history_text = "\n\n".join(f"{speaker}: {text}" for speaker, text in history)
            current_request_text = f"{last_turn[0]}: {last_turn[1]}"
        else:
            last_turn = dialogue[0]
            current_request_text = last_turn[1]

    # Calculate budget: prioritize current request, then recent history, then system instructions
    if len(current_request_text) > 40_000:
        current_request_text = current_request_text[-40_000:]

    remaining_budget = MAX_PROMPT_CHARS - len(current_request_text) - 500

    if len(system_text) + len(history_text) > remaining_budget:
        # Keep up to 30KB of system text, and remaining for history
        max_sys = min(30_000, remaining_budget // 2)
        if len(system_text) > max_sys:
            system_text = system_text[:max_sys] + "\n...[System prompt truncated for length]..."
        
        remaining_for_hist = max(0, remaining_budget - len(system_text))
        if len(history_text) > remaining_for_hist:
            history_text = "...[Earlier history truncated]...\n" + history_text[-remaining_for_hist:]

    prompt_parts: List[str] = []
    if system_text:
        prompt_parts.append(f"[System Instructions]\n{system_text}")
    if history_text:
        prompt_parts.append(f"[Conversation History]\n{history_text}")
    if current_request_text:
        if dialogue and len(dialogue) > 1:
            prompt_parts.append(f"[Current Request]\n{current_request_text}")
        elif system_text:
            prompt_parts.append(f"[User Request]\n{current_request_text}")
        else:
            prompt_parts.append(current_request_text)

    return "\n\n".join(prompt_parts)

def resolve_model(requested_model: Optional[str]) -> str:
    """Resolve requested model name or alias to an agy-supported model."""
    if not requested_model:
        return config.DEFAULT_MODEL
    
    if "/" in requested_model:
        requested_model = requested_model.split("/")[-1]

    normalized = requested_model.strip().lower()
    if normalized in ("default", "agy", "agy-default", "auto"):
        return config.DEFAULT_MODEL
    
    for m in config.KNOWN_MODELS:
        if m["id"].lower() == normalized:
            return m["id"]

    return requested_model

async def run_agy_non_stream(
    prompt: str,
    model: Optional[str] = None,
    conversation_id: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    timeout: Optional[int] = None,
    preferred_worker: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute agy -p with JSON output across the worker pool and return response dictionary."""
    model_name = resolve_model(model)
    cwd = workspace_dir or config.WORKSPACE_DIR
    selected_effort = effort or config.DEFAULT_EFFORT
    run_timeout = timeout or config.DEFAULT_TIMEOUT

    async with GLOBAL_POOL.lease_worker(preferred_worker=preferred_worker) as worker:
        cmd = [
            worker["bin"],
            "-p", prompt,
            "--output-format", "json",
            "--model", model_name,
        ]

        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        if selected_effort:
            cmd.extend(["--effort", selected_effort])

        if config.AUTO_APPROVE:
            cmd.append("--dangerously-skip-permissions")

        logger.info(
            "Executing non-streaming agy on worker=%s with model=%s, conv=%s",
            worker["id"], model_name, conversation_id
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(run_timeout)
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise TimeoutError(f"AGY worker {worker['id']} timed out after {run_timeout} seconds")

        stdout_str = stdout_data.decode("utf-8", errors="replace").strip()
        stderr_str = stderr_data.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error("agy worker %s exited with code %s. Stderr: %s", worker["id"], proc.returncode, stderr_str)
            raise RuntimeError(f"AGY worker {worker['id']} failed: {stderr_str or stdout_str}")

        try:
            parsed = json.loads(stdout_str)
            parsed["worker"] = worker["id"]
            parsed["worker_data_dir"] = worker["data_dir"]
            return parsed
        except json.JSONDecodeError as err:
            logger.warning("Failed to parse agy output as JSON: %s. Raw output: %s", err, stdout_str)
            return {
                "conversation_id": conversation_id or "",
                "status": "SUCCESS",
                "response": stdout_str,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0
                },
                "worker": worker["id"],
                "worker_data_dir": worker["data_dir"],
            }

async def run_agy_stream(
    prompt: str,
    model: Optional[str] = None,
    conversation_id: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    timeout: Optional[int] = None,
    preferred_worker: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute agy with stream-json output across the worker pool and yield delta/event dictionaries."""
    model_name = resolve_model(model)
    cwd = workspace_dir or config.WORKSPACE_DIR
    selected_effort = effort or config.DEFAULT_EFFORT
    run_timeout = timeout or config.DEFAULT_TIMEOUT

    async with GLOBAL_POOL.lease_worker(preferred_worker=preferred_worker) as worker:
        cmd = [
            worker["bin"],
            "-p", prompt,
            "--output-format", "stream-json",
            "--model", model_name,
        ]

        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        if selected_effort:
            cmd.extend(["--effort", selected_effort])

        if config.AUTO_APPROVE:
            cmd.append("--dangerously-skip-permissions")

        logger.info(
            "Starting streaming agy command on worker=%s with model=%s, conv=%s",
            worker["id"], model_name, conversation_id
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        current_conv_id = conversation_id or ""
        total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=float(run_timeout))
                except asyncio.TimeoutError:
                    logger.error("Timeout waiting for token stream from agy worker %s", worker["id"])
                    yield {
                        "type": "error",
                        "error": f"Stream timed out after {run_timeout} seconds on worker {worker['id']}",
                        "worker": worker["id"],
                    }
                    break

                if not line_bytes:
                    break

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")
                if event == "init":
                    current_conv_id = data.get("conversation_id", current_conv_id)
                    yield {
                        "type": "init",
                        "conversation_id": current_conv_id,
                        "worker": worker["id"],
                        "worker_data_dir": worker["data_dir"],
                    }
                elif event == "step_update":
                    step = data.get("step_update", {})
                    delta = step.get("text_delta")
                    if delta:
                        yield {
                            "type": "delta",
                            "content": delta,
                            "conversation_id": current_conv_id,
                            "worker": worker["id"],
                        }
                    if step.get("usage"):
                        u = step["usage"]
                        total_usage["input_tokens"] = u.get("input_tokens", 0)
                        total_usage["output_tokens"] = u.get("output_tokens", 0)
                        total_usage["total_tokens"] = u.get("total_tokens", 0)
                elif event == "result":
                    res = data.get("result", {})
                    if res.get("usage"):
                        u = res["usage"]
                        total_usage["input_tokens"] = u.get("input_tokens", 0)
                        total_usage["output_tokens"] = u.get("output_tokens", 0)
                        total_usage["total_tokens"] = u.get("total_tokens", 0)
                    current_conv_id = res.get("conversation_id", current_conv_id)
                    yield {
                        "type": "finish",
                        "finish_reason": "stop",
                        "conversation_id": current_conv_id,
                        "usage": total_usage,
                        "worker": worker["id"],
                        "worker_data_dir": worker["data_dir"],
                    }

            await proc.wait()
            if proc.returncode != 0:
                stderr_out = await proc.stderr.read()
                stderr_str = stderr_out.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning("agy worker %s stderr output: %s", worker["id"], stderr_str)

        except asyncio.CancelledError:
            logger.info("Client disconnected, terminating agy worker %s subprocess PID %s", worker["id"], proc.pid)
            try:
                proc.terminate()
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
            except Exception:
                pass
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
