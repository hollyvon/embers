#!/usr/bin/env python3
"""Deterministic synthetic Claude Code transcript corpus.

Implements the fixture requirements of docs/P0-schema-recon.md §f (items 1-16).
Every byte of text is invented here; nothing is copied from a real transcript.

    python -m fixtures.gen --out /tmp/fx [--seed 1234] [--legacy-summary]

Output layout:
    <out>/root1/<projdir>/<session-uuid>.jsonl
    <out>/root1/<projdir>/<session-uuid>/subagents/agent-<17hex>.jsonl
    <out>/root2/...
    <out>/sessions-index/<session-uuid>.json   (createdAt / title / cwd / cliSessionId)
    <out>/truth.json                            (ground truth for tests)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import random
import string
import uuid as _uuid
from typing import Any, Dict, List, Optional

B62 = string.ascii_letters + string.digits
B64 = string.ascii_letters + string.digits + "+/"
HEX = "0123456789abcdef"

VERSIONS = ["2.1.156", "2.1.190", "2.1.221", "2.1.247", "2.1.270", "2.1.299"]
MODELS = ["claude-fable-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
TOOLS = ["Bash", "Edit", "Read", "WebSearch", "Write", "Grep", "Glob", "MCP"]
ATTACHMENT_TYPES = [
    ("total_tokens_reminder", 77), ("task_reminder", 5), ("skill_listing", 4), ("deferred_tools_delta", 4),
    ("file", 3), ("edited_text_file", 2), ("date_change", 2), ("instructions", 2), ("hook_additional_context", 1),
]
SYSTEM_SUBTYPES = [("stop_hook_summary", 50), ("informational", 25), ("api_error", 10), ("local_command", 10),
                   ("model_refusal_no_fallback", 5)]
MODES = ["default", "plan", "acceptEdits", "auto"]
SPLIT_DIST = [(1, 24), (2, 50), (3, 23), (4, 2), (5, 0.5), (6, 0.3), (7, 0.1), (8, 0.1)]

NOUNS = ["parser", "cache", "index", "watermark", "fixture", "ledger", "beacon", "lattice", "orchard", "compass",
         "kernel", "socket", "buffer", "queue", "schema", "tokenizer", "manifest", "snapshot", "harbor", "meadow",
         "lantern", "quokka", "glacier", "saffron", "pylon", "turbine", "vellum", "zephyr", "marble", "cobalt",
         "ratchet", "ballast", "trellis", "gantry", "sextant", "anvil", "ember", "fjord", "quartz", "sprocket"]
VERBS = ["rebuild", "inspect", "stitch", "dedupe", "normalize", "retry", "flush", "compact", "resume", "index",
         "trace", "validate", "serialize", "migrate", "prune", "refactor", "benchmark", "annotate", "replay", "shard"]
ADJS = ["stale", "incremental", "canonical", "sparse", "brittle", "lazy", "amber", "quiet", "nested", "verbose",
        "orphaned", "monotonic", "frozen", "partial", "hollow", "tidy", "crooked", "spare", "pale", "dense"]
UNICODE_BITS = ["naïve café ✓", "日本語のテキスト", "emoji 🚀🧪🔍", "RTL: שלום עולם", "عربى نص", "ünïcödé — dash",
                "tab\tseparated", "multi\nline\ntext"]
FILE_STEMS = ["ingest", "cli", "schema", "fixtures", "probe", "stitch", "windows", "search", "stats", "db"]


# ----------------------------------------------------------------------------- helpers

def rng_uuid(rng: random.Random) -> str:
    return str(_uuid.UUID(int=rng.getrandbits(128), version=4))


def rng_b62(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(B62) for _ in range(n))


def rng_hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(HEX) for _ in range(n))


def weighted(rng: random.Random, table):
    vals = [v for v, _ in table]
    wts = [w for _, w in table]
    return rng.choices(vals, weights=wts, k=1)[0]


def iso(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


class Clock:
    def __init__(self, start: _dt.datetime):
        self.t = start

    def tick(self, rng: random.Random, lo: float = 0.3, hi: float = 25.0) -> None:
        self.t += _dt.timedelta(seconds=rng.uniform(lo, hi))

    def jump(self, rng: random.Random, lo_h: float, hi_h: float) -> None:
        self.t += _dt.timedelta(hours=rng.uniform(lo_h, hi_h))

    def iso(self) -> str:
        return iso(self.t)


class Lex:
    """Invented prose. Deterministic given the rng."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def word(self) -> str:
        return self.rng.choice(NOUNS)

    def sentence(self) -> str:
        r = self.rng
        tpl = r.randint(0, 4)
        if tpl == 0:
            return "%s the %s %s before the %s %ss." % (r.choice(VERBS).capitalize(), r.choice(ADJS), r.choice(NOUNS),
                                                       r.choice(NOUNS), r.choice(VERBS))
        if tpl == 1:
            return "The %s %s should %s when the %s is %s." % (r.choice(ADJS), r.choice(NOUNS), r.choice(VERBS),
                                                            r.choice(NOUNS), r.choice(ADJS))
        if tpl == 2:
            return "Why does the %s %s after we %s the %s?" % (r.choice(NOUNS), r.choice(VERBS), r.choice(VERBS),
                                                             r.choice(NOUNS))
        if tpl == 3:
            return "Decision: %s the %s, keep the %s %s." % (r.choice(VERBS), r.choice(NOUNS), r.choice(ADJS),
                                                           r.choice(NOUNS))
        return "%s-%s is %s; %s it." % (r.choice(NOUNS), r.choice(NOUNS), r.choice(ADJS), r.choice(VERBS))

    def text(self, lo: int, hi: int, exotic: float = 0.08) -> str:
        """Prose of roughly lo..hi bytes."""
        target = self.rng.randint(lo, hi)
        out: List[str] = []
        n = 0
        while n < target:
            s = self.sentence()
            roll = self.rng.random()
            if roll < exotic:
                s += " " + self.rng.choice(UNICODE_BITS)
            elif roll < exotic + 0.03:
                s += "\n<system-reminder>\nInvented reminder: %s\n</system-reminder>" % self.sentence()
            out.append(s)
            n += len(s) + 1
        return " ".join(out)

    def title(self) -> str:
        r = self.rng
        return "%s %s %s" % (r.choice(VERBS).capitalize(), r.choice(ADJS), r.choice(NOUNS))

    def b64(self, n: int) -> str:
        return "".join(self.rng.choice(B64) for _ in range(n))

    def path(self) -> str:
        return "/home/dev/projects/%s/%s.py" % (self.rng.choice(NOUNS), self.rng.choice(FILE_STEMS))


# ----------------------------------------------------------------------------- conversation builder

class Ctx:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.lex = Lex(self.rng)
        self.clock = Clock(_dt.datetime(2026, 3, 2, 8, 0, 0))
        self.origin_file: Dict[str, str] = {}   # uuid -> relpath of the file that generated it
        self.planted: List[Dict[str, Any]] = []
        self.phrase_n = 0

    def phrase(self) -> str:
        self.phrase_n += 1
        r = self.rng
        return "passphrase-%s-%s-%d" % (r.choice(ADJS), r.choice(NOUNS), self.phrase_n)


class Conv:
    """Builds the records of ONE jsonl file (a session or a subagent)."""

    def __init__(self, ctx: Ctx, relpath: str, session_id: str, cwd: str, sidechain: bool = False,
                 agent_id: Optional[str] = None, allow_meta: bool = True, plant: Optional[List[str]] = None,
                 agent_budget: int = 0, parent_dir: str = ""):
        self.ctx = ctx
        self.rng = ctx.rng
        self.lex = ctx.lex
        self.clock = ctx.clock
        self.relpath = relpath
        self.session_id = session_id
        self.cwd = cwd
        self.sidechain = sidechain
        self.agent_id = agent_id
        self.allow_meta = allow_meta
        self.plant = list(plant or [])
        self.agent_budget = agent_budget
        self.parent_dir = parent_dir  # relpath dir where subagent files go
        self.records: List[Dict[str, Any]] = []
        self.last_uuid: Optional[str] = None
        self.version = self.rng.choice(VERSIONS)
        self.git_branch = self.rng.choice(["main", "develop", "feature/%s" % self.lex.word()])
        self.slug = "%s-%s" % (self.rng.choice(ADJS), self.rng.choice(NOUNS))
        self.human_uuids: List[str] = []
        self.compactions: List[Dict[str, str]] = []
        self.subagents: List[Dict[str, Any]] = []   # {"relpath","agent_id","records"}
        self.final_title: Optional[str] = None
        self.final_last_prompt: Optional[str] = None
        self.final_mode: Optional[str] = None
        self.task_notifications = 0

    # --- envelope -------------------------------------------------------------------------
    def env(self, parent: Optional[str], root: bool = False) -> Dict[str, Any]:
        r = self.rng
        self.clock.tick(r)
        u = rng_uuid(r)
        rec: Dict[str, Any] = {
            "uuid": u,
            "parentUuid": None if root else parent,
            "sessionId": self.session_id,
            "timestamp": self.clock.iso(),
            "cwd": self.cwd,
            "gitBranch": self.git_branch,
            "version": self.version,
            "entrypoint": "claude-desktop",
            "userType": "external",
            "isSidechain": self.sidechain,
        }
        if self.sidechain:
            rec["agentId"] = self.agent_id
        if r.random() < 0.115:
            rec["slug"] = self.slug
        # schema drift: drop optional keys / add unknown keys
        if r.random() < 0.05:
            rec.pop("gitBranch", None)
        if r.random() < 0.03:
            rec["xExperimentalHint"] = {"flag": True, "note": self.lex.sentence()}
        if r.random() < 0.02:
            rec["traceSpanId"] = rng_b62(r, 12)
        self.ctx.origin_file[u] = self.relpath
        return rec

    def push(self, rec: Dict[str, Any], chain: bool = True) -> Dict[str, Any]:
        self.records.append(rec)
        if chain and "uuid" in rec:
            self.last_uuid = rec["uuid"]
        return rec

    # --- conversation records --------------------------------------------------------------
    def user_text(self, text: str, origin: Optional[Dict[str, str]], human: bool, root: bool = False,
                  with_image: bool = False, with_doc: bool = False, as_array: bool = False,
                  huge_image: bool = False, meta: bool = False) -> Dict[str, Any]:
        r = self.rng
        rec = self.env(self.last_uuid, root=root)
        rec["type"] = "user"
        if origin is not None:
            rec["origin"] = origin
        if r.random() < 0.7:
            rec["promptId"] = rng_uuid(r)
        if r.random() < 0.8:
            rec["permissionMode"] = r.choice(["acceptEdits", "auto"])
        if meta:
            rec["isMeta"] = True
        content: Any
        if as_array or with_image or with_doc:
            content = [{"type": "text", "text": text}]
            if with_image:
                n = 5 * 1024 * 1024 if huge_image else r.randint(200, 2000)
                data = self.lex.b64(4096) * 1280 if huge_image else self.lex.b64(n)
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
            if with_doc:
                content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": self.lex.b64(600)}, "title": "invented.pdf"})
        else:
            content = text
        rec["message"] = {"role": "user", "content": content}
        self.push(rec)
        if human:
            self.human_uuids.append(rec["uuid"])
        self.noise()
        return rec

    def usage(self) -> Dict[str, Any]:
        r = self.rng
        u: Dict[str, Any] = {
            "input_tokens": r.randint(1, 2000), "output_tokens": r.randint(1, 900),
            "cache_creation_input_tokens": r.randint(0, 30000), "cache_read_input_tokens": r.randint(0, 90000),
            "cache_creation": {"ephemeral_5m_input_tokens": r.randint(0, 5000), "ephemeral_1h_input_tokens": r.randint(0, 20000)},
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard", "speed": r.choice(["standard", "fast"]), "inference_geo": None,
            "fallback_credit": None,
        }
        if r.random() < 0.25:
            u["output_tokens_details"] = {"thinking_tokens": r.randint(0, 800)}
        if r.random() < 0.1:
            u["iterations"] = [{"type": "message", "input_tokens": r.randint(1, 100), "output_tokens": r.randint(1, 50)}]
        return u

    def assistant_message(self, blocks: List[Dict[str, Any]], stop: Optional[str] = "end_turn") -> List[Dict[str, Any]]:
        """One API message split into one line per block (P0 §c)."""
        r = self.rng
        msg_id = "msg_" + rng_b62(r, 24)
        req_id = "req_" + rng_b62(r, 22)
        model = "<synthetic>" if r.random() < 0.004 else r.choice(MODELS)
        if model == "<synthetic>":
            blocks = [{"type": "text", "text": "Invented harness error: %s" % self.lex.sentence()}]
            stop = None
        usage = self.usage()
        out = []
        for i, blk in enumerate(blocks):
            last = i == len(blocks) - 1
            rec = self.env(self.last_uuid)
            rec["type"] = "assistant"
            rec["requestId"] = req_id
            if r.random() < 0.3:
                rec["apiBlockIndex"] = i
            if blk["type"] == "thinking" and r.random() < 0.5:
                rec["thinkingDurationMs"] = r.randint(100, 20000)
            if r.random() < 0.1:
                rec["effort"] = r.choice(["low", "medium", "high"])
            if blk["type"] == "tool_use" and r.random() < 0.05:
                rec["wireToolInputs"] = {blk["id"]: {"raw": json.dumps(blk["input"])}}
            rec["message"] = {
                "id": msg_id, "role": "assistant", "type": "message", "model": model,
                "stop_reason": stop if last else None, "stop_sequence": None,
                "usage": usage, "content": [blk],
            }
            if r.random() < 0.2:
                rec["message"]["container"] = None
                rec["message"]["context_management"] = None
            self.push(rec)
            out.append(rec)
            self.noise()
        return out

    def make_blocks(self, tool: Optional[Dict[str, Any]], plant_class: Optional[str] = None) -> List[Dict[str, Any]]:
        r = self.rng
        n = weighted(r, SPLIT_DIST)
        blocks: List[Dict[str, Any]] = []
        if n >= 2 or r.random() < 0.4:
            th = {"type": "thinking", "thinking": self.lex.text(300, 1500)}
            if r.random() < 0.7:
                th["signature"] = rng_b62(r, 88)
            if plant_class == "thinking":
                th["thinking"] += " " + self.plant_phrase("thinking")
            blocks.append(th)
        while len(blocks) < n - (1 if tool else 0):
            if blocks and blocks[-1]["type"] == "thinking" and r.random() < 0.3:
                blocks.append({"type": "thinking", "thinking": self.lex.text(100, 600)})
            else:
                t = self.lex.text(80, 700)
                if plant_class == "assistant":
                    t += " " + self.plant_phrase("assistant")
                    plant_class = None
                blocks.append({"type": "text", "text": t})
        if tool:
            blocks.append(tool)
        if not blocks:
            blocks.append({"type": "text", "text": self.lex.text(40, 300)})
        if plant_class == "assistant":
            for b in blocks:
                if b["type"] == "text":
                    b["text"] += " " + self.plant_phrase("assistant")
                    break
        return blocks

    def plant_phrase(self, cls: str) -> str:
        p = self.ctx.phrase()
        self.ctx.planted.append({"phrase": p, "file": self.relpath, "class": cls, "session_id": self.session_id})
        return p

    def tool_use(self, name: Optional[str] = None) -> Dict[str, Any]:
        r = self.rng
        name = name or r.choice(TOOLS)
        if name == "MCP":
            name = "mcp__%s__%s" % (r.choice([rng_hex(r, 32), "notion", "browser-" + rng_hex(r, 6)]),
                                   r.choice(["search", "fetch_page", "list_items", "run_query"]))
        inp: Dict[str, Any]
        if name == "Bash":
            inp = {"command": "%s --%s %s" % (r.choice(VERBS), r.choice(ADJS), self.lex.path()), "description": self.lex.sentence()}
        elif name in ("Edit", "Write"):
            inp = {"file_path": self.lex.path(), "old_string": self.lex.sentence(), "new_string": self.lex.text(50, 400)}
        elif name in ("Read", "Grep", "Glob"):
            inp = {"file_path": self.lex.path(), "pattern": r.choice(NOUNS)}
        elif name == "Agent":
            inp = {"description": self.lex.title(), "prompt": self.lex.text(100, 500), "subagent_type": "general-purpose"}
        elif name == "WebSearch":
            inp = {"query": "%s %s" % (r.choice(ADJS), r.choice(NOUNS)), "mode": "standard"}
        else:
            inp = {"query": self.lex.sentence(), "limit": r.randint(1, 20)}
        if r.random() < 0.02:
            inp = {"__unparsedToolInput": json.dumps(inp)[:-3]}
        blk: Dict[str, Any] = {"type": "tool_use", "id": "toolu_" + rng_b62(r, 24), "name": name, "input": inp}
        if r.random() < 0.8:
            blk["caller"] = {"type": "direct"}
        return blk

    def tool_result(self, assistant_uuid: str, tool_blk: Dict[str, Any], plant_class: Optional[str] = None) -> Dict[str, Any]:
        r = self.rng
        rec = self.env(self.last_uuid)
        rec["type"] = "user"
        rec["sourceToolAssistantUUID"] = assistant_uuid
        is_err = r.random() < 0.036
        text = ("Error: %s" % self.lex.sentence()) if is_err else self.lex.text(50, 1200, exotic=0.05)
        if plant_class == "tool_result":
            text += " " + self.plant_phrase("tool_result")
        if tool_blk["name"] == "Agent":
            content: Any = [{"type": "text", "text": text}]
        elif r.random() < 0.75:
            content = text
        else:
            content = [{"type": "text", "text": text}]
            if r.random() < 0.5:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                           "data": self.lex.b64(r.randint(200, 2500))}})
            if r.random() < 0.3:
                content.append({"type": "tool_reference", "tool_name": r.choice(TOOLS[:5])})
        trb: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_blk["id"], "content": content}
        if is_err or r.random() < 0.5:
            trb["is_error"] = is_err
        if r.random() < 0.01:
            trb["content"] = None if r.random() < 0.5 else ""
        rec["message"] = {"role": "user", "content": [trb]}
        # redundant structured duplicate (P0 §e: 26% of bytes)
        dup_kind = r.random()
        if tool_blk["name"] == "Agent":
            rec["toolUseResult"] = {"agentId": tool_blk["_agent_id"], "agentType": "general-purpose",
                                    "totalTokens": r.randint(100, 50000), "totalToolUseCount": r.randint(0, 30),
                                    "toolStats": {"Read": r.randint(0, 9)}, "usage": self.usage(),
                                    "content": [{"type": "text", "text": text}]}
        elif dup_kind < 0.6:
            rec["toolUseResult"] = {"stdout": text + "\n" + self.lex.text(300, 2500), "stderr": "",
                                    "interrupted": False, "isImage": False, "returnCodeInterpretation": "ok"}
        elif dup_kind < 0.8:
            rec["toolUseResult"] = text + "\n" + self.lex.text(200, 1500)
        else:
            rec["toolUseResult"] = [{"type": "text", "text": text}]
        if r.random() < 0.03:
            rec["mcpMeta"] = {"_meta": {"x-" + rng_hex(r, 6): self.lex.sentence(), "progressToken": r.randint(1, 99)}}
        if r.random() < 0.02:
            rec["contentNotInModelContext"] = True
        self.push(rec)
        self.noise()
        return rec

    def attachment(self) -> None:
        r = self.rng
        rec = self.env(self.last_uuid)
        rec["type"] = "attachment"
        at = weighted(r, ATTACHMENT_TYPES)
        att: Dict[str, Any] = {"type": at, "timestamp": rec["timestamp"]}
        body = "Invented %s boilerplate. %s" % (at.replace("_", " "), self.lex.text(200, 1500, exotic=0.02))
        if at == "file":
            att["content"] = body
            att["filename"] = self.lex.path()
        elif at == "skill_listing":
            att["entries"] = [{"name": self.lex.word(), "description": self.lex.sentence()} for _ in range(r.randint(1, 4))]
        else:
            att["text"] = body
        if self.plant and self.plant[0] == "attachment":
            att["text"] = (att.get("text") or "") + " " + self.plant_phrase("attachment")
            self.plant.pop(0)
        rec["attachment"] = att
        self.push(rec)

    def system(self, subtype: Optional[str] = None) -> None:
        r = self.rng
        subtype = subtype or weighted(r, SYSTEM_SUBTYPES)
        rec = self.env(self.last_uuid)
        rec["type"] = "system"
        rec["subtype"] = subtype
        rec["level"] = r.choice(["suggestion", "error", "info", "notice", "warning"])
        rec["content"] = self.lex.text(20, 200)
        if subtype == "stop_hook_summary":
            rec["hookInfos"] = [{"command": "%s --check" % r.choice(VERBS)}]
            rec["hookCount"] = 1
        if subtype == "api_error":
            rec["error"] = {"status": r.choice([429, 500, 529]), "message": self.lex.sentence(),
                            "retryAttempt": r.randint(1, 5), "retryInMs": r.randint(500, 30000)}
            rec["isApiErrorMessage"] = True
        if subtype == "local_command" and r.random() < 0.5:
            rec["toolUseID"] = "toolu_" + rng_b62(r, 24)
        self.push(rec)

    def compact(self) -> None:
        r = self.rng
        prev = self.last_uuid
        rec = self.env(None, root=True)
        rec["type"] = "system"
        rec["subtype"] = "compact_boundary"
        rec["level"] = "info"
        rec["content"] = "Conversation compacted (invented)."
        rec["logicalParentUuid"] = prev
        pre = r.randint(80000, 190000)
        rec["compactMetadata"] = {"trigger": r.choice(["auto", "auto", "auto", "manual"]), "preTokens": pre,
                                  "postTokens": r.randint(5000, 30000), "durationMs": r.randint(2000, 60000),
                                  "cumulativeDroppedTokens": r.randint(0, 400000), "preservedSegment": {},
                                  "preservedMessages": {}, "preCompactDiscoveredTools": []}
        self.push(rec)
        summ = self.env(rec["uuid"])
        summ["type"] = "user"
        summ["isCompactSummary"] = True
        summ["isVisibleInTranscriptOnly"] = True
        summ["message"] = {"role": "user", "content": "Invented compaction summary. " + self.lex.text(400, 2000, exotic=0.0)}
        self.push(summ)
        self.compactions.append({"boundary": rec["uuid"], "logicalParentUuid": prev, "summary": summ["uuid"]})

    # --- metadata churn -------------------------------------------------------------------
    def meta(self, kind: str) -> None:
        r = self.rng
        sid = self.session_id
        rec: Dict[str, Any]
        if kind == "queue-operation":
            text = self.lex.text(10, 120)
            self.clock.tick(r, 0.1, 3)
            self.push({"type": "queue-operation", "operation": "enqueue", "sessionId": sid,
                       "timestamp": self.clock.iso(), "content": text}, chain=False)
            self.clock.tick(r, 0.1, 3)
            op = "remove" if r.random() < 0.1 else "dequeue"
            self.push({"type": "queue-operation", "operation": op, "sessionId": sid,
                       "timestamp": self.clock.iso(), "content": text}, chain=False)
            return
        if kind == "custom-title":
            self.final_title = self.lex.title()
            rec = {"type": "custom-title", "customTitle": self.final_title, "sessionId": sid}
        elif kind == "ai-title":
            rec = {"type": "ai-title", "aiTitle": self.lex.title(), "sessionId": sid}
        elif kind == "last-prompt":
            self.final_last_prompt = self.lex.text(10, 150)
            rec = {"type": "last-prompt", "lastPrompt": self.final_last_prompt, "sessionId": sid}
        elif kind == "mode":
            self.final_mode = r.choice(MODES)
            rec = {"type": "mode", "mode": self.final_mode, "sessionId": sid}
        elif kind == "agent-name":
            rec = {"type": "agent-name", "agentName": "%s-%s" % (r.choice(ADJS), r.choice(NOUNS)), "sessionId": sid}
        elif kind == "atis-latch":
            rec = {"type": "atis-latch", "atis": r.choice(["on", "off", "latched"]), "sessionId": sid}
        elif kind == "bridge-session":
            rec = {"type": "bridge-session", "bridgeSessionId": rng_uuid(r), "lastSequenceNum": r.randint(1, 500),
                   "ownerAccountUuid": rng_uuid(r), "ownerOrganizationUuid": rng_uuid(r)}
            if r.random() < 0.5:
                rec["sessionId"] = sid
        elif kind == "file-history-snapshot":
            mid = rng_uuid(r)
            backups = {}
            for _ in range(r.randint(1, 4)):
                backups[self.lex.path()] = {"backupFileName": rng_hex(r, 16) + ".bak", "version": r.randint(1, 9),
                                            "backupTime": self.clock.iso()}
            rec = {"type": "file-history-snapshot", "messageId": mid,
                   "snapshot": {"messageId": mid, "timestamp": self.clock.iso(), "trackedFileBackups": backups},
                   "isSnapshotUpdate": r.random() < 0.3}
            if r.random() < 0.7:
                rec["sessionId"] = sid
        elif kind == "file-history-delta":
            rec = {"type": "file-history-delta", "backup": {"backupFileName": rng_hex(r, 16) + ".bak", "version": 2,
                                                            "backupTime": self.clock.iso()},
                   "trackingPath": self.lex.path(), "snapshotMessageId": rng_uuid(r)}
        elif kind == "cost-state":
            model = r.choice(MODELS)
            rec = {"type": "cost-state", "totalCostUSD": round(r.uniform(0, 40), 4), "totalAPIDuration": r.randint(0, 900000),
                   "totalAPIDurationWithoutRetries": r.randint(0, 900000), "totalDuration": r.randint(0, 9000000),
                   "totalToolDuration": r.randint(0, 900000), "totalLinesAdded": r.randint(0, 5000),
                   "totalLinesRemoved": r.randint(0, 2000),
                   "modelUsage": {model: {"inputTokens": r.randint(0, 999999), "outputTokens": r.randint(0, 99999)}},
                   "hasUnknownModelCost": False, "startTime": self.clock.iso()}
            if r.random() < 0.5:
                rec["sessionId"] = sid
        else:
            raise ValueError(kind)
        if r.random() < 0.03:
            rec["unknownMetaKey"] = r.randint(0, 9)
        self.push(rec, chain=False)

    # per-conversation-line probabilities derived from the P0 §c mix (see module docstring in tests)
    NOISE = [("attachment", 0.30), ("system", 0.14), ("queue-operation", 0.109), ("custom-title", 0.087),
             ("last-prompt", 0.087), ("ai-title", 0.043), ("atis-latch", 0.052), ("mode", 0.05),
             ("bridge-session", 0.048), ("agent-name", 0.046), ("file-history-snapshot", 0.05),
             ("file-history-delta", 0.0045), ("cost-state", 0.0012)]

    def noise(self) -> None:
        r = self.rng
        for kind, p in self.NOISE:
            if r.random() >= p:
                continue
            if kind == "attachment":
                self.attachment()
            elif kind == "system":
                if self.allow_meta:
                    self.system()
            elif self.allow_meta:
                self.meta(kind)

    # --- turns ----------------------------------------------------------------------------
    def human_prompt(self, first: bool, scheduled: bool) -> None:
        r = self.rng
        text = self.lex.text(20, 400)
        plant = self.plant[0] if self.plant else None
        if plant == "human":
            text += " " + self.plant_phrase("human")
            self.plant.pop(0)
        origin: Optional[Dict[str, str]]
        if first and scheduled:
            text = '<scheduled-task id="task_%s" name="nightly-%s" fired-at="%s">\n%s\n</scheduled-task>' % (
                rng_b62(r, 10), self.lex.word(), self.clock.iso(), text)
            origin = {"kind": "task-notification", "subkind": "scheduled-trigger", "producer": "session-task"} \
                if r.random() < 0.5 else None
            self.user_text(text, origin, human=False, root=first)
            return
        if not first and r.random() < 0.06:
            # injected notification record, not a human prompt
            self.task_notifications += 1
            self.user_text("<task-notification>\n%s\n</task-notification>" % self.lex.text(30, 200),
                           {"kind": "task-notification", "producer": "session-task"}, human=False)
        if not first and r.random() < 0.03:
            self.user_text("<command-name>/%s</command-name>" % r.choice(VERBS), None, human=False, meta=True)
        origin = {"kind": "human"} if r.random() < 0.6 else None
        roll = r.random()
        if roll < 0.70:
            self.user_text(text, origin, human=True, root=first)
        elif roll < 0.82:
            self.user_text(text, origin, human=True, root=first, as_array=True)
        elif roll < 0.98:
            self.user_text(text, origin, human=True, root=first, with_image=True)
        else:
            self.user_text(text, origin, human=True, root=first, with_doc=True)

    def turn(self, first: bool = False, scheduled: bool = False) -> None:
        r = self.rng
        if self.sidechain and first:
            # subagent root: the prompt the parent gave it (not typed by a human)
            self.user_text(self.lex.text(100, 400), None, human=False, root=True)
        else:
            self.human_prompt(first, scheduled)
        rounds = r.choice([0, 0, 1, 1, 1, 2, 3, 4])
        for _ in range(rounds):
            name = None
            if self.agent_budget > 0 and not self.sidechain and r.random() < 0.5:
                name = "Agent"
            tb = self.tool_use(name)
            if tb["name"] == "Agent":
                self.agent_budget -= 1
                tb["_agent_id"] = rng_hex(r, 17)
            plant = self.plant[0] if self.plant else None
            lines = self.assistant_message(self.make_blocks(tb, plant if plant in ("thinking", "assistant") else None),
                                           stop="tool_use")
            if plant in ("thinking", "assistant") and self.ctx.planted and self.ctx.planted[-1]["class"] == plant:
                self.plant.pop(0)
            aid = tb.pop("_agent_id", None)
            plant = self.plant[0] if self.plant else None
            self.tool_result(lines[-1]["uuid"], dict(tb, _agent_id=aid) if aid else tb,
                             "tool_result" if plant == "tool_result" else None)
            if plant == "tool_result":
                self.plant.pop(0)
            if aid:
                self.spawn_subagent(aid)
        plant = self.plant[0] if self.plant else None
        stop = "refusal" if r.random() < 0.01 else "end_turn"
        self.assistant_message(self.make_blocks(None, plant if plant in ("thinking", "assistant") else None), stop=stop)
        if plant in ("thinking", "assistant") and self.ctx.planted and self.ctx.planted[-1]["class"] == plant:
            self.plant.pop(0)

    def spawn_subagent(self, agent_id: str) -> None:
        rel = "%s/subagents/agent-%s.jsonl" % (self.parent_dir, agent_id)
        sub = Conv(self.ctx, rel, self.session_id, self.cwd, sidechain=True, agent_id=agent_id, allow_meta=False)
        target = self.rng.randint(10, 30)
        sub.turn(first=True)
        while len(sub.records) < target:
            sub.turn()
        del sub.records[target:]  # keep within 10-30 lines
        self.subagents.append({"relpath": rel, "agent_id": agent_id, "records": sub.records})

    def build(self, target_lines: int, scheduled: bool, n_compact: int) -> None:
        self.turn(first=True, scheduled=scheduled)
        compact_at = sorted(self.rng.sample(range(max(target_lines // 4, 3), max(target_lines, 4)), k=min(n_compact, 3))) \
            if n_compact else []
        while len(self.records) < target_lines:
            if compact_at and len(self.records) >= compact_at[0]:
                compact_at.pop(0)
                self.compact()
            self.turn()
        if n_compact and len(self.compactions) < n_compact:
            self.compact()
        if self.rng.random() < 0.3 or len(self.records) < 60:
            self.meta("cost-state")


# ----------------------------------------------------------------------------- corpus planner

def write_jsonl(path: str, records: List[Dict[str, Any]], truncate_tail: bool = False) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if truncate_tail and lines:
            for ln in lines[:-1]:
                f.write(ln + "\n")
            f.write(lines[-1][: max(1, len(lines[-1]) // 2)])
            return len(lines) - 1
        for ln in lines:
            f.write(ln + "\n")
    return len(lines)


def copy_prefix(ctx: Ctx, src: List[Dict[str, Any]], n: int, new_sid: str, variant: str) -> List[Dict[str, Any]]:
    """Verbatim copy of the first n records of src (P0 §d.2)."""
    out = []
    for i, rec in enumerate(src[:n]):
        rec = json.loads(json.dumps(rec))  # deep copy, nothing shared
        if "sessionId" in rec:
            if variant == "rewrite" or (variant == "mixed" and i % 2 == 0):
                rec["sessionId"] = new_sid
        out.append(rec)
    return out


class Planner:
    def __init__(self, ctx: Ctx, out: str, legacy_summary: bool):
        self.ctx = ctx
        self.rng = ctx.rng
        self.out = out
        self.legacy = legacy_summary
        self.truth: Dict[str, Any] = {"files": {}, "planted": [], "copies": [], "components": [],
                                      "canonical_owner": {}, "edge_cases": {}, "subagents": {}}
        self.sessions: Dict[str, Dict[str, Any]] = {}   # relpath -> session info incl. records
        self.roots = {"root1": ["-home-dev-projects-alpha", "-home-dev-projects-beta", "-home-dev-work-gamma"],
                      "root2": ["-sessions-amber-fox", "-sessions-quiet-harbor"]}
        self.index: List[Dict[str, Any]] = []
        self.legacy_files: List[str] = []

    def loc(self, pref: Optional[str] = None):
        r = self.rng
        root = pref or r.choice(["root1", "root1", "root2"])
        proj = r.choice(self.roots[root])
        cwd = "/sessions/%s/" % proj[len("-sessions-"):] if root == "root2" else proj.replace("-", "/")
        return root, proj, cwd

    def new_session(self, target: int, scheduled: bool, n_compact: int = 0, plant: Optional[List[str]] = None,
                    agent_budget: int = 0, root: Optional[str] = None, copy_from: Optional[str] = None,
                    copy_n: int = 0, variant: str = "rewrite") -> str:
        r = self.rng
        self.ctx.clock.jump(r, 0.5, 30)
        rootname, proj, cwd = self.loc(root)
        if copy_from:
            # a resume/fork lands in the same project dir as its source
            rootname, proj, cwd = (self.sessions[copy_from][k] for k in ("root", "projdir", "cwd"))
        sid = rng_uuid(r)
        rel = "%s/%s/%s.jsonl" % (rootname, proj, sid)
        conv = Conv(self.ctx, rel, sid, cwd, plant=plant, agent_budget=agent_budget,
                    parent_dir="%s/%s/%s" % (rootname, proj, sid))
        copied = 0
        if copy_from:
            src = self.sessions[copy_from]
            prefix = copy_prefix(self.ctx, src["records"], copy_n, sid, variant)
            copied = len(prefix)
            conv.records = prefix
            # resume continues the chain from the last copied conversation record
            for rec in reversed(prefix):
                if "uuid" in rec:
                    conv.last_uuid = rec["uuid"]
                    break
            conv.cwd = src["cwd"]
            conv.compactions = [c for c in src["compactions"] if any(x.get("uuid") == c["boundary"] for x in prefix)]
            if variant == "noncontiguous":
                # interleave a few new metadata records inside the copied prefix
                for k in sorted(r.sample(range(1, max(2, copied)), k=min(3, max(1, copied - 1))), reverse=True):
                    conv.records.insert(k, {"type": "custom-title", "customTitle": self.ctx.lex.title(), "sessionId": sid})
                    conv.final_title = conv.records[k]["customTitle"]
            self.truth["copies"].append({"child": rel, "source": copy_from, "copied_records": copied, "variant": variant})
        conv.build(copied + target, scheduled and not copy_from, n_compact)
        self.sessions[rel] = {"relpath": rel, "session_id": sid, "root": rootname, "projdir": proj, "cwd": cwd,
                              "records": conv.records, "conv": conv, "compactions": conv.compactions,
                              "copied": copied, "scheduled": scheduled and not copy_from, "copy_from": copy_from}
        for sub in conv.subagents:
            self.sessions[sub["relpath"]] = {"relpath": sub["relpath"], "session_id": sid, "root": rootname,
                                             "projdir": proj, "cwd": cwd, "records": sub["records"], "conv": None,
                                             "compactions": [], "copied": 0, "scheduled": False, "copy_from": None,
                                             "subagent_of": rel, "agent_id": sub["agent_id"]}
        return rel

    def plan(self) -> None:
        r = self.rng

        def size() -> int:
            return max(12, min(2400, int(60 * math.exp(r.gauss(0, 1.9)))))

        plants = [["human", "assistant"], ["thinking"], ["tool_result"], ["attachment"]]
        originals: List[str] = []
        # 28 original sessions; ~35% scheduled; ~10% compacted; subagents from a handful of parents
        for i in range(28):
            scheduled = r.random() < 0.35
            n_compact = r.randint(1, 3) if i in (0, 5, 11, 19) else 0
            plant = plants.pop(0) if plants else None
            agent_budget = 2 if i in (2, 7, 13, 21) else 0
            target = r.randint(5, 9) if i in (3, 9, 17) else size()
            originals.append(self.new_session(target, scheduled, n_compact, plant, agent_budget))
        # resume/fork chain A -> B -> C (C copies B's whole content, which includes A's prefix)
        a = originals[0]
        n_a = len(self.sessions[a]["records"])
        b = self.new_session(size(), False, 0, copy_from=a, copy_n=max(5, min(n_a, r.randint(20, 270))))
        c = self.new_session(size(), False, 0, copy_from=b, copy_n=len(self.sessions[b]["records"]))
        # fan-out: one parent copied by 5 children (one keeps the original sessionId, one mixes)
        p = originals[1]
        n_p = len(self.sessions[p]["records"])
        for k in range(5):
            variant = {0: "keep", 1: "mixed"}.get(k, "rewrite")
            self.new_session(size(), False, 0, copy_from=p, copy_n=max(5, min(n_p, r.randint(20, 270))), variant=variant)
        # a few more copies, one with non-contiguous shared lines, one compacted-after-resume
        for k, src_i in enumerate((4, 6, 8, 10)):
            src = originals[src_i]
            n = len(self.sessions[src]["records"])
            variant = "noncontiguous" if k == 0 else "rewrite"
            self.new_session(size(), False, 1 if k == 1 else 0, copy_from=src, copy_n=max(5, min(n, r.randint(20, 270))),
                             variant=variant)
        self.originals = originals
        self.chain = (a, b, c)
        self.fanout_parent = p

    # ----------------------------------------------------------------------------- edge cases + write

    def apply_edge_cases(self) -> None:
        r = self.rng
        tops = [rel for rel, s in self.sessions.items() if "subagent_of" not in s]
        big = [rel for rel in tops if len(self.sessions[rel]["records"]) >= 40]
        huge_rel = big[0]
        trunc_rels = [big[1], big[2]]
        dup_rel = big[3]
        # 5 MB single line: a human prompt with a huge image payload
        s = self.sessions[huge_rel]
        conv: Conv = s["conv"]
        conv.user_text(conv.lex.text(20, 100), {"kind": "human"}, human=True, with_image=True, huge_image=True)
        conv.assistant_message(conv.make_blocks(None))
        # duplicate uuid within one file: re-emit an earlier record verbatim at the end
        d = self.sessions[dup_rel]
        src = [x for x in d["records"] if x.get("type") == "assistant"][0]
        d["records"].append(json.loads(json.dumps(src)))
        # zero-length file
        rootname, proj, _ = self.loc()
        zero_rel = "%s/%s/%s.jsonl" % (rootname, proj, rng_uuid(r))
        # legacy summary records (flag)
        if self.legacy:
            for rel in tops[:3]:
                recs = self.sessions[rel]["records"]
                leaf = [x for x in recs if "uuid" in x][-1]["uuid"]
                recs.insert(0, {"type": "summary", "summary": "Legacy invented summary: " + self.ctx.lex.sentence(),
                                "leafUuid": leaf})
                self.legacy_files.append(rel)
        self.truth["edge_cases"] = {"huge_line": [huge_rel], "truncated_last_line": trunc_rels,
                                    "duplicate_uuid_in_file": [dup_rel], "zero_length": [zero_rel],
                                    "legacy_summary": self.legacy_files}
        self.zero_rel = zero_rel
        self.trunc_rels = trunc_rels

    def write(self) -> None:
        r = self.rng
        truth = self.truth
        for rel, s in self.sessions.items():
            trunc = rel in self.trunc_rels
            n = write_jsonl(os.path.join(self.out, rel), s["records"], truncate_tail=trunc)
            tcounts: Dict[str, int] = {}
            for x in s["records"][:n]:
                tcounts[x.get("type", "?")] = tcounts.get(x.get("type", "?"), 0) + 1
            conv = s["conv"]
            info: Dict[str, Any] = {
                "session_id": s["session_id"], "root": s["root"], "projdir": s["projdir"], "cwd": s["cwd"],
                "complete_lines": n, "truncated_tail": trunc, "copied_records": s["copied"],
                "copy_from": s["copy_from"], "scheduled": s["scheduled"], "type_counts": tcounts,
                "compactions": s["compactions"],
                "is_subagent": "subagent_of" in s,
            }
            if conv is not None:
                info["human_prompt_uuids"] = [u for u in conv.human_uuids if any(x.get("uuid") == u for x in s["records"][:n])]
                info["final_title"] = conv.final_title
                info["final_last_prompt"] = conv.final_last_prompt
                info["final_mode"] = conv.final_mode
                # title as the ingester should resolve it: last custom-title else last ai-title
                title = None
                for x in s["records"][:n]:
                    if x.get("type") == "custom-title":
                        title = x["customTitle"]
                ai = None
                for x in s["records"][:n]:
                    if x.get("type") == "ai-title":
                        ai = x["aiTitle"]
                info["resolved_title"] = title or ai
            else:
                info["parent"] = s["subagent_of"]
                info["agent_id"] = s["agent_id"]
                truth["subagents"][rel] = {"parent": s["subagent_of"], "agent_id": s["agent_id"],
                                           "parent_session_id": s["session_id"]}
            truth["files"][rel] = info
        # zero-length file
        zp = os.path.join(self.out, self.zero_rel)
        os.makedirs(os.path.dirname(zp), exist_ok=True)
        open(zp, "wb").close()
        truth["files"][self.zero_rel] = {"session_id": os.path.basename(self.zero_rel)[:-6], "root": self.zero_rel.split("/")[0],
                                         "complete_lines": 0, "truncated_tail": False, "copied_records": 0,
                                         "copy_from": None, "scheduled": False, "type_counts": {}, "compactions": [],
                                         "is_subagent": False, "human_prompt_uuids": [], "resolved_title": None}
        # components: connected components of files sharing uuids (computed from the copy graph)
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for rel, s in self.sessions.items():
            find(rel)
            if s["copy_from"]:
                parent[find(rel)] = find(s["copy_from"])
        comps: Dict[str, List[str]] = {}
        for rel in self.sessions:
            if "subagent_of" in self.sessions[rel]:
                continue
            comps.setdefault(find(rel), []).append(rel)
        truth["components"] = sorted((sorted(v) for v in comps.values()), key=lambda c: c[0])
        truth["components"].append([self.zero_rel])
        # canonical owner for every uuid that appears in more than one file
        owners: Dict[str, List[str]] = {}
        for rel, s in self.sessions.items():
            seen = set()
            for x in s["records"][: truth["files"][rel]["complete_lines"]]:
                u = x.get("uuid")
                if u and u not in seen:
                    seen.add(u)
                    owners.setdefault(u, []).append(rel)
        truth["canonical_owner"] = {u: self.ctx.origin_file[u] for u, o in owners.items() if len(o) > 1}
        truth["shared_uuid_count"] = len(truth["canonical_owner"])
        truth["planted"] = self.ctx.planted
        truth["chain"] = list(self.chain)
        truth["fanout_parent"] = self.fanout_parent
        # sessions-index: one small JSON per top-level session
        idx_dir = os.path.join(self.out, "sessions-index")
        os.makedirs(idx_dir, exist_ok=True)
        tops = [(rel, s) for rel, s in self.sessions.items() if "subagent_of" not in s]
        tops.sort(key=lambda t: next((x["timestamp"] for x in t[1]["records"] if "timestamp" in x), ""))
        windows: Dict[str, Any] = {}
        for w, (rel, s) in enumerate(tops, start=1):
            created = next((x["timestamp"] for x in s["records"] if "timestamp" in x), "")
            base = truth["files"][rel]["resolved_title"] or self.ctx.lex.title()
            roll = r.random()
            if roll < 0.6:
                title = "[%d] %s" % (w, base)
            elif roll < 0.8:
                title = "Context window #%d — %s" % (w, base)
            else:
                title = base
                w = None  # type: ignore[assignment]
            entry = {"createdAt": created, "title": title, "cwd": s["cwd"], "cliSessionId": s["session_id"]}
            with open(os.path.join(idx_dir, s["session_id"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, indent=1)
            windows[s["session_id"]] = {"window": w, "title": title, "createdAt": created}
        truth["windows"] = windows
        truth["seed"] = self.seed
        truth["legacy_summary"] = self.legacy
        with open(os.path.join(self.out, "truth.json"), "w", encoding="utf-8") as f:
            json.dump(truth, f, ensure_ascii=False, indent=1, sort_keys=True)


def generate(out: str, seed: int = 1234, legacy_summary: bool = False) -> Dict[str, Any]:
    ctx = Ctx(seed)
    pl = Planner(ctx, out, legacy_summary)
    pl.seed = seed
    pl.plan()
    pl.apply_edge_cases()
    pl.write()
    return pl.truth


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output directory (created; must be empty or absent)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--legacy-summary", action="store_true",
                    help="also emit legacy {type:summary, leafUuid} records at the top of three files")
    a = ap.parse_args(argv)
    if os.path.isdir(a.out) and os.listdir(a.out):
        ap.error("--out %s exists and is not empty" % a.out)
    truth = generate(a.out, a.seed, a.legacy_summary)
    n_files = len(truth["files"])
    n_lines = sum(f["complete_lines"] for f in truth["files"].values())
    print("wrote %d files, %d lines, %d multi-file components, %d shared uuids -> %s" % (
        n_files, n_lines, sum(1 for c in truth["components"] if len(c) > 1), truth["shared_uuid_count"], a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
