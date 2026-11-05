# Kivi conversation agent: where small local models fit

**Status:** design, ranked additions for the owner's decision. Since 2026-11-05 (about 15:50) Kivi
runs on Kiais `fast` through Home Assistant's core `llama_cpp` integration with
`prefer_local_intents` on ([deployed](kiais-integration.md#deployed-2026-11-05)); this note ranks what
to add to that. This study changed nothing in Home Assistant, the cluster or GitOps state.
**Date:** 2026-11-05
**Evidence:** [small local models study](experiments/small-local-models.md) (this design's
measurements), [conversation agent evaluation](experiments/conversation-agent-eval.md) (Kiais),
[Kiais integration](kiais-integration.md), [STT hint list](stt-hint-list-and-learning.md).
Labels as in those documents: documented, observed, measured (on the workstation), inferred,
hypothesis.

## Goal

Owner's target: home commands handled in the house, fast and accurately; general and live
questions answered by the Kiais gateway. Constraint: the cluster's GTX 1060 has about **1.0 GiB**
left for anything new once Whisper's transcription peak and a possible Jellyfin transcode are
reserved.

## What the measurements say

1. **A model that fits 1.0 GiB cannot be the home agent.** With Home Assistant's own tool schema,
   every model from 270 M to 1.5 B parameters passed 0 to 16 % of the control phrases; the first
   usable size was `qwen3:4b-instruct` (2.5 GB), which handled 10 of the 13 graded phrases that
   Home Assistant's built-in intents miss (Kiais `fast`: 11 of 13).
2. **The best "router" already exists.** With `prefer_local_intents`, Home Assistant's strict
   built-in matching answers the plain commands locally and never claims a general question. What
   it misses (colloquial phrasing, state questions, "pon ... a N" forms, STT errors) is exactly
   what needs a capable agent. A sentence-embedding classifier (278 M, about 10 ms) separates home
   from general requests well (92 % on Whisper output at +5 dB of TV noise) and is cheaper and as
   accurate as any small LLM router, but it only pays off if a local agent exists to send the home
   part to.
3. **Small LLMs do not fix Whisper's errors.** As transcript correctors they recovered about as many
   commands as they broke, and they rewrote non-command questions (section 7 of the study).
4. **The useful non-LLM fix is the Whisper prompt, and the verbs carry it.** The generic command
   verbs as `initial_prompt` (with or without the home's names) raised built-in intent success from
   82 to 88 % on clean synthetic audio and from 65 to 74 % at +5 dB of TV noise; names alone gave
   less (70 % at +5 dB, nothing on clean audio). Whisper `small`, `medium` and `large-v3` are not
   better than the deployed `large-v3-turbo`.
5. **Whisper itself costs 0.86 s on the 1060** (observed), almost independent of how long the
   command is.

## Candidate configurations, ranked

Latency is from the end of the user's speech to the first audio of the answer, at the median,
excluding end-of-speech detection and audio transport (the same for every option). Components:
Whisper on the 1060 0.86 s (observed); built-in intent match about 0.01 s (measured) plus the
action; Piper first audio 0.02 to 0.19 s for one sentence (measured, workstation CPU); Kiais `fast`
time to first speech 3.06 s for a home command with a tool call and 1.20 s for a general question
(observed by the conversation agent evaluation from the workstation); `qwen3:4b-instruct` on the
`prusik` CPU about 2.7 s for a tool round plus the answer's first sentence with a warm prompt
cache (inferred from the workstation benchmark, where it is 3.7 s; study section 10). Command
accuracy is on the fake home's 44 control phrases (reference text), which is optimistic for real audio (study
section 5).

| Rank | Configuration | Home commands: accuracy / latency | General and live | New VRAM / CPU | Privacy | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| live | **A. Built-in first, Kiais `fast` behind it** (`prefer_local_intents` on; deployed), **plus the Whisper prompt with the command verbs** (the addition ranked first) | 41 of 43 graded (95 %: 30 by built-in intents, 11 of the other 13 by Kiais); **about 1.0 s** when the built-in intents match (30 of 44 reference texts; 56 % of the control clips of clean synthetic audio without the prompt, 44 % at +5 dB of TV noise), about 4.1 s when Kiais handles it | Kiais: 100 % on the set, about 2.3 s | none | the unmatched third of commands and all general questions leave the house, with the exposed names in the system prompt | agent and settings done; the Whisper prompt is low effort ([hint list](stt-hint-list-and-learning.md)) |
| 2 | **B. A, plus a local `qwen3:4b-instruct` on the `prusik` CPU for home requests the built-in intents miss; an embedding router sends the rest to Kiais** | 40 of 43 (93 %: 30 built-in, 10 of 13 local); about 1.0 s matched, **about 3.7 s** local fallback (4.7 s at the workstation's speed), plus 16 to 35 s once after every model load or prompt change | Kiais, as in A, plus about 0.01 s for the router (built-in match first, then the embedding centroid: 4 % of general questions misrouted to the local model and 7 % of home phrases sent to Kiais, on Whisper output of clean audio) | 0 VRAM; 4 to 6 GiB RAM and bursts of 8 to 16 cores on `prusik` | home commands stay in the house | high: a router component (custom conversation agent or inside Kiais), an Ollama on CPU, cache care |
| 3 | B with the model split between CPU and the 1060 (about 11 of 36 layers in 1.0 GiB) | as B, decode about 20 % faster (inferred) | as B | 1.0 GiB VRAM | as B | as B plus the VRAM guard; risks Whisper's burst; not worth it |
| 4 | A small LLM (≤ 1.0 GiB) on the 1060 as router or STT corrector | router: no gain over the embedding model on CPU; corrector: net loss or wrong targets for every model that fits | | 0.4 to 1.0 GiB | as A | medium; **not recommended** |
| 5 | A small LLM (≤ 1.0 GiB) as the home agent | 0 to 16 % | 0 to 85 % | 0.4 to 1.0 GiB | local | **rejected** |
| 6 | Whisper on CPU, the whole 1060 for an LLM | every command +2.8 s (Whisper `large-v3-turbo` takes 3.6 s on 16 CPU threads, measured), and the only agent that fits 3 GB (`qwen2.5:3b`) passed 37 % | | 0 / 16 cores per request | local | **rejected** |
| 7 | Smaller Whisper (`small`, `medium`) to free VRAM | built-in intent success drops 6 to 24 points (measured) | | frees 0.3 to 0.8 GiB | | **rejected** |

**Recommendation, as additions to the live setup:**

1. **Add the Whisper prompt with the command verbs now** (a static `--initial-prompt`, below), and
   the home's names later with the [hint list](stt-hint-list-and-learning.md) plan. It moves more
   commands onto the 1 s built-in path: built-in intent success from 82 to 88 % on clean synthetic
   audio and from 65 to 74 % at +5 dB of TV noise (measured), with no measurable latency and no
   VRAM. The rest of the live setup stays as it is.
2. **Optional: a local fallback agent** (B: `qwen3:4b-instruct` on the `prusik` CPU, an embedding
   router in front). Worth it only if home commands should not leave the house, or as resilience:
   Kiais `fast` returned 783 errors in two days of October (conversation agent evaluation), and
   the live pipeline has no second agent. It costs about the same latency as Kiais for those
   commands, a custom routing component and RAM and CPU on `prusik`.
3. **Do not add a small model on the 1060** as router, corrector or agent (rows 4 and 5), and keep
   Whisper `large-v3-turbo` on the GPU (rows 6 and 7).

## Proposed GitOps change for approval

**For the first addition (Whisper prompt): no Ollama change.** The pipeline settings and the
Kiais agent are already deployed ([integration note](kiais-integration.md)). The verbs-only prompt
works on the deployed image (`--initial-prompt` exists in wyoming-faster-whisper 2.4.0,
documented) and contains no home data, so it can go into `apps/home-assistant/values.yaml` as it
is (coordinate with the [hint list](stt-hint-list-and-learning.md)'s image change, which keeps a
static prompt before the names):

```diff
           args:
             - --model
             - large-v3-turbo
             - --language
             - es
             - --device
             - cuda
             - --compute-type
             - int8
+            - --initial-prompt
+            - "Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio, pon, temporizador, minutos, volumen, brillo, por ciento, grados."
```

Rollback: revert the commit; ArgoCD restarts the Whisper pod either way (about 30 s without STT,
model already on the volume). Check after the change: Whisper's per-request time in Loki stays at
about 0.86 s, and the share of "no intent matched" answers does not rise. The in-cluster Ollama stays as it is (idle, no
model, GPU reserved but unused); removing it is a separate decision.

**For B (only if chosen): Ollama on the `prusik` CPU, the GPU left to Whisper and Jellyfin.**
Proposed diff to `apps/ollama/values.yaml` (inferred sizes: `qwen3:4b-instruct` Q4_K_M is 2.5 GB
on disk; resident about 3 GB plus 1.2 GB of KV cache at 8192 tokens; the embedding model 0.6 GB):

```diff
 ollama:
   ollama:
     gpu:
-      enabled: true
+      enabled: false
       type: 'nvidia'
     models:
-      pull: []
-        # - qwen2.5:3b
+      pull:
+        - qwen3:4b-instruct
+        - granite-embedding:278m
       run: []

   extraEnv:
-    - name: NVIDIA_VISIBLE_DEVICES
-      value: all
     - name: TZ
       value: Europe/Madrid
     - name: OLLAMA_DEBUG
       value: "0"
+    - name: OLLAMA_KEEP_ALIVE        # no cold load (16 to 35 s of prompt prefill) between requests
+      value: "-1"
+    - name: OLLAMA_NUM_PARALLEL      # one cache slot: keeps the agent's system prompt cached
+      value: "1"
+    - name: OLLAMA_MAX_LOADED_MODELS # agent and embedding model resident together
+      value: "2"
+    - name: OLLAMA_CONTEXT_LENGTH
+      value: "8192"
   ...
   resources:
     requests:
-      cpu: 10m
-      memory: 20Mi
+      cpu: "1"
+      memory: 4Gi
     limits:
-      memory: 2Gi
-      nvidia.com/gpu: 1
+      memory: 6Gi

-  runtimeClassName: nvidia
+  nodeSelector:
+    kubernetes.io/hostname: prusik
```

No CPU limit, so a request can use the idle cores of `prusik` (about 1 of 32 busy, observed);
`prusik` had about 17 GB of RAM free (observed). Rollback: `git revert` of the commit and an ArgoCD
sync; the pulled models stay on the `ollama-data` volume until removed with `ollama rm`.

If the owner prefers to keep a GPU option open, the guard for a model on the 1060 next to Whisper
is `OLLAMA_GPU_OVERHEAD: "943718400"` (900 MiB reserved: with Whisper idle Ollama then sees about
1.0 GiB, and less while Whisper or Jellyfin run) plus `OLLAMA_NUM_PARALLEL: "1"` and a context
cap; the variable exists in Ollama 0.35.1 (documented, `ollama serve --help`) and must be checked on
0.40.13.

## Owner-approval experiment on the real cluster

Only needed if B (or a GPU model) is pursued; A needs no model measurement. Nothing below has been
run.

- **Question:** do the workstation-derived figures hold on `prusik`: `qwen3:4b-instruct` speed on its
  CPU (inferred 20 to 23 tokens per second generated, 200 to 230 per second of prompt), the time to
  first speech with a warm prompt cache, and no effect on Whisper?
- **Prerequisites:** the owner's approval of the B diff above (commit, push, ArgoCD sync); the model
  pulls by the chart's `models.pull` (about 3.1 GB on the `ollama-data` volume).
- **Steps (read-only after the sync, over the existing ingress, no `kubectl exec`):**
  1. Baseline for 10 minutes: `DCGM_FI_DEV_FB_USED`, `node_cpu_seconds_total` of `prusik`, and
     Whisper's per-request time from its Loki lines (as in study section 2).
  2. Speed: five `POST https://ollama.internal.grigri.cloud/api/generate` with `num_predict` 64
     and `options.num_ctx` 8192; read `prompt_eval_count`, `prompt_eval_duration`, `eval_count`,
     `eval_duration` from the responses.
  3. Agent: `scripts/voice-assistant/agent_eval/run.py run --base-url
     https://ollama.internal.grigri.cloud/v1 --model qwen3:4b-instruct --categories control,followup
     --repeat-control 2 --warmup --extra-body '{"reasoning_effort":"none"}'` (synthetic phrases,
     dry run, nothing executed in Home Assistant). Record the time to first speech of the second
     and later requests (warm cache) separately from the first.
  4. Router: `small_models/router_eval.py --server https://ollama.internal.grigri.cloud --methods
     emb:granite-embedding:278m`.
  5. Repeat step 1 while step 3 runs: Whisper latency and VRAM must not change; `prusik` CPU
     stays below about 50 % on average.
- **Expected:** decode at least 15 tokens per second, warm time to first speech under 4 s,
  Whisper p50 still about 0.86 s.
- **Failure interpretation:** slower than 10 tokens per second or Whisper affected means B is not
  viable on this node; keep A.
- **Duration:** about 30 minutes plus the pull. **Rollback:** `git revert` of the values change and
  an ArgoCD sync; remove the models with `ollama rm` through the API if the space is needed.

A GPU variant (a model on the 1060 next to Whisper) is not proposed: no role in this study needs
it. If the owner wants the real 1060 numbers anyway, add `OLLAMA_GPU_OVERHEAD: "943718400"` and
`OLLAMA_NUM_PARALLEL: "1"`, keep `gpu.enabled`, pull `qwen2.5:0.5b` (0.4 GB), and measure as in
step 2 while watching `DCGM_FI_DEV_FB_USED` stay below about 2.6 GiB during a transcription.

## Wiring in Home Assistant 2026.10 (feasibility)

| Piece | Where it can live | Status | Effort |
| --- | --- | --- | --- |
| Built-in intents first | pipeline option `prefer_local_intents` (since 2024.12); strict matching only, no fuzzy matching for Spanish (the 2026.10 gazetteer fallback is English only and not on this path) | documented, 2026.10.4 source | a UI setting (owner's yes) |
| Kiais as the agent | core `llama_cpp` integration with Kiais' base URL ([integration note](kiais-integration.md)) | deployed 2026-11-05 | done |
| Whisper prompt | `--initial-prompt` exists in the deployed wyoming-faster-whisper 2.4.0; 3.6.0 and later add `--hass-token` (names fetched from Home Assistant); the command verbs need the static part | documented | low ([hint list](stt-hint-list-and-learning.md)) |
| Local agent (B) | core `ollama` integration against the in-cluster Ollama (`num_ctx` default 8192, `keep_alive` default -1, `think` off) | documented | low once Ollama runs |
| Router (B) | Home Assistant has one agent per pipeline and no fallback chain (documented). Options: a small custom conversation agent that embeds the text and calls `conversation.async_converse` with the local or the Kiais agent id (the function is exported, documented); or a "kivi" model inside the Kiais gateway that forwards home requests to the local model (hypothesis: depends on Kiais' code) | design only | about 200 lines plus tests, or a Kiais change |
| STT corrector | a Wyoming STT proxy in front of Whisper that rewrites the transcript (Wyoming RapidFuzz Proxy does this without an LLM, unverified), or a wrapping custom conversation agent | not recommended by the measurements | medium |

Cache care for B (inferred from Home Assistant 2026.10.4's `chat_log.py`, documented): the system
prompt is the instructions, the Assist API prompt with the static list of exposed names, and the
date and time only when no `GetDateTime` tool is offered. With the Assist API it is stable between
requests, so llama.cpp's prefix cache keeps prompt processing to the new turn (tens of tokens).
Anything else that uses the same model and slot with a different prompt (a router call, another
client) evicts the cache, and the next command pays the full prefill (about 3600 tokens for the
fake home: 16 to 47 s on a CPU). Use a separate model for the router (the embedding model is) and
no other clients on the agent model.

## Open questions for the owner

1. **Privacy and resilience:** is it acceptable that home commands the built-in intents miss
   (about a third on clean audio, more under TV noise) go to Kiais together with the exposed names,
   and that Kivi cannot answer them when Kiais is down, or should a local fallback (B) be added?
2. **Latency:** is about 4 s acceptable for those commands? Neither A nor B gets them under 3 s on
   this hardware; only the built-in path (about 1 s) does.
3. **Whisper prompt:** approve the verbs-only `--initial-prompt` change now (generic text, no home
   data)? The names can follow with `--hass-token` (they stay in the cluster).
4. **Ollama:** keep the idle in-cluster Ollama (it holds a GPU slot and a volume but no model), move
   it to CPU for B, or remove it?
5. **Real names:** the built-in hit rate depends on the real entity names, aliases and exposure; may
   an agent check the owner's phrases against Home Assistant's read-only recognition endpoint
   (`conversation/agent/homeassistant/debug`, executes nothing) to replace the fake-home figures?
6. **B's cost:** if B is wanted, approve the Ollama-on-CPU diff and the experiment above (4 to 6 GiB
   RAM and CPU bursts on `prusik`).
