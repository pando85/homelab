# Small local models for Kivi: GPU budget, router, STT corrector, local agent, and non-LLM fixes

**Status:** DONE for the PC-only measurements; the real GTX 1060 measurement needs the owner's
approval (proposal in [the design note](../conversation-agent.md#proposed-gitops-change-for-approval))
**Date:** 2026-11-05
**Systems:** homelab node `prusik` (GTX 1060 3 GB, Pascal `sm_61`) read-only; measurements on the
workstation `z800` (RTX 4060 Ti 16 GB, Ryzen 9 5950X 16 cores / 32 threads, DDR4)
**Related:** [conversation agent evaluation](conversation-agent-eval.md) (Kiais metrics and the
harness reused here), [STT hint list](../stt-hint-list-and-learning.md) (uses the prompt numbers of
[section 5](#5-whisper-prompt-hotwords-beam-and-model-size)), satellite-side
[backlog section 12](https://github.com/pando85/webos-voice-satellite/blob/main/docs/project/backlog.md#12-conversation-agent-for-the-kiwi-pipeline)
(formerly EXP-019 of that repository)
**Risk:** read-only on the cluster and Home Assistant; no TV, no sound, no microphone. Nothing was
pulled into the cluster Ollama. Kiais received no requests from this study.
**Scripts:** `scripts/voice-assistant/small_models/` (this study) on top of
`scripts/voice-assistant/agent_eval/` (phrases, fake home, built-in intent matcher, chat harness)

Evidence labels: **documented** (read in source, a manifest or a model card, version named),
**observed** (read from the cluster, Prometheus or Loki on the date above), **measured** (run on
`z800` in this study), **inferred** (computed from the above, not run on the target),
**hypothesis**.

## Question

Can a small model on the cluster's GTX 1060 (or on a node CPU) make the Spanish pipeline "Kiwi"
better, as a router (home command or general question), as a corrector of Whisper transcripts, or
as the home-control agent itself, and how does that compare with non-LLM fixes (Whisper prompt,
model size, Home Assistant's built-in intents)? Owner's target: commands handled locally, fast and
accurate; general and live questions routed to the Kiais gateway.

## Summary

1. **VRAM budget (observed):** Whisper `large-v3-turbo` int8 holds 1140 MiB on the 1060 and peaks
   at 1556 MiB while transcribing; Jellyfin may also use the GPU. The owner set the LLM budget to
   **1.0 GiB**, guarded so it can never take Whisper's burst.
2. **No model that fits 1.0 GiB is a usable Home Assistant agent (measured).** Nine models of
   270 M to 1.5 B parameters passed 0 to 16 % of the control phrases with Home Assistant's tool
   schema; the first usable size is `qwen3:4b-instruct` (65 % of all control phrases, and 10 of 13
   of the phrases the built-in intents miss, against 11 of 13 for Kiais `fast`). It needs about
   2.5 GB plus cache, so on this cluster it would run on the `prusik` CPU or split CPU/GPU.
3. **The router does not need an LLM.** Home Assistant's own strict matching
   (`prefer_local_intents`) already sends every general and live question away (21 of 21) and keeps
   32 of 47 home phrases local. A multilingual sentence-embedding model (278 M) with 3 nearest
   neighbours, behind the built-in match, reaches 66 of 68 (97 %) on the reference texts in tens of
   milliseconds on a CPU. Small LLM routers were worse (section 6).
4. **The Whisper prompt is the cheapest accuracy gain (measured):** area and device names as
   `initial_prompt` raised built-in intent success from 65 % to 70 % at +5 dB of TV content (no
   gain on clean audio); the generic command verbs, **with or without the names**, raised it to
   **85 to 88 % clean, 74 to 75 % at +5 dB and 57 % at 0 dB** (from 82, 65 and 46 %; verbs only:
   52 phrases fixed and 3 broken over the four conditions). A static verbs-only prompt works on the
   deployed image today. `hotwords` gave output identical to `initial_prompt`. Whisper `small` and
   `medium` were significantly worse than the deployed `large-v3-turbo`, and `large-v3` was not
   better.
5. **LLM transcript correctors do not pay off (measured):** every model that fits broke about as
   many commands as it fixed, or more, and some rewrote a command to a different device; the only
   clearly positive one (`qwen2.5:3b`, 1.9 GB) gained less than the Whisper prompt.
6. **Latency on the 1060 (observed from its logs):** Whisper takes 0.78 s plus 0.03 s per second of
   audio (p50 0.86 s), almost independent of the command length, because the encoder always runs a
   30 s window.

## 1. GPU budget on `prusik` (observed 2026-11-05)

| Item | Value | Source |
| --- | --- | --- |
| GPU | GTX 1060 3 GB, 3072 MiB, driver 580.159.03, Pascal `sm_61` | DCGM exporter, `nvidia-smi` |
| Sharing | time-slicing, 8 replicas, no memory isolation (`failRequestsGreaterThanOne: false`) | `system/gpu-operator/time-slicing-config.yaml` |
| GPU pods | `home-assistant-whisper`, `jellyfin-0`, `ollama` (each `nvidia.com/gpu: 1`) | `kubectl get pods` |
| Resident now | one process, 1138 MiB: Whisper. Ollama has no model loaded (`ollama list` empty, 0.40.13); Jellyfin idle | `nvidia-smi` in the DCGM pod |
| Used, 7 and 30 days | min 1118, p95 1140, max **1556 MiB** | `DCGM_FI_DEV_FB_USED` |
| Peaks | 1556 MiB only during transcriptions; encoder and decoder utilisation of NVENC/NVDEC 0 for 30 days | `DCGM_FI_DEV_ENC_UTIL`, `DEC_UTIL` |
| Ollama chart | `ollama/ollama:0.40.13`, chart 1.94.0, `limits.memory: 2Gi`, `nvidia.com/gpu: 1`, `models.pull: []` | `apps/ollama/values.yaml` |
| Whisper | `pando85/wyoming-whisper:2.4.0`, `--model large-v3-turbo --language es --device cuda --compute-type int8`; beam size default 5, no prompt, no VAD filter | `apps/home-assistant/values.yaml`; wyoming-faster-whisper 2.4.0 source |

Budget (inferred): 3072 MiB minus Whisper's peak (1556 MiB) leaves 1516 MiB. A Jellyfin transcode
reserves roughly 250 to 300 MiB (hypothesis, not observed here), and a CUDA context of a second
process costs roughly 100 to 150 MiB on Pascal (hypothesis), so **about 1.0 GiB** remains for an
LLM's weights, KV cache and compute buffers. The owner confirmed 1.0 GiB with Jellyfin coexisting.

Whisper `large-v3-turbo` is not the "small model": it is the second-largest Spanish-capable
Whisper and already takes about half of the card. CTranslate2 runs int8 on `sm_61` as
`int8_float32` (documented, CTranslate2 quantization table).

A risk of the current setup (inferred): Ollama sizes its offload against the free VRAM when it
loads a model. While Whisper is idle it sees about 1.86 GiB free and can take the 416 MiB that
Whisper needs during a transcription, after which Whisper's CUDA allocation fails. The guard is
`OLLAMA_GPU_OVERHEAD` plus a context cap (design note).

### Cluster nodes (observed)

| Node | CPU | RAM | Load now | Fit for CPU inference |
| --- | --- | --- | --- | --- |
| `prusik` | AMD family 25 model 97 (Zen 4 "Raphael", 16 cores / 32 threads, AM5 board X670; inferred Ryzen 9 7950X class), DDR5 | 62 GB, 73 % used | about 1 core | yes; hosts the GPU too |
| `k8s-amd64-1` | AMD family 23 model 104 (Zen 2 mobile, 6 cores / 12 threads, ASUS PN51) | 15 GB | idle | small models only |
| `grigri` | Intel family 6 model 77 (Atom C2000 class, 8 cores), Supermicro A1SAi | 32 GB | 2 cores | no |

The workstation used for the CPU measurements is a Ryzen 9 5950X (Zen 3, 16 cores, DDR4); the
owner considers it similar to the nodes. `prusik`'s Zen 4 with DDR5 and AVX-512 should be as fast
or faster, so CPU figures here are labelled inferred for `prusik`.

## 2. Whisper latency on the 1060 (observed from the Whisper pod's own logs)

`wyoming-faster-whisper` logs `Processing audio with duration ...` before and the transcript after
each request at INFO level (documented, `handler.py` of 2.4.0), so Loki holds a timing per request.
Over 2026-10-16 to 2026-11-05, 445 requests (the transcripts themselves are private and were not
copied into this repository):

| Audio length | n | STT p50 / p90 (s) |
| --- | ---: | --- |
| under 2 s | 132 | 0.84 / 0.89 |
| 2 to 3 s | 104 | 0.85 / 0.90 |
| 3 to 4 s | 74 | 0.85 / 0.91 |
| 4 to 6 s | 35 | 0.88 / 0.96 |
| 6 to 10 s | 73 | 0.93 / 0.99 |
| over 10 s | 25 | 0.89 / 1.06 |

Least squares: STT = 0.78 s + 0.029 s per second of audio. The fixed part is the encoder over a
30 s window. The same model and settings take 0.19 s per item on the 4060 Ti (measured), so the
1060 is about **4.5 times slower on this compute-bound work**; this factor is used below for
prefill estimates (inferred). Decoding of an LLM is memory-bound and scales with bandwidth
(192 against 288 GB/s, about 1.5 times; inferred).

## 3. Method

- **Phrases and fake home:** `agent_eval/phrases_es.yaml` (44 home control, 15 general, 11 live
  information, 6 two-turn follow-ups) and `agent_eval/inventory_es.yaml` (a fake Spanish home). No
  real data.
- **Built-in intents:** `agent_eval/builtin_intents.py` runs hassil 3.12.1 with
  home-assistant-intents 2026.10.6 (the pins of HA 2026.10.4) with the default agent's slot lists
  and the satellite area as context; inferred, not a run of Home Assistant. Of the 44 control
  reference texts, 29 resolve to the expected call; 15 fall through (5 because Home Assistant hands
  `HassMediaSearchAndPlay` to an LLM agent with control, 9 without a match, 1 with no tool twin).
- **Semi-synthetic audio** (`small_models/stt_corpus.py`): the 70 single-turn phrases spoken by the
  seven Spanish Piper voices of the satellite project's Phase 2 corpus, at -22 dBFS active speech,
  clean and mixed with random excerpts of its TV content set (Spanish narration, English film
  dialogue, songs with vocals) at +10, +5 and 0 dB speech-to-content ratio: 1960 clips. The two
  `es_ES-mls_*-low` voices are unintelligible even digitally (also found in the satellite's Phase 2
  session 2) and are excluded from every figure; 5 voices remain (2 ES male and female, 1 ES male,
  2 MX), 350 clips per condition. No room acoustics or echo-canceller residue are modelled.
- **Transcription** (`small_models/stt_eval.py`): faster-whisper 1.2.1 with the call of
  wyoming-faster-whisper 2.4.0 (language `es`, beam 5, optional `initial_prompt`; no VAD, no
  hotwords) on the 4060 Ti. Scored by WER (the satellite's Phase 2 normalisation v1.1 with numerals
  spelled on both sides) and by **built-in intent success**: the share of the 29 handleable control
  phrases whose transcript still resolves to the right call. Paired differences use the exact
  McNemar test; items of the same phrase in different voices are not independent, so p-values are
  indicative.
- **Router** (`small_models/router_eval.py`), **corrector** (`small_models/corrector_eval.py`) and
  **agent** (`agent_eval/run.py` against a private Ollama 0.35.1 bound to `127.0.0.1:11500`, models
  from the Ollama library at their default quantisation, `num_ctx` 8192 as Home Assistant's Ollama
  integration sets by default, thinking off).
- **Latency:** every timing excludes the one-off model load (a warm-up request first; the model
  store was on a hard disk). The workstation was shared with a wake-word training job (load average
  25 to 98) and the GPU with the other runs of this study, so accuracy figures are clean but the
  timing figures of the routers, corrector and agent runs are upper bounds; the dedicated speed
  benchmarks are in [section 9](#9-speed-gpu-cpu-and-hybrid).

## 4. Candidate models and fit

Ollama's own size estimate at load (weights, KV cache and compute graph) on the 4060 Ti, measured;
licences from the model pages (documented).

| Model (Ollama tag) | Quant | Download | Loaded at 2k / 4k / 8k ctx | Tools | Licence | Fits 1.0 GiB? |
| --- | --- | ---: | --- | --- | --- | --- |
| `functiongemma:270m` | Q8_0 | 300 MB | 321 / 323 / 363 MB | yes | Gemma Terms of Use | yes |
| `gemma3:270m` | Q8_0 | 291 MB | not measured | no | Gemma Terms of Use | yes |
| `granite4:350m` | BF16 | 708 MB | 784 / 845 / 994 MB | yes | Apache-2.0 | yes (to 8k) |
| `qwen2.5:0.5b` | Q4_K_M | 397 MB | about 0.4 / 481 / 580 MB | yes | Apache-2.0 | yes |
| `qwen3:0.6b` | Q4_K_M | 522 MB | 693 / 930 MB / 1.4 GB | yes | Apache-2.0 | to 4k |
| `qwen3.5:0.8b` | Q8_0 | 1.3 GB with vision and draft parts | 554 / 560 / 597 MB reported, but **1536 / 1737 MiB** measured at 1k / 4k | yes | Apache-2.0 | no |
| `gemma3:1b` | Q4_K_M | 806 MB | 875 / 877 / 952 MB | no | Gemma Terms of Use | yes, barely |
| `lfm2.5-thinking:1.2b` | Q4_K_M | 731 MB | 814 / 841 / 963 MB | yes | LFM Open License 1.0 (revenue cap, not OSI) | yes, barely |
| `qwen2.5:1.5b` | Q4_K_M | 986 MB | 1.1 / 1.2 / 1.4 GB | yes | Apache-2.0 | no |
| `qwen3:1.7b` | Q4_K_M | 1.4 GB | 1.5 / 1.7 / 2.2 GB | yes | Apache-2.0 | no |
| `llama3.2:1b` | Q8_0 | 1.3 GB | 1.4 / 1.5 / 1.7 GB | yes | Llama 3.2 Community Licence | no |
| `qwen2.5:3b` | Q4_K_M | 1.9 GB | (not loaded on the 1060) | yes | Qwen Research Licence (non-commercial) | no |
| `qwen3:4b-instruct` | Q4_K_M | 2.5 GB | (not loaded on the 1060) | yes | Apache-2.0 | no |
| `granite-embedding:278m`, `paraphrase-multilingual` | F16 | 562 MB each | embedding only | n/a | Apache-2.0 | yes |

Measured VRAM deltas on an otherwise idle card (`nvidia-smi` before and after loading, so they
include the runner's CUDA context): `granite-embedding:278m` 313 MiB, `gemma3:1b` 987 / 1003 MiB at
1k / 4k context, `qwen2.5:1.5b` 1175 / 1261 MiB, `qwen3.5:0.8b` 1536 / 1737 MiB. Ollama's own
estimate is low for `qwen3.5:0.8b` (its hybrid attention and extra parts), so check the real
footprint before trusting an estimate on the 1060. Of the LLMs, only those up to about 0.6 B
parameters at a short context clearly fit 1.0 GiB.

The Home Assistant style system prompt plus the 26 tool definitions of the fake home is **3633
tokens** (measured with `qwen2.5:1.5b`'s tokenizer), so a tool agent needs `num_ctx` of at least
4096, and Home Assistant's Ollama integration uses 8192 by default (documented). A real home with
more exposed entities needs more.

Runtimes (documented unless marked): llama.cpp (the engine inside Ollama) supports `sm_61` with
integer `dp4a` kernels and partial offload (`-ngl`) and AVX-512 on Zen 4; Ollama adds model
management and the API Home Assistant's `ollama` integration speaks; Home Assistant 2026.10.4 also
has a core `llama_cpp` integration (base URL of a `llama-server`) and a core `litellm` integration
(URL plus key, OpenAI client) (documented, `config_flow.py` at tag 2026.10.4). CTranslate2 already
runs Whisper; its LLM support is narrower (hypothesis that it brings nothing over llama.cpp here).
ExLlamaV2, vLLM and TensorRT-LLM rely on FP16 tensor maths; Pascal consumer cards run FP16 at a
small fraction of FP32 speed and lack tensor cores, so they are not realistic on the 1060
(inferred). ONNX Runtime GenAI could run small models on CPU (hypothesis; not measured).

## 5. Whisper: prompt, hotwords, beam and model size

Built-in intent success on the 29 handleable control phrases (145 clips per condition) and WER
over all 70 phrases (350 clips per condition), 4060 Ti, measured.

| Configuration | Clean | +10 dB | +5 dB | 0 dB | WER clean / +5 / 0 dB |
| --- | --- | --- | --- | --- | --- |
| `large-v3-turbo` int8, beam 5 (as deployed) | 119/145 (82 %) | 103 (71 %) | 94 (65 %) | 67 (46 %) | 0.044 / 0.117 / 0.341 |
| same + `initial_prompt` (areas and device names) | 117 (81 %) | 108 (74 %) | 102 (70 %) | 76 (52 %) | 0.044 / 0.111 / 0.296 |
| same + `hotwords` (same text) | identical to the prompt row | | | | |
| same + `initial_prompt` names **and command verbs** | **123 (85 %)** | **114 (79 %)** | **109 (75 %)** | **82 (57 %)** | 0.035 / 0.102 / 0.271 |
| same + `initial_prompt` **command verbs only** (no names) | **128 (88 %)** | **113 (78 %)** | **108 (74 %)** | **83 (57 %)** | 0.033 / 0.103 / 0.279 |
| same + verbs first, then names (the order of wyoming-faster-whisper 3.8.1: static prompt, then `--hass-token` names) | 122 (84 %) | 111 (77 %) | 107 (74 %) | 80 (55 %) | 0.035 / 0.097 / 0.284 |
| `large-v3-turbo`, beam 1 | 116 (80 %) | 104 (72 %) | 89 (61 %) | 59 (41 %) | 0.051 / 0.138 / 0.380 |
| `small` int8, beam 5 | 102 (70 %) | 80 (55 %) | 59 (41 %) | 42 (29 %) | 0.079 / 0.201 / 0.438 |
| `small` + names prompt | 107 (74 %) | 83 (57 %) | 71 (49 %) | 48 (33 %) | 0.068 / 0.176 / 0.415 |
| `medium` int8, beam 5 | 110 (76 %) | 82 (57 %) | 71 (49 %) | 52 (36 %) | 0.050 / 0.149 / 0.406 |
| `medium` + names prompt | 112 (77 %) | 96 (66 %) | 84 (58 %) | 63 (43 %) | 0.042 / 0.112 / 0.323 |
| `large-v3` int8 + names prompt | 122 (84 %) | 109 (75 %) | 91 (63 %) | 76 (52 %) | 0.036 / 0.099 / 0.271 |

Paired, prompt against no prompt (fixed / broken phrases, exact McNemar): clean 1 / 3 (p 0.63),
+10 dB 7 / 2 (p 0.18), +5 dB 12 / 4 (p 0.08), 0 dB 12 / 3 (p 0.035); pooled over the three noisy
conditions **31 / 9**. Model size, paired against `large-v3-turbo` with the same prompt setting:
`small` breaks 18 to 38 phrases per condition and fixes 1 to 7 (p < 0.001 everywhere); `medium`
breaks 14 to 31 and fixes 3 to 9 (p < 0.07); `large-v3` (with the names prompt, against turbo with
it) is level on clean, +10 and 0 dB and worse at +5 dB (6 fixed, 17 broken, p 0.035), and the
largest new process during its run peaked at about 2.4 GiB on the 4060 Ti, so on the 1060 it would
leave no room for anything else. Names and command verbs against no prompt: clean 4 / 0
(p 0.13), +10 dB 13 / 2 (p 0.007), +5 dB 17 / 2 (p 0.001), 0 dB 18 / 3 (p 0.001), pooled noisy
**48 / 7**; against names only: 29 fixed and 4 broken over the four conditions. Beam 1 against
beam 5: pooled noisy 4 / 16 (worse). The names prompt was
`"Kivi, asistente de casa. "` followed by the 32 distinct area and device names and aliases of the
fake home (about 80 tokens); it made Whisper write "Kivi" into 3 of 1400 transcripts whose audio
has no "Kivi" (keep the assistant's name out of the prompt). `hotwords` in faster-whisper fills the
same prompt slot, which explains the identical output.

What still fails with the prompt on clean audio is mostly the **command verb at the start of a
short utterance** ("Reanuda la tele" heard as "Renuda la tele", "Apaga la tele" as "Hasta acá la
tele", "Pausa la tele" as "Pops a la tele", "Apaga la luz" as "Para la luz") and words outside the
names list ("temporizador" as "Tiempoizador", "toldo" as "tolo"). Names alone cannot fix verbs, so a
third variant appends the generic command vocabulary of Home Assistant's Spanish intents
(`"Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio, pon,
temporizador, minutos, volumen, brillo, por ciento, grados."`, no test sentence, about 40 more
tokens; `VERBS_ES` in `stt_eval.py`). The rows in bold, with and without the names, are the
largest gain of the study. Caveat: the verbs are the same generic verbs the test phrases use, as real commands do; the
gain on the owner's own phrasing is to be confirmed with real transcripts (hint-list phase 2).

Verbs only against no prompt: clean 9 / 0 (p 0.004), +10 dB 11 / 1 (p 0.006), +5 dB 15 / 1
(p 0.001), 0 dB 17 / 1 (p < 0.001). Verbs first then names against no prompt: 4 / 1, 10 / 2, 14 / 1,
16 / 3. Against names then verbs, neither variant differs significantly (p 0.06 to 1.0). So on this
fake home **the verbs carry the gain**; the names add nothing measurable, probably because the fake
home's names are common Spanish words that Whisper already knows (real names may differ). Neither
verbs variant leaked "Kivi" (0 of 1400 each).

**For the [hint list](../stt-hint-list-and-learning.md):** put the command verbs in the prompt,
not only names; a static verbs-only `--initial-prompt` already works on the deployed 2.4.0 image
and needs no home data; the 3.8.1 order (verbs first, then the names) is as good. Keep "Kivi" out
(it leaked into 3 of 1400 transcripts in every run whose prompt started with it); the prompt's
latency cost is in [section 9](#9-speed-gpu-cpu-and-hybrid).

## 6. Router

Task: label a transcript "casa" (handled in the house: built-in intents or a local agent) or
"kiais" (general knowledge, chat, open-world live information). Labels: all 44 control phrases plus
"¿Qué tiempo hace fuera?", "¿Qué hora es?" and "¿Qué día es hoy?" are "casa" (47); the 15 general
and the other live phrases are "kiais" (21); the two calendar phrases are not scored. Methods:

- **builtin:** "casa" when Home Assistant's strict built-in matching resolves an intent (what
  `prefer_local_intents` does; `HassMediaSearchAndPlay` and `HassGetState` count as not matched,
  as Home Assistant hands them to an LLM agent with control).
- **embedding:** `granite-embedding:278m` or `paraphrase-multilingual` (both multilingual,
  Apache-2.0), nearest class centroid or 3 nearest neighbours over 80 synthetic training phrases
  written for this study (40 per class, no sentence shared with the test set; in `router_eval.py`).
- **LLM:** JSON-schema constrained `{"ruta": "casa" | "kiais"}` with a Spanish instruction, the
  area and device names and four few-shot pairs; about 400 prompt tokens.
- **builtin + X:** built-in match first, X only for the rest (how X would sit behind
  `prefer_local_intents`).

Reference texts (68 items), measured:

| Method | Accuracy | Home kept local | General/live to Kiais | p50 ms |
| --- | --- | --- | --- | --- |
| builtin | 53 (78 %) | 32/47 | 21/21 | 10 |
| granite-embedding, 3-NN (with or without builtin) | 66 (97 %) | 47/47 | 19/21 | 9 to 89 |
| builtin + granite-embedding, centroid | 66 (97 %) | 45/47 | 21/21 | |
| paraphrase-multilingual, centroid, + builtin | 64 (94 %) | 45/47 | 19/21 | |
| `qwen3.5:0.8b` (with or without builtin; 1.5 GiB, does not fit) | 67 (99 %) | 47/47 | 20/21 | 153 |
| builtin + `qwen3:0.6b` | 65 (96 %) | 44/47 | 21/21 | 77 |
| `gemma3:1b` | 63 (93 %) | 47/47 | 16/21 | 126 |
| `qwen2.5:1.5b` | 60 (88 %) | 47/47 | 13/21 | 100 |
| `qwen2.5:0.5b` | 56 (82 %) | 47/47 | 9/21 | 73 |
| builtin + `granite4:350m` | 53 (78 %) | 44/47 | 9/21 | 74 |

Without the few-shot pairs and without quoting the transcript, every small LLM answered the question
instead of classifying it, and the constrained output fell to the first label for all 68 items: the
prompt format matters more than the model at this size.

Whisper transcripts of the same phrases (5 voices, 340 items per condition, `large-v3-turbo` as
deployed, no prompt), measured:

| Method | Clean: acc. / home local / to Kiais | +5 dB | 0 dB | p50 ms |
| --- | --- | --- | --- | --- |
| builtin | 69 % / 131 of 235 / 105 of 105 | 61 % / 103 / 105 | 54 % / 77 / 105 | 2 |
| granite-embedding 3-NN | 93 % / 233 / 83 | 92 % / 230 / 83 | 83 % / 209 / 73 | 9 to 13 |
| builtin + granite-embedding centroid | 94 % / 218 / 101 | 89 % / 201 / 101 | 79 % / 167 / 103 | |
| builtin + paraphrase-multilingual centroid | 92 % / 219 / 95 | 90 % / 210 / 95 | 84 % / 191 / 93 | |
| `gemma3:1b` | 92 % / 231 / 81 | 91 % / 229 / 80 | 83 % / 203 / 79 | 73 to 147 |
| `qwen2.5:1.5b` | 86 % / 232 / 60 | 84 % / 228 / 59 | 76 % / 194 / 63 | 53 to 108 |
| `qwen2.5:0.5b` | 84 % / 234 / 51 | 83 % / 232 / 50 | 78 % / 215 / 51 | 30 to 73 |

Reading: built-in matching is a perfect "send away" filter (it never claims a general question) but
loses a third to two thirds of the home phrases under STT errors, which then go to the fallback
agent. The embedding router keeps nearly all home phrases local and costs about 10 ms on the
workstation GPU (the CPU figure is in section 9), with errors leaning towards "casa" for general
questions. Which error is cheaper depends on the fallback (design note). Timings are from a shared
GPU and include the HTTP round trip to Ollama.

## 7. STT corrector

Task: rewrite a Whisper transcript into the intended command using only the area and device names
of the home, and return anything that is not about the home unchanged. JSON-constrained output,
Spanish instruction, the name lists, three few-shot pairs (one verb fix, one name fix, one question
left alone). Input: the deployed Whisper's transcripts at +5 and 0 dB (700 items: 290 handleable
control items, 260 general and live items, the other control phrases ignored). Scored by Home
Assistant's built-in matching on the corrected text, measured:

| Corrector | Control pass raw -> corrected | Recovered | Broken | False commands on general/live | General/live text changed | p50 ms (4060 Ti, shared) | Fits 1.0 GiB |
| --- | --- | ---: | ---: | --- | --- | ---: | --- |
| none | 161 -> 161 | 0 | 0 | 0 | 0 | 0 | |
| `granite4:350m` | 161 -> 165 | 4 | 0 | 0 | 28 | 150 | yes |
| `qwen2.5:0.5b` | 161 -> 115 | 6 | 52 | 1 | 29 | 139 | yes |
| `qwen3:0.6b` | 161 -> 143 | 14 | 32 | 1 | 73 | 166 | to 4k |
| `qwen3.5:0.8b` | 161 -> 131 | 13 | 43 | 2 | 115 | 310 | no (1.5 GiB) |
| `gemma3:1b` | 161 -> 145 | 18 | 34 | 1 | 158 | 244 | barely |
| `qwen2.5:1.5b` | 161 -> 165 | 11 | 7 | 1 | 40 | 232 | no |
| `qwen2.5:3b` | 161 -> 174 | 17 | 4 | 1 | 18 | 162 | no |
| for comparison: Whisper `initial_prompt`, names (section 5) | 161 -> 178 | 24 | 7 | | | +0 (inside STT) | no VRAM |
| for comparison: Whisper `initial_prompt`, verbs only (section 5) | 161 -> 191 | 32 | 2 | | | +0 (inside STT) | no VRAM |

Observed behaviour: real fixes ("Apara todas las luces" to "Apaga todas las luces", "pesiana" to
"persiana") come with **wrong-target rewrites** ("Pon la luz de la encimera al máximo" became
"Pon la luz del techo al máximo", which would operate a different device) and with rewrites of
ordinary requests ("Cuéntame un chiste corto" became "¿Te gustaría que te cuente un chiste
corto?"). A corrector sits in front of every request, so its broken and wrong-target cases cost
more than the misses it fixes. The only clearly positive model, `qwen2.5:3b` (+13 net), does not fit
the budget and gains less than the Whisper prompt, which costs no VRAM.

## 8. Local tool agent

The `agent_eval` harness (26 Home Assistant style tools, dry run, fake home, automatic grading),
every category once, against the private Ollama with `num_ctx` 8192 and thinking off, measured.
Pass % excludes "review" phrases. Kiais `fast` from the
[conversation agent evaluation](conversation-agent-eval.md) for comparison (control twice there).

| Model | Control | Control, action | Follow-ups | General | Live | Fall-through 13 | Fits 1.0 GiB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `functiongemma:270m` | 0 % | 0 % | 0 % | 0 % | 0 % | | yes |
| `granite4:350m` | 14 % | 6 % | 8 % | 8 % | 0 % | | yes |
| `qwen2.5:0.5b` | 14 % | 10 % | 0 % | 15 % | 36 % | | yes |
| `qwen3:0.6b` | 0 % | 0 % | 0 % | 46 % | 18 % | | 4k only |
| `qwen3.5:0.8b` | 14 % | 3 % | 50 % | 38 % | 55 % | | no (1.5 GiB) |
| `lfm2.5-thinking:1.2b` | 7 % | 3 % | 25 % | 46 % | 36 % | | barely |
| `qwen2.5:1.5b` | 16 % | 13 % | 33 % | 85 % | 64 % | | no |
| `qwen2.5:3b` | 37 % | 29 % | 50 % | 85 % | 64 % | 7 of 13 | no |
| `qwen3:4b-instruct` | 65 % | 58 % | 83 % | 100 % | 64 % | **10 of 13** | no (2.5 GB) |
| Kiais `fast` | 93 % | 94 % | 100 % | 100 % | 100 % | 11 of 13 | cloud |

"Fall-through 13" are the control phrases Home Assistant's built-in intents do not handle and that
are graded (c05, c13 to c16, c26, c36 to c41, c44): the phrases a fallback agent behind
`prefer_local_intents` actually receives. `qwen3:4b-instruct` passed the colloquial ("Está muy
oscuro aquí", "Quita la tele", "Ponme la tele más bajita", "Apágalo todo en la cocina"), state and
missing-target phrases, and failed "Pon la luz del salón al cuarenta por ciento", "Para el toldo"
and "Pon la siguiente canción en el altavoz de la cocina".

Failure modes of the small models (measured, from the stored calls): they fill every optional slot
(`floor`, `device_class: light`, colour, temperature), so the target no longer resolves, or they
answer in text without calling a tool (`qwen2.5:1.5b` once answered in Chinese). This is not a
prompt-format problem that a few-shot fix removes: the tool schema is Home Assistant's own and
cannot be slimmed without a custom LLM API. Timings of these runs (TTFS p50 0.1 to 1.1 s on the
4060 Ti) are not representative of the 1060 or a CPU; see section 9.

## 9. Speed: GPU, CPU and hybrid

### CPU (measured on the workstation, inferred for `prusik`)

`llama-bench` (llama.cpp CPU build) on the exact GGUF files Ollama downloaded, 2 repetitions, in a
window when the wake-word training was paused (load average 7.6 to 9.5 from other sessions,
recorded per run). Tokens per second; `pp` is prompt processing, `tg` generation.

| Model | 16 threads: pp64 / pp512 / tg | 8 threads: pp64 / pp512 / tg | pp3600 (16 threads) |
| --- | --- | --- | --- |
| `granite-embedding:278m` (F16) | pp16 1761, pp64 2533 | pp16 1390, pp64 1803 | |
| `qwen2.5:0.5b` | 685 / 742 / 96 | 438 / 440 / 98 | 581 |
| `gemma3:1b` | 376 / 402 / 46 | 231 / 247 / 45 | 352 |
| `qwen2.5:1.5b` | 397 / 356 / 36 | 268 / 270 / 39 | 189 (load 18 to 29) |
| `qwen2.5:3b` | 200 / 204 / 20 | 130 / 128 / 20 | |
| `qwen3:4b-instruct` | 152 / 156 / **15.3** | 100 / 98 / 15.6 | 77 ± 17 (load 18 to 29) |

The pp3600 column was measured after the training resumed (load in brackets): long prompts are
also slower per token because attention grows with the context. Generation is memory-bound (8
threads are as fast as 16); prompt processing scales with cores.
`prusik`'s Zen 4 with DDR5 should generate about 1.4 to 1.6 times faster if its memory runs at
4800 MT/s or more (bandwidth ratio; inferred, memory speed not read) and process prompts 1.3 to
1.5 times faster (AVX-512, newer cores; inferred).

Other CPU figures (measured, same window): Whisper `large-v3-turbo` int8 on 16 threads takes
**3.6 s** p50 for 2.7 s of audio (40 clean clips; load 16), against 0.86 s on the 1060; Piper
`es_ES-sharvard-medium` synthesises one sentence in 18 ms ("Hecho.") to 187 ms (76 characters), and
the embedding of a 16-token sentence takes about 9 ms on 16 threads (11.5 ms on 8).

### GPU

Ollama's own timing fields on the 4060 Ti (about 3600-token prompt with no cached prefix, 64
generated tokens, median of 3), measured while the card was shared with the wake-word training
and one Whisper run, so these are lower bounds; the 1060 columns divide prompt processing by the
observed 4.5 (compute-bound) and generation by 1.5 (bandwidth ratio), inferred:

| Model | 4060 Ti pp / tg (tokens/s) | 1060 pp / tg (inferred) | 1060: 3600-token prompt / 35-token call (inferred) |
| --- | --- | --- | --- |
| `qwen2.5:0.5b` | 28 700 / 291 | about 6 400 / 190 | 0.6 s / 0.2 s |
| `gemma3:1b` | 15 200 / 186 | about 3 400 / 124 | 1.1 s / 0.3 s |
| `qwen3.5:0.8b` | 9 300 / 170 | about 2 100 / 113 | 1.7 s / 0.3 s |
| `qwen2.5:1.5b` | 8 800 / 67 (contended) | about 2 000 / 45 or more | 1.8 s / 0.8 s |
| `qwen3:4b-instruct` | 3 400 / 37 (contended) | does not fit | |

`granite4:350m` stopped after one token in this test and is left out. A small router on the 1060
(about 20 new tokens after a cached few-shot prefix and 8 generated) would take roughly 0.05 to
0.1 s (inferred), against about 0.01 s for the embedding model on the CPU.

### Whisper prompt cost

Back to back on the 4060 Ti (100 clean clips each; the GPU was shared with the wake-word
training at 100 % utilisation at the start, so absolute times are upper bounds): p50 / p90 0.192 /
0.227 s without a prompt, 0.187 / 0.198 s with the names, 0.189 / 0.201 s with names and verbs.
**No measurable cost**; on the 1060 the prompt adds well under 0.05 s (inferred: the 120 prompt
tokens are processed once per beam in the decoder, which is small next to the 30 s encoder pass).
The doubled times in the section 5 runs came from GPU contention, not from the prompt.

### Hybrid CPU and 1060 (inferred)

With 1.0 GiB of the 1060, about 11 of the 36 layers of `qwen3:4b-instruct` Q4_K_M (2.3 GiB of
weights) fit next to their share of the KV cache and the compute buffers. Generation time per
token is roughly the sum over layers of bytes over bandwidth: about 70 % of the weights still come
from system memory, so generation would be about 20 % faster than CPU-only, and prompt processing
gains a little more. That does not justify the risk to Whisper's transcription peak.

## 10. End-to-end latency budgets

From the end of speech to the first audio of the answer, p50, for the components that differ
between options (end-of-speech detection, audio transport and playback are the same for all and
excluded). Sources: Whisper on the 1060 0.86 s (observed); prompt +0 to 0.05 s (inferred);
built-in match 2 to 10 ms (measured, hassil) plus the action (tens of ms, inferred); Piper 0.02 to
0.19 s (measured, workstation); Kiais `fast` time to first speech 3.06 s with a tool call and
1.20 s for a general question (observed by the conversation agent evaluation from the workstation;
from the cluster it should be similar).

Local `qwen3:4b-instruct` with a warm prompt cache (inferred from the CPU table): a tool round is
about 30 new prompt tokens (0.2 s) and about 35 generated tokens for the call (2.3 s at 15 tokens
per second), then about 60 tokens of tool result (0.4 s) and the first answer token; the first
sentence (about 12 tokens, 0.8 s) can go to TTS while the rest streams. Workstation: about 3.7 s
to the first sentence; `prusik`: about 2.7 s. **Cold** (first request after a model load or after
the system prompt changed): add the full prompt, about 3600 tokens for the fake home: 23 s at the
pp512 rate to 47 s at the measured pp3600 rate on the workstation, about 16 to 35 s on `prusik`.

| Path | STT | Routing | Agent / intent | TTS first audio | Total p50 |
| --- | --- | --- | --- | --- | --- |
| Built-in intent matched | 0.86 | 0.01 | 0.05 | 0.05 | **about 1.0 s** |
| Command through Kiais `fast` | 0.86 | 0.01 | 3.06 + first sentence 0.1 | 0.1 | about 4.1 s |
| General question through Kiais `fast` | 0.86 | 0.01 | 1.20 + 0.1 | 0.1 | about 2.3 s |
| Command through local `qwen3:4b` on `prusik` CPU (warm) | 0.86 | 0.02 | about 2.7 | 0.1 | about 3.7 s (16 to 35 s cold) |
| Any path with Whisper on CPU instead of the 1060 | 3.6 | | | | +2.8 s on each row |
| Small model (≤ 1.0 GiB) as agent on the 1060 | 0.86 | | about 0.5 to 1.0 (inferred) | 0.1 | about 1.5 to 2 s, but 0 to 16 % correct |

## Conclusion and confidence

- **No small model on the 1060 earns its VRAM.** As the home agent the models that fit fail (0 to
  16 %); as a router an embedding model on the CPU is as good and faster; as an STT corrector they
  break as much as they fix. Confidence high for this fake home and tool schema (the failure modes
  are structural: slot over-filling and no tool call), medium for other homes.
- **The best accuracy gain is the Whisper prompt with the generic command verbs** (names added
  nothing measurable on this fake home; a static verbs-only prompt works on the deployed image).
  Measured on synthetic voices and TV-content mixes: confidence medium that the size of the gain
  carries over to real voices in the room, high that it does not cost latency.
- **A local agent needs at least about 4 B parameters** (`qwen3:4b-instruct`), which on this
  cluster means the `prusik` CPU: about as accurate and about as fast as Kiais `fast` for the
  commands the built-in intents miss, with a cold-start cost of tens of seconds (inferred from the
  workstation; the owner-approval experiment in the design note would measure it).
- The ranked options and the proposed GitOps change are in
  [the design note](../conversation-agent.md).

## Rollback and cleanup

Nothing was changed in the cluster or Home Assistant. Read-only `kubectl exec` was used three times
before the coordinator asked for none (`nvidia-smi` in the DCGM exporter pod, `ollama list` and
`ollama --version` in the Ollama pod); no other command ran in the cluster. Local: the private
Ollama on `127.0.0.1:11500` was stopped and its model store and the Python environment deleted;
the generated audio, the per-item results and the private copy of the Whisper log lines stay
outside this repository.

## Open questions

- The fake home's names were chosen to be natural; the owner's real names, aliases and exposure
  decide the real built-in hit rate. Recheck with the owner's phrases against Home Assistant's
  read-only recognition endpoint.
- Real voices in the room (reverberation, echo-canceller residue, distance) are not modelled; the
  satellite project's real-voice set would confirm the Whisper prompt gain.
- `prusik`'s real CPU speed for `qwen3:4b-instruct` and the cold-start time (owner-approval
  experiment in the design note).
- Whether a router should err towards the local agent or towards Kiais depends on whether a local
  agent is added at all.
