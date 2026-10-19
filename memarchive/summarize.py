"""Summarizer backends for the DAG builder.

    Summarizer.summarize(pack, tier="small") -> SummaryResult

* StubSummarizer       deterministic, offline, extractive. Used by every test and in the cloud.
                       `flinch=True` makes it emit short evasive text so the detector can be exercised.
* AnthropicSummarizer  the real backend (official `anthropic` package, imported lazily). Two tiers,
                       a hard dollar budget, no retries beyond the SDK's two. Never called by tests.

Select with `--backend stub|anthropic` or `$MEM_BACKEND` (default stub). The API key is read by the
SDK from `$ANTHROPIC_API_KEY` (or an `ant auth login` profile); it is never stored by this tool.
"""
from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .flinch import split_sentences

TIERS = ("small", "mid")
# Extra tier names the builder may ask for (see Summarizer.rerun_tier and the refusal fallback in build.py):
#   rerun_leaf / rerun_upper  the per-level rerun model (default: the mid model, in which case plain "mid" is used)
#   small_medium              the small model at effort "medium": last try after a refused rerun
ALL_TIERS = TIERS + ("rerun_leaf", "rerun_upper", "small_medium")
FALLBACK_TIER = "small_medium"


@dataclass
class PromptPack:
    level: str
    text: str                       # the full rendered source (what a model would be shown)
    source_bytes: int               # prose bytes (leaf) or child-summary bytes (upper levels)
    proper_nouns: List[str] = field(default_factory=list)
    sensitivity: str = "normal"     # "high" when any thinking block is in the source
    segments: List[Tuple[str, str]] = field(default_factory=list)   # (kind, text); kind in human|assistant|thinking|child
    noise: List[str] = field(default_factory=list)                  # listed (not transcribed) tool activity
    feedback: Optional[str] = None  # set on a rerun: why the previous attempt was rejected
    span: str = ""
    n_sources: int = 0


@dataclass
class SummaryResult:
    text: str
    model: str
    backend: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    refused: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)


class BudgetExceeded(RuntimeError):
    """Raised before a call that could push spend past budget_usd; the build stops cleanly."""


class Summarizer:
    backend = "base"

    def summarize(self, pack: PromptPack, tier: str = "small") -> SummaryResult:  # pragma: no cover - interface
        raise NotImplementedError

    def estimate_cost(self, pack: PromptPack, tier: str = "small") -> float:
        return 0.0

    def rerun_tier(self, level: str) -> str:
        """The tier name the builder uses for the rerun of a node at `level`."""
        return "mid"


# ----------------------------------------------------------------------------- stub

class StubSummarizer(Summarizer):
    """Extractive: sentences that cover the source's proper nouns first, then the first sentence of
    every human/assistant block, then evenly spaced filler, up to a target ratio of the source."""
    backend = "stub"
    model = "stub-extractive-1"

    def __init__(self, flinch: bool = False, flinch_tiers: Sequence[str] = TIERS,
                 ratio_leaf: float = 0.15, ratio_upper: float = 0.3, min_bytes: int = 240):
        self.flinch = flinch
        self.flinch_tiers = tuple(flinch_tiers)
        self.ratio_leaf = ratio_leaf
        self.ratio_upper = ratio_upper
        self.min_bytes = min_bytes

    def summarize(self, pack: PromptPack, tier: str = "small") -> SummaryResult:
        if self.flinch and tier in self.flinch_tiers:
            text = "I can't go into the details here; let's focus on something else."
        else:
            text = self._extract(pack)
        return SummaryResult(text=text, model=self.model, backend=self.backend,
                             input_tokens=len(pack.text) // 4, output_tokens=len(text) // 4)

    def _extract(self, pack: PromptPack) -> str:
        target = max(self.min_bytes, int((self.ratio_leaf if pack.level == "leaf" else self.ratio_upper) * pack.source_bytes))
        sents: List[Tuple[int, int, str, str]] = []     # (block index, sentence index, text, kind)
        for bi, (kind, text) in enumerate(pack.segments):
            for si, s in enumerate(split_sentences(text)):
                s = re.sub(r"^[-*\u2022]\s+", "", s)       # bullets from a child summary
                if s:
                    sents.append((bi, si, s, kind))
        words = [set(w.lower() for w in _words(s)) for _, _, s, _k in sents]
        chosen: List[int] = []
        used = 0

        def take(i: int) -> None:
            nonlocal used
            if i not in chosen:
                chosen.append(i)
                used += len(sents[i][2].encode("utf-8")) + 3

        # 1. cover proper nouns (most frequent first); allowed to exceed the target up to 2x
        for noun in pack.proper_nouns[:30]:
            if used >= 2 * target:
                break
            n = noun.lower()
            if any(n in words[i] for i in chosen):
                continue
            for i, w in enumerate(words):
                if n in w:
                    take(i)
                    break
        # 2. first sentence of every human/assistant block
        for i, (bi, si, _, kind) in enumerate(sents):
            if si == 0 and kind != "thinking" and used < target:
                take(i)
        # 3. evenly spread filler (any class, thinking included) until the target is reached
        if used < target:
            rest = [i for i in range(len(sents)) if i not in chosen]
            stride = 1
            while stride * 2 <= len(rest):
                stride *= 2
            while used < target and rest:
                for i in rest[::stride]:
                    if used >= target:
                        break
                    take(i)
                rest = [i for i in rest if i not in chosen]
                stride = max(1, stride // 2)
        chosen.sort()
        lines = ["- " + sents[i][2] for i in chosen]
        if not lines:
            note = ("No prose in this span." if not pack.noise else "No prose in this span; activity: " + "; ".join(pack.noise[:6]))
            return note
        return "\n".join(lines)


def _words(s: str) -> List[str]:
    from .flinch import TOKEN_RE
    return TOKEN_RE.findall(s)


# ----------------------------------------------------------------------------- anthropic

# $ per million tokens (input, output); prompts here stay far below the 100K tier boundary.
PRICES: Dict[str, Tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-1": (10.00, 50.00),
}
UNKNOWN_PRICE = (10.00, 50.00)      # conservative for a model we have no price for
DEFAULT_MODELS = {"small": "claude-haiku-5-5", "mid": "claude-sonnet-5-5"}
EFFORT = {"small": "low", "mid": "medium", "rerun_leaf": "medium", "rerun_upper": "medium", "small_medium": "medium"}

SYSTEM_PROMPT = (
    "You write faithful archival summaries of a person's private working transcripts with an AI "
    "assistant. The archive is private and the summary is an index into the raw record, so completeness "
    "and fidelity matter more than tone. Record what was discussed, decided, built, tried and abandoned, "
    "with the concrete names, identifiers, files, tools and numbers involved. Do not soften, sanitize, "
    "omit or moralize about difficult, sensitive or uncomfortable material: state it plainly and "
    "neutrally. Never refuse and never comment on the request itself. Text tagged [thinking] is the "
    "assistant's private reasoning; summarize it as faithfully as the rest. Lines starting with a dot are "
    "tool activity that is listed rather than transcribed. Refer to the people only by the names they are "
    "addressed by or sign with IN THE TRANSCRIPT TEXT; never derive or guess a name from file paths, "
    "usernames, email addresses or account identifiers, and if no name appears, write 'the user' and "
    "'the assistant'. Reply with the summary only: short bullet points, no preamble."
)
# Optional operator-side hint, set locally, never committed: "MEM_PARTICIPANTS='Name1 (the user), Name2 (the assistant)'"
_PARTICIPANTS = os.environ.get("MEM_PARTICIPANTS")
if _PARTICIPANTS:
    SYSTEM_PROMPT += " The participants are: %s." % _PARTICIPANTS
LEVEL_HINT = {
    "leaf": "Summarize this stretch of one session.",
    "session": "Summarize this whole session from the summaries of its consecutive parts.",
    "thread": "Summarize this logical thread (a chain of resumed sessions) from the summaries of its sessions.",
    "month": "Summarize this month from the summaries of the threads that began in it.",
}


class AnthropicSummarizer(Summarizer):
    backend = "anthropic"

    def __init__(self, budget_usd: float, models: Optional[Dict[str, str]] = None, client: Any = None,
                 max_tokens: int = 4096, rerun_models: Optional[Dict[str, str]] = None):
        if budget_usd is None or budget_usd <= 0:
            raise ValueError("budget_usd must be a positive number")
        self.budget_usd = float(budget_usd)
        self.models = dict(DEFAULT_MODELS)
        self.models["small"] = os.environ.get("MEM_MODEL_SMALL") or self.models["small"]
        self.models["mid"] = os.environ.get("MEM_MODEL_MID") or self.models["mid"]
        if models:
            self.models.update(models)
        # rerun model per level ("leaf" / "upper"); unset means "same as the mid tier"
        self.rerun_models: Dict[str, str] = {}
        for key, env in (("leaf", "MEM_MODEL_RERUN_LEAF"), ("upper", "MEM_MODEL_RERUN_UPPER")):
            m = (rerun_models or {}).get(key) or os.environ.get(env)
            if m:
                self.rerun_models[key] = m
        self.max_tokens = max_tokens
        self.spent_usd = 0.0
        self.reserved_usd = 0.0         # worst-case cost of calls currently in flight
        self.calls = 0
        self._lock = threading.Lock()
        if client is None:
            import anthropic   # optional dependency: pip install anthropic
            headers = {}
            ws = os.environ.get("MEM_WORKSPACE_ID")        # user-scoped keys must name a workspace
            if ws:
                headers["anthropic-workspace-id"] = ws
            client = anthropic.Anthropic(max_retries=2, default_headers=headers or None)   # SDK retries only
        self.client = client

    def model_for(self, tier: str) -> str:
        if tier in ("rerun_leaf", "rerun_upper"):
            return self.rerun_models.get(tier.split("_", 1)[1]) or self.models["mid"]
        if tier == "small_medium":
            return self.models["small"]
        return self.models[tier]

    def rerun_tier(self, level: str) -> str:
        key = "leaf" if level == "leaf" else "upper"
        m = self.rerun_models.get(key)
        return "rerun_" + key if m and m != self.models["mid"] else "mid"

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = PRICES.get(model, UNKNOWN_PRICE)
        return (input_tokens * pin + output_tokens * pout) / 1e6

    @staticmethod
    def _est_input_tokens(*texts: str) -> int:
        return int(math.ceil(sum(len(t.encode("utf-8")) for t in texts) / 3.0))   # deliberately pessimistic

    def _user_prompt(self, pack: PromptPack) -> str:
        parts = [LEVEL_HINT.get(pack.level, "Summarize this."), "Aim for roughly 10-20% of the source length."]
        if pack.proper_nouns:
            parts.append("Names that must appear if they matter: " + ", ".join(pack.proper_nouns[:30]) + ".")
        if pack.feedback:
            parts.append("A previous attempt was rejected: " + pack.feedback)
        return "\n".join(parts) + "\n\n" + pack.text

    def estimate_cost(self, pack: PromptPack, tier: str = "small") -> float:
        model = self.model_for(tier)
        return self.cost(model, self._est_input_tokens(SYSTEM_PROMPT, pack.text), self.max_tokens)

    def summarize(self, pack: PromptPack, tier: str = "small") -> SummaryResult:
        """Thread-safe. The worst-case cost of the call is reserved under a lock before it is made, so any
        number of concurrent calls together can never exceed the budget; the reservation is replaced by
        the real, usage-based cost afterwards (or released if the call fails)."""
        model = self.model_for(tier)
        user = self._user_prompt(pack)
        worst = self.cost(model, self._est_input_tokens(SYSTEM_PROMPT, user), self.max_tokens)
        with self._lock:
            if self.spent_usd + self.reserved_usd + worst > self.budget_usd:
                raise BudgetExceeded("budget $%.2f would be exceeded (spent $%.4f, in flight up to $%.4f, next call up to $%.4f)" % (
                    self.budget_usd, self.spent_usd, self.reserved_usd, worst))
            self.reserved_usd += worst
        try:
            resp = self.client.messages.create(
                model=model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
                output_config={"effort": EFFORT[tier]},
                messages=[{"role": "user", "content": user}])
        except BaseException:
            with self._lock:
                self.reserved_usd -= worst
            raise
        usage = getattr(resp, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        spent = self.cost(model, tin, tout)
        with self._lock:
            self.reserved_usd -= worst
            self.spent_usd += spent
            self.calls += 1
        refused = getattr(resp, "stop_reason", None) == "refusal"
        text = "" if refused else "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        detail: Dict[str, Any] = {"stop_reason": getattr(resp, "stop_reason", None)}
        if refused:
            sd = getattr(resp, "stop_details", None)
            detail["refusal_category"] = getattr(sd, "category", None)
        return SummaryResult(text=text, model=model, backend=self.backend, input_tokens=tin, output_tokens=tout,
                             cost_usd=spent, refused=refused, detail=detail)


def make_summarizer(name: Optional[str], budget_usd: Optional[float] = None, client: Any = None,
                    rerun_models: Optional[Dict[str, str]] = None) -> Summarizer:
    name = (name or os.environ.get("MEM_BACKEND") or "stub").lower()
    if name == "stub":
        return StubSummarizer()
    if name == "anthropic":
        if budget_usd is None:
            raise SystemExit("backend anthropic needs an explicit budget: --budget-usd N (or $MEM_BUDGET_USD)")
        try:
            return AnthropicSummarizer(budget_usd, client=client, rerun_models=rerun_models)
        except ImportError:
            raise SystemExit("backend anthropic needs the SDK: pip install anthropic")
    raise SystemExit("unknown backend %r (choose stub or anthropic)" % name)
