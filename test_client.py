import asyncio
import json
import sys
import aiohttp

BASE_URL = "http://127.0.0.1:8000"

async def run_tests():
    print(f"Connecting to AGY-API Bridge at {BASE_URL}...")
    async with aiohttp.ClientSession() as session:
        # 1. Health check
        print("\n--- 1. Testing GET /health ---")
        async with session.get(f"{BASE_URL}/health") as resp:
            print("Status:", resp.status)
            data = await resp.json()
            print("Response:", json.dumps(data, indent=2))
            assert resp.status == 200, "Health check failed"

        # 2. Models list
        print("\n--- 2. Testing GET /v1/models ---")
        async with session.get(f"{BASE_URL}/v1/models") as resp:
            print("Status:", resp.status)
            data = await resp.json()
            models = [m["id"] for m in data.get("data", [])]
            print(f"Found {len(models)} models:", models[:5], "...")
            assert resp.status == 200, "Models endpoint failed"

        # 3. Non-streaming Chat Completion
        print("\n--- 3. Testing POST /v1/chat/completions (stream=False) ---")
        payload = {
            "model": "gemini-3.8-flash-high",
            "messages": [
                {"role": "system", "content": "You are a test agent. Answer concisely."},
                {"role": "user", "content": "Respond with exactly the words: 'Antigravity bridge online'"}
            ],
            "stream": False
        }
        conv_id = None
        async with session.post(f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
            print("Status:", resp.status)
            res_json = await resp.json()
            print("Result:", json.dumps(res_json, indent=2))
            assert resp.status == 200, "Chat completion failed"
            message_content = res_json["choices"][0]["message"]["content"]
            print("Agent reply:", message_content.strip())
            conv_id = res_json.get("conversation_id")
            print("Captured Conversation ID:", conv_id)

        # 4. Streaming Chat Completion (SSE)
        print("\n--- 4. Testing POST /v1/chat/completions (stream=True) ---")
        stream_payload = {
            "model": "gemini-3.8-flash-high",
            "messages": [
                {"role": "user", "content": "Count from 1 to 3 separated by commas."}
            ],
            "stream": True
        }
        async with session.post(f"{BASE_URL}/v1/chat/completions", json=stream_payload) as resp:
            print("Status:", resp.status)
            assert resp.status == 200, "Streaming chat completion failed"
            print("Streaming tokens: ", end="", flush=True)
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue
                raw_data = decoded[6:]
                if raw_data == "[DONE]":
                    print("\n[Stream Complete]")
                    break
                try:
                    chunk = json.loads(raw_data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        sys.stdout.write(content)
                        sys.stdout.flush()
                except Exception as e:
                    print("Error parsing stream chunk:", e)

        # 5. Concurrent 4-Worker Parallel Execution
        print("\n--- 5. Testing Concurrent 4-Worker Execution (4 Simultaneous Requests) ---")
        async def send_req(req_idx: int):
            req_payload = {
                "model": "gemini-3.8-flash-high",
                "messages": [
                    {"role": "user", "content": f"Say 'Worker test {req_idx} success' in 4 words."}
                ],
                "stream": False
            }
            async with session.post(f"{BASE_URL}/v1/chat/completions", json=req_payload) as r:
                res = await r.json()
                assigned_worker = r.headers.get("X-Agy-Worker") or res.get("worker", "unknown")
                reply = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                print(f"  [Req #{req_idx}] Handled by: {assigned_worker} -> '{reply}'")
                return assigned_worker

        worker_results = await asyncio.gather(*(send_req(i) for i in range(1, 5)))
        print(f"Assigned workers across 4 parallel requests: {worker_results}")

    print("\n✅ All End-to-End Tests Passed Successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests())
