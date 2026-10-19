"""Pure functions over single JSONL records: classification and block extraction.

No I/O here. Everything tolerates unknown keys, missing keys and odd value types,
because the real schema drifts across Claude Code versions (P0 §b).

Block classes (SPEC addendum + P0 §d):
    human        text typed into a user turn (string content or text blocks)
    assistant    assistant prose (text blocks)
    thinking     assistant thinking blocks -- their own class, indexed, never dropped
    tool_use     assistant tool calls (name + JSON input)
    tool_result  tool output carried in a user record
    attachment   harness-injected context (attachment records)
    system       system records (hook summaries, api errors, compaction boundaries)
    summary      compaction summaries (isCompactSummary) and legacy `summary` records
The first six are the classes the spec names; `system` and `summary` exist so that
nothing with text is dropped while keeping it out of the conversational classes.

Base64 image/document payloads and the redundant top-level `toolUseResult` are never
stored: a block with `skipped_kind` and `skipped_bytes` records a pointer only.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

CONVERSATION_TYPES = ("user", "assistant", "system", "attachment")
META_LAST_WINS = {
    # record type -> (payload key, session_meta key)
    "custom-title": ("customTitle", "custom_title"),
    "ai-title": ("aiTitle", "ai_title"),
    "last-prompt": ("lastPrompt", "last_prompt"),
    "mode": ("mode", "mode"),
    "agent-name": ("agentName", "agent_name"),
    "atis-latch": ("atis", "atis"),
}
NON_HUMAN_OPENERS = ("<scheduled-task", "<task-notification", "<command-name>", "<local-command",
                     "<system-reminder>")


class Block:
    __slots__ = ("cls", "block_type", "text", "tool_name", "tool_use_id", "is_error",
                 "skipped_kind", "skipped_bytes", "jsonpath")

    def __init__(self, cls: str, block_type: str, text: str = "", tool_name: Optional[str] = None,
                 tool_use_id: Optional[str] = None, is_error: Optional[bool] = None,
                 skipped_kind: Optional[str] = None, skipped_bytes: int = 0, jsonpath: str = ""):
        self.cls = cls
        self.block_type = block_type
        self.text = text
        self.tool_name = tool_name
        self.tool_use_id = tool_use_id
        self.is_error = is_error
        self.skipped_kind = skipped_kind
        self.skipped_bytes = skipped_bytes
        self.jsonpath = jsonpath

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Block(%s/%s %r)" % (self.cls, self.block_type, self.text[:40])


def sanitize(obj: Any) -> Any:
    """Replace lone UTF-16 surrogates (e.g. a prompt truncated mid-emoji, escaped as
    ``\\ud83d`` in the JSONL) with U+FFFD, recursively. SQLite rejects such strings
    (``UnicodeEncodeError: surrogates not allowed``). Seen once in a real corpus, in a
    ``last-prompt`` record; applied to every parsed record at the ingest boundary."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    return obj


def _s(v: Any) -> str:
    """Coerce a possibly-missing text value to str."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(v)


def _nbytes(v: Any) -> int:
    if isinstance(v, (bytes, bytearray)):
        return len(v)
    if isinstance(v, str):
        return len(v.encode("utf-8", "replace"))
    try:
        return len(json.dumps(v, ensure_ascii=False).encode("utf-8", "replace"))
    except (TypeError, ValueError):
        return len(str(v))


def record_text_of_user_content(content: Any) -> str:
    """Concatenate the text parts of a user `message.content` (string or block array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(_s(b.get("text")))
        return "\n".join(parts)
    return ""


def has_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def is_human_prompt(rec: Dict[str, Any]) -> bool:
    """P0 §d.4 predicate: is this record a prompt a person typed?

    Rules, in order:
      1. only `type:"user"` records qualify; sidechain (subagent), isMeta and
         compaction-summary records never do.
      2. if `origin` is present, `origin.kind == "human"` decides (task-notification,
         scheduled-trigger and anything else are not human).
      3. if `origin` is absent: a record carrying a tool_result block is not human;
         otherwise the text decides -- empty/null content is not a prompt, text whose
         first tag is a harness tag (<scheduled-task…>, <task-notification…>,
         <command-name>, <local-command…>, <system-reminder>) is not, everything
         else (including image-only prompts) is.
    The ambiguous cases are enumerated in tests/test_records.py.
    """
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return False
    if rec.get("isSidechain") is True or rec.get("isMeta") is True or rec.get("isCompactSummary") is True:
        return False
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    origin = rec.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    if has_tool_result(content):
        return False
    if isinstance(content, str):
        text = content
        has_media = False
    elif isinstance(content, list):
        text = record_text_of_user_content(content)
        has_media = any(isinstance(b, dict) and b.get("type") in ("image", "document") for b in content)
    else:
        return False
    stripped = text.lstrip()
    if not stripped:
        return has_media
    return not stripped.startswith(NON_HUMAN_OPENERS)


def opener_kind(first_user_text: Optional[str]) -> str:
    if first_user_text is None:
        return "unknown"
    s = first_user_text.lstrip()
    if s.startswith("<scheduled-task"):
        return "scheduled"
    if s.startswith("<task-notification"):
        return "task-notification"
    return "plain"


def _image_block(cls: str, b: Dict[str, Any], jsonpath: str) -> Block:
    src = b.get("source") if isinstance(b.get("source"), dict) else {}
    data = src.get("data")
    kind = b.get("type") or "image"
    return Block(cls, kind, "", skipped_kind=kind if src.get("type") == "base64" or data else kind,
                 skipped_bytes=_nbytes(data) if data is not None else _nbytes(b), jsonpath=jsonpath)


def extract_blocks(rec: Dict[str, Any]) -> List[Block]:
    """Turn one record into zero or more Blocks. Never raises on odd shapes."""
    t = rec.get("type")
    out: List[Block] = []
    if t == "summary":
        out.append(Block("summary", "legacy_summary", _s(rec.get("summary")), jsonpath="summary"))
        return out
    if t == "system":
        text = _s(rec.get("content"))
        sub = _s(rec.get("subtype")) or "system"
        if sub == "compact_boundary":
            cm = rec.get("compactMetadata")
            if isinstance(cm, dict):
                text = (text + " [compact trigger=%s preTokens=%s postTokens=%s]" % (
                    cm.get("trigger"), cm.get("preTokens"), cm.get("postTokens"))).strip()
        err = rec.get("error")
        if isinstance(err, dict) and err.get("message"):
            text = (text + "\n" + _s(err.get("message"))).strip()
        if text:
            out.append(Block("system", sub, text, jsonpath="content"))
        return out
    if t == "attachment":
        att = rec.get("attachment")
        if not isinstance(att, dict):
            return out
        at = _s(att.get("type")) or "attachment"
        parts = []
        for key in ("text", "content"):
            v = att.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
            elif v is not None and not isinstance(v, str):
                parts.append(_s(v))
        entries = att.get("entries")
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict):
                    parts.append(": ".join(_s(e.get(k)) for k in ("name", "description") if e.get(k)))
                else:
                    parts.append(_s(e))
        text = "\n".join(p for p in parts if p)
        if text:
            out.append(Block("attachment", at, text, jsonpath="attachment"))
        return out
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return out
    content = msg.get("content")
    role = msg.get("role") or t
    if t == "user" or role == "user":
        cls = "summary" if rec.get("isCompactSummary") is True else "human"
        if isinstance(content, str):
            if content:
                out.append(Block(cls, "text", content, jsonpath="message.content"))
        elif isinstance(content, list):
            for i, b in enumerate(content):
                jp = "message.content[%d]" % i
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if _s(b.get("text")):
                        out.append(Block(cls, "text", _s(b.get("text")), jsonpath=jp))
                elif bt in ("image", "document"):
                    out.append(_image_block(cls, b, jp))
                elif bt == "tool_result":
                    out.extend(_tool_result_blocks(b, jp))
                else:
                    out.append(Block(cls, _s(bt) or "unknown", _s(b), jsonpath=jp))
        tur = rec.get("toolUseResult")
        if tur is not None:
            out.append(Block("tool_result", "toolUseResult", "", skipped_kind="toolUseResult",
                             skipped_bytes=_nbytes(tur), jsonpath="toolUseResult"))
        return out
    if t == "assistant" or role == "assistant":
        if isinstance(content, str):
            if content:
                out.append(Block("assistant", "text", content, jsonpath="message.content"))
            return out
        if not isinstance(content, list):
            return out
        for i, b in enumerate(content):
            jp = "message.content[%d]" % i
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                if _s(b.get("text")):
                    out.append(Block("assistant", "text", _s(b.get("text")), jsonpath=jp))
            elif bt == "thinking":
                # signature is opaque; byte count only
                sig = b.get("signature")
                out.append(Block("thinking", "thinking", _s(b.get("thinking")),
                                 skipped_kind="signature" if sig else None,
                                 skipped_bytes=_nbytes(sig) if sig else 0, jsonpath=jp))
            elif bt == "redacted_thinking":
                out.append(Block("thinking", "redacted_thinking", "", skipped_kind="redacted_thinking",
                                 skipped_bytes=_nbytes(b.get("data")), jsonpath=jp))
            elif bt == "tool_use":
                name = _s(b.get("name"))
                inp = b.get("input")
                text = (name + " " + _s(inp)).strip()
                out.append(Block("tool_use", "tool_use", text, tool_name=name or None,
                                 tool_use_id=_s(b.get("id")) or None, jsonpath=jp))
            elif bt in ("image", "document"):
                out.append(_image_block("assistant", b, jp))
            else:
                out.append(Block("assistant", _s(bt) or "unknown", _s(b), jsonpath=jp))
        return out
    # unknown record type that still carries a message: keep its text, class by role
    if isinstance(content, str) and content:
        out.append(Block("human" if role == "user" else "assistant", "text", content, jsonpath="message.content"))
    return out


def _tool_result_blocks(b: Dict[str, Any], jp: str) -> List[Block]:
    out: List[Block] = []
    tid = _s(b.get("tool_use_id")) or None
    is_err = b.get("is_error")
    is_err = bool(is_err) if is_err is not None else None
    c = b.get("content")
    if isinstance(c, str):
        out.append(Block("tool_result", "tool_result", c, tool_use_id=tid, is_error=is_err, jsonpath=jp))
    elif isinstance(c, list):
        texts = []
        for j, item in enumerate(c):
            if not isinstance(item, dict):
                texts.append(_s(item))
                continue
            it = item.get("type")
            if it == "text":
                texts.append(_s(item.get("text")))
            elif it in ("image", "document"):
                out.append(_image_block("tool_result", item, "%s.content[%d]" % (jp, j)))
            elif it == "tool_reference":
                texts.append("[tool_reference %s]" % _s(item.get("tool_name")))
            else:
                texts.append(_s(item))
        out.insert(0, Block("tool_result", "tool_result", "\n".join(t for t in texts if t),
                            tool_use_id=tid, is_error=is_err, jsonpath=jp))
    elif c is None:
        out.append(Block("tool_result", "tool_result", "", tool_use_id=tid, is_error=is_err, jsonpath=jp))
    else:
        out.append(Block("tool_result", "tool_result", _s(c), tool_use_id=tid, is_error=is_err, jsonpath=jp))
    return out


def agent_id_from_tool_use_result(rec: Dict[str, Any]) -> Optional[str]:
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
        return tur["agentId"]
    return None
