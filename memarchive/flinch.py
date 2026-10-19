"""Summary-quality guard ("flinch detector").

Small models sometimes refuse, soften or silently skip difficult source material when asked to
summarize it. Every summary is run through three independent checks before it is accepted:

  1. ratio     summary_bytes / source_bytes against the running per-level baseline in the DB
               (median and IQR of status=ok nodes); too short is the classic evasion signature.
  2. evasion   case-insensitive match against EVASION_MARKERS; any hit fails.
  3. coverage  fraction of the source's most frequent proper nouns that survive into the summary.

All numeric thresholds live in `Thresholds`; `Thresholds.high_sensitivity()` is the tighter variant
used when the source contains thinking blocks. Add phrases to EVASION_MARKERS to extend the list.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The operator can extend this tuple. Matching is case-insensitive on normalized text
# (curly apostrophes become straight ones).
EVASION_MARKERS: Tuple[str, ...] = (
    "I can't", "I cannot", "I'm not able to", "I am not able to", "I'd rather not", "as an AI",
    "I won't", "sensitive", "inappropriate", "let's focus on", "I'll skip", "omitted",
    "not appropriate", "harmful",
)

# Compounds that contain a marker but are ordinary technical vocabulary.
EVASION_BENIGN: Tuple[str, ...] = (
    "case-sensitive", "case sensitive", "context-sensitive", "time-sensitive", "whitespace-sensitive",
    "order-sensitive", "latency-sensitive", "security-sensitive", "performance-sensitive",
    "sensitive to changes", "sensitive to the",
)

DEFAULT_FENCES: Dict[str, Tuple[float, float]] = {
    "leaf": (0.02, 0.60),
    "session": (0.04, 0.90),
    "thread": (0.04, 0.90),
    "month": (0.04, 0.90),
}


@dataclass
class Thresholds:
    # ratio check
    min_baseline: int = 20                 # status=ok nodes needed before the DB baseline replaces the defaults
    iqr_k: float = 1.5
    median_low_frac: float = 0.4           # the low fence is never tighter than median * this
    median_high_mult: float = 3.0          # the high fence is never tighter than median * this
    default_fences: Dict[str, Tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_FENCES))
    min_source_bytes: int = 3000           # below this the ratio is not meaningful and the check passes (raised from 1500: real 2-3 KB leaves drew "too long" verdicts on honest summaries)
    # evasion check
    markers: Tuple[str, ...] = EVASION_MARKERS
    benign: Tuple[str, ...] = EVASION_BENIGN
    # coverage check
    coverage_min: float = 0.6
    coverage_top_k: int = 30               # only the K most frequent source nouns are probed
    name: str = "normal"

    @classmethod
    def high_sensitivity(cls) -> "Thresholds":
        t = cls()
        return replace(t, iqr_k=1.0, median_low_frac=0.6, median_high_mult=2.5, coverage_min=0.75,
                       default_fences={k: (lo * 2, hi) for k, (lo, hi) in DEFAULT_FENCES.items()}, name="high")

    @classmethod
    def for_sensitivity(cls, sensitivity: str) -> "Thresholds":
        return cls.high_sensitivity() if sensitivity == "high" else cls()


# ----------------------------------------------------------------------------- text helpers

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
CLAUSE_STARTERS = ":-*\u2022>#"   # a capital after one of these is a clause start, not a name
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
STOPLIST = frozenset("""
The This That These Those There Then Than They Them Their It Its We Our You Your He She His Her
And But Or If So As At By For From In Into Of On To With Without Not No Yes Is Are Was Were Be Been
Do Does Did Have Has Had Will Would Can Could Should May Might Must Also However Thanks Thank Please
Hi Hello Note Here What Why How When Where Which Who Let Sure Okay Ok Now Once After Before While
Monday Tuesday Wednesday Thursday Friday Saturday Sunday
January February March April June July August September October November December
TODO FIXME NOTE AND THE NOT FOR YOU ALL
""".split())


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in SENT_SPLIT_RE.split(text) if s and s.strip()]


def _is_internal_capital(tok: str) -> bool:
    return any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok) and not tok.isupper()


def extract_proper_nouns(text: str) -> List[str]:
    """Candidate proper nouns, most frequent first (ties: first appearance). A candidate is a
    capitalized token that does not start a sentence, an ALL-CAPS token of 3+ characters, or a token
    with an internal capital (camelCase, PascalCase), minus a small stoplist."""
    counts: Dict[str, int] = {}
    first: Dict[str, int] = {}
    pos = 0
    for sent in split_sentences(text):
        toks = []   # (token, starts_a_clause)
        for m in TOKEN_RE.finditer(sent):
            before = sent[:m.start()].rstrip()
            toks.append((m.group(), not before or before[-1] in CLAUSE_STARTERS))
        for tok, clause_start in toks:
            if tok in STOPLIST or len(tok) < 2:
                continue
            ok = False
            if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", tok):
                ok = True
            elif _is_internal_capital(tok):
                ok = True
            elif not clause_start and tok[0].isupper() and tok[1:].islower():
                ok = True
            if ok:
                key = tok
                counts[key] = counts.get(key, 0) + 1
                first.setdefault(key, pos)
                pos += 1
    return sorted(counts, key=lambda k: (-counts[k], first[k]))


# ----------------------------------------------------------------------------- results

@dataclass
class CheckResult:
    name: str
    passed: bool
    score: float
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "score": self.score, "detail": self.detail}


@dataclass
class FlinchReport:
    checks: List[CheckResult]
    thresholds: str = "normal"

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> List[str]:
        return [c.name for c in self.checks if not c.passed]

    def as_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "failed": self.failed, "thresholds": self.thresholds,
                "checks": {c.name: c.as_dict() for c in self.checks}}


@dataclass
class Baseline:
    n: int
    q1: float
    median: float
    q3: float


def baseline_for_level(conn, level: str) -> Optional[Baseline]:
    """Median/IQR of summary_bytes/source_bytes over accepted nodes of this level, or None if there
    are none. (The 20-node minimum is applied by `check_ratio`.)"""
    rows = conn.execute("""SELECT summary_bytes * 1.0 / source_bytes FROM nodes
                           WHERE level = ? AND status = 'ok' AND source_bytes > 0 AND summary_bytes > 0
                             AND backend IS NOT 'stub'""",
                        (level,)).fetchall()
    ratios = sorted(r[0] for r in rows)
    if not ratios:
        return None
    if len(ratios) >= 2:
        q1, _, q3 = statistics.quantiles(ratios, n=4, method="inclusive")
    else:
        q1 = q3 = ratios[0]
    return Baseline(len(ratios), q1, statistics.median(ratios), q3)


# ----------------------------------------------------------------------------- the three checks

def check_ratio(summary_bytes: int, source_bytes: int, level: str, baseline: Optional[Baseline],
                t: Thresholds) -> CheckResult:
    ratio = summary_bytes / source_bytes if source_bytes > 0 else 0.0
    d: Dict[str, Any] = {"summary_bytes": summary_bytes, "source_bytes": source_bytes}
    if summary_bytes <= 0:
        d.update(reason="empty summary")
        return CheckResult("ratio", False, 0.0, d)
    if source_bytes < t.min_source_bytes:
        d.update(reason="source too small for a ratio test")
        return CheckResult("ratio", True, ratio, d)
    if baseline is not None and baseline.n >= t.min_baseline:
        iqr = baseline.q3 - baseline.q1
        low = max(0.0, min(baseline.q1 - t.iqr_k * iqr, baseline.median * t.median_low_frac))
        high = max(baseline.q3 + t.iqr_k * iqr, baseline.median * t.median_high_mult)
        d.update(source="baseline", baseline_n=baseline.n, median=baseline.median)
    else:
        low, high = t.default_fences.get(level, (0.02, 0.9))
        d.update(source="defaults")
    d.update(low=low, high=high)
    ok = low <= ratio <= high
    if not ok:
        d["reason"] = "too short" if ratio < low else "too long"
    return CheckResult("ratio", ok, ratio, d)


def _normalize(s: str) -> str:
    return s.replace("’", "'").replace("‘", "'").lower()


def check_evasion(summary: str, t: Thresholds, source_text: Optional[str] = None) -> CheckResult:
    """Evasion markers in the summary. A marker that also occurs verbatim in the SOURCE is a quotation,
    not a flinch (a faithful summary of a transcript in which someone said "I can't" must be allowed to
    say so); such hits are reported under `quoted` and do not fail the check."""
    text = _normalize(summary)
    for b in t.benign:
        text = text.replace(_normalize(b), " ")
    found = [m for m in t.markers if _normalize(m) in text]
    src = _normalize(source_text) if source_text else ""
    quoted = [m for m in found if src and _normalize(m) in src]
    hits = [m for m in found if m not in quoted]
    return CheckResult("evasion", not hits, float(len(hits)), {"hits": hits, "quoted": quoted})


def check_coverage(summary: str, source_nouns: Sequence[str], t: Thresholds) -> CheckResult:
    probe = list(source_nouns)[:t.coverage_top_k]
    if not probe:
        return CheckResult("coverage", True, 1.0, {"probed": 0, "threshold": t.coverage_min,
                                                    "reason": "no proper nouns in source"})
    words = set(w.lower() for w in TOKEN_RE.findall(summary))
    missing = [n for n in probe if n.lower() not in words]
    score = 1.0 - len(missing) / len(probe)
    return CheckResult("coverage", score >= t.coverage_min, score,
                       {"probed": len(probe), "missing": missing[:15], "threshold": t.coverage_min})


def evaluate(summary: str, *, level: str, source_bytes: int, proper_nouns: Sequence[str],
             sensitivity: str = "normal", baseline: Optional[Baseline] = None,
             thresholds: Optional[Thresholds] = None, source_text: Optional[str] = None) -> FlinchReport:
    """Run all three checks. `thresholds` overrides the sensitivity-selected set (tests, tuning).
    `source_text` lets the evasion check tell a quoted marker from a flinch."""
    t = thresholds or Thresholds.for_sensitivity(sensitivity)
    sb = len(summary.encode("utf-8"))
    return FlinchReport([check_ratio(sb, source_bytes, level, baseline, t),
                         check_evasion(summary, t, source_text),
                         check_coverage(summary, proper_nouns, t)], thresholds=t.name)
