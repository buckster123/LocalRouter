# vast_manager.py — Architectural Map
# 3064 lines, single-file TUI for managing LLM endpoints
# Generated from exhaustive line-by-line analysis

## 1. IMPORTS (Lines 11-67)

### Standard Library (11-20)
- os, sys, re, json, signal, subprocess, time
- urllib.request, urllib.parse
- pathlib.Path

### Third-party (22-32) — with ImportError fallback
- questionary, questionary.Style
- rich.console.Console, rich.panel.Panel, rich.table.Table, rich.box

### Optional local modules (50-67) — with stub fallbacks
- usage_tracker: format_summary, check_rate_limit, format_rate_status (L50-59)
- endpoint_proxy: resolve_target (L62-67)


## 2. CONSTANTS / GLOBALS (Lines 35-83, 171-177, 771-772, 1173-1178, 2114-2131)

### Paths (35-47)
- L35  ROOT            = Path(__file__).parent.resolve()
- L36  LAST_INST       = ROOT / ".last_instance"
- L37  TUNNEL_PID      = Path("/tmp/vastai-gguf-tunnel.pid")
- L38  HF_PIN          = ROOT / ".hf_pin"
- L41  PROVIDER_DIR    = Path.home() / ".vastai-gguf"
- L42  PROVIDER_CFG    = PROVIDER_DIR / "config.toml"
- L45  LOCAL_INSTANCES = PROVIDER_DIR / "local_instances"
- L46  LOCAL_LOGS      = PROVIDER_DIR / "local_logs"
- L47  LOCAL_PID_SUFFIX = ".pid"

### Network/Ports (69-72)
- L69  console          = Console()
- L70  LOCAL_PORT       = 8800
- L71  LOCAL_TUNNEL_PORT = 8800
- L72  PROXY_PORT       = 8888

### Style (74-83)
- L74  MENU_STYLE       = Style([...])  (questionary theme)

### Provider defaults (172-177)
- L172 DEFAULT_PROVIDERS = {"together": {"base_url": "https://api.together.ai/v1", "label": "Together AI"}}

### Usage tracking (771-772)
- L771 USAGE_LOG        = PROVIDER_DIR / "usage.log"
- L772 USAGE_DIR        = PROVIDER_DIR

### Sampling presets (1173-1178)
- L1173 SAMPLING_PRESETS = {"thinking": [...], "coding": [...], "nonthinking": [...]}

### Launch wizard enums (2114-2131)
- L2114 GEOS     = {"EU Nordic ...": "EU_NORDIC", "EU Broad ...": "EU", "US": "US", "Any": "ANY"}
- L2121 MODES    = {"thinking ...": "thinking", "coding ...": "coding", "nonthinking ...": "nonthinking"}
- L2127 KV_TYPES = {"q8_0 ...": "q8_0", "q4_0 ...": "q4_0", "bf16 ...": "bf16"}


## 3. CLASSES

None. The entire file is procedural — no classes defined.


## 4. STANDALONE FUNCTIONS (all with line ranges and signatures)

### Config / Recipe Loading
- L87-140    _load_toml(path)                — Minimal TOML parser for recipes.toml subset
- L143-156   load_config()                   — Load recipes.toml, returns (cfg, recipes, gpu_tiers, docker_cfg)
- L159-161   image_for_type(docker_cfg, image_type) — Return docker image string for a given type
- L164-167   cold_start_estimate(image_type)  — Human-readable cold start time estimate

### Provider Config
- L179-219   load_provider_config()           — Load provider API keys from ~/.vastai-gguf/config.toml + env vars
- L222-245   save_provider_config(config)     — Write provider config back to disk
- L248-288   test_together_connection(base_url, api_key) — Test Together AI by listing models, returns (ok, msg)
- L291-321   run_together_completion(base_url, api_key, model_id, prompt) — Quick completion test, returns (ok, msg)

### Together AI Endpoint
- L585-661   activate_together_endpoint(provider_cfg, model_id) — Validate + record Together endpoint in .active_endpoint
- L664-696   get_active_endpoint()            — Get currently active endpoint (Vast/Together/Local)

### Cost Estimation
- L701-752   estimate_cost(ctx_tokens, output_tokens, provider_cfg) — Multi-provider cost estimates
- L755-766   format_cost_comparison(ctx_tokens, output_tokens, provider_cfg) — Format cost as readable string

### Usage Tracking
- L774-775   ensure_usage_dir()               — Create USAGE_DIR if needed
- L777-800   log_completion(provider, model_id, prompt_tokens, completion_tokens) — Log completion to usage.jsonl
- L803-844   get_session_costs()              — Summarize usage costs from log
- L847-862   format_usage_summary(provider_cfg) — Format usage as readable text

### Rate Limiting
- L1031-1062 check_together_rate_limits(provider_cfg) — Check Together API rate limit headers
- L1065-1082 format_rate_limits(provider_cfg)  — Format rate limit info for display

### Shell Helpers
- L1087-1090 capture(cmd, timeout=15)          — Run shell cmd, return (stdout, stderr, rc)
- L1092-1093 run(cmd, **kw)                   — Run shell cmd (fire-and-forget)
  NOTE: L1095-1100 contain DEAD CODE — duplicate capture() and run() definitions
- L1102-1106 last_instance()                  — Read .last_instance file
- L1108-1116 tunnel_running()                 — Check if SSH tunnel PID is alive
- L1118-1125 get_instance_json(inst_id)       — Get Vast instance JSON via vastai CLI
- L1127-1131 get_ssh(inst_id)                 — Extract ssh_host, ssh_port from instance
- L1133-1139 ssh_run(inst_id, remote_cmd, timeout) — Run command over SSH on remote instance
- L1141-1142 ask_back(choices)                — Append "← Back" to choices list
- L1144-1148 hr(title="")                     — Print horizontal rule with optional title
- L1150-1151 press_enter()                    — Wait for Enter keypress
- L1153-1158 _fmt_bytes(n)                    — Format byte count as human-readable string
- L1160-1165 _hf_token()                      — Read cached HuggingFace token
- L1167-1168 _expand_tilde(p)                 — Expand ~ in path and resolve

### Local Endpoint Management
- L1181-1183 _ensure_local_dirs()             — Create LOCAL_INSTANCES and LOCAL_LOGS dirs
- L1186-1272 discover_local(models_dir=None)  — Auto-discover llama.cpp binaries, GGUF models, backends
- L1275-1322 _get_local_server_args(recipe, binary_path) — Convert recipe to llama-server CLI args
- L1325-1461 start_local_instance(recipe)     — Start local llama-server, health-check loop, returns (ok, msg)
- L1464-1521 stop_local_instance(name)        — Stop local instance by SIGTERM/SIGKILL, returns (ok, msg)
- L1524-1535 is_local_running(name)           — Check if named local instance PID is alive
- L1538-1553 list_local_instances()           — List all local instance metadata from JSON files

### Diagnostics Helpers
- L1634-1647 _net_rx_delta(inst_id, seconds=4) — Measure network RX bytes over SSH
- L1813-1824 _get_container_env(inst_id)      — Read env vars from running container process
- L1826-1867 _restart_launch(inst_id)         — Kill stalled download and restart launch.sh

### HuggingFace
- L1921-1930 _hf_list_files(repo_id, token=None) — Fetch file list from HF API

### Vast.ai Offers
- L2023-2110 browse_offers(gpu_key, geo_key, max_price, tier_cfg, num_gpus, min_cuda) — Search/display Vast offers

### Proxy Helpers
- L2809-2835 _proxy_up()                     — Start endpoint_proxy.py as background process
- L2838-2851 _proxy_down(pid_file)           — Stop proxy by SIGTERM
- L2854-2870 tail_proxy_logs()               — Live-tail proxy log file
- L2873-2916 proxy_status_detail()           — Show provider availability table

### Other
- L3005-3011 banner(docker_img)              — Print app banner panel


## 5. MENU FUNCTIONS (interactive TUI screens)

- L326-355   menu_providers(provider_cfg)     — Provider config menu (loop)
- L357-447   _configure_together(provider_cfg) — Together AI config wizard (API key, URL, test)
- L452-580   menu_together_models(provider_cfg) — Browse/pin Together AI models
- L867-1026  menu_batch_compare(provider_cfg) — Side-by-side provider comparison
- L1556-1631 show_status(provider_cfg=None)   — Status panel (not a menu, but display function)
- L1649-1809 menu_diagnose(provider_cfg=None) — Deep diagnostics (usage, SSH probes, stall detection)
- L1871-1917 menu_watch_boot()               — Live boot progress poller
- L1932-2019 menu_hf_browser(recipes)         — HuggingFace model file browser + pin
- L2136-2194 menu_local_config()             — Local hardware scan / config (loop)
- L2197-2270 menu_local_status(provider_cfg=None) — View/manage local instances
- L2273-2363 menu_local_launch(recipes)       — Local endpoint launch wizard
- L2366-2660 menu_launch(recipes, gpu_tiers, docker_cfg, provider_cfg=None) — Main launch wizard (Vast/Local/Together)
- L2664-2681 menu_tunnel()                   — SSH tunnel management (loop)
- L2685-2706 menu_destroy()                  — Destroy Vast instance
- L2710-2740 menu_smoke(provider_cfg=None)   — Smoke test runner
- L2745-2806 menu_proxy()                    — Proxy management (loop)
- L2921-2963 menu_instances()                — List/reattach Vast instances
- L2967-3002 menu_local_dispatch(provider_cfg=None) — Local endpoints umbrella menu (loop)


## 6. MAIN / ENTRY POINT (Lines 3013-3064)

```
main() @ L3013:
  1. load_config() -> cfg, recipes, gpu_tiers, docker_cfg
  2. load_provider_config() -> provider_cfg
  3. Main loop:
     - console.clear()
     - banner(docker_img)
     - show_status(provider_cfg)
     - questionary.select() with 13 choices
     - Dispatch to: menu_launch, menu_local_dispatch, menu_providers,
       menu_together_models, menu_batch_compare, menu_watch_boot,
       menu_diagnose, menu_instances, menu_hf_browser, menu_tunnel,
       menu_smoke, menu_proxy, menu_destroy

if __name__ == "__main__": (L3059)
  try: main()
  except KeyboardInterrupt: exit(0)
```


## 7. FUNCTION CALL GRAPH (key relationships)

```
main()
├── load_config() → _load_toml()
├── load_provider_config() → _load_toml()
├── banner()
├── show_status() → last_instance(), tunnel_running(), get_active_endpoint(),
│                    get_instance_json(), load_config(), format_cost_comparison(),
│                    capture()
├── menu_launch()
│   ├── menu_local_launch() → discover_local(), is_local_running(),
│   │                         start_local_instance() → _get_local_server_args()
│   ├── activate_together_endpoint() → test_together_connection(),
│   │                                   log_completion()
│   ├── browse_offers() → capture()
│   ├── estimate_cost()
│   └── image_for_type(), cold_start_estimate()
├── menu_local_dispatch()
│   ├── menu_local_launch()
│   ├── menu_local_status() → list_local_instances(), stop_local_instance()
│   └── menu_local_config() → discover_local()
├── menu_providers() → _configure_together()
│   └── _configure_together() → test_together_connection(),
│                                log_completion(), save_provider_config()
├── menu_together_models() → test_together_connection()
├── menu_batch_compare() → tunnel_running(), capture(), log_completion()
├── menu_watch_boot() → last_instance(), get_instance_json(), capture(),
│                        tunnel_running()
├── menu_diagnose() → format_summary(), check_rate_limit(), format_rate_status(),
│                      get_instance_json(), ssh_run(), _net_rx_delta(),
│                      _restart_launch() → _get_container_env(), ssh_run()
├── menu_instances() → capture()
├── menu_hf_browser() → _hf_list_files(), _hf_token()
├── menu_tunnel() → tunnel_running(), run()
├── menu_smoke() → get_active_endpoint(), tunnel_running(), run()
├── menu_proxy() → _proxy_up(), _proxy_down(), tail_proxy_logs(),
│                   proxy_status_detail() → resolve_target(), load_provider_config()
└── menu_destroy() → last_instance(), tunnel_running(), run()

get_active_endpoint()
├── is_local_running()
├── last_instance()
└── get_instance_json()
```


## 8. NATURAL MODULE BOUNDARIES — Suggested Split

### A. `config.py` (~160 lines)
Lines: 35-47, 69-83, 87-168, 2114-2131
- Path constants (ROOT, LAST_INST, etc.)
- MENU_STYLE
- _load_toml(), load_config(), image_for_type(), cold_start_estimate()
- GEOS, MODES, KV_TYPES, SAMPLING_PRESETS constants

### B. `providers.py` (~260 lines)
Lines: 172-321, 585-696
- DEFAULT_PROVIDERS
- load_provider_config(), save_provider_config()
- test_together_connection(), run_together_completion()
- activate_together_endpoint(), get_active_endpoint()

### C. `cost.py` (~170 lines)
Lines: 699-862, 1029-1082
- estimate_cost(), format_cost_comparison()
- USAGE_LOG, ensure_usage_dir(), log_completion(), get_session_costs(), format_usage_summary()
- check_together_rate_limits(), format_rate_limits()

### D. `helpers.py` (~100 lines)
Lines: 1085-1168
- capture(), run(), last_instance(), tunnel_running()
- get_instance_json(), get_ssh(), ssh_run()
- ask_back(), hr(), press_enter(), _fmt_bytes(), _hf_token(), _expand_tilde()

### E. `local_endpoint.py` (~400 lines)
Lines: 1171-1553
- SAMPLING_PRESETS (move here or share from config)
- _ensure_local_dirs(), discover_local(), _get_local_server_args()
- start_local_instance(), stop_local_instance(), is_local_running(), list_local_instances()

### F. `vast_ops.py` (~320 lines)
Lines: 1634-1867, 2023-2110
- _net_rx_delta(), _get_container_env(), _restart_launch()
- browse_offers()
- Vast-specific SSH diagnostics

### G. `hf_browser.py` (~100 lines)
Lines: 1919-2019
- _hf_list_files(), menu_hf_browser()

### H. `menus.py` (~900 lines) — or split further into menu_*.py
Lines: 326-580, 867-1026, 1556-1631, 1649-1809, 1871-1917,
       2134-2363, 2366-2660, 2662-2706, 2708-2740, 2743-2916,
       2919-2963, 2967-3064
- All menu_* functions
- show_status(), banner(), main()
- Could further split into:
  - `menus/providers.py` — menu_providers, _configure_together, menu_together_models
  - `menus/local.py` — menu_local_config, menu_local_status, menu_local_launch, menu_local_dispatch
  - `menus/vast.py` — menu_launch, menu_tunnel, menu_destroy, menu_instances, menu_watch_boot
  - `menus/tools.py` — menu_diagnose, menu_smoke, menu_batch_compare, menu_proxy
  - `menus/main.py` — main(), banner(), show_status()


## 9. BUGS / ISSUES NOTICED

1. **DEAD CODE** (L1095-1100): Duplicate `capture()` and `run()` definitions that are
   unreachable because they follow a `return` statement in the first `run()` at L1093.

2. **UNDEFINED VARIABLE** (L2465): `gpu_choices` is referenced but never constructed
   from `gpu_tiers`. The Vast GGUF branch of `menu_launch()` will crash with NameError.

3. **SYNTAX ERROR** (L2897): Unterminated f-string in `proxy_status_detail()`:
   `"-H", f"Authorization: Bearer ***` — missing closing quote and continuation.

4. **DUPLICATE FUNCTION NAMES** (L1092-1093 and L1099-1100): Two `run()` definitions;
   second one shadows first but is unreachable anyway.
