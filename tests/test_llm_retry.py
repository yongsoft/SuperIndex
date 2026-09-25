#!/usr/bin/env python3
"""
Tests for the LLM retry/escalation logic in nav/llm.py.

The bug these cover: a reply that hits `max_tokens` comes back with
`finish_reason="length"` and empty content. Retrying the *identical* request
cannot help, so `chat()` must escalate the budget and drop reasoning effort.

No network: `litellm` is replaced with a scripted stub.

    python tests/test_llm_retry.py
    pytest tests/test_llm_retry.py
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nav import llm  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── stub litellm ─────────────────────────────────────────────────────────
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish):
        self.message = _Msg(content)
        self.finish_reason = finish


class _Resp:
    def __init__(self, content, finish):
        self.choices = [_Choice(content, finish)]


def truncated() -> _Resp:
    """What a provider returns when the output budget runs out."""
    return _Resp("", "length")


def reply(text: str) -> _Resp:
    return _Resp(text, "stop")


class Stub:
    """Returns scripted responses in order, recording every call's kwargs."""

    suppress_debug_info = True

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        item = self.script.pop(0) if self.script else _Resp("", "length")
        if isinstance(item, Exception):
            raise item
        return item


@contextlib.contextmanager
def stubbed(script):
    """Swap nav.llm's litellm for a scripted stub, restoring it afterwards."""
    stub = Stub(script)
    real = llm._litellm
    llm._litellm = lambda: stub          # type: ignore[assignment]
    try:
        yield stub
    finally:
        llm._litellm = real              # type: ignore[assignment]


# ── tests ────────────────────────────────────────────────────────────────
def test_escalates_on_length() -> None:
    print("\n[finish_reason=length → 扩大预算并关闭 reasoning]")
    with stubbed([truncated(), reply("恢复后的内容")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=1000, retries=2)
        check("最终拿到了内容", out == "恢复后的内容", out)
        check("一共调用了 2 次", len(stub.calls) == 2, str(len(stub.calls)))
        c1, c2 = stub.calls
        check("第 1 次带 reasoning_effort", c1.get("reasoning_effort") == "low")
        check("第 1 次用原始预算", c1.get("max_tokens") == 1000,
              str(c1.get("max_tokens")))
        check("第 2 次预算翻倍", c2.get("max_tokens") == 2000,
              str(c2.get("max_tokens")))
        check("第 2 次不再传 reasoning_effort",
              "reasoning_effort" not in c2, str(c2.get("reasoning_effort")))


def test_escalation_capped() -> None:
    print("\n[预算升级有上限，且单调不减]")
    with stubbed([truncated(), truncated(), reply("ok")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=8000, retries=2)
        check("拿到内容", out == "ok", out)
        budgets = [c["max_tokens"] for c in stub.calls]
        check("预算不超过 ceiling",
              all(b <= llm.MAX_TOKEN_CEILING for b in budgets), str(budgets))
        check("预算单调不减", budgets == sorted(budgets), str(budgets))


def test_no_reasoning_escalates_budget_only() -> None:
    print("\n[本来就没开 reasoning → 只扩预算]")
    with stubbed([truncated(), reply("ok")]) as stub:
        out = llm.chat("p", effort="", max_tokens=1000, retries=2)
        check("拿到内容", out == "ok", out)
        check("第 1 次无 reasoning_effort", "reasoning_effort" not in stub.calls[0])
        check("第 2 次仍无 reasoning_effort",
              "reasoning_effort" not in stub.calls[1])
        check("预算仍然翻倍", stub.calls[1]["max_tokens"] == 2000,
              str(stub.calls[1]["max_tokens"]))


def test_exhausted_raises_with_detail() -> None:
    print("\n[全部截断 → 报错且信息可诊断]")
    with stubbed([truncated(), truncated(), truncated()]) as stub:
        try:
            llm.chat("p", effort="low", max_tokens=1000, retries=2)
            check("应抛 RuntimeError", False, "未抛")
        except RuntimeError as exc:
            msg = str(exc)
            check("抛 RuntimeError", True)
            check("说明是 length 截断", "finish_reason=length" in msg, msg[:100])
            check("带上了当时的 max_tokens", "max_tokens=" in msg, msg[:100])
            check("说明 reasoning 状态", "reasoning=" in msg, msg[:100])
        check("确实试满 3 次", len(stub.calls) == 3, str(len(stub.calls)))


def test_transport_error_retries_unchanged() -> None:
    print("\n[传输错误按原样重试，不改预算]")
    with stubbed([ConnectionError("boom"), reply("重试成功")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=1000, retries=2)
        check("重试后成功", out == "重试成功", out)
        check("传输错误不改变预算",
              [c["max_tokens"] for c in stub.calls] == [1000, 1000],
              str([c["max_tokens"] for c in stub.calls]))
        check("传输错误不丢 reasoning_effort",
              stub.calls[1].get("reasoning_effort") == "low")


def test_stream_fallback_gets_more_room() -> None:
    print("\n[流式返回空 → 回退时预算翻倍]")
    # The stub's completion() is not iterable, so chat_stream raises and
    # chat_stream_text falls through to the non-streaming path. The first
    # scripted item is consumed by the stream attempt.
    with stubbed([reply("流式这一发不可迭代"), reply("非流式回退的内容")]) as stub:
        out = "".join(llm.chat_stream_text("p", effort="low", max_tokens=1000))
        check("回退拿到了内容", out == "非流式回退的内容", out)
        check("回退预算翻倍", stub.calls[1]["max_tokens"] == 2000,
              str([c["max_tokens"] for c in stub.calls]))


def main() -> int:
    print("=" * 74)
    print("LLM 重试与预算升级测试（stub 掉 litellm，不联网）")
    print("=" * 74)
    test_escalates_on_length()
    test_escalation_capped()
    test_no_reasoning_escalates_budget_only()
    test_exhausted_raises_with_detail()
    test_transport_error_retries_unchanged()
    test_stream_fallback_gets_more_room()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
