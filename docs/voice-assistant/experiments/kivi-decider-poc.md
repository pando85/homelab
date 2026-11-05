# Kivi decider PoC: is there headroom behind the built-in intents?

**Status:** P0 headroom analysis DONE (offline, synthetic); P1 to P3 (data at scale, fine-tuning,
gates) NOT STARTED by owner decision, pending this analysis and the
[zero-shot baselines](decision-models.md)
**Date:** 2026-11-05
**Systems:** workstation `z800` (Ryzen 9 5950X, RTX 4060 Ti 16 GB, shared with the wake-word
training); nothing ran on the cluster, Home Assistant or a TV
**Related:** [Kivi plan, workstream d](../plan.md), [decision models (P0 zero-shot)](decision-models.md),
[small local models](small-local-models.md) (corpus, Whisper prompt, GPU budget),
[conversation agent evaluation](conversation-agent-eval.md) (phrases, fake home, grader)
**Risk:** offline. No cloud request (a key was looked up by name, never used, and the file holding it
was shredded), no Home Assistant access, no real names. All audio is synthetic (Piper voices, TV
content mixes).
**Scripts:** `scripts/voice-assistant/decider_poc/` on top of `agent_eval/` and `small_models/`

Evidence labels: **documented** (model card, package source), **measured** (run on `z800` here),
**inferred** (computed from the above), **hypothesis**. Every built-in intent figure is an
inference of Home Assistant's behaviour: hassil 3.12.1 with home-assistant-intents 2026.10.6 (the
pins of HA 2026.10.4), the slot lists of a fake home and `prefer_local_intents` semantics, as in
`agent_eval/builtin_intents.py`. It is not a run of Home Assistant.

## Question

The planned "Kivi decider" is a small local model behind Home Assistant's built-in intents. When
the built-in sentences miss, it decides "home command, which intent, which target" and executes;
anything uncertain goes to Kiais. Before data, training or a teacher: **how many commands do the
built-in intents miss in a way such a decider could rescue, and how many of those a cheap fix
(aliases, custom sentences) already covers?**

## Summary

1. **Teacher (P0b): not feasible locally (documented).** `pplx-decider-v1.1-27b` needs about
   49 GiB of bf16 weights on one CUDA GPU and its inference code has no quantised path. Cloud
   access is ruled out by the owner. The teacher was dropped without downloading the weights. If
   P2 ever runs, it trains on gold labels only (or Laya as a teacher).
2. **The hand-written corpus (39 commands, 5 voices, verbs prompt):** the built-in intents miss
   **34 % of commands on clean audio, 45 % at +5 dB and 57 % at 0 dB** of TV content. Of all
   commands, a decider could rescue at most **20 % (b1 phrasing) + 3 % (b2 near-miss)**. Another
   12 % are misheard beyond recovery and 8 % lost only their verb. Upper bound of time saved:
   **0.7 s per command** (b × 3 s / all commands). **Custom sentences cover 8 of the 10 phrasing
   misses** and cut that bound to **0.25 s**.
3. **`kivi-fake-v1`** (new, shared with the zero-shot study): 2273 synthetic items over four fake
   homes (39 to 116 entities) and 17 phrasing or negative styles. 673 items are paraphrases by a
   local `qwen3:14b`, validated to keep their label; hand-checked label noise is about 2 % (5 %
   counting marginal cases). 500 items also have audio (clean, +5, 0 dB; Whisper `large-v3-turbo`
   with the verbs prompt).
4. **On `kivi-fake-v1` text, the built-in intents get 22 % of commands right** (44 % for the direct
   and polite templates, 4 to 7 % for colloquial, LatAm, word-order and disfluent phrasing, 0 % for
   "un poco más"). They take no wrong action on commands. On route-Kiais items they act only on
   bare device names ("Aire": HA's own sentences turn a bare name on).
5. **Cheap fix versus model:** 92 custom sentences, written in about an hour from the template
   families, lift built-in success on the **templates from 21 % to 70 % (0 wrong actions)** but on
   the **LLM paraphrases of the same commands only from 25 % to 33 %**. They cover 62 % of
   the misses whose phrasing they were written for and **11 % of misses in unseen phrasing**. Unseen phrasing
   is the room left for a model. How large that room is depends on how varied the household's real
   phrasing is, which only the real-usage replay (workstream c) can tell.
6. **Audio (500 items, 302 commands):** with the built-in sentences, misheard commands (a) are
   8 / 16 / 29 % at clean / +5 / 0 dB and rescuable phrasing (b1 + b2) is 64 / 58 / 50 %. That bounds
   the saving at **1.9 / 1.8 / 1.5 s per command**, or 1.2 / 1.2 / 1.0 s with the custom sentences.
   On this deliberately varied mix, phrasing outweighs Whisper errors except at 0 dB.

Verdict for the owner: there is real headroom on synthetic data, largest for phrasing nobody
anticipated. Custom sentences remove most of it for phrasing someone did anticipate, at no runtime
cost. Recommendation: **ship the custom sentences first** (after a review against the real names),
then decide P1 to P3 on the real-usage replay and the zero-shot baselines. Build the decider only if
real misses look like the "unseen phrasing" rows here, not like the misheard rows.

## 1. Teacher (P0b): `pplx-decider-v1.1-27b`

| Item | Finding | Label |
| --- | --- | --- |
| Weights | Apache-2.0, `Qwen3_5Model` backbone (Qwen3.8-27B) plus a BF16 `[255, 5120]` readout; the card asks for "a CUDA GPU with room for roughly 49 GiB of weights plus working memory" | documented (model card) |
| Inference code | `source/src/autojev/model.py` loads bf16 on CUDA and float32 on CPU (about 108 GB); no quantised loading; non-causal full-attention layers that a generic runtime does not reproduce | documented (source, read without running) |
| API | Perplexity `/v1/decisions` needs a Perplexity key (none exists), the model is not listed on OpenRouter, and the owner ruled out cloud calls | documented, observed |
| This PC | 16 GB VRAM, of which about 8 GB in use by the wake-word training at the time; 62 GB RAM, about 27 GB available | observed |

A 4-bit load would need a patched loader (bitsandbytes) and CPU offload of about half the layers.
bitsandbytes cannot offload 4-bit weights (they would stay fp32 on the CPU, about 28 GB). It also
risks breaking the non-causal attention setup. That is over the budget of about 1 h of setup and
1 example/s, so the teacher was dropped (inferred). No weights were downloaded. If P2 runs, it
uses gold labels from the generator, optionally with Laya soft labels.

## 2. Headroom on the existing transcript corpus

Corpus of the [small-models study](small-local-models.md#3-method): the 39 control phrases of
`agent_eval/phrases_es.yaml` with a tool expectation (state questions c35 to c37, c43 and the
missing-target c44 are not commands), 5 intelligible Piper voices, clean and +10 / +5 / 0 dB of TV
content. Transcripts: `large-v3-turbo` int8 with the static verbs-only `initial_prompt` (run
`turbo_b5_vonly`).

Rules (`decider_poc/headroom.py`), for each command the built-in intents miss (no match, a match HA
ignores for an LLM agent, or an unresolved target):

- **Key words:** the words of the reference text after the Phase 2 normalisation (numbers spelled
  out), minus articles, prepositions and fillers. A key word survives if it occurs in the
  transcript exactly, or fuzzily (rapidfuzz ratio ≥ 80 against a word or a two-word join). The
  spoken number must be parsed back unchanged.
- **a (misheard):** a key word or the number is lost; intent or target cannot be recovered from the
  text. **a-verb:** only the leading verb is lost ("Con un temporizador de 10 minutos"); target and
  value survive, so the intent is sometimes inferable (counted apart, not in the bound).
- **b1 (phrasing):** all key words survive and the reference text itself is not handled by the
  built-in sentences: a decider or a custom sentence can rescue it.
- **b2 (near-miss):** all key words survive but some only fuzzily ("pesiana", "Apara"), and the
  reference is handled: strict matching breaks on a small mishearing that a robust decider could
  absorb.
- **c (other):** all key words survive exactly and the reference is handled, yet the transcript
  misses (extra words, number format).

Built-in sentences only (measured; % of all commands of the condition):

| Condition | Commands | Built-in correct | Built-in wrong | Miss | a misheard | a-verb | b1 phrasing | b2 near-miss | c other | Upper bound (b1+b2) × 3 s per command |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| clean | 195 | 128 (66 %) | 0 | 67 (34 %) | 6 (3 %) | 10 (5 %) | 47 (24 %) | 4 (2 %) | 0 | 0.78 s |
| +10 dB | 195 | 113 (58 %) | 1 | 81 (42 %) | 9 (5 %) | 19 (10 %) | 42 (22 %) | 9 (5 %) | 2 (1 %) | 0.78 s |
| +5 dB | 195 | 108 (55 %) | 0 | 87 (45 %) | 22 (11 %) | 17 (9 %) | 41 (21 %) | 7 (4 %) | 0 | 0.74 s |
| 0 dB | 195 | 83 (43 %) | 1 | 111 (57 %) | 58 (30 %) | 16 (8 %) | 27 (14 %) | 5 (3 %) | 5 (3 %) | 0.49 s |
| all | 780 | 432 (55 %) | 2 | 346 (44 %) | 95 (12 %) | 62 (8 %) | 157 (20 %) | 25 (3 %) | 7 (1 %) | 0.70 s |

Spot check (by hand, 6 to 8 per category): every sampled a-item had lost the device or verb
("Pon al volumen del Atelier 30", "Mirá."); every b1 item was a correct transcript of a phrase
the built-in sentences do not cover; b2 items were one-letter slips ("Abre el tolo de la terraza",
"Renuda la tele"); c items had trailing TV words ("Silencia la tele. Jerk, Tom.").

The b1 misses come from 10 phrases: c15 "Pon el aire del salón a 22 grados", c40 "Ponme la tele más
bajita", c41 "Apágalo todo en la cocina", c38 "Está muy oscuro aquí", c26 "Pon la siguiente canción
en el altavoz de la cocina", c05 "Pon la luz del salón al cuarenta por ciento", c13 "Pon la persiana
del salón a la mitad", c14 "Para el toldo", c39 "Quita la tele", c16 "Pon la temperatura del
dormitorio a veintiuno y medio". Several of them fall through because "pon ..." matches
`HassMediaSearchAndPlay`, which `prefer_local_intents` ignores when the agent is an LLM.

**Cheap fix (measured):** with `decider_poc/custom_sentences_es.yaml` matched before the built-in
sentences, as Home Assistant's default agent does, 37 of the 39 reference texts resolve (all but c16
"veintiuno y medio" and the implicit c38). Built-in success rises to 83 / 74 / 71 / 52 % (clean,
+10, +5, 0 dB), b1 falls to 4 % of commands and the bound to **0.25 s per command**, with no new wrong
action. Caveat: the sentences were written after reading these phrases, so on this corpus they are
not a blind test (the paraphrase result in section 4 is).

## 3. Dataset `kivi-fake-v1`

Location: `/work/workspaces/kivi-decider-poc/data/kivi-fake-v1/` on the workstation (not in git).
Schema: `SCHEMA.md` next to the data. Generators: `decider_poc/fake_v1.py` (homes, templates,
negatives) and `decider_poc/paraphrase.py` (local LLM paraphrases and validation). All names are
invented; nothing comes from the owner's Home Assistant.

| Home | Naming style | Entities exposed | Target options | Items |
| --- | --- | ---: | ---: | ---: |
| `generica` | generic, the room repeated in the name ("Luz del salón", "Persiana de invitados") | 39 | 46 | 570 |
| `coloquial` | colloquial and brand-like invented names with aliases ("Lámpara Arco", "Split", "Altavoz Lumo") | 56 | 46 | 577 |
| `personas` | rooms named after invented people ("Cuarto de Candela"), the same device names in many rooms, two floors | 77 | 67 | 558 |
| `latam` | Mexican Spanish ("recámara", "foco", "pantalla", "bocina", "clima"), many duplicates, two floors | 116 | 92 | 568 |

Sensors fill the homes to a realistic count; only controllable domains are targets. Target options
are the entities plus per-area groups (lights, covers, "everything") and "all lights", built from
the inventory at request time (`schema.target_options`).

| Kind (route) | Styles | Items |
| --- | --- | ---: |
| command (execute) | direct 301, polite 268, LatAm 259, word order 228, disfluent (fillers, repeats, self-corrections) 216, short ("Lámpara off") 203, Spain colloquial 189, relative volume ("Sube un poco el volumen") 112 | 1776 (673 paraphrased) |
| question (Kiais) | general 40, live 32, chit-chat 35, TV dialogue with command verbs ("¡Apaga eso y vámonos!") 48, state of the home 65 | 220 |
| other (Kiais) | out of domain 29, devices or rooms the home does not have 60 | 89 |
| unclear (Kiais) | implicit ("No veo nada aquí", "Hace frío") 39, verb-less ("Luz salón") 26 | 65 |
| multi (Kiais) | two actions in one request | 63 |
| unsupported (Kiais) | relative brightness, temperature or cover moves ("Sube dos grados el aire") | 60 |

Gold labels come from structured actions (intent, target option, value), never from text. Implicit
requests are labelled unclear, as the owner asked. Relative volume is a command (HA has
`HassSetVolumeRelative`), while relative brightness, temperature and position are "unsupported"
(no HA intent; Kiais).

**Paraphrases.** A private Ollama 0.35.1 on the loopback interface (store on `/datasets`) ran
`qwen3:14b` with thinking off on the 4060 Ti. It rewrote 400 stratified template commands, two
versions each, in a requested register (colloquial Spain, polite, word order, Mexican, telegraphic,
filler, indirect). The validator (`paraphrase.validate`) keeps a paraphrase only if:

- the target name or alias is still there (and the area, if the source named it);
- it adds no other area of the home;
- `numbers_es` parses the same value;
- an intent cue word is present and no cue of the opposite intent;
- it is a single request.

Result: **673 of 800 kept**. Rejected: 101 for a lost intent cue, 12 identical, 4 two requests,
3 opposite intent, 3 another area added, 2 lost target, 2 wrong value. Hand check:

- **60 kept paraphrases:** 1 clear label error (an invented "de la habitación") and 2 marginal ones
  ("Sigue activando la bocina" for resume), so label noise is about **2 %, 5 % counting the marginal
  ones**. Two more errors of the same kind (an area added when the source had none) turned up among
  the built-in "wrong" actions and led to the "other area" rule.
- **15 rejected paraphrases:** 7 real label changes ("apaga" became "enciende", "para el toldo"
  became "abre el toldo") and 8 false rejections ("aumenta", "disminuye", "reduce", "eleva" are not
  cue words). The kept paraphrases are therefore biased towards common verbs, which makes them
  slightly easier for the built-in sentences than free speech.

**Audio subset.** 500 items stratified over kind, style and source: 302 commands and 198 route-Kiais
items. Each was spoken by one of the 5 intelligible Piper voices at -22 dBFS active speech, clean and
mixed with the TV content set at +5 and 0 dB (1500 clips, 141 MB). Transcripts come from
`large-v3-turbo` int8 on CUDA, beam 5, language `es` and the verbs-only prompt (`audio.py`).

## 4. Built-in coverage on `kivi-fake-v1` (text)

Commands, built-in sentences only, then with the custom sentences (measured):

| Style | Template n | Built-in | + custom | Paraphrase n | Built-in | + custom |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| direct | 213 | 44 % | 93 % | 88 | 23 % | 26 % |
| polite | 190 | 43 % | 73 % | 78 | 40 % | 49 % |
| short | 113 | 19 % | 39 % | 90 | 20 % | 32 % |
| word order | 132 | 7 % | 75 % | 96 | 28 % | 35 % |
| disfluent | 124 | 6 % | 15 % | 92 | 30 % | 39 % |
| LatAm | 164 | 6 % | 71 % | 95 | 23 % | 33 % |
| Spain colloquial | 106 | 4 % | 92 % | 83 | 27 % | 35 % |
| relative volume | 61 | 0 % | 100 % | 51 | 0 % | 10 % |
| **all** | 1103 | **21 %** | **70 %** | 673 | **25 %** | **33 %** |

By home (all commands, built-in / + custom): `generica` 26 / 56 %, `coloquial` 27 / 59 %, `personas`
19 / 56 %, `latam` 16 / 54 %. Duplicate names and LatAm vocabulary cost the most.

- **Wrong actions on commands:** 0 of 1103 templates. 3 of 673 paraphrases, all label problems:
  "Enciende todas las luces" is the whole house in the gold label but the satellite's area for HA,
  and "la persiana de la habitación" names a room that does not exist.
- **Route-Kiais items (497):** the built-in sentences (with or without the custom ones) act on 7,
  all bare device names ("Aire", "Tele del salón"). HA's Spanish sentences turn a bare name on,
  which is HA's design rather than a mistake. 33 more get a local answer (time, timer status), and
  none of the 48 TV-dialogue lines, 60 missing-target commands or 63 multi requests triggers an
  action.
- **Cheap fix versus model:** of the 1381 built-in misses (78 % of commands), the custom sentences
  rescue 603 (44 %). That is 62 % of template misses but only 11 % of paraphrase misses. A phrasing
  someone wrote down is cheap to cover, and the rest is what a decider would have to learn.

Upper bound of time saved on text (every miss of a command rescued, × 3 s, divided by all
commands): **2.3 s per command** with the built-in sentences and **1.3 s** with the custom
sentences. These are bounds on a deliberately varied synthetic mix, not on real usage frequencies.

## 5. Audio subset of `kivi-fake-v1`

302 commands and 198 route-Kiais items, each spoken once and heard in three conditions (measured,
synthetic voices). The subset is balanced over styles, so its built-in rate on clean audio (19 %) is
below the text figure of the natural mix (22 %). Same categories as [section 2](#2-headroom-on-the-existing-transcript-corpus),
with the item text as the reference; % of all commands of the condition.

| Condition | Sentences | Correct | Wrong | Miss | a misheard | a-verb | b1 phrasing | b2 near-miss | c other | Upper bound (b1+b2) × 3 s per command |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| clean | built-in | 56 (19 %) | 1 | 245 (81 %) | 24 (8 %) | 27 (9 %) | 192 (64 %) | 1 (0 %) | 1 (0 %) | 1.92 s |
| +5 dB | built-in | 43 (14 %) | 1 | 258 (85 %) | 49 (16 %) | 32 (11 %) | 173 (57 %) | 3 (1 %) | 1 (0 %) | 1.75 s |
| 0 dB | built-in | 34 (11 %) | 1 | 267 (88 %) | 89 (29 %) | 24 (8 %) | 143 (47 %) | 8 (3 %) | 3 (1 %) | 1.50 s |
| clean | + custom | 126 (42 %) | 2 | 174 (58 %) | 24 (8 %) | 24 (8 %) | 112 (37 %) | 9 (3 %) | 5 (2 %) | 1.20 s |
| +5 dB | + custom | 99 (33 %) | 1 | 202 (67 %) | 49 (16 %) | 31 (10 %) | 104 (34 %) | 13 (4 %) | 5 (2 %) | 1.16 s |
| 0 dB | + custom | 82 (27 %) | 2 | 218 (72 %) | 88 (29 %) | 23 (8 %) | 79 (26 %) | 19 (6 %) | 9 (3 %) | 0.97 s |

- **Misheard grows with noise:** 8 % of commands at clean, 16 % at +5 dB and 29 % at 0 dB. The 0 dB
  transcripts of a are often TV lines ("Que no soportó el esfuerzo y cayó muerto." for "Por favor,
  para el toldo"). No text model can rescue them.
- **Phrasing dominates the rescuable part** on this varied mix: b1 is 47 to 64 % of commands with
  the built-in sentences and 26 to 37 % with the custom ones. b2 (one-letter slips such as "Apara la
  luz de la cochera", "pepsiana", "tolo") is 1 to 6 %. A decider would need fuzzy name matching to
  rescue b2.
- **Wrong actions (measured):**
  - Route-Kiais items: 5, 2 and 4 of 198 per condition, all bare device names ("Altavoz Lumo",
    "Tele del salón") that HA's own sentences turn on.
  - Commands: the one label-noise paraphrase ("la persiana de la habitación"). With the custom
    sentences, "Detener persianas sala" was also heard as "Detener persiana sala" and stopped one
    blind instead of the group (clean and 0 dB). Strict sentences are safe only as long as the
    transcript is: a singular/plural slip already picks another target.

## 6. Limitations

- Synthetic only: Piper voices, a mixed-in TV bed with no room acoustics or AEC residue, invented
  homes. The style mix is chosen to be varied, so miss rates are not usage frequencies. The
  real-usage replay (workstream c) decides how often each style really occurs.
- The built-in figures infer HA's behaviour with hassil and a fake resolver, not with a live Home
  Assistant. The custom sentences were matched as HA's default agent orders them, but they were
  not loaded into a real HA.
- The paraphrase validator is conservative (false rejections of "aumenta" or "reduce"), so the kept
  paraphrases under-represent unusual verbs. Label noise is about 2 to 5 %.
- "a-verb" and "unclear" items might be partly rescuable with context; they are excluded from the
  upper bound on purpose.

## 7. Reproduce, data and cleanup

```bash
POC=/work/workspaces/kivi-decider-poc; D=$POC/data/kivi-fake-v1
cd scripts/voice-assistant/decider_poc
python headroom.py [--custom custom_sentences_es.yaml]        # corpus, needs $KIVI_PRIVATE/stt-runs
python fake_v1.py --out $D --per-home 400                      # template items and homes
OLLAMA_HOST=127.0.0.1:11510 python paraphrase.py --data $D --n 400 && python paraphrase.py --data $D --merge
python audio.py synth --items $D/items.jsonl --data $D --split fake --audio $D/audio --conds clean snr+5 snr+0
python audio.py transcribe --data $D --split fake --audio $D/audio
python headroom_v1.py --data $D [--custom custom_sentences_es.yaml] [--audio]
python3 -m unittest discover -s . -p 'test_decider_poc.py'
```

Dependencies: hassil 3.12.1, home-assistant-intents 2026.10.6, pyyaml, rapidfuzz, numpy,
soundfile, piper-tts, and faster-whisper 1.2.1 with the CUDA 12 cuBLAS and cuDNN wheels
(`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) on `LD_LIBRARY_PATH`. Kept on the workstation: the
dataset and its audio under `/work/workspaces/kivi-decider-poc/data/`, the venv, and
`env.sh`. Removed at the end: the Ollama model store on `/datasets/kivi-decider`, the Whisper model
cache, uv cache, and an unused first generator output (`data/items_*.jsonl` of the training-split
draft, see `gen_data.py`).

`gen_data.py` is the train/dev/test generator drafted for P1 before the scope change (held-out
template families and name pools). It is kept for P1 but no audio or training was run with it.

## 8. Open questions for the owner

1. Ship the custom sentences (after checking them against the real names and aliases), and re-run
   this analysis on the real-usage replay before any decider work?
2. Go on to P1 to P3 only if the real misses are mostly "unseen phrasing" rather than "misheard"?
3. Treat "todas las luces" as the whole house or the satellite's area (HA uses the area)?
