"""Host-side guardrails, enforced in code. Configured in config/guardrails.json.

Where this sits among the controls (see docs/05b):
  * MCP server (authoritative): auth, scopes, tenant guard, schemas, shaping.
  * Host (this module): what the USER sends to the model, and what the MODEL
    says back to the user.
  * Provider: Azure OpenAI content filters on your deployment.

Input rules (applied in file order to the user's message, before the LLM sees it):
  redact : replace matches (e.g. IMSI/ICCID): minimise what reaches the provider
  block  : don't send the message at all; answer with a fixed message
  warn   : send it, but record a guardrail event (for review / metrics)

Output rule, "grounded identifiers": a full identifier (MSISDN, IMSI) may
appear in the final answer ONLY if a tool returned it verbatim during this
turn. The server already decided who may see full numbers (`pii:read`), so the
host doesn't need to know scopes. Anything else (hallucinated, pulled from
the user's earlier text, reconstructed from a masked value) is redacted.

Events never contain the matched values, only rule names and counts.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Action = Literal["redact", "block", "warn"]


@dataclass(frozen=True)
class InputRule:
    name: str
    pattern: re.Pattern[str]
    action: Action
    replacement: str = "[removed]"
    message: str = "I can't send that message."


@dataclass(frozen=True)
class OutputRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


@dataclass
class GuardResult:
    text: str
    blocked_message: str | None = None
    events: list[dict] = field(default_factory=list)


class Guardrails:
    def __init__(
        self, max_input_chars: int, input_rules: list[InputRule], output_rules: list[OutputRule]
    ) -> None:
        self.max_input_chars = max_input_chars
        self.input_rules = input_rules
        self.output_rules = output_rules

    @classmethod
    def load(cls, path: Path) -> "Guardrails":
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            inputs = [
                InputRule(
                    r["name"],
                    re.compile(r["regex"]),
                    r["action"],
                    r.get("replacement", "[removed]"),
                    r.get("message", "I can't send that message."),
                )
                for r in raw["input"]["rules"]
            ]
            outputs = [
                OutputRule(r["name"], re.compile(r["regex"]), r["replacement"])
                for r in raw["output"]["grounded_identifiers"]
            ]
        except re.error as exc:
            raise ValueError(f"{path}: invalid regex: {exc}") from exc
        for rule in inputs:
            if rule.action not in ("redact", "block", "warn"):
                raise ValueError(f"{path}: rule {rule.name!r} has unknown action {rule.action!r}")
        return cls(int(raw["input"]["max_chars"]), inputs, outputs)

    @classmethod
    def disabled(cls) -> "Guardrails":
        return cls(max_input_chars=1_000_000, input_rules=[], output_rules=[])

    # ------------------------------------------------------------------ input
    def check_input(self, text: str) -> GuardResult:
        result = GuardResult(text=text)
        if len(text) > self.max_input_chars:
            result.events.append({"rule": "max_chars", "action": "block", "count": 1})
            result.blocked_message = (
                f"Your message is too long ({len(text)} characters; the limit is "
                f"{self.max_input_chars}). Please shorten it."
            )
            return result
        for rule in self.input_rules:
            hits = len(rule.pattern.findall(result.text))
            if not hits:
                continue
            result.events.append({"rule": rule.name, "action": rule.action, "count": hits})
            if rule.action == "redact":
                result.text = rule.pattern.sub(rule.replacement, result.text)
            elif rule.action == "block":
                result.blocked_message = rule.message
                return result
        return result

    # ----------------------------------------------------------------- output
    def check_output(self, answer: str, grounding: list[str]) -> GuardResult:
        """Redact identifiers in `answer` that no tool result (`grounding`) contained."""
        result = GuardResult(text=answer)
        corpus = "\n".join(grounding)
        for rule in self.output_rules:
            removed = 0

            def replace(m: re.Match[str], rule: OutputRule = rule) -> str:
                nonlocal removed
                if m.group(0) in corpus:
                    return m.group(0)  # grounded: the server returned this exact value
                removed += 1
                return rule.replacement

            result.text = rule.pattern.sub(replace, result.text)
            if removed:
                result.events.append({"rule": rule.name, "action": "redact", "count": removed})
        return result
