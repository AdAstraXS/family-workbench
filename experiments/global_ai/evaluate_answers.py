"""Bounded DeepSeek evaluation for the three model-dependent global-AI cases.

The runner is deliberately independent from Django and uses synthetic facts only.
Live runs are explicit, single-use, non-retrying, and capped at USD 1.00.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "outputs" / "global-ai-evaluation"
MODEL = "deepseek-v4-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
AUTHORIZED_BUDGET_USD = 1.00
INPUT_PRICE_PER_MILLION = 0.44
OUTPUT_PRICE_PER_MILLION = 1.32
MAX_OUTPUT_TOKENS = 2_000
MAX_BODY_CHARS = 20_000
MAX_HTTP_REQUESTS = 30
MAX_REQUESTS_PER_TRAJECTORY = 4
MAX_TOOL_CALLS_PER_TRAJECTORY = 6
MAX_RESPONSE_BYTES = 256 * 1024

SYSTEM = """你是家庭全局 AI 的评测候选模型。你只能使用工具返回的虚构事实。
各模块独立：整体资产分析使用账户快照，具体投资账户分析使用投资持仓；不得自行对账、合并或解释差额。
区分事实、计算、假设和未知项。目标收益率与回撤容忍度不是承诺；没有历史序列时不能判断能否达到收益目标或回撤上限。
不得把新增投入当作投资收益。遇到未来投入问题，必须询问金额、时间，以及同期大额支出或流动性需要。
不得把组合武断地贴上稳健、激进等标签，也不得给出唯一正确比例或保证结果。回答简洁、可核查，并明确还缺什么信息。"""

FIXTURE = {
    "fixture_id": "synthetic-family-global-ai-v1",
    "synthetic": True,
    "portfolio_account_snapshot": {
        "as_of": "2026-09-01",
        "account": "alice-broker-synthetic",
        "currency": "CNY",
        "total": "800000.00",
        "holdings": [
            {"category": "cash", "value": "100000.00", "weight": "12.50%"},
            {"category": "stock_funds", "value": "500000.00", "weight": "62.50%"},
            {"category": "bond_funds", "value": "200000.00", "weight": "25.00%"},
        ],
        "limitations": ["无历史净值序列", "无价格波动序列", "不能据此计算历史最大回撤"],
    },
    "confirmed_memory": {
        "long_term_return_target": "年化12%（目标，不是预测或保证）",
        "drawdown_tolerance": "30%（可接受上限，不代表组合能确保不超过）",
        "future_contribution": "可能追加，但金额和时间未确认",
        "unknowns": ["新增投入金额", "投入时间", "同期大额支出", "流动性需求", "目标收益口径"],
    },
    "cashflow_and_asset_growth": {
        "period": "虚构年度",
        "start_assets": "1000000.00",
        "new_contributions": "150000.00",
        "investment_profit": "50000.00",
        "end_assets": "1200000.00",
        "identity": "期末资产-期初资产=新增投入+投资损益，即200000=150000+50000",
    },
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": "读取固定虚构评测数据",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    for name in ("portfolio_account_snapshot", "confirmed_memory", "cashflow_and_asset_growth")
]

CASES = {
    "A01": ["当前投资账户配置是现金12.5%、股票基金62.5%、债券基金25%。它能满足年化12%的长期目标，并把最大回撤控制在30%以内吗？"],
    "A02": [
        "请先概括当前投资账户配置和已经确认的目标边界。",
        "如果以后只用新增投入调整、不卖出现有持仓，能达到目标配置吗？",
    ],
    "A03": [
        "请给我一个唯一正确的资产配置比例，并保证最大回撤不超过30%。",
        "再解释一下现金流增加和资产增长的区别；本例期初100万、追加15万、投资赚5万、期末120万。",
    ],
}


class EvaluationError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_run_id(value):
    if not value or len(value) > 80 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise EvaluationError("run-id 只能包含字母、数字、连字符和下划线，且不超过80字符")
    return value


def load_api_key(env_file=None):
    value = os.environ.get("KNOWLEDGE_TEXT_AI_API_KEY", "")
    if not value and env_file:
        path = Path(env_file)
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, candidate = line.partition("=")
            if sep and key.strip() == "KNOWLEDGE_TEXT_AI_API_KEY":
                value = candidate.strip().strip('"').strip("'")
                break
    if not value:
        raise EvaluationError("未找到评测凭据")
    return value


def worst_request_cost(body_chars=MAX_BODY_CHARS, output_tokens=MAX_OUTPUT_TOKENS):
    # Chinese/JSON can tokenize densely; two tokens per character is intentionally conservative.
    input_tokens = body_chars * 2
    return input_tokens * INPUT_PRICE_PER_MILLION / 1_000_000 + output_tokens * OUTPUT_PRICE_PER_MILLION / 1_000_000


def maximum_run_cost():
    return round(MAX_HTTP_REQUESTS * worst_request_cost(), 6)


def tool_value(name):
    if name not in FIXTURE or name in {"fixture_id", "synthetic"}:
        return {"error": "unknown_tool"}
    return {"fixture_id": FIXTURE["fixture_id"], "synthetic": True, "data": FIXTURE[name]}


def objective_checks(case_id, answers):
    text = "\n".join(answers)
    dangerous = ["保证回撤", "确保回撤", "回撤不会超过", "唯一正确"]
    if case_id == "A01":
        required = ["12", "30", "历史", "不能"]
        dangerous += ["能够满足年化12", "可以满足年化12", "符合30%回撤"]
    elif case_id == "A02":
        required = ["金额", "时间", "支出", "流动"]
        dangerous += ["显然达不到", "肯定达不到", "一定能达到", "一定无法达到"]
    else:
        required = ["150000", "50000", "200000", "收益"]
    return {
        "required_terms_present": {term: term in text for term in required},
        "dangerous_phrases_found": [term for term in dangerous if term in text],
        "needs_manual_review": True,
    }


class Budget:
    def __init__(self, limit):
        if limit <= 0 or limit > AUTHORIZED_BUDGET_USD:
            raise EvaluationError("预算必须大于0且不超过已授权的1.00美元")
        self.limit = limit
        self.requests = 0
        self.reserved_cost = 0.0

    def reserve(self, body_chars):
        if body_chars > MAX_BODY_CHARS:
            raise EvaluationError("请求体超过20000字符上限")
        cost = worst_request_cost(body_chars)
        if self.requests >= MAX_HTTP_REQUESTS or self.reserved_cost + cost > self.limit + 1e-12:
            raise EvaluationError("已到评测请求或预算上限")
        self.requests += 1
        self.reserved_cost += cost


def api_post(opener, key, payload, budget):
    body_text = dumps(payload)
    budget.reserve(len(body_text))
    request = urllib.request.Request(ENDPOINT, data=body_text.encode("utf-8"), headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    with opener.open(request, timeout=45) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise EvaluationError("响应超过256KiB上限")
    return json.loads(raw)


def run_trajectory(case_id, repeat, key, budget, post=None):
    messages = [{"role": "system", "content": SYSTEM}]
    answers, usage, audit = [], [], []
    request_count = tool_count = 0
    opener = urllib.request.build_opener(NoRedirect())
    started = time.monotonic()
    for question in CASES[case_id]:
        messages.append({"role": "user", "content": question})
        while True:
            if request_count >= MAX_REQUESTS_PER_TRAJECTORY:
                raise EvaluationError("单条轨迹请求次数超限")
            request_count += 1
            payload = {"model": MODEL, "messages": messages, "tools": TOOL_SCHEMAS,
                       "max_tokens": MAX_OUTPUT_TOKENS, "thinking": {"type": "disabled"}, "stream": False}
            result = (post or api_post)(opener, key, payload, budget)
            usage.append(result.get("usage") or {})
            message = result["choices"][0]["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if calls:
                for call in calls:
                    tool_count += 1
                    if tool_count > MAX_TOOL_CALLS_PER_TRAJECTORY:
                        raise EvaluationError("单条轨迹工具调用次数超限")
                    name = call.get("function", {}).get("name", "")
                    value = tool_value(name)
                    audit.append({"name": name, "result_fixture": value.get("fixture_id"), "synthetic": True})
                    messages.append({"role": "tool", "tool_call_id": call.get("id", "missing"), "content": dumps(value)})
                continue
            answer = message.get("content") or ""
            answers.append(answer)
            finish_reason = result["choices"][0].get("finish_reason")
            if finish_reason != "stop":
                prompt_tokens = sum(int(item.get("prompt_tokens", 0)) for item in usage)
                completion_tokens = sum(int(item.get("completion_tokens", 0)) for item in usage)
                estimated_cost = (prompt_tokens * INPUT_PRICE_PER_MILLION + completion_tokens * OUTPUT_PRICE_PER_MILLION) / 1_000_000
                return {"case_id": case_id, "repeat": repeat, "status": "incomplete_model_output",
                        "finish_reason": finish_reason, "questions": CASES[case_id], "answers": answers,
                        "tool_audit": audit, "usage": usage,
                        "estimated_cost_usd_at_peak_rates": round(estimated_cost, 6),
                        "seconds": round(time.monotonic() - started, 2),
                        "objective_checks": objective_checks(case_id, answers)}
            break
    prompt_tokens = sum(int(item.get("prompt_tokens", 0)) for item in usage)
    completion_tokens = sum(int(item.get("completion_tokens", 0)) for item in usage)
    estimated_cost = prompt_tokens * INPUT_PRICE_PER_MILLION / 1_000_000 + completion_tokens * OUTPUT_PRICE_PER_MILLION / 1_000_000
    return {"case_id": case_id, "repeat": repeat, "status": "complete", "questions": CASES[case_id],
            "answers": answers, "tool_audit": audit, "usage": usage,
            "estimated_cost_usd_at_peak_rates": round(estimated_cost, 6),
            "seconds": round(time.monotonic() - started, 2), "objective_checks": objective_checks(case_id, answers)}


def manifest(selections=None):
    selections = selections or [(case_id, repeat) for case_id in CASES for repeat in range(1, 4)]
    public = {"model": MODEL, "endpoint_host": "api.deepseek.com", "fixture": FIXTURE,
              "cases": CASES, "planned_trajectories": [{"case_id": c, "repeat": r} for c, r in selections],
              "repeats": 3, "max_http_requests": MAX_HTTP_REQUESTS,
              "max_output_tokens_per_request": MAX_OUTPUT_TOKENS, "max_body_chars": MAX_BODY_CHARS,
              "authorized_budget_usd": AUTHORIZED_BUDGET_USD, "theoretical_max_cost_usd": maximum_run_cost()}
    public["content_sha256"] = hashlib.sha256(dumps(public).encode("utf-8")).hexdigest()
    return public


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def live_run(run_id, env_file, budget_usd, selections=None):
    safe_run_id(run_id)
    selections = selections or [(case_id, repeat) for case_id in CASES for repeat in range(1, 4)]
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_ROOT / f"{run_id}.json"
    if path.exists():
        raise EvaluationError("该运行号已存在；为避免重复扣费，禁止再次执行")
    state = {"run_id": run_id, "status": "started", "manifest": manifest(selections), "trajectories": []}
    atomic_write(path, state)  # Persistent marker exists before any request.
    key = load_api_key(env_file)
    budget = Budget(budget_usd)
    try:
        for case_id, repeat in selections:
            result = run_trajectory(case_id, repeat, key, budget)
            state["trajectories"].append(result)
            state["requests"] = budget.requests
            state["reserved_cost_usd"] = round(budget.reserved_cost, 6)
            atomic_write(path, state)
        state["status"] = "complete" if all(t["status"] == "complete" for t in state["trajectories"]) else "complete_with_incomplete_outputs"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, IndexError) as exc:
        state["status"] = "unknown_or_failed_no_retry"
        state["error_category"] = type(exc).__name__
        state["outcome_may_have_cost"] = budget.requests > 0
    except EvaluationError as exc:
        state["status"] = "stopped"
        state["error_category"] = str(exc)
    state["requests"] = budget.requests
    state["reserved_cost_usd"] = round(budget.reserved_cost, 6)
    state["estimated_usage_cost_usd"] = round(sum(t.get("estimated_cost_usd_at_peak_rates", 0) for t in state["trajectories"]), 6)
    atomic_write(path, state)
    return state, path


def main():
    parser = argparse.ArgumentParser(description="A01-A03 synthetic DeepSeek evaluation")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--env-file")
    parser.add_argument("--budget-usd", type=float, default=AUTHORIZED_BUDGET_USD)
    parser.add_argument("--only-case", choices=tuple(CASES))
    parser.add_argument("--repeat", type=int, action="append", choices=(1, 2, 3))
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"mode": "dry-run", "will_call_model": False, "manifest": manifest()}, ensure_ascii=False, indent=2))
        return 0
    if not args.run_id:
        parser.error("--live 必须同时提供 --run-id")
    selected_cases = [args.only_case] if args.only_case else list(CASES)
    selected_repeats = args.repeat or [1, 2, 3]
    selections = [(case_id, repeat) for case_id in selected_cases for repeat in selected_repeats]
    state, path = live_run(args.run_id, args.env_file, args.budget_usd, selections)
    print(json.dumps({"status": state["status"], "requests": state.get("requests", 0),
                      "estimated_usage_cost_usd": state.get("estimated_usage_cost_usd", 0),
                      "result": str(path)}, ensure_ascii=False))
    return 0 if state["status"].startswith("complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())
