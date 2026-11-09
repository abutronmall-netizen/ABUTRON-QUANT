"""Optional local-LLM trade reviewer (Ollama).

Deliberately peripheral. The audit's lesson was blunt: adding models does not fix
a broken signal-to-execution path. This reviewer can only ever *reduce* risk:

  * ``advisory`` (default): annotates the trade journal, never blocks;
  * ``veto``: blocks a trade on an explicit, parsed negative verdict.

It fails open on timeout or malformed output, is capped at one call per decision
with a short timeout, and never sees or needs account credentials.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ReviewerConfig:
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"
    timeout_s: float = 6.0
    mode: str = "advisory"          # advisory | veto


# Kept as plain concatenation: the schema below contains literal JSON braces,
# and str.format treats those as fields (that bug made every review raise).
PROMPT_HEADER = """You are a risk reviewer for a short-term scalping desk.
Given one trade proposal, reply with STRICT JSON only:
{"approve": true|false, "confidence": 0..1, "risk_note": "<= 20 words"}
Reject only for clear structural problems: entering into a range extreme,
counter-trend in a strong trend, or incoherent context.
Trade proposal:
"""


def build_prompt(payload: dict[str, Any]) -> str:
    return PROMPT_HEADER + json.dumps(payload, default=str)


class OllamaReviewer:
    def __init__(self, cfg: Optional[ReviewerConfig] = None):
        self.cfg = cfg or ReviewerConfig()

    # ------------------------------------------------------------------ #
    def health(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(f"{self.cfg.url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            models = [m.get("name") for m in data.get("models", [])]
            return True, f"{len(models)} model(s): {', '.join(models[:4])}"
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return False, str(exc)

    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.cfg.model,
            "prompt": build_prompt(payload),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 80},
        }).encode()
        req = urllib.request.Request(f"{self.cfg.url}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                raw = json.loads(resp.read().decode()).get("response", "")
            parsed = json.loads(raw)
            return {"approve": bool(parsed.get("approve", True)),
                    "confidence": float(parsed.get("confidence", 0.0)),
                    "risk_note": str(parsed.get("risk_note", ""))[:160]}
        except Exception as exc:            # fail open, never block trading on an LLM
            return {"approve": True, "confidence": 0.0,
                    "risk_note": f"reviewer unavailable ({type(exc).__name__})"}

    def gate(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        out = self.review(payload)
        if self.cfg.mode == "veto" and out["approve"] is False and out["confidence"] >= 0.6:
            return False, out
        return True, out
