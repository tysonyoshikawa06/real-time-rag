"""LLM-as-judge for the one semantic assertion Step 19B's grading needs
(`novel_error`'s first assertion — "describes an error pattern distinct in
kind..."). A single, tools-free Anthropic completion call, separate from
`agent/loop.py`'s tool-use loop — no tools are needed to judge a fixed piece
of text against a fixed assertion.
"""

import os
import re

import anthropic
from dotenv import dotenv_values

# Same .env-loading pattern as agent/loop.py and consumer/config.py.
_env = dotenv_values(".env")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 256

_client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", _env.get("ANTHROPIC_API_KEY"))
)

_VERDICT_RE = re.compile(r"^(PASS|FAIL)\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)

_PROMPT_TEMPLATE = """You are grading a single assertion about an AI assistant's answer in a \
payment-transaction monitoring system.

Assertion to check: "{assertion_text}"

The actual novel error signature present in the underlying data this turn was:
{context}

The assistant's answer to grade:
---
{answer_text}
---

Does the assistant's answer satisfy the assertion above? Reply with a strict verdict as the \
first line of your response, in exactly this form (no other text on that line):
PASS: <one-sentence reason>
or
FAIL: <one-sentence reason>
"""


def judge(answer_text: str, assertion_text: str, context: str) -> dict:
    """Run one no-tools completion call and parse a strict PASS/FAIL verdict.

    Returns {"passed": bool | None, "reason": str}. `passed` is None (with
    `reason` explaining why) if the API call fails or the response doesn't
    match the expected `PASS:`/`FAIL:` shape — this must never raise, so one
    bad judge call can't crash the whole grading pass (per spec Edge cases).
    """
    prompt = _PROMPT_TEMPLATE.format(
        assertion_text=assertion_text,
        context=context or "(none)",
        answer_text=answer_text or "",
    )
    try:
        response = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # a judge failure must degrade, never crash grading
        return {"passed": None, "reason": f"llm judge call failed: {exc}"}

    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    first_line = text.splitlines()[0] if text else ""
    match = _VERDICT_RE.match(first_line)
    if not match:
        return {"passed": None, "reason": f"unparseable judge response: {text[:200]!r}"}

    verdict = match.group(1).upper()
    reason = match.group(2).strip() or text[:200]
    return {"passed": verdict == "PASS", "reason": reason}
