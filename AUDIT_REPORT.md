# vast_manager.py — Comprehensive Code Audit Report

**File:** /home/andre/Projects/qwen36-vast/vast_manager.py (3064 lines)
**Date:** 2026-05-03
**Auditor:** Claude (code audit subagent)

---

## 1. ARCHITECTURE

### Overview
vast_manager.py is a single-file CLI/TUI application using `questionary` for
menu navigation and `rich` for display. It manages LLM endpoints across three
providers: Vast.ai GGUF, Together AI, and Local llama-server.

### Key Components
- **Config loading:** `_load_toml()` (custom minimal TOML parser), `load_config()`, `load_provider_config()`
- **Provider management:** Together AI config/test/activation (lines 170-760)
- **Usage tracking:** `log_completion()`, `get_session_costs()`, `format_usage_summary()` (lines 769-863)
- **Batch comparison:** `menu_batch_compare()` (lines 867-1027)
- **Helpers:** `capture()`, `run()`, SSH helpers, formatting (lines 1085-1170)
- **Local endpoint lifecycle:** `discover_local()`, `start_local_instance()`, `stop_local_instance()`, etc. (lines 1171-1555)
- **Status/diagnostics:** `show_status()`, `menu_diagnose()` (lines 1556-1810)
- **HF browser:** `menu_hf_browser()` (lines 1919-2020)
- **Vast launch wizard:** `menu_launch()` (lines 2366-2660)
- **Tunnel/Destroy/Smoke/Proxy:** Various menus (lines 2662-2917)
- **Main loop:** `main()` with 13 menu items (lines 3013-3064)

### Data Flow
Config is loaded once in `main()`. Provider config lives at `~/.vastai-gguf/config.toml`.
Active endpoint state is tracked via `.active_endpoint` JSON file. Vast instance
state uses `.last_instance`. Local instances use PID files under `~/.vastai-gguf/local_instances/`.

---

## 2. BUGS

### BUG-01: CRITICAL — `gpu_choices` is never defined in `menu_launch()`
- **File:** vast_manager.py, line 2465
- **Description:** After the Together AI flow returns (line 2462), the Vast GGUF
  path falls through to line 2465 which references `gpu_choices` — a variable that
  is NEVER defined anywhere in the function or file. This causes a `NameError`
  crash whenever a user tries to launch a Vast.ai instance.
- **Severity:** CRITICAL — the entire Vast launch flow is broken
- **Fix:** Add GPU tier selection logic before line 2465. Something like:
  ```python
  gpu_choices = {}
  for key, tier in gpu_tiers.items():
      label = tier.get("label", key)
      gpu_choices[label] = key
  ```

### BUG-02: CRITICAL — Syntax error / broken string literal in `proxy_status_detail()`
- **File:** vast_manager.py, lines 2897-2898
- **Description:** The f-string is broken across lines with a missing closing quote
  and concatenation:
  ```python
  "-H", f"Authorization: Bearer ***
       "https://api.together.ai/v1/models"],
  ```
  This is an unterminated f-string. The `***` mask also means the actual API key
  is never sent, so the test always fails. Python may interpret this as implicit
  string concatenation of `f"Authorization: Bearer ***\n                     "` + 
  `"https://api.together.ai/v1/models"` which is mangled nonsense.
- **Severity:** CRITICAL — syntax error that may prevent file from loading, or at
  minimum makes proxy_status_detail always fail
- **Fix:** Should be two separate list elements with proper quoting:
  ```python
  "-H", f"Authorization: Bearer {api_key}",
  "https://api.together.ai/v1/models"],
  ```

### BUG-03: MAJOR — Duplicate `capture()` and `run()` functions (dead code / copy-paste error)
- **File:** vast_manager.py, lines 1087-1100
- **Description:** There are TWO copies of both `capture()` and `run()`. Lines
  1092-1097 contain `run()` followed by unreachable dead code (a second copy of
  `capture()`'s body that references undefined `timeout`). Then lines 1099-1100
  redefine `run()` again. The second `run()` silently replaces the first.
  The dead code at lines 1095-1097 is unreachable (after `return` on line 1093)
  and would crash if reached because `timeout` is not in scope.
- **Severity:** MAJOR — while currently harmless (dead code), it indicates a bad
  merge/paste that could mask real issues
- **Fix:** Delete lines 1095-1100 entirely (keep only the first `capture()` and
  first `run()`).

### BUG-04: MAJOR — `menu_diagnose()` calls `press_enter()` in the middle, then tries to use `inst_id`
- **File:** vast_manager.py, lines 1687-1693
- **Description:** After showing usage summary and rate limits, line 1687 calls
  `press_enter()`, then line 1688 prints "Instance {inst_id} — gathering data..."
  and proceeds with SSH diagnostics. But `inst_id` might be `None` (line 1651
  allows continuing if `get_active_endpoint()` is truthy even when `inst_id` is None).
  Line 1690 calls `get_instance_json(None)` which will fail or return None.
- **Severity:** MAJOR — crashes when using local/Together endpoint without a Vast instance
- **Fix:** Guard lines 1688-1808 with `if inst_id:` or return early after the
  usage summary if no Vast instance exists.

### BUG-05: MAJOR — `ask_back()` double-appends "← Back"
- **File:** vast_manager.py, lines 1141-1142 + 2246 + 2250
- **Description:** `ask_back()` appends "← Back" to the list. But at line 2246,
  `action_choices` already ends with "← Back", and then line 2250 wraps it in
  `ask_back()` again, producing a list with TWO "← Back" entries.
- **Severity:** MINOR — cosmetic but confusing UX
- **Fix:** Remove `ask_back()` wrapper on line 2250, since `action_choices`
  already includes "← Back".

### BUG-06: MINOR — `tail_proxy_logs()` shadows `time` module
- **File:** vast_manager.py, line 2861
- **Description:** `import time as t` shadows the module-level `time` import
  within this function scope. While not a bug per se (it works), it's confusing.
- **Severity:** MINOR
- **Fix:** Use `time.sleep(1)` directly instead of re-importing as `t`.

### BUG-07: MINOR — batch comparison token estimate uses float
- **File:** vast_manager.py, line 1022
- **Description:** `prompt_tokens=len(prompt.split()) * 1.3` passes a float to
  `log_completion()`, which expects an int for token counts. The float propagates
  into the usage log JSON.
- **Severity:** MINOR — doesn't crash but logs dirty data
- **Fix:** `prompt_tokens=int(len(prompt.split()) * 1.3)`

---

## 3. EDGE CASES

### EDGE-01: `_load_toml()` fails on multiline values, inline tables, boolean `true`/`false`
- **File:** vast_manager.py, lines 87-140
- **Severity:** MAJOR
- **Description:** The custom TOML parser only handles simple `key = "string"`,
  integer, float, string arrays, `[table]`, and `[[array]]`. It will silently
  drop or mangle: booleans (`true`/`false`), multiline strings, inline tables
  `{key = val}`, dotted keys like `a.b.c = val`, and `"""` strings.
- **Fix:** Use `tomllib` (Python 3.11+) or `tomli` as a fallback.

### EDGE-02: `discover_local()` can be very slow with large model directories
- **File:** vast_manager.py, line 1254
- **Description:** `d.rglob("*.gguf")` recursively walks the entire HuggingFace
  cache (`~/.cache/huggingface/hub`), which can contain thousands of directories.
- **Fix:** Add a max depth or timeout, or only scan first-level subdirs.

### EDGE-03: No graceful handling of `questionary.ask()` returning `None` in some paths
- **File:** vast_manager.py, multiple locations (e.g. lines 398, 640)
- **Description:** When `questionary.confirm().ask()` returns `None` (Ctrl-C),
  calling `.ask()` on the result is fine, but the subsequent code path may not
  handle `None` properly (e.g. line 640 calls `.ask()` which could be `None`).
- **Fix:** Add explicit `None` checks after every `.ask()` call.

### EDGE-04: Race condition in PID file management
- **File:** vast_manager.py, lines 1335-1343, 1524-1535
- **Description:** PID files are checked and then acted upon non-atomically.
  A process could die between the `os.kill(pid, 0)` check and subsequent operations.
- **Severity:** MINOR — unlikely in practice but possible.

### EDGE-05: `start_local_instance()` leaks file descriptor
- **File:** vast_manager.py, line 1385
- **Description:** `open(log_file, "w")` is passed directly to `Popen(stdout=...)`
  without a context manager. The file descriptor is never explicitly closed.
- **Fix:** Open with a context manager or store the fd and close after Popen.

---

## 4. DEAD CODE

### DEAD-01: Duplicate `capture()` body (unreachable)
- **File:** vast_manager.py, lines 1095-1097
- **Description:** After `return` on line 1093, lines 1095-1097 are dead code —
  a second copy of `capture()`'s body that can never execute.

### DEAD-02: Duplicate `run()` definition
- **File:** vast_manager.py, lines 1092-1093 vs 1099-1100
- **Description:** `run()` is defined twice. The second definition silently
  replaces the first. Both are identical, so no behavioral impact.

### DEAD-03: `format_cost_comparison()` mostly unused
- **File:** vast_manager.py, lines 755-766
- **Description:** `format_cost_comparison()` is called at line 1581 but the
  result is assigned to `cost_str` and never used — immediately followed by
  a hardcoded string `"$0.00x (per-token)"` on line 1582.

### DEAD-04: `_hf_token()` import of re inside `_load_toml`
- **File:** vast_manager.py, line 89
- **Description:** `import re` inside `_load_toml()` is redundant — `re` is
  already imported at module level (line 13).

### DEAD-05: `resolve_target()` fallback uses `LOCAL_PORT` before it's defined
- **File:** vast_manager.py, line 67
- **Description:** The fallback `resolve_target()` references `LOCAL_PORT` which
  is defined later at line 70. This works due to Python's late binding in closures
  but is fragile and confusing.

---

## 5. SECURITY

### SEC-01: MAJOR — Shell injection via `capture()` and `run()`
- **File:** vast_manager.py, lines 1087-1100
- **Description:** Both `capture()` and `run()` use `shell=True` with string
  commands. User-controlled values like `inst_id` (from `.last_instance` file),
  SSH host/port, model paths, etc. are interpolated into shell command strings
  without sanitization. Example: line 1119 `f"vastai show instance {inst_id} --raw"`.
- **Severity:** MAJOR — if `.last_instance` contains `; rm -rf /`, it will execute.
- **Fix:** Use `subprocess.run()` with argument lists instead of `shell=True`, or
  sanitize/validate all interpolated values.

### SEC-02: MAJOR — SSH commands with `StrictHostKeyChecking=no`
- **File:** vast_manager.py, lines 1137-1138, 1896
- **Description:** All SSH commands disable host key checking, enabling MITM attacks.
- **Severity:** MAJOR (though typical for Vast.ai workflows)
- **Fix:** At minimum, document the risk. Ideally, use `StrictHostKeyChecking=accept-new`.

### SEC-03: MINOR — API key written to plaintext file
- **File:** vast_manager.py, lines 222-245
- **Description:** `save_provider_config()` writes API keys in plaintext to
  `~/.vastai-gguf/config.toml`. Also, the API key is written to `.active_endpoint`
  JSON at line 1427.
- **Fix:** Use OS keyring or at minimum set file permissions to 0600.

### SEC-04: MINOR — API key leaked in proxy_status_detail curl command
- **File:** vast_manager.py, line 2897
- **Description:** The masked `***` is clearly wrong (see BUG-02), but even if
  fixed, passing API keys as `-H` arguments makes them visible in `ps` output.
- **Fix:** Use Python's `urllib` instead of shelling out to `curl`.

---

## 6. CODE QUALITY

### CQ-01: File is 3064 lines — far too long for a single module
- **Severity:** MAJOR
- **Fix:** Split into modules: `providers.py`, `local_endpoints.py`, `vast_flow.py`,
  `menus.py`, `helpers.py`.

### CQ-02: `menu_launch()` is ~295 lines (lines 2366-2660)
- **Severity:** MAJOR — single function doing provider selection, model browsing,
  GPU tier selection, offer browsing, env building, and subprocess launch.
- **Fix:** Extract sub-functions for each provider path.

### CQ-03: Massive code duplication in HTTP request handling
- **Description:** The pattern of building `urllib.request.Request`, calling
  `urlopen`, parsing JSON response, handling `HTTPError` is copy-pasted at least
  6 times (lines 258, 300, 414, 469, 611, 955).
- **Fix:** Create a `_api_request(url, headers, payload=None, timeout=15)` helper.

### CQ-04: Inconsistent function naming
- **Description:** Mix of `menu_*` (public menus), `_*` (private helpers),
  and bare names (`capture`, `run`, `hr`, `press_enter`). Some menu functions
  are prefixed `menu_` but `browse_offers` is not.
- **Fix:** Standardize: all menu entry points as `menu_*`, all private as `_*`.

### CQ-05: Missing type hints throughout
- **Severity:** MINOR
- **Description:** No function signatures use type hints, making it harder to
  understand expected types.

### CQ-06: Magic numbers scattered throughout
- **Description:** `60` (health check timeout, line 1434), `12` (offers limit,
  line 2078), `50` (model list limit, line 548), `2000` (log truncation, lines
  2264/2867), `8800` / `8888` / `8100` (ports) — all hardcoded without constants.

---

## 7. TOGETHER AI INTEGRATION

### Status: FUNCTIONAL but with code duplication

**What works:**
- API key configuration and persistence (lines 357-447)
- Connection testing (lines 248-288)
- Completion testing (lines 291-321)
- Model browsing with family grouping (lines 452-580)
- Endpoint activation with smoke test (lines 585-661)
- Model pinning for launch wizard (lines 562-578)
- Rate limit checking (lines 1031-1082)
- Cost estimation (lines 699-766)
- Usage logging (lines 777-801)
- Batch comparison support (lines 867-1027)

**Issues:**
- TOGETHER-01: The completion test code is duplicated 3 times (in `_configure_together`,
  `activate_together_endpoint`, and `run_together_completion`). `run_together_completion()`
  exists but is NEVER CALLED — the other two locations duplicate its logic inline.
- TOGETHER-02: Rate limit checking (`check_together_rate_limits`) probes `/models`
  endpoint which may not return rate limit headers. No fallback.
- TOGETHER-03: Cost estimates use hardcoded pricing that will go stale (lines 718-726).

---

## 8. LOCAL ENDPOINT MANAGEMENT

### Status: WELL IMPLEMENTED — the most complete subsystem

**What works:**
- Binary discovery with backend detection (lines 1186-1272)
- GGUF model scanning with size display (lines 1240-1268)
- Recipe-based launch with full CLI arg building (lines 1275-1322)
- Process lifecycle: start with health polling, graceful stop with SIGTERM/SIGKILL
  escalation, PID management, stale PID cleanup (lines 1325-1535)
- Instance listing with status tracking (lines 1538-1553)
- Log viewing (lines 2257-2264)
- Active endpoint registration (lines 1416-1429)
- Sampling presets (thinking/coding/nonthinking) (lines 1173-1178)

**Issues:**
- LOCAL-01: File descriptor leak in `start_local_instance()` (see EDGE-05)
- LOCAL-02: Backend auto-detection at line 1358-1360 has flawed logic — if
  target_backend is "rocm" but no binary path contains "rocm", it falls through
  to `preferred = b["path"]` for ANY binary (including a vulkan one) due to the
  `or (preferred is None)` clause.
- LOCAL-03: No port conflict detection — if two recipes use the same port,
  both will try to start and the second will fail confusingly.

---

## 9. VAST LAUNCH FLOW

### Status: BROKEN — crashes at `gpu_choices` (BUG-01)

**Trace of the Vast GGUF path through `menu_launch()`:**
1. Line 2394: User selects "Vast GGUF"
2. Lines 2396-2406: `provider_label` check — falls through (not Local, not Together)
3. Line 2465: **CRASH** — `NameError: name 'gpu_choices' is not defined`

**What SHOULD happen (if gpu_choices were defined):**
4. GPU tier selection from `gpu_tiers` dict
5. Recipe filtering by GPU tier
6. Mode selection (thinking/coding/nonthinking)
7. GEO preference selection
8. KV cache type selection
9. Vision (mmproj) option
10. Max price input
11. Offer browsing or auto-select
12. Summary confirmation
13. Environment variable construction
14. `vast_up.sh` subprocess execution

**Additional risks if BUG-01 is fixed:**
- VAST-01: `browse_offers()` uses `shell=True` with user price input (line 2048) —
  if `max_price` contains shell metacharacters, injection is possible.
- VAST-02: `vast_up.sh` is called as a subprocess — if it doesn't exist, the
  error message is cryptic (just shows return code).
- VAST-03: After launch, user is told to use "Watch" and "Tunnel" but there's no
  automatic transition to the boot watcher.

---

## 10. MENU/TUI

### Menu Structure (13 items in main menu)
```
LocalRouter Main Menu
├── Launch      → provider selection → Vast/Local/Together sub-flows
├── Local       → Launch / Status / Configure
├── Providers   → Configure Together AI
├── Together    → model browser with family grouping
├── Batch       → multi-provider comparison
├── Watch       → boot watcher (Vast only)
├── Diagnose    → usage stats, rate limits, SSH diagnostics
├── Instances   → list/reattach Vast instances
├── HF Browse   → HuggingFace model file browser
├── Tunnel      → SSH tunnel up/down/status/logs
├── Smoke       → endpoint smoke test
├── Proxy       → unified proxy server management
└── Exit
```

### UX Issues

**MENU-01:** MAJOR — 13 main menu items is too many. Group related items.
Suggestion: "Vast.ai →" submenu (Launch/Watch/Tunnel/Diagnose/Destroy/Instances),
"Local →" submenu (already exists), "Together →" submenu, "Tools →" (Smoke/Proxy/HF Browse/Batch).

**MENU-02:** MINOR — The `ask_back()` utility is inconsistently used. Some menus
build their own "← Back" (e.g. lines 347, 2246), others use `ask_back()` (line 528),
and one wraps an already-back-containing list with `ask_back()` (line 2250, see BUG-05).

**MENU-03:** MINOR — `console.clear()` in the main loop (line 3018) clears ALL
previous output. If a user wants to scroll back to see a previous result, they can't.
Consider only clearing when entering a menu, not on every loop iteration.

**MENU-04:** MINOR — No keyboard shortcut hints. All 13 items require arrow navigation.
`use_shortcuts=False` is explicitly set (line 3040).

**MENU-05:** MINOR — The Diagnose menu (line 1649-1809) is 160 lines of linear
code with no sub-menu structure. If the user only wants rate limits, they still
have to wait through SSH probes.

**MENU-06:** MINOR — `show_status()` makes network calls to `vastai` and `curl`
on EVERY main menu display (line 3020). This adds latency even when the user just
wants to navigate menus. Consider caching or lazy-loading.

---

## SUMMARY OF CRITICAL FIXES NEEDED

| Priority | ID | Description |
|----------|------|---------------------------------------------|
| P0 | BUG-01 | `gpu_choices` undefined — Vast launch crashes |
| P0 | BUG-02 | Broken string literal in proxy_status_detail |
| P1 | BUG-03 | Duplicate capture()/run() dead code cleanup |
| P1 | BUG-04 | menu_diagnose crashes with no Vast instance |
| P1 | SEC-01 | Shell injection via capture()/run() |
| P2 | CQ-01 | Split 3064-line file into modules |
| P2 | CQ-03 | DRY up HTTP request handling |
| P2 | TOGETHER-01 | Remove duplicated completion test code |
| P2 | LOCAL-02 | Fix backend auto-detection logic |
| P3 | EDGE-01 | Replace custom TOML parser with tomllib |
