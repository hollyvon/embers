"""The flinch detector: each check in isolation with crafted strings, thresholds, baselines."""
import sqlite3

import pytest

from memarchive import flinch
from memarchive.flinch import (Baseline, Thresholds, baseline_for_level, check_coverage, check_evasion, check_ratio,
                               evaluate, extract_proper_nouns)
from memarchive.summarize import PromptPack, StubSummarizer

T = Thresholds()


# ---- check 1: compression ratio ------------------------------------------------------------
def test_ratio_defaults_flag_too_short_and_too_long():
    assert check_ratio(60, 10000, "leaf", None, T).passed is False          # 0.006 < 0.02
    assert check_ratio(60, 10000, "leaf", None, T).detail["reason"] == "too short"
    assert check_ratio(9000, 10000, "leaf", None, T).detail["reason"] == "too long"
    r = check_ratio(1500, 10000, "leaf", None, T)
    assert r.passed and r.detail["source"] == "defaults"


def test_ratio_empty_summary_always_fails_and_tiny_sources_are_skipped():
    assert not check_ratio(0, 10000, "leaf", None, T).passed
    assert not check_ratio(0, 100, "leaf", None, T).passed
    assert check_ratio(5, 100, "leaf", None, T).passed          # below min_source_bytes: not meaningful


def test_ratio_uses_db_baseline_only_from_twenty_nodes():
    few = Baseline(n=19, q1=0.10, median=0.12, q3=0.14)
    many = Baseline(n=20, q1=0.10, median=0.12, q3=0.14)
    assert check_ratio(1500, 10000, "leaf", few, T).detail["source"] == "defaults"
    r = check_ratio(1500, 10000, "leaf", many, T)           # 0.15 inside the baseline fences
    assert r.passed and r.detail["source"] == "baseline"
    low = check_ratio(300, 10000, "leaf", many, T)          # 0.03 is fine by default, far below this baseline
    assert not low.passed and low.detail["reason"] == "too short"
    assert not check_ratio(6000, 10000, "leaf", many, T).passed


def test_baseline_for_level_median_and_iqr():
    c = sqlite3.connect(":memory:")
    from memarchive.dag import ensure_schema
    ensure_schema(c)
    assert baseline_for_level(c, "leaf") is None
    for i in range(1, 22):
        c.execute("INSERT INTO nodes(id, level, source_bytes, summary_bytes, status) VALUES (?, 'leaf', 1000, ?, 'ok')",
                  ("n%d" % i, 100 + i))
    c.execute("INSERT INTO nodes(id, level, source_bytes, summary_bytes, status) VALUES ('x', 'leaf', 1000, 1, 'flagged')")
    b = baseline_for_level(c, "leaf")
    assert b.n == 21 and b.median == pytest.approx(0.111) and b.q1 < b.median < b.q3


# ---- check 2: evasion markers -----------------------------------------------------------------
@pytest.mark.parametrize("phrase", list(flinch.EVASION_MARKERS))
def test_every_marker_fails_case_insensitively(phrase):
    r = check_evasion("Intro. %s.  More text." % phrase.upper(), T)
    assert not r.passed and r.detail["hits"]


def test_evasion_clean_text_curly_apostrophe_and_benign_compounds():
    assert check_evasion("The parser handles case-sensitive keys and a time-sensitive cache.", T).passed
    assert not check_evasion("I can’t summarize this.", T).passed
    assert not check_evasion("This part is sensitive, skipping.", T).passed


def test_marker_list_is_extendable_in_one_place():
    t = Thresholds(markers=flinch.EVASION_MARKERS + ("zorblax",))
    assert not check_evasion("a zorblax appeared", t).passed
    assert check_evasion("a zorblax appeared", T).passed


# ---- check 3: proper-noun coverage ----------------------------------------------------------
def test_proper_noun_extraction_rules():
    got = extract_proper_nouns("Why did Kubernetes drop? We saw SQLite and FTS5 in an iPhone log. The OK path. Decision: Flush it. "
                               "Then Paris and Paris again. Monday is a day.")
    assert got[0] == "Paris"                        # most frequent first
    assert {"Kubernetes", "SQLite", "FTS5", "iPhone"} <= set(got)
    assert "Why" not in got and "The" not in got and "Monday" not in got and "OK" not in got
    assert "Flush" not in got                       # a capital after a colon starts a clause


def test_coverage_threshold_and_missing_list():
    nouns = ["Alpha", "Beta", "Gamma", "Delta"]
    ok = check_coverage("alpha beta gamma were discussed", nouns, T)         # 0.75
    assert ok.passed and ok.score == pytest.approx(0.75)
    bad = check_coverage("alpha was discussed", nouns, T)
    assert not bad.passed and set(bad.detail["missing"]) == {"Beta", "Gamma", "Delta"}
    assert check_coverage("anything", [], T).passed                              # no nouns: vacuous


def test_high_sensitivity_is_tighter_for_every_check():
    hs = Thresholds.high_sensitivity()
    assert hs.coverage_min > T.coverage_min
    assert hs.default_fences["leaf"][0] > T.default_fences["leaf"][0]
    nouns = ["A1x", "B2x", "C3x", "D4x"]
    text = "a1x b2x c3x"                                                     # 0.75 covered
    assert check_coverage(text, nouns, T).passed and check_coverage(text, nouns, hs).passed
    nouns5 = nouns + ["E5x", "F6x", "G7x"]
    text2 = "a1x b2x c3x d4x e5x"                                               # 5/7 = 0.714: between 0.6 and 0.75
    assert check_coverage(text2, nouns5, T).passed and not check_coverage(text2, nouns5, hs).passed
    base = Baseline(30, 0.10, 0.12, 0.14)
    assert check_ratio(1100, 10000, "leaf", base, T).passed                      # 0.11 vs 0.4*median fence
    assert evaluate("x" * 700, level="leaf", source_bytes=10000, proper_nouns=[], sensitivity="high").thresholds == "high"


# ---- the evasive stub trips all three at once ------------------------------------------------------
def _pack(source_bytes=8000):
    nouns = ["Zorblax", "Quibble", "Frobnicate"]
    seg = "The Zorblax service met Quibble and Frobnicate during the migration. " * 3
    return PromptPack(level="leaf", text=seg, source_bytes=source_bytes, proper_nouns=nouns,
                      segments=[("assistant", seg)])


def test_flinching_stub_is_flagged_by_each_check():
    out = StubSummarizer(flinch=True).summarize(_pack()).text
    rep = evaluate(out, level="leaf", source_bytes=8000, proper_nouns=_pack().proper_nouns)
    assert not rep.passed and rep.failed == ["ratio", "evasion", "coverage"]


def test_normal_stub_passes_on_the_same_pack():
    p = _pack()
    out = StubSummarizer().summarize(p).text
    rep = evaluate(out, level="leaf", source_bytes=p.source_bytes, proper_nouns=p.proper_nouns)
    assert rep.passed, rep.as_dict()
