"""Bounded local probe. --live is explicit; never imports the application's models."""
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from experiments.global_ai.core import (
    Context, Denied, Store, Tools, TOOL_SCHEMAS, SYSTEM, QUESTION, FOLLOWUP, dumps,
    evidence_pack, seed,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = "deepseek-v4-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_REQUESTS = 4


def api_key():
    # Read only this named credential; never log config contents or credentials.
    value = os.environ.get("KNOWLEDGE_TEXT_AI_API_KEY", "")
    if not value:
        path = ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, candidate = line.partition("=")
                if sep and key.strip() == "KNOWLEDGE_TEXT_AI_API_KEY":
                    value = candidate.strip().strip('"').strip("'")
                    break
    if not value:
        raise Denied("No configured experiment credential")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def direct(tools, key, post=None):
    """Same tools and prompts as harness; hard request ceiling, no HTTP retries."""
    messages = [{"role": "system", "content": SYSTEM}]
    answers, usage, attempts = [], [], 0
    schema = [{"type": "function", "function": {"name": t["name"],
              "description": t["description"], "parameters": t["inputSchema"]}} for t in TOOL_SCHEMAS]
    opener = urllib.request.build_opener(NoRedirect())

    def http_post(payload):
        body = dumps(payload).encode("utf-8")
        if len(body) > 40000:
            raise Denied("Input budget exceeded")
        request = urllib.request.Request(ENDPOINT, data=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with opener.open(request, timeout=45) as response:
            data = response.read(262145)
        if len(data) > 262144:
            raise Denied("Response budget exceeded")
        return json.loads(data)

    started = time.monotonic()
    for question in (QUESTION, FOLLOWUP):
        messages.append({"role": "user", "content": question})
        while True:
            if attempts >= MAX_REQUESTS:
                return {"status": "request_limit", "attempts": attempts, "answers": answers, "usage": usage}
            attempts += 1
            payload = {"model": MODEL, "messages": messages, "tools": schema,
                       "max_tokens": 1200, "thinking": {"type": "disabled"}, "stream": False}
            try:
                result = (post or http_post)(payload)
                usage.append(result.get("usage"))
                message = result["choices"][0]["message"]
                finish = result["choices"][0].get("finish_reason")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if len(calls) > 8:
                    raise Denied("Too many tool calls")
                if calls:
                    for call in calls:
                        function = call["function"]
                        try:
                            value = tools.call(function["name"], json.loads(function["arguments"]))
                        except (Denied, json.JSONDecodeError):
                            value = {"error": "Access denied or invalid arguments"}
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": dumps(value)})
                    continue
                answers.append(message.get("content") or "")
                if finish != "stop":
                    return {"status": "incomplete", "attempts": attempts, "answers": answers, "usage": usage}
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                return {"status": "network_or_http_failure_no_retry", "attempts": attempts,
                        "usage": usage, "answers": answers, "outcome_may_have_cost": True}
    return {"status": "complete", "attempts": attempts, "seconds": round(time.monotonic()-started, 2),
            "answers": answers, "usage": usage, "tool_calls": tools.audit}


class Rpc:
    """Single-client probe; notifications retained and approval requests always rejected."""
    def __init__(self, command, env, cwd):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages = queue.Queue()
        self.events = []
        self.errors = []
        self.sequence = 0
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self.stdout_thread = threading.Thread(target=self._stdout, daemon=True)
        self.stderr_thread.start()
        self.stdout_thread.start()

    def _stderr(self):
        for line in self.process.stderr:
            # In-memory only; never serialize stderr, which can contain auth details.
            if sum(map(len, self.errors)) < 16000:
                self.errors.append(line)

    def _stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(dumps(message) + "\n")
        self.process.stdin.flush()

    def next(self, timeout):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise EOFError("App server exited")
        if "method" in message and "id" in message:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Probe denies server requests"}})
        return message

    def request(self, method, params, timeout=20):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next(max(.01, deadline-time.monotonic()))
            if message.get("id") == request_id and "method" not in message:
                return message
            self.events.append(message)
        raise TimeoutError()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


def codex_probe(output, live=False, key=None):
    binary = shutil.which("codex")
    if not binary:
        return {"status": "codex_missing"}
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    if version != "codex-cli 0.153.4":
        return {"status": "version_not_validated", "version": version}
    # Keep experiment config separate from the user's real Codex home and project config.
    with tempfile.TemporaryDirectory(prefix="family-ai-", dir=output) as temp:
        home = Path(temp)
        working = home / "workspace"
        working.mkdir()
        config = [
            'project_root_markers = []', 'web_search = "disabled"', 'approval_policy = "never"',
            'sandbox_mode = "read-only"',
        ]
        if live:
            config += ['model_provider = "probe_deepseek"', 'model = "' + MODEL + '"',
                       '[model_providers.probe_deepseek]', 'name = "Synthetic DeepSeek probe"',
                       'base_url = "https://api.deepseek.com"', 'wire_api = "chat"',
                       'env_key = "FAMILY_SPIKE_API_KEY"', 'request_max_retries = 0',
                       'stream_max_retries = 0']
        config += ['[features]', 'shell_tool = false',
            'multi_agent = false', 'shell_snapshot = false',
            '[mcp_servers.family_fixture]', 'command = ' + json.dumps(sys.executable),
            'args = ' + json.dumps(["-m", "experiments.global_ai.mcp_server", "--actor", "alice"]),
            'required = true', '[mcp_servers.family_fixture.env]',
            'PYTHONPATH = ' + json.dumps(str(ROOT)), 'PYTHONIOENCODING = "utf-8"',
        ]
        (home / "config.toml").write_text("\n".join(config), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_", "DEEPSEEK_"))}
        env.update({"CODEX_HOME": str(home), "PYTHONIOENCODING": "utf-8"})
        if key:
            env["FAMILY_SPIKE_API_KEY"] = key
        rpc = Rpc([binary, "app-server"], env, str(working))
        result = {"version": version, "live": live, "model": MODEL if live else None}
        try:
            init = rpc.request("initialize", {"clientInfo": {"name": "family-spike", "version": "0.1"}})
            if "error" in init:
                return {**result, "status": "initialize_rejected"}
            result["handshake_passed"] = True
            rpc.send({"method": "initialized", "params": {}})
            if not live:
                created = rpc.request("thread/start", {"cwd": str(working), "sandbox": "read-only",
                    "approvalPolicy": "never", "baseInstructions": SYSTEM})
                if "error" in created:
                    return {**result, "status": "thread_start_rejected", "handshake": True}
                thread_id = created["result"]["thread"]["id"]
                result["thread_created"] = True
                read = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
                result["read_passed"] = "result" in read
                servers = rpc.request("mcpServerStatus/list", {})
                tool_counts = {entry["name"]: len(entry.get("tools", {}))
                               for entry in servers.get("result", {}).get("data", [])}
                return {**result, "status": "handshake_and_thread_passed", "read_passed": "result" in read,
                        "mcp_tool_counts": tool_counts}
            # No fallback to a different model/account or wire-protocol proxy.
            created = rpc.request("thread/start", {"cwd": str(working), "sandbox": "read-only",
                "approvalPolicy": "never", "baseInstructions": SYSTEM, "model": MODEL})
            if "error" in created:
                reason = str(created["error"].get("message", "Unknown rejection"))
                if key:
                    reason = reason.replace(key, "[REDACTED]")
                return {**result, "status": "thread_start_rejected", "no_turn_submitted": True,
                        "reason": reason[:1200]}
            # This version rejects chat at thread/start. Keep this probe non-billable;
            # a future compatible provider needs a separately validated turn adapter.
            return {**result, "status": "provider_configuration_accepted", "no_turn_submitted": True}
        except (EOFError, BrokenPipeError, queue.Empty, TimeoutError):
            stderr = "".join(rpc.errors)
            category = "app_server_exit_or_timeout"
            if "chat" in stderr and ("unknown variant" in stderr or "unsupported" in stderr):
                category = "chat_wire_protocol_not_supported"
            return {**result, "status": category, "no_turn_submitted": True,
                    "observed_event_methods": sorted({e.get("method", "") for e in rpc.events})}
        finally:
            rpc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Send synthetic data, at most four direct API requests")
    parser.add_argument("--run-id", default="synthetic-v1", help="Persistent idempotency key; do not change to retry uncertain requests")
    args = parser.parse_args()
    safe_id = args.run_id
    if not safe_id or len(safe_id) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in safe_id):
        raise Denied("Invalid run identifier")
    output = ROOT / "outputs" / "global-ai-spike"
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output / "probe.sqlite3")
    seed(store)
    ctx = Context("alice")
    tools = Tools(ctx, store)
    result = {"synthetic_only": True, "evidence": evidence_pack(tools), "live": args.live}
    tools.audit.clear()
    result["codex_offline"] = codex_probe(output)
    if args.live:
        key = api_key()
        store.claim_run(ctx, args.run_id)
        try:
            result["codex_provider"] = codex_probe(output, live=True, key=key)
            result["direct"] = direct(tools, key)
            store.finish_run(ctx, args.run_id, "complete" if result["direct"]["status"] == "complete" else "unknown")
        except BaseException:
            store.finish_run(ctx, args.run_id, "unknown")
            raise
    # Unique records preserve the initial failed-quality sample after a prompt change.
    report = output / (f"{safe_id}-result.json" if args.live else "offline-result.json")
    report.write_text(dumps(result), encoding="utf-8")
    store.close()
    # Report contains only fixture data, outputs and aggregate metrics. Never credentials/stderr.
    print(dumps({"report": str(report), "codex": result["codex_offline"],
                 "direct_status": result.get("direct", {}).get("status")}))


if __name__ == "__main__":
    main()
