#!/usr/bin/env python3
"""schema_probe.py -- structural reconnaissance of Claude Code JSONL transcripts.

Streams every ~/.claude/projects/**/*.jsonl line by line (read-only) and emits ONE JSON
report of STRUCTURE ONLY: field paths, types, presence %, enum counts for a whitelist of
low-cardinality keys, continuation/linkage statistics, and a byte-share noise profile.
No transcript text is ever stored in the report (only key names, counts, tool names,
tag names, and whitelisted enum values).

Usage:  python3 -I schema_probe.py [--root ~/.claude/projects] [--out report.json]
Re-runnable; never writes under --root.
"""
import json, os, re, sys, glob, collections, argparse, statistics

ENUM_KEYS = {"type", "subtype", "role", "userType", "entrypoint", "version", "level", "model",
             "stop_reason", "isSidechain", "isMeta", "isCompactSummary", "isApiErrorMessage",
             "permissionMode", "gitBranch", "service_tier", "operation", "name", "stop_sequence",
             "compactMetadata.trigger", "compactMetadata.preTokens", "isVisibleInTranscriptOnly",
             "isSynthetic", "requestId_present"}
ENUM_CAP = 40
IDLIKE = re.compile(r"^[0-9a-fA-F-]{16,}$|^toolu_|^msg_|^req_")
REM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
TAG = re.compile(r"^\s*<([A-Za-z][\w-]{0,40})")

def tname(v):
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer",
            float: "number", type(None): "null"}[type(v)]

def walk(o, path, out, depth=0):
    out[path] = out.get(path, set()) | {tname(o)} if False else out.get(path, set())
    out[path].add(tname(o))
    if depth > 9:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            k = "<id>" if IDLIKE.match(k) else ("<path-key>" if ("/" in k or len(k) > 60) else k)
            walk(v, path + "." + k if path else k, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            walk(v, path + "[]", out, depth + 1)

def blen(x):
    return len(x.encode("utf-8", "ignore")) if isinstance(x, str) else len(json.dumps(x).encode())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", default="schema_probe_report.json")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.root, "**", "*.jsonl"), recursive=True))
    paths = collections.defaultdict(lambda: [0, set()])  # path -> [lines present, types]
    enums = collections.defaultdict(collections.Counter)
    rtypes = collections.Counter()
    nlines = 0
    bad = 0
    noise = collections.Counter()
    noise_by_role = collections.Counter()
    uuid_file = {}
    per_file = []
    blocktypes = collections.Counter()
    toolnames = collections.Counter()
    first_tags = collections.Counter()
    trname = collections.Counter()
    tsshape = collections.Counter()
    pathshape = collections.Counter()
    for fp in files:
        st = os.stat(fp)
        rel = os.path.relpath(fp, a.root)
        depth = rel.count(os.sep)
        pathshape[re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", "/".join(["<projdir>"] + rel.split(os.sep)[1:]))] += 1
        info = {"bytes": st.st_size, "mtime": st.st_mtime, "depth": depth, "lines": 0,
                "sessionIds": collections.Counter(), "first_type": None, "types": collections.Counter(),
                "uuids": set(), "parents": [], "sidechain": 0, "summary_leaf": [],
                "fname": os.path.basename(fp)[:-6], "first_user_tag": None, "agent": 0,
                "compact_boundary": 0, "isCompactSummary": 0, "logicalParent": [], "seq": [], "ts": []}
        with open(fp, "rb") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except Exception:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                    continue
                nlines += 1
                info["lines"] += 1
                size = len(raw)
                t = rec.get("type")
                rtypes[t] += 1
                info["types"][t] += 1
                if info["first_type"] is None:
                    info["first_type"] = t
                local = {}
                walk(rec, "", local)
                for p, ts in local.items():
                    paths[p][0] += 1
                    paths[p][1] |= ts
                for k in ENUM_KEYS:
                    if "." in k:
                        cur = rec
                        for part in k.split("."):
                            cur = cur.get(part) if isinstance(cur, dict) else None
                        v = cur
                    else:
                        v = rec.get(k)
                        if v is None and isinstance(rec.get("message"), dict):
                            v = rec["message"].get(k)
                    if v is not None and not isinstance(v, (dict, list)):
                        if len(enums[(t, k)]) < ENUM_CAP or v in enums[(t, k)]:
                            enums[(t, k)][str(v)] += 1
                sid = rec.get("sessionId")
                if sid:
                    info["sessionIds"][sid] += 1
                u = rec.get("uuid")
                ts = rec.get("timestamp")
                if isinstance(ts, str):
                    tsshape[re.sub(r"\d", "9", ts)] += 1
                    info["ts"].append(ts)
                if u:
                    info["seq"].append((u, sid))
                    info["uuids"].add(u)
                    uuid_file.setdefault(u, fp)
                pu = rec.get("parentUuid")
                if pu:
                    info["parents"].append(pu)
                lp = rec.get("logicalParentUuid")
                if lp:
                    info["logicalParent"].append(lp)
                if rec.get("isSidechain"):
                    info["sidechain"] += 1
                if rec.get("agentId"):
                    info["agent"] += 1
                if rec.get("isCompactSummary"):
                    info["isCompactSummary"] += 1
                if rec.get("subtype") == "compact_boundary":
                    info["compact_boundary"] += 1
                if t == "summary" and rec.get("leafUuid"):
                    info["summary_leaf"].append(rec["leafUuid"])
                # ---- noise accounting
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
                accounted = 0
                role = (msg or {}).get("role") or t
                content = msg.get("content") if msg else None
                if t == "summary":
                    n = blen(rec.get("summary", "")); noise["summary_record"] += n; accounted += n
                def textcat(s, role):
                    nonlocal accounted
                    n = blen(s)
                    rem = sum(blen(m) for m in REM.findall(s))
                    noise["system_reminder"] += rem
                    cat = "human_prose" if role == "user" else "assistant_prose"
                    noise[cat] += n - rem
                    noise_by_role[role] += n
                    accounted += n
                if isinstance(content, str):
                    textcat(content, role)
                    if role == "user" and info["first_user_tag"] is None:
                        m = TAG.match(content)
                        info["first_user_tag"] = m.group(1) if m else "(plain)"
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type"); blocktypes[(role, bt)] += 1
                        if bt == "text":
                            textcat(b.get("text", ""), role)
                            if role == "user" and info["first_user_tag"] is None:
                                m = TAG.match(b.get("text", ""))
                                info["first_user_tag"] = m.group(1) if m else "(plain)"
                        elif bt == "thinking":
                            n = blen(b.get("thinking", "")) + blen(b.get("signature", ""))
                            noise["thinking"] += n; accounted += n
                        elif bt == "tool_use":
                            toolnames[b.get("name")] += 1
                            n = blen(b.get("input", {})); noise["tool_use_input"] += n; accounted += n
                        elif bt == "tool_result":
                            c = b.get("content"); n = 0
                            if isinstance(c, str):
                                n = blen(c); noise["tool_result_text"] += n
                            elif isinstance(c, list):
                                for cb in c:
                                    if isinstance(cb, dict) and cb.get("type") == "image":
                                        d = len(((cb.get("source") or {}).get("data")) or "")
                                        noise["image_base64"] += d; n += d
                                    elif isinstance(cb, dict):
                                        k = blen(cb.get("text", "")); noise["tool_result_text"] += k; n += k
                            accounted += n
                        elif bt == "image":
                            d = len(((b.get("source") or {}).get("data")) or "")
                            noise["image_base64"] += d; accounted += d
                        else:
                            n = blen(b); noise["other_block"] += n; accounted += n
                tur = rec.get("toolUseResult")
                if tur is not None:
                    n = blen(tur); noise["toolUseResult_duplicate"] += n; accounted += n
                noise["envelope_metadata"] += max(size - accounted, 0)
                noise["TOTAL_LINE_BYTES"] += size
        per_file.append(info)

    # ---- continuation analysis (counts only)
    fset = {i["fname"] for i in per_file}
    cont = collections.Counter()
    for i in per_file:
        sids = set(i["sessionIds"])
        cont["files"] += 1
        if len(sids) > 1: cont["files_multi_sessionId"] += 1
        if sids and i["fname"] not in sids: cont["files_name_ne_any_sessionId"] += 1
        if i["fname"] in sids and len(sids) > 1: cont["files_name_in_multi"] += 1
        if i["first_type"] == "summary": cont["files_first_record_summary"] += 1
        if i["depth"] >= 2: cont["files_nested_depth2plus"] += 1
        if i["sidechain"]: cont["files_with_sidechain_lines"] += 1
        if i["agent"]: cont["files_with_agentId"] += 1
        if i["compact_boundary"]: cont["files_with_compact_boundary"] += 1
        if i["isCompactSummary"]: cont["files_with_isCompactSummary"] += 1
        own = i["uuids"]
        orphan = [p for p in i["parents"] if p not in own]
        if orphan:
            cont["files_with_cross_file_or_dangling_parent"] += 1
            res = [p for p in orphan if p in uuid_file]
            cont["orphan_parents_total"] += len(orphan)
            cont["orphan_parents_resolving_to_other_file"] += len(res)
        lpo = [p for p in i["logicalParent"] if p not in own]
        cont["logicalParent_total"] += len(i["logicalParent"])
        cont["logicalParent_in_other_file"] += sum(1 for p in lpo if p in uuid_file)
        if i["summary_leaf"]:
            cont["files_with_summary_records"] += 1
            cont["summary_leaf_total"] += len(i["summary_leaf"])
            cont["summary_leaf_resolving_other_file"] += sum(1 for l in i["summary_leaf"] if l in uuid_file and uuid_file[l] != "")
        dup = 0
    # uuids shared across files (resume copies history?)
    owners = collections.defaultdict(set)
    for i in per_file:
        for u in i["uuids"]:
            owners[u].add(i["fname"])
    cont["uuids_total"] = len(owners)
    cont["uuids_in_multiple_files"] = sum(1 for v in owners.values() if len(v) > 1)
    # ---- shared-history analysis (resume/continuation copies)
    share = []
    for i in per_file:
        seq = i["seq"]
        sh = [(u, sd) for u, sd in seq if len(owners[u]) > 1]
        if not sh:
            continue
        idx = [n for n, (u, _) in enumerate(seq) if len(owners[u]) > 1]
        prefix = idx == list(range(len(idx)))
        others = set().union(*[owners[u] for u, _ in sh]) - {i["fname"]}
        share.append({"shared_lines": len(sh), "uuid_lines": len(seq), "contiguous_prefix": prefix,
                      "n_other_owner_files": len(others),
                      "shared_sid_is_own_name": sum(1 for _, sd in sh if sd == i["fname"]),
                      "shared_sid_is_other_name": sum(1 for _, sd in sh if sd in others),
                      "own_name_is_sid_somewhere": i["fname"] in i["sessionIds"],
                      "depth": i["depth"], "first_ts_rank": None})
    sizes = sorted(i["bytes"] for i in per_file)
    first_tag = collections.Counter(i["first_user_tag"] for i in per_file)
    rep = {
        "inventory": {"files": len(files), "bytes": sum(sizes), "min": sizes[0] if sizes else 0,
                      "median": statistics.median(sizes) if sizes else 0, "max": sizes[-1] if sizes else 0,
                      "p90": sizes[int(len(sizes) * .9)] if sizes else 0,
                      "mtime_min": min(i["mtime"] for i in per_file), "mtime_max": max(i["mtime"] for i in per_file),
                      "projdirs": len({os.path.dirname(os.path.relpath(f, a.root)).split(os.sep)[0] for f in files}),
                      "lines": nlines, "bad_lines": bad,
                      "lines_per_file_median": statistics.median(i["lines"] for i in per_file)},
        "record_types": dict(rtypes),
        "paths": {p: {"lines": v[0], "pct": round(100 * v[0] / nlines, 3), "types": sorted(v[1])}
                  for p, v in sorted(paths.items())},
        "enums": {f"{t}|{k}": dict(c.most_common(ENUM_CAP)) for (t, k), c in sorted(enums.items(), key=str)},
        "block_types": {f"{r}|{b}": n for (r, b), n in blocktypes.items()},
        "tool_names": dict(toolnames.most_common(60)),
        "continuation": dict(cont),
        "shared_history_files": share,
        "timestamp_shapes": dict(tsshape),
        "path_shapes": dict(pathshape),
        "first_user_tag_by_file": dict(first_tag.most_common(30)),
        "first_record_type_by_file": dict(collections.Counter(i["first_type"] for i in per_file)),
        "noise_bytes": dict(noise),
        "noise_pct": {k: round(100 * v / noise["TOTAL_LINE_BYTES"], 2) for k, v in noise.items()},
        "per_file": [{"bytes": i["bytes"], "lines": i["lines"], "depth": i["depth"],
                      "n_sessionIds": len(i["sessionIds"]), "name_is_sid": i["fname"] in i["sessionIds"],
                      "first_type": i["first_type"], "sidechain_lines": i["sidechain"], "agent_lines": i["agent"],
                      "first_user_tag": i["first_user_tag"], "types": dict(i["types"])} for i in per_file],
    }
    with open(a.out, "w") as f:
        json.dump(rep, f, indent=1, default=str)
    print("wrote", a.out, "files", len(files), "lines", nlines, "bad", bad)

if __name__ == "__main__":
    main()
