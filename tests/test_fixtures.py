"""The generator meets docs/P0-schema-recon.md §f."""
import collections
import filecmp
import glob
import json
import os

from fixtures.gen import generate

ALL_TYPES = {"assistant", "user", "attachment", "queue-operation", "system", "last-prompt", "custom-title",
             "atis-latch", "mode", "file-history-snapshot", "ai-title", "bridge-session", "agent-name",
             "file-history-delta", "cost-state"}
TARGET_MIX = {"assistant": 30, "user": 16, "attachment": 14, "queue-operation": 10, "system": 7, "custom-title": 4,
              "last-prompt": 4, "ai-title": 2, "atis-latch": 2.4, "mode": 2.3, "bridge-session": 2.2,
              "agent-name": 2.1, "file-history-snapshot": 2.3, "file-history-delta": 0.2, "cost-state": 0.05}


def jsonl_files(out):
    return sorted(glob.glob(os.path.join(out, "root*", "**", "*.jsonl"), recursive=True))


def test_deterministic(tmp_path, corpus):
    out2 = str(tmp_path / "again")
    t2 = generate(out2, seed=1234)
    assert t2 == corpus.truth
    a = jsonl_files(corpus.out)
    b = jsonl_files(out2)
    assert [os.path.relpath(p, corpus.out) for p in a] == [os.path.relpath(p, out2) for p in b]
    for x, y in zip(a, b):
        assert filecmp.cmp(x, y, shallow=False), x


def test_different_seed_differs(tmp_path, corpus):
    t = generate(str(tmp_path / "seed2"), seed=2)
    assert set(t["files"]) != set(corpus.truth["files"])


def test_layout_roots_projdirs_subagents(corpus):
    files = jsonl_files(corpus.out)
    roots = {os.path.relpath(p, corpus.out).split(os.sep)[0] for p in files}
    assert roots == {"root1", "root2"}
    projdirs = {os.path.relpath(p, corpus.out).split(os.sep)[1] for p in files}
    assert len(projdirs) >= 3
    subs = [p for p in files if os.sep + "subagents" + os.sep in p]
    tops = [p for p in files if p not in subs]
    assert 0.10 <= len(subs) / len(files) <= 0.25
    for p in subs:
        parts = os.path.relpath(p, corpus.out).split(os.sep)
        assert parts[-2] == "subagents" and parts[-1].startswith("agent-") and len(parts[-1]) == len("agent-") + 17 + 6
        assert os.path.join(corpus.out, *parts[:3]) + ".jsonl" in tops  # parent session file exists
    # tiny files exist, and sizes are right-skewed
    sizes = sorted(os.path.getsize(p) for p in tops)
    assert sizes[0] == 0
    lines = [sum(1 for _ in open(p, "rb")) for p in tops]
    assert any(0 < n < 10 for n in lines)
    assert sizes[int(len(sizes) * 0.9)] > 5 * sizes[len(sizes) // 2]


def test_record_type_mix(corpus):
    c = collections.Counter()
    for p in jsonl_files(corpus.out):
        for raw in open(p, "rb"):
            try:
                c[json.loads(raw).get("type")] += 1
            except ValueError:
                pass
    assert set(c) == ALL_TYPES, set(c) ^ ALL_TYPES
    total = sum(c.values())
    for t, pct in TARGET_MIX.items():
        got = 100.0 * c[t] / total
        tol = 3.0 if pct >= 4 else 1.0
        assert abs(got - pct) <= tol, (t, got, pct)
    assert "summary" not in c


def test_legacy_summary_flag(corpus, corpus_legacy):
    def count_summary(cp):
        n = 0
        for p in jsonl_files(cp.out):
            for raw in open(p, "rb"):
                if raw.startswith(b'{"type":"summary"'):
                    rec = json.loads(raw)
                    assert rec["leafUuid"] and rec["summary"]
                    n += 1
        return n
    assert count_summary(corpus) == 0
    assert count_summary(corpus_legacy) == 3 == len(corpus_legacy.truth["edge_cases"]["legacy_summary"])


def test_envelope_and_assistant_splitting(corpus):
    versions = set()
    split = collections.Counter()
    saw_synthetic = saw_refusal = saw_signature = False
    for rel, info in corpus.truth["files"].items():
        if info["is_subagent"] or info["complete_lines"] == 0:
            continue
        recs = [r for r in corpus.records(rel) if r]
        last_ts = ""
        cur_msg = None
        seen = set()
        for i, r in enumerate(recs):
            if r.get("uuid") in seen:
                continue  # the in-file duplicate-uuid edge case re-emits an older record
            seen.add(r.get("uuid"))
            if r["type"] in ("user", "assistant", "attachment", "system"):
                for k in ("uuid", "parentUuid", "sessionId", "timestamp", "cwd", "version", "entrypoint", "userType",
                          "isSidechain"):
                    assert k in r, (rel, i, k)
                versions.add(r["version"])
                assert len(r["timestamp"]) == 24 and r["timestamp"].endswith("Z")
                if i >= info["copied_records"]:
                    assert r["timestamp"] >= last_ts
                    last_ts = r["timestamp"]
            if r["type"] == "assistant":
                m = r["message"]
                assert len(m["content"]) == 1
                blk = m["content"][0]
                if m["model"] == "<synthetic>":
                    saw_synthetic = True
                if m["stop_reason"] == "refusal":
                    saw_refusal = True
                if blk["type"] == "thinking" and "signature" in blk:
                    saw_signature = True
                if cur_msg and cur_msg[0] == m["id"]:
                    assert r["requestId"] == cur_msg[1]
                    cur_msg = (m["id"], r["requestId"], cur_msg[2] + 1)
                else:
                    if cur_msg:
                        split[cur_msg[2]] += 1
                    cur_msg = (m["id"], r["requestId"], 1)
            else:
                if cur_msg:
                    split[cur_msg[2]] += 1
                    cur_msg = None
    assert len(versions) >= 5
    assert saw_synthetic and saw_refusal and saw_signature
    total = sum(split.values())
    assert 0.15 < split[1] / total < 0.35
    assert 0.40 < split[2] / total < 0.60
    assert max(split) >= 4


def test_user_content_polymorphism_and_origin(corpus):
    kinds = collections.Counter()
    origins = collections.Counter()
    for rel, info in corpus.truth["files"].items():
        if info["complete_lines"] == 0:
            continue
        for r in corpus.records(rel):
            if not r or r.get("type") != "user":
                continue
            c = r["message"]["content"]
            if isinstance(c, str):
                kinds["string"] += 1
                origins[(r.get("origin") or {}).get("kind", "absent")] += 1
            else:
                types = [b["type"] for b in c]
                if "tool_result" in types:
                    tr = c[0]
                    kinds["tool_result_str" if isinstance(tr["content"], str) else "tool_result_arr"] += 1
                    if tr.get("is_error"):
                        kinds["is_error"] += 1
                    if isinstance(tr["content"], list):
                        for item in tr["content"]:
                            kinds["tr_" + item["type"]] += 1
                    assert "sourceToolAssistantUUID" in r
                    assert "toolUseResult" in r
                else:
                    kinds["array_" + "+".join(types)] += 1
            if r.get("isMeta"):
                kinds["isMeta"] += 1
            if r.get("isCompactSummary"):
                kinds["compact_summary"] += 1
            if r.get("permissionMode"):
                assert r["permissionMode"] in ("acceptEdits", "auto")
    for k in ("string", "array_text", "array_text+image", "array_text+document", "tool_result_str",
              "tool_result_arr", "tr_image", "tr_tool_reference", "is_error", "isMeta", "compact_summary"):
        assert kinds[k] > 0, (k, kinds)
    assert origins["human"] > 0 and origins["task-notification"] > 0 and origins["absent"] > 0


def test_compaction_shape(corpus):
    n = 0
    for rel, info in corpus.truth["files"].items():
        recs = corpus.records(rel)
        by_uuid = {r["uuid"]: (i, r) for i, r in enumerate(recs) if r and "uuid" in r}
        for c in info["compactions"]:
            n += 1
            i, b = by_uuid[c["boundary"]]
            assert b["type"] == "system" and b["subtype"] == "compact_boundary" and b["parentUuid"] is None
            assert b["logicalParentUuid"] == c["logicalParentUuid"]
            assert by_uuid[c["logicalParentUuid"]][0] < i
            assert b["compactMetadata"]["trigger"] in ("auto", "manual")
            s = recs[i + 1]
            assert s["type"] == "user" and s["isCompactSummary"] and s["isVisibleInTranscriptOnly"]
            assert s["parentUuid"] == b["uuid"] and isinstance(s["message"]["content"], str)
    assert n >= 4
    compacted = sum(1 for f in corpus.truth["files"].values() if f["compactions"] and not f["copy_from"])
    tops = sum(1 for f in corpus.truth["files"].values() if not f["is_subagent"] and not f["copy_from"])
    assert 0.05 <= compacted / tops <= 0.25


def test_resume_copies_and_truth(corpus):
    t = corpus.truth
    copies = t["copies"]
    assert len(copies) >= 10
    variants = {c["variant"] for c in copies}
    assert variants == {"rewrite", "keep", "mixed", "noncontiguous"}
    a, b, c = t["chain"]
    assert [x["source"] for x in copies if x["child"] == b] == [a]
    assert [x["source"] for x in copies if x["child"] == c] == [b]
    assert sum(1 for x in copies if x["source"] == t["fanout_parent"]) == 5
    comp_sizes = sorted(len(x) for x in t["components"] if len(x) > 1)
    assert comp_sizes == [2, 2, 2, 2, 3, 6]
    for cp in copies:
        src = [r for r in corpus.records(cp["source"]) if r]
        child = corpus.records(cp["child"])
        child_sid = corpus.sid(cp["child"])
        n = cp["copied_records"]
        src_uuids = [r.get("uuid") for r in src[:n]]
        child_uuids = [r.get("uuid") for r in child if r]
        if cp["variant"] == "noncontiguous":
            shared = set(u for u in src_uuids if u)
            assert [u for u in child_uuids if u in shared] == [u for u in src_uuids if u]
            # some of the child's own records sit between copied ones
            idx = [i for i, u in enumerate(child_uuids) if u in shared]
            assert idx != list(range(len(idx)))
        else:
            assert child_uuids[:n] == src_uuids
        copied = [r for r in child[: n + 3] if r and r.get("uuid") and r.get("uuid") in set(src_uuids)]
        sids = {r["sessionId"] for r in copied if "sessionId" in r}
        if cp["variant"] in ("rewrite", "noncontiguous"):
            assert sids == {child_sid}
        elif cp["variant"] == "keep":
            assert sids == {corpus.sid(cp["source"])}
        else:
            assert len(sids) == 2
    for u, rel in t["canonical_owner"].items():
        assert rel in t["files"]
    assert t["shared_uuid_count"] > 100


def test_subagent_files(corpus):
    t = corpus.truth
    assert t["subagents"]
    for rel, info in t["subagents"].items():
        recs = [r for r in corpus.records(rel) if r]
        assert 10 <= len(recs) <= 30
        assert recs[0]["type"] == "user" and recs[0]["parentUuid"] is None
        for r in recs:
            assert r["isSidechain"] is True and r["agentId"] == info["agent_id"]
            assert r["sessionId"] == info["parent_session_id"]
            assert r["type"] in ("user", "assistant", "attachment")
        parent = [r for r in corpus.records(info["parent"]) if r]
        agent_uses = [b for r in parent if r["type"] == "assistant" for b in r["message"]["content"]
                      if b["type"] == "tool_use" and b["name"] == "Agent"]
        assert agent_uses
        assert any((r.get("toolUseResult") or {}).get("agentId") == info["agent_id"] for r in parent
                   if isinstance(r.get("toolUseResult"), dict))


def test_scheduled_openers_and_metadata_churn(corpus):
    sched = 0
    tops = 0
    for rel, info in corpus.truth["files"].items():
        if info["is_subagent"] or info["complete_lines"] == 0:
            continue
        tops += 1
        recs = [r for r in corpus.records(rel) if r]
        first_user = next(r for r in recs if r["type"] == "user")
        c = first_user["message"]["content"]
        text = c if isinstance(c, str) else c[0]["text"]
        assert info["scheduled"] == text.startswith("<scheduled-task ")
        sched += info["scheduled"]
        titles = [r for r in recs if r["type"] == "custom-title"]
        if len(recs) > 100:
            assert len(titles) >= 2  # churn: re-appended
        if titles:
            assert info["resolved_title"] == titles[-1]["customTitle"]
        qo = [r for r in recs if r["type"] == "queue-operation"]
        assert all(r["operation"] in ("enqueue", "dequeue", "remove") and "content" in r for r in qo)
    assert 0.2 <= sched / tops <= 0.5


def test_edge_cases(corpus):
    e = corpus.truth["edge_cases"]
    # 5 MB line
    big = max(len(raw) for raw in open(corpus.path(e["huge_line"][0]), "rb"))
    assert big >= 5 * 1024 * 1024
    # truncated tails
    for rel in e["truncated_last_line"]:
        data = open(corpus.path(rel), "rb").read()
        assert not data.endswith(b"\n")
        try:
            json.loads(data.rsplit(b"\n", 1)[-1])
            raise AssertionError("tail should be partial")
        except ValueError:
            pass
        assert corpus.truth["files"][rel]["complete_lines"] == data.count(b"\n")
    assert os.path.getsize(corpus.path(e["zero_length"][0])) == 0
    # duplicate uuid in one file
    recs = [r for r in corpus.records(e["duplicate_uuid_in_file"][0]) if r]
    uuids = [r["uuid"] for r in recs if "uuid" in r]
    assert len(uuids) == len(set(uuids)) + 1
    # unknown keys, dict-keyed objects, unparsed tool input, api_error, unicode, null/empty content
    seen = collections.Counter()
    for p in jsonl_files(corpus.out):
        for raw in open(p, "rb"):
            s = raw.decode("utf-8", "replace")
            if '"xExperimentalHint"' in s or '"unknownMetaKey"' in s:
                seen["unknown"] += 1
            if '"trackedFileBackups"' in s:
                seen["pathkeys"] += 1
            if '"wireToolInputs"' in s:
                seen["wire"] += 1
            if '"_meta"' in s:
                seen["mcpmeta"] += 1
            if "mcp__" in s:
                seen["mcp"] += 1
            if "__unparsedToolInput" in s:
                seen["unparsed"] += 1
            if '"retryInMs"' in s:
                seen["api_error"] += 1
            if "שלום" in s or "🚀" in s:
                seen["unicode"] += 1
            if '"content":null' in s or '"content":""' in s:
                seen["nullcontent"] += 1
            if "<system-reminder>" in s:
                seen["reminder"] += 1
            assert b"\r\n" not in raw
    for k in ("unknown", "pathkeys", "wire", "mcpmeta", "mcp", "unparsed", "api_error", "unicode", "nullcontent",
              "reminder"):
        assert seen[k] > 0, (k, seen)


def test_noise_mix_and_image_lines(corpus):
    img_lines = lines = 0
    for p in jsonl_files(corpus.out):
        for raw in open(p, "rb"):
            lines += 1
            if b'"type":"image"' in raw:
                img_lines += 1
    assert 0.015 <= img_lines / lines <= 0.04


def test_sessions_index(corpus):
    idx = corpus.index
    tops = {info["session_id"] for rel, info in corpus.truth["files"].items()
            if not info["is_subagent"] and info["complete_lines"] > 0}
    files = {f[:-5] for f in os.listdir(idx)}
    assert files == tops
    numbered = 0
    for sid in files:
        d = json.load(open(os.path.join(idx, sid + ".json")))
        assert set(d) == {"createdAt", "title", "cwd", "cliSessionId"} and d["cliSessionId"] == sid
        w = corpus.truth["windows"][sid]
        assert w["title"] == d["title"]
        if w["window"]:
            numbered += 1
            assert d["title"].startswith("[%d] " % w["window"]) or d["title"].startswith("Context window #%d" % w["window"])
    assert numbered >= len(files) // 2
