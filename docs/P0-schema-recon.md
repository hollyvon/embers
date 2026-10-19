# P0 — Claude Code JSONL schema recon (structure only; zero transcript content)
*Run 2026-10-19 by a local recon session. Reproduce with `python3 -I tools/schema_probe.py --out P0-schema-report.json` (full-corpus pass, ~16 s). Raw machine report: `P0-schema-report.json` (local only). Corpus = a FULL pass, not a sample.*
*Claude Code versions seen: 2.1.156 → 2.1.299 (38 distinct); all `entrypoint = claude-desktop`, `userType = external`. Schema drifts across versions — the parser must tolerate unknown/missing keys.*

## (a) Corpus inventory
| item | value |
|---|---|
| project dirs under `~/.claude/projects/` | 3 |
| `.jsonl` files | 124 (104 top-level session files `<projdir>/<session-uuid>.jsonl`; 20 subagent files `<projdir>/<parent-session-uuid>/subagents/agent-<17-hex>.jsonl`) |
| total bytes | 1,196,947,220 (≈1.2 GB; `du` 1.17 GB) |
| file size min / median / p90 / max | 54 KB / 2.05 MB / 30.1 MB / 79.5 MB (heavily right-skewed) |
| total lines (records) | 190,139; unparseable lines: 0; median lines/file ≈ 386 |
| mtime range | 2026-08-16 → 2026-10-19 (≈2 months; corpus is young and growing) |
| all lines are single JSON objects, UTF-8, `\n`-terminated | yes |

## (b) Field inventory
Presence = % of ALL lines. Top-level keys (type in brackets). Nested keys are listed for the structurally important objects; the full path list (≈700 paths) is in `P0-schema-report.json` → `paths`.

**Envelope (on "conversation" records: user/assistant/attachment/system; 68.2% of lines):** `uuid` [str], `parentUuid` [str|null], `sessionId` [str; 97.5% — metadata records carry it too], `timestamp` [str; 78.3%], `type` [str, 100%], `cwd`, `gitBranch`, `version`, `entrypoint`, `userType`, `isSidechain` [bool] (all 68.2%).
**Optional envelope:** `isMeta` [bool, 0.3%], `slug` [str, 11.5% — human-readable session slug], `promptId` [str, 16.4%], `permissionMode` [str: `acceptEdits`|`auto`, user lines], `origin` {`kind`,`subkind`,`producer`,`runId`,`fireReason`} (7.2%, user lines), `agentId` (0.9%, subagent files), `requestId` [str, 30.5%, assistant], `sourceToolAssistantUUID` [str, 8.7%, tool-result user lines], `toolUseResult` (8.4%), `effort`/`perTurnEffort`/`advisorModel`/`requestedModel`/`apiBlockIndex`/`thinkingDurationMs` (assistant-side extras), `attributionSkill|McpServer|McpTool|Plugin|Agent`.
**`message` object (47.0%):** `role` [`user`|`assistant`], `content` [**string OR array of blocks**], and on assistant lines (30.5%): `id` [str `msg_…`], `model`, `type` (`message`), `stop_reason` [`tool_use`|`end_turn`|`refusal`|`stop_sequence`|null], `stop_sequence`, `stop_details`, `container`, `context_management`, `diagnostics`, `input_transformations`, and `usage`.
**`message.usage`:** `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` [int]; `cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`; `server_tool_use{web_search_requests, web_fetch_requests}`; `service_tier`, `speed`, `inference_geo` [str|null]; `iterations[]` (array of per-iteration usage objects, same int keys + `type`); `output_tokens_details{thinking_tokens}` (23.8%); `fallback_credit` [null].
**Models seen (assistant `message.model`):** 10 real ids + the literal `<synthetic>` (24 lines; harness-injected assistant lines, e.g. errors). **No per-message dollar cost.** The only cost data is `type:"cost-state"` records (84 lines): `totalCostUSD`, `totalAPIDuration`, `totalAPIDurationWithoutRetries`, `totalDuration`, `totalToolDuration`, `totalLinesAdded|Removed`, `modelUsage{<model-id>:{…}}`, `hasUnknownModelCost`, `startTime`.
**Timestamp shape:** exactly one format on every timestamped line: `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, ms, `Z`). Also `snapshot.timestamp`, `attachment.timestamp`, `backup.backupTime` use the same shape.
**ID shapes:** `uuid`/`sessionId`/`parentUuid`/`leafUuid` = lowercase hyphenated UUIDv4 strings; `message.id` = `msg_` + base62; `requestId` = `req_` + base62; tool_use `id` = `toolu_` + base62; subagent `agentId` = 17 lowercase hex chars (file `agent-<agentId>.jsonl`).

## (c) Record types (15) and shapes
Counts: assistant 58,046 · user 31,246 · attachment 26,529 · queue-operation 18,616 · system 13,883 · last-prompt 7,740 · custom-title 7,796 · atis-latch 4,640 · mode 4,446 · file-history-snapshot 4,319 · ai-title 4,174 · bridge-session 4,165 · agent-name 3,980 · file-history-delta 459 · cost-state 84.
**There is NO `summary` record and NO top-level `tool_result` record type** (both assumed by the spec; see §g). Tool results are `user` records carrying a `tool_result` block.

Synthetic shapes (values are placeholders):
```jsonc
// user — human turn (content as plain string)
{"type":"user","uuid":"U1","parentUuid":"P0|null","sessionId":"S1","timestamp":"2026-01-01T00:00:00.000Z",
 "cwd":"/x","gitBranch":"main","version":"2.1.247","entrypoint":"claude-desktop","userType":"external",
 "isSidechain":false,"promptId":"…","permissionMode":"acceptEdits","origin":{"kind":"human"},
 "message":{"role":"user","content":"<text>"}}
// user — human turn with attachments: content = [{"type":"text","text":"…"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}}]  (one "document" block exists)
// user — tool result
{"type":"user", …envelope…, "sourceToolAssistantUUID":"A1",
 "message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_X","content":"<text>" /* OR [{"type":"text","text":…},{"type":"image","source":{…}}, {"type":"tool_reference","tool_name":…}] */, "is_error":false}]},
 "toolUseResult": {…tool-specific structured duplicate… | "<string>" | [ {type,text} ]}}
// assistant — ONE CONTENT BLOCK PER LINE (see below)
{"type":"assistant","uuid":"A1","parentUuid":"U1","requestId":"req_X",
 "message":{"id":"msg_Y","role":"assistant","type":"message","model":"claude-fable-5","stop_reason":null|"tool_use"|"end_turn","usage":{…},
   "content":[{"type":"thinking","thinking":"<t>","signature":"<sig>"}]  // or [{"type":"text","text":…}] or [{"type":"tool_use","id":"toolu_X","name":"Bash","input":{…},"caller":{"type":"direct"}}]}}
// system
{"type":"system","subtype":"stop_hook_summary|api_error|compact_boundary|informational|model_refusal_no_fallback|local_command","level":"suggestion|error|info|notice|warning", "content":"…", "toolUseID":"…", "hookInfos":[{"command":"…"}], "hookCount":1, …envelope…}
// attachment (harness-injected context; text payloads live at attachment.text / .content / .entries)
{"type":"attachment","attachment":{"type":"total_tokens_reminder|task_reminder|skill_listing|deferred_tools_delta|file|edited_text_file|date_change|instructions|hook_additional_context|…","text":"…"}, …envelope…}
// metadata records (no uuid/parentUuid; sessionId usually present; no timestamp except where noted)
{"type":"queue-operation","operation":"enqueue|dequeue|remove","sessionId":"S1","timestamp":"…","content":"<queued text>"}
{"type":"custom-title","customTitle":"<t>","sessionId":"S1"}   {"type":"ai-title","aiTitle":"<t>","sessionId":"S1"}
{"type":"last-prompt","lastPrompt":"<text>","sessionId":"S1"}   {"type":"mode","mode":"<m>","sessionId":"S1"}
{"type":"agent-name","agentName":"<n>","sessionId":"S1"}        {"type":"atis-latch","atis":"<s>","sessionId":"S1"}
{"type":"bridge-session","bridgeSessionId":"…","lastSequenceNum":1,"ownerAccountUuid":"…","ownerOrganizationUuid":"…"}
{"type":"file-history-snapshot","messageId":"…","snapshot":{"messageId":"…","timestamp":"…","trackedFileBackups":{"<abs-path>":{"backupFileName":"…","version":1,"backupTime":"…"}}},"isSnapshotUpdate":false}
{"type":"file-history-delta", "backup":{…}, "trackingPath":"…","snapshotMessageId":"…"}
{"type":"cost-state", "totalCostUSD":0.0, "modelUsage":{"<model>":{…}}, …}
```
**Content-block counts:** assistant: text 19,818 · thinking 21,678 · tool_use 16,569; user: tool_result 16,591 · text 1,213 · image 1,139 · document 1. `tool_result.content` is a **string** or an **array** (text/image/tool_reference items). `message.content` for user lines is a string in the common human case (~13.6k `origin.kind=human`) and an array otherwise. Top 8 tool names: Bash 6.5k, Edit 2.3k, Read 2.1k, WebSearch, Write, MCP browser/Notion/Gmail tools (`mcp__<server-id>__<tool>`; server ids are opaque UUIDs or names).
**Key dict-keyed objects to special-case:** `snapshot.trackedFileBackups` (keys are absolute file paths — never treat as schema), `wireToolInputs` / `wireIngestContext` (keys are tool-use ids), `mcpMeta._meta` (keys vary), `modelUsage` (keys are model ids).
**⚠ Assistant messages are split across lines:** one API message (`message.id`) appears as 1–8 consecutive `assistant` lines, **one content block per line**, each with its own `uuid` but the same `message.id`/`requestId`, and `usage` repeated (so summing `usage` across lines over-counts). Of 27.8k distinct messages: 6.8k have 1 line, 14.1k 2, 6.4k 3, 0.4k 4, tail to 8. `stop_reason` is usually null on early blocks and set on the last.

## (d) Stitching rules
**1. Compaction does NOT start a new file.** It is in-file: a `system` record `subtype:"compact_boundary"` with `parentUuid:null` (a new chain root!) and `logicalParentUuid` = uuid of the last pre-compaction record (always in the same file; 27/27), plus `compactMetadata{trigger:"auto"|"manual", preTokens, postTokens, durationMs, cumulativeDroppedTokens, preservedSegment{}, preservedMessages{}, preCompactDiscoveredTools[]}`. It is followed by a `user` record with `isCompactSummary:true` (+ `isVisibleInTranscriptOnly:true`), content = the summary as a plain **string**, and a non-null `parentUuid` pointing at the boundary. 13 files contain 27 boundaries (22 auto, 5 manual). Consequence: files have 1 root normally (95/104) but 2–7 `parentUuid:null` roots when compacted (= boundaries + 1). **Pre-compaction text is fully retained in the file** (confirms the SPEC field note). A parser that follows only `parentUuid` will lose the pre-compaction chain; follow `logicalParentUuid` across the boundary, or simply order by file line order/timestamp.
**2. Resume/fork creates a NEW top-level file that re-copies history.** 36 of 104 session files begin (contiguously, 33 of 36 exactly as a prefix) with lines whose `uuid`s are identical to lines in one or more other files (14,282 of 86,983 distinct uuids, 16%, occur in >1 file; 1–8 owner files; copied prefixes of 242–2,700 lines). In 34/36 the copied lines have `sessionId` rewritten to the NEW file's own id (uuid and timestamps unchanged); in 2 the copied prefix keeps the *original* file's sessionId, and 1 file mixes several sessionIds. There is **no explicit "resumedFrom/parentSession" field and no leading `summary` record** — lineage is inferable only by uuid overlap. No `parentUuid` ever dangles across files (0 cross-file parents; every parent resolves in its own file).
**Stitching rule:** logical thread = connected component of files under the relation "shares ≥1 message uuid"; within a component dedupe messages by `uuid` (keep one copy; prefer the earliest-timestamp owner file as canonical, record all owner files), order by `timestamp` then line index. A file's *new* material = its lines whose uuid is not in an earlier-ordered file. `sessionId` is NOT a reliable thread key (rewritten on copy); the filename stem is the file's own session id (true for all 104 top-level files except the multi-sid case).
**3. Subagents/sidechains:** separate files at `<projdir>/<parent-session-uuid>/subagents/agent-<agentId>.jsonl` (20 files). Their lines have `isSidechain:true`, `agentId`, `sessionId` = the PARENT session's id (not the filename), and begin with a `user` record with `parentUuid:null` (own root). Types inside: user/assistant/attachment only (no metadata records). **No `isSidechain:true` line exists in any top-level file** (0). Link to parent: the parent's `Agent` tool_use and its tool_result `toolUseResult{agentId, agentType, totalTokens, totalToolUseCount, toolStats, usage…}` (rare, 0.012% of lines) carry the `agentId`. Index sidechains as children of the parent session, flagged as subagent.
**4. Scheduled-task runs:** distinguishable at the first user message: 43 of 124 files start with a user text whose first XML-ish tag is `<scheduled-task…>` (tag-name only recorded; the rest of the content is not reproduced here); the other 81 start with plain text. Also `origin.kind:"task-notification"` (+`subkind:"scheduled-trigger"` 26 lines; `producer:"session-task"` 24 lines) marks injected notifications; `origin.kind:"human"` (13,577 lines) marks real human prompts. `origin` is absent on 54k+ user lines (tool results, meta), so "human prompt" = `type=user ∧ origin.kind=="human"` OR (origin absent ∧ content is not a tool_result ∧ isMeta≠true) — verify (§g).
**Other per-file facts:** first record type is `queue-operation` (79 files), `user` (20 = subagents), `custom-title` (25). Session title = LAST `custom-title` (else last `ai-title`); titles are re-appended repeatedly (7.8k custom-title lines in 124 files), same for `last-prompt`, `mode` → **dedupe metadata records, keep last**.

## (e) Noise profile (share of total bytes, whole corpus)
| class | % bytes |
|---|---|
| image base64 (`image` blocks, tool_result images; ≈1.1k user image blocks + tool-result screenshots) | 40.2 |
| `toolUseResult` top-level structured duplicate of the tool_result (file contents, stdout, patches — mostly redundant with the `tool_result` block) | 25.8 |
| envelope/metadata/attachments/system/queue/snapshots (everything not itemized; includes attachment payloads, hook infos, file-history, usage objects) | 19.2 |
| thinking blocks (+ signatures) | 7.7 |
| tool_result text | 3.0 |
| assistant prose (text blocks) | 2.2 |
| tool_use `input` | 1.2 |
| human prose | 0.8 |
| inline `<system-reminder>` spans inside message text | ≈0.0 (reminders arrive as `attachment` records, 11.6% of lines carry `attachment.text`, bytes counted in envelope row) |
Caveat: image data nested inside `toolUseResult` is counted once there, not under images; true image share is somewhat higher. **Index-worthy prose (human + assistant text) ≈ 3% of bytes (~36 MB)**; with tool_result text ≈ 6%; with thinking ≈ 14%. ~66–70% of the corpus is base64 + duplicated structured results → skip. Line counts: tool-related user lines 16.6k vs human prompts ~13.6k.

## (f) REQUIREMENTS for the synthetic fixture generator
A fake corpus must be a directory tree `<root>/<projdir>/…` with ≥3 project dirs (dir names = path-with-slashes→dashes style, arbitrary), UTF-8 JSONL, one object per line, deterministic (seeded). Required reproduction:
1. **Layout:** top-level `<projdir>/<uuid>.jsonl` sessions (~84%) and `<projdir>/<parent-uuid>/subagents/agent-<17hex>.jsonl` (~16%). Size distribution log-normal: min ~50 KB real-scale → for fixtures scale down 100–1000× but keep ratios: median ≈ 1×, p90 ≈ 15×, max ≈ 40×; plus a few tiny (<10 lines) files.
2. **Record types & mix (by line %):** assistant 30, user 16, attachment 14, queue-operation 10, system 7, custom-title 4, last-prompt 4, ai-title 2, atis-latch 2.4, mode 2.3, bridge-session 2.2, agent-name 2.1, file-history-snapshot 2.3, file-history-delta 0.2, cost-state 0.05. Include ALL 15 types. Do NOT emit `summary` records by default; emit a clearly-labelled variant flag (`--legacy-summary`) so ingest can be tested for tolerance of an unknown/legacy `{"type":"summary","summary":"…","leafUuid":"…"}` record.
3. **Envelope:** conversation records carry uuid, parentUuid, sessionId, timestamp (`YYYY-MM-DDTHH:MM:SS.mmmZ`, monotonic non-decreasing within a file except copied prefixes keep original), cwd, gitBranch, version (vary across ≥5 versions), entrypoint, userType, isSidechain. Metadata types omit uuid/parentUuid/(usually) timestamp but carry sessionId — except `file-history-*`, `cost-state`, `bridge-session` which may omit sessionId.
4. **Chain structure:** parentUuid forms a linear chain in a file with occasional side branches (rare); first record parentUuid null. Tool loop: assistant(tool_use) → user(tool_result, `sourceToolAssistantUUID` = that assistant uuid, plus `toolUseResult`) → assistant….
5. **Assistant splitting:** each API message = 1–8 consecutive lines, one content block each (distribution 24%/50%/23%/2%/tail), shared `message.id` & `requestId`, repeated `usage`, `stop_reason` null until last block. Some thinking blocks carry `signature`. Include `<synthetic>` model lines and `stop_reason:"refusal"` rarely.
6. **User content polymorphism:** string content; array with text; array with image block (base64, short fake data); `document` block (rare); tool_result with string content; tool_result with array content (text + image + tool_reference); `is_error:true` (~3.6% of blocks have is_error present).
7. **`origin` & meta:** ~45% of user-with-string lines `origin:{kind:"human"}`; some `task-notification` (+`scheduled-trigger` subkind, `producer:"session-task"`); `isMeta:true` rare; `permissionMode` ∈ {acceptEdits, auto}.
8. **Compaction:** in ~10% of sessions insert 1–3 `compact_boundary` system records (parentUuid null, logicalParentUuid → previous record, compactMetadata with trigger auto/manual, realistic token ints) followed by a `user` record `isCompactSummary:true, isVisibleInTranscriptOnly:true`, string content, parentUuid → boundary.
9. **Resume/fork copies:** ~30% of top-level sessions are produced by copying a contiguous prefix (200–2,700 lines scaled) of another session verbatim (same uuids/timestamps) with `sessionId` rewritten to the new file's id (variant ~6%: sessionId left as the original; variant: one file containing mixed sessionIds, non-contiguous shared lines), then appending new records. Include chains: A→B→C (C copies B's whole content incl. A's prefix) and fan-out (one parent copied by up to 8 children). Ground-truth manifest (`truth.json`: component membership, canonical owner per uuid) must be emitted for tests.
10. **Subagents:** parent has `Agent` tool_use + tool_result with `toolUseResult.agentId`; child file: all lines `isSidechain:true` + `agentId`, `sessionId` = parent id, first user record parentUuid null; types limited to user/assistant/attachment; 10–30 lines each.
11. **Scheduled-task runs:** ~35% of sessions open with a user text beginning with a `<scheduled-task …>` XML-ish tag (synthetic body), others plain; sprinkle `task-notification` origin records.
12. **Metadata churn:** title/last-prompt/mode/agent-name/atis-latch records re-emitted many times per file (dedupe-last test); `queue-operation` triples (enqueue→dequeue, occasional remove) carrying `content`.
13. **Attachments:** types from the observed list (total_tokens_reminder dominant ~77%, task_reminder, skill_listing, deferred_tools_delta, file, edited_text_file, date_change, instructions, hook_additional_context…); payload in `attachment.text` (long, boilerplate — ~11.6% of lines have it).
14. **Noise mix (by bytes) to hit:** ≈40% image base64, ≈26% `toolUseResult` duplicates, ≈19% envelope/attachments, ≈8% thinking, ≈3% tool_result text, ≈2% assistant prose, ≈1% tool_use input, <1% human prose. (Generator may scale image payloads down but must keep image-bearing lines to the same *line share*, ~2–3%, and keep a few huge single lines — up to 5 MB — to test line-length limits.)
15. **Edge cases:** dict-keyed objects with path/ID keys (`snapshot.trackedFileBackups`, `wireToolInputs`, `mcpMeta._meta`); MCP tool names `mcp__<opaque>__<tool>`; unicode/emoji/RTL, embedded newlines and `<system-reminder>` text in content; `input` of a tool_use containing `__unparsedToolInput`; `error` objects on `api_error` system records with retryAttempt/retryInMs; `content: null`/empty-string content; truncated final line (partial JSON) in ≥1 file; a file being appended while read; CRLF absent; zero-length file; duplicate uuids within one file (check — see §g).
16. **Schema drift:** vary `version` and randomly drop optional keys; emit a few unknown extra keys so the ingester proves forward-tolerance.

## (g) Open questions
1. Resume semantics: is the copied prefix a *resume* or a *fork* (`--resume`/`/clear`/branch)? No explicit parent pointer exists; the uuid-overlap rule is inferential. Confirm by checking that every overlapping pair has the later-mtime file as the superset (not done) and decide canonical-owner policy.
2. The 2 files whose copied prefix retains the ORIGINAL sessionId and the 1 mixed-sessionId file: probably different resume modes; characterize with a larger sample.
3. Legacy `summary` records / `leafUuid` appear in older Claude Code versions; none here (versions ≥2.1.156). Older transcripts on other machines would need the variant — keep tolerance.
4. Exact human-prompt predicate (origin.kind absent cases; `isMeta`; `queue-operation` content vs user records — queued prompts appear in BOTH `queue-operation.content` and later `user` lines, risking double-indexing).
5. Are `toolUseResult` and `tool_result` ever non-redundant (e.g. truncated model-side vs full UI-side: `contentNotInModelContext`, `persistedOutputPath` flags exist)? Decide whether to index toolUseResult.stdout instead of the block.
6. `thinking` blocks: index or not? (7.7% of bytes; privacy-heavy; signatures are opaque.) Policy decision for the owner.
7. Reminder text arrives via `attachment` records, not inline; confirm which attachment types carry user-meaningful text (e.g. `file`, `edited_text_file`, `queued_command`) vs boilerplate.
8. Only 3 project dirs and 2 months of data: older sessions on disk may be pruned by Claude Code retention — confirm no cleanup setting is deleting history (SPEC says raw is kept forever; the *source* may not be).
9. Cost: no per-message USD; derive from `usage` + model price table, or use `cost-state` snapshots (84 lines) — decide which `mem` should expose.
