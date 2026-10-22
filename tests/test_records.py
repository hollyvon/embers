"""Record classification: the human-prompt predicate (P0 §d.4) and block extraction."""
import pytest

from embers.ingest import parse_window
from embers.records import extract_blocks, is_human_prompt, opener_kind


def user(content, **extra):
    rec = {"type": "user", "uuid": "u", "parentUuid": None, "sessionId": "s", "timestamp": "2026-01-01T00:00:00.000Z",
           "message": {"role": "user", "content": content}}
    rec.update(extra)
    return rec


TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}

HUMAN_PROMPT_CASES = [
    # (description, record, expected)
    ("origin human, string content", user("hello", origin={"kind": "human"}), True),
    ("origin human, array text", user([{"type": "text", "text": "hello"}], origin={"kind": "human"}), True),
    ("origin human wins even for a <scheduled-task> opener",
     user("<scheduled-task id='x'>body</scheduled-task>", origin={"kind": "human"}), True),
    ("origin task-notification", user("<task-notification>x</task-notification>",
                                      origin={"kind": "task-notification", "subkind": "scheduled-trigger"}), False),
    ("origin present but kind missing", user("hello", origin={}), False),
    ("origin absent, plain string", user("hello"), True),
    ("origin absent, array text", user([{"type": "text", "text": "hello"}]), True),
    ("origin absent, tool_result block", user(TOOL_RESULT), False),
    ("origin absent, tool_result + text", user([{"type": "text", "text": "x"}] + TOOL_RESULT), False),
    ("origin absent, scheduled-task opener", user("<scheduled-task id='x'>body</scheduled-task>"), False),
    ("origin absent, scheduled-task opener with leading whitespace", user("  \n<scheduled-task>b</scheduled-task>"), False),
    ("origin absent, task-notification text", user("<task-notification>x</task-notification>"), False),
    ("origin absent, slash command meta text", user("<command-name>/compact</command-name>"), False),
    ("origin absent, local command output", user("<local-command-stdout>x</local-command-stdout>"), False),
    ("origin absent, bare system-reminder", user("<system-reminder>x</system-reminder>"), False),
    ("text that merely mentions a tag later is human", user("please read <scheduled-task> docs"), True),
    ("isMeta", user("hello", isMeta=True), False),
    ("isMeta with origin human still not a prompt", user("hello", isMeta=True, origin={"kind": "human"}), False),
    ("compaction summary", user("summary text", isCompactSummary=True), False),
    ("sidechain (subagent) user turn", user("do the thing", isSidechain=True), False),
    ("image-only prompt (no text)", user([IMAGE]), True),
    ("image + text", user([{"type": "text", "text": "see"}, IMAGE]), True),
    ("empty string content", user(""), False),
    ("whitespace-only content", user("  \n "), False),
    ("null content", user(None), False),
    ("empty array content", user([]), False),
    ("content is a number (garbage)", user(42), False),
    ("message missing", {"type": "user", "uuid": "u"}, False),
    ("message not a dict", {"type": "user", "uuid": "u", "message": "hi"}, False),
    ("assistant record", {"type": "assistant", "uuid": "u", "message": {"role": "assistant", "content": "x"}}, False),
    ("attachment record", {"type": "attachment", "uuid": "u", "attachment": {"type": "file", "text": "x"}}, False),
    ("queue-operation (queued prompt text, not a user record)",
     {"type": "queue-operation", "operation": "enqueue", "content": "hello"}, False),
    ("not a dict", "garbage", False),
]


@pytest.mark.parametrize("desc,rec,expected", HUMAN_PROMPT_CASES, ids=[c[0] for c in HUMAN_PROMPT_CASES])
def test_is_human_prompt(desc, rec, expected):
    assert is_human_prompt(rec) is expected, desc


def test_opener_kind():
    assert opener_kind(None) == "unknown"
    assert opener_kind("<scheduled-task id='1'>x") == "scheduled"
    assert opener_kind("\n <scheduled-task>") == "scheduled"
    assert opener_kind("<task-notification>") == "task-notification"
    assert opener_kind("hello") == "plain"
    assert opener_kind("") == "plain"


def test_parse_window():
    assert parse_window("[28] Fix the thing") == 28
    assert parse_window("Context window #7 — stuff") == 7
    assert parse_window("context window #12") == 12
    assert parse_window("  [3]x") == 3
    assert parse_window("Fix [28] later") is None
    assert parse_window("no number") is None
    assert parse_window(None) is None
    assert parse_window("") is None


# --------------------------------------------------------------------------- block extraction

def classes(rec):
    return [(b.cls, b.block_type) for b in extract_blocks(rec)]


def test_user_string_and_array():
    assert classes(user("hi")) == [("human", "text")]
    assert classes(user("")) == []
    assert classes(user(None)) == []
    bs = extract_blocks(user([{"type": "text", "text": "see"}, IMAGE,
                              {"type": "document", "source": {"type": "base64", "data": "QUJDRA=="}}]))
    assert [(b.cls, b.block_type) for b in bs] == [("human", "text"), ("human", "image"), ("human", "document")]
    assert bs[1].text == "" and bs[1].skipped_kind == "image" and bs[1].skipped_bytes == 4
    assert bs[2].skipped_bytes == 8
    assert bs[1].jsonpath == "message.content[1]"


def test_compaction_summary_is_summary_class():
    assert classes(user("the summary", isCompactSummary=True)) == [("summary", "text")]


def test_tool_result_string_array_and_duplicate():
    rec = user([{"type": "tool_result", "tool_use_id": "toolu_9", "is_error": True,
                 "content": [{"type": "text", "text": "line one"}, IMAGE, {"type": "tool_reference", "tool_name": "Bash"},
                             "loose string", {"type": "weird", "x": 1}]}],
               toolUseResult={"stdout": "x" * 100, "stderr": ""})
    bs = extract_blocks(rec)
    assert [(b.cls, b.block_type) for b in bs] == [("tool_result", "tool_result"), ("tool_result", "image"),
                                                   ("tool_result", "toolUseResult")]
    assert bs[0].text == "line one\n[tool_reference Bash]\nloose string\n" + '{"type": "weird", "x": 1}'
    assert bs[0].is_error is True and bs[0].tool_use_id == "toolu_9"
    assert bs[1].skipped_kind == "image"
    assert bs[2].text == "" and bs[2].skipped_kind == "toolUseResult" and bs[2].skipped_bytes > 100
    s = extract_blocks(user([{"type": "tool_result", "tool_use_id": "t", "content": "plain"}]))
    assert len(s) == 1 and s[0].text == "plain" and s[0].is_error is None
    n = extract_blocks(user([{"type": "tool_result", "tool_use_id": "t", "content": None}]))
    assert len(n) == 1 and n[0].text == ""
    e = extract_blocks(user([{"type": "tool_result", "tool_use_id": "t", "content": ""}]))
    assert len(e) == 1 and e[0].text == ""


def assistant(block, **extra):
    rec = {"type": "assistant", "uuid": "a", "message": {"id": "msg_1", "role": "assistant", "model": "m",
                                                          "content": [block]}}
    rec.update(extra)
    return rec


def test_assistant_blocks():
    th = extract_blocks(assistant({"type": "thinking", "thinking": "let me think", "signature": "sig" * 10}))
    assert len(th) == 1 and th[0].cls == "thinking" and th[0].text == "let me think"
    assert th[0].skipped_kind == "signature" and th[0].skipped_bytes == 30
    th2 = extract_blocks(assistant({"type": "thinking", "thinking": "no sig"}))
    assert th2[0].skipped_kind is None and th2[0].skipped_bytes == 0
    rt = extract_blocks(assistant({"type": "redacted_thinking", "data": "abcd"}))
    assert rt[0].cls == "thinking" and rt[0].text == "" and rt[0].skipped_bytes == 4
    tx = extract_blocks(assistant({"type": "text", "text": "prose"}))
    assert [(b.cls, b.text) for b in tx] == [("assistant", "prose")]
    tu = extract_blocks(assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}))
    assert tu[0].cls == "tool_use" and tu[0].tool_name == "Bash" and tu[0].tool_use_id == "toolu_1"
    assert tu[0].text == 'Bash {"command": "ls -la"}'
    up = extract_blocks(assistant({"type": "tool_use", "id": "t", "name": "mcp__abc__run",
                                   "input": {"__unparsedToolInput": "{\"x\": 1"}}))
    assert "__unparsedToolInput" in up[0].text and up[0].tool_name == "mcp__abc__run"
    assert classes(assistant({"type": "mystery", "foo": 1})) == [("assistant", "mystery")]
    assert classes(assistant({"type": "text", "text": ""})) == []
    # string content on an assistant record (seen in older versions)
    rec = assistant({"type": "text", "text": "x"})
    rec["message"]["content"] = "plain assistant"
    assert extract_blocks(rec)[0].text == "plain assistant"
    # non-dict entries in content do not crash
    rec["message"]["content"] = ["loose", None, 5]
    assert extract_blocks(rec) == []


def test_attachment_system_summary_blocks():
    att = {"type": "attachment", "uuid": "x", "attachment": {"type": "skill_listing", "text": "T", "content": "C",
                                                              "entries": [{"name": "n", "description": "d"}, "raw"]}}
    bs = extract_blocks(att)
    assert len(bs) == 1 and bs[0].cls == "attachment" and bs[0].block_type == "skill_listing"
    assert bs[0].text == "T\nC\nn: d\nraw"
    assert extract_blocks({"type": "attachment", "uuid": "x", "attachment": "not a dict"}) == []
    assert extract_blocks({"type": "attachment", "uuid": "x", "attachment": {"type": "date_change"}}) == []
    sy = extract_blocks({"type": "system", "subtype": "api_error", "content": "failed",
                         "error": {"message": "boom", "retryAttempt": 2, "retryInMs": 500}})
    assert sy[0].cls == "system" and sy[0].block_type == "api_error" and sy[0].text == "failed\nboom"
    cb = extract_blocks({"type": "system", "subtype": "compact_boundary", "content": "compacted",
                         "compactMetadata": {"trigger": "auto", "preTokens": 10, "postTokens": 2}})
    assert cb[0].block_type == "compact_boundary" and "trigger=auto" in cb[0].text
    assert extract_blocks({"type": "system", "subtype": "informational"}) == []
    lg = extract_blocks({"type": "summary", "summary": "old style", "leafUuid": "l"})
    assert [(b.cls, b.block_type, b.text) for b in lg] == [("summary", "legacy_summary", "old style")]


def test_unknown_record_types_and_keys_tolerated():
    assert extract_blocks({"type": "queue-operation", "content": "x"}) == []
    assert extract_blocks({"type": "brand-new-type", "uuid": "u", "message": {"role": "user", "content": "kept"}})[0].cls == "human"
    assert extract_blocks({"type": "brand-new-type", "whatever": [1, 2]}) == []
    assert extract_blocks({}) == []
    rec = user("hi", xUnknown={"a": [1, {"b": None}]}, slug="x")
    assert classes(rec) == [("human", "text")]


def test_lone_surrogates_are_sanitized(tmp_path):
    """A string truncated mid-emoji decodes from JSON as a lone surrogate, which SQLite
    cannot store. sanitize() must replace it and leave valid text (incl. full emoji) alone."""
    import json
    import sqlite3
    from embers.records import sanitize

    raw = b'{"type":"last-prompt","lastPrompt":"abc \\ud83d","nested":{"ok":"\\ud83d\\ude00 fine","xs":["\\udc00"]}}'
    rec = json.loads(raw)
    with pytest.raises(UnicodeEncodeError):
        rec["lastPrompt"].encode("utf-8")
    clean = sanitize(rec)
    assert clean["lastPrompt"] == "abc �"
    assert clean["nested"]["ok"] == "\U0001f600 fine"
    assert clean["nested"]["xs"] == ["�"]
    assert clean["type"] == "last-prompt"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (clean["lastPrompt"],))   # no UnicodeEncodeError
    assert sanitize(rec) is not rec and sanitize("plain") == "plain" and sanitize(7) == 7


def test_attachment_base64_payload_is_pointer_only():
    rec = {"type": "attachment", "uuid": "x",
           "attachment": {"type": "file", "content": {"file": {"base64": "JVBERi0x" * 100}}}}
    bs = extract_blocks(rec)
    assert len(bs) == 1
    b = bs[0]
    assert b.cls == "attachment" and b.text == ""
    assert b.skipped_kind == "document" and b.skipped_bytes == 800
