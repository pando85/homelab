# Decision models for Kivi: can a small classifier rescue the commands the built-in intents miss?

**Status:** DONE for the synthetic corpus, the fake homes of `kivi-fake-v1` (text) and the owner's
real names (text, counts only); offline, measured on the workstation
**Date:** 2026-11-05
**Systems:** workstation `z800` (Ryzen 9 5950X 16 cores, RTX 4060 Ti 16 GB, shared with another GPU
job during the runs); inferred for `prusik` (Zen 4 CPU, GTX 1060 3 GB Pascal `sm_61`)
**Related:** [small local models](small-local-models.md) (corpus, transcripts, embedding router,
GPU budget), [conversation agent design](../conversation-agent.md),
[conversation agent evaluation](conversation-agent-eval.md) (phrases, fake home, grader)
**Risk:** offline. Home Assistant was read once, read-only (registry lists, exposure list and
states over the websocket API; no service call, no conversation request). No cluster change, no TV,
no Kiais request.
**Scripts:** `scripts/voice-assistant/decision_models/` on top of `agent_eval/` and `small_models/`

Evidence labels: **documented** (model card, package source, version named), **measured** (run on
`z800` in this study), **inferred** (computed from the above, not run on the target),
**hypothesis**. All audio is synthetic (Piper voices, TV-content mixes); no real voice was used.

## Question

Kivi's pipeline today: Whisper `large-v3-turbo` on the GTX 1060, then Home Assistant's built-in
intents (`prefer_local_intents`, about 1 s end to end), and everything they do not answer goes to the
Kiais LLM (about 4.1 s for a command, about 2.3 s for a general question;
[design note](../conversation-agent.md)). Can a small **non-generative decision model** (picks one
option per typed question with a probability, generates no text) sit between the two and execute the
home commands the built-in intents miss, fast and **without wrong actions**, handing everything it is
unsure about, and every general question, to Kiais?

```text
built-in intents miss -> decision stage: request type, action, area, device kind (one pass),
                         then which device if several match; our code parses the number
   |- every used answer at or above the threshold, and the combination is valid -> execute locally
   |- otherwise (unsure, general question, invalid combination)                    -> Kiais
```

## Summary

1. **Not worth wiring in as they are (measured).** Used zero-shot (typed questions over the home's
   options, no training), no candidate rescues a useful share of the missed commands at a near-zero
   wrong-action rate. With the gate set so that wrong actions stay at about 1 % of what reaches the
   stage, the decision models execute correctly **0 to 8 %** of the commands the built-in intents
   miss on the corpus transcripts, **4 to 6 %** on the `kivi-fake-v1` text, **3 to 6 %** on its audio
   subset, and **at most 1 of 27** on commands built from the owner's real names. Everything else
   still goes to Kiais.
2. **The embedding and fuzzy-matching baseline is as good or better** on everything except the
   corpus: 11 % against 4 to 6 % on `kivi-fake-v1` text (paired McNemar p < 1e-6), about the same
   rescues on audio. Its confidence is coarse, though: on audio it cannot be gated below 1.6 % wrong
   actions, because Whisper drops the question marks and state questions ("¿Está abierta la luz?")
   look like orders.
3. **Best decision model: Quyet-1.0-Tiny** (183 M, Apache-2.0) with English instructions: 20 of 265
   missed corpus commands (8 %, against 3 for the baseline, p < 0.001), 6 % on fake-v1 text and audio.
   **Laya multilingual** (322 M) is close to chance zero-shot on the action and area questions in
   Spanish, as its card warns ("not a zero-shot decision engine"); the English Laya root does not
   understand Spanish. **Quyet-Small** (328 M) is no better than Tiny.
4. **The confidence gate does not transfer**: a threshold fitted on half of the phrases gives up to
   17 wrong actions on the other half. Negatives are not the problem at those thresholds (TV
   dialogue with command verbs, general, live and chit-chat questions: none acted on in the text sets,
   1 of 54 TV-dialogue transcripts on audio by Quyet-Tiny `en`); the problem is that a safe threshold
   hands off almost every command too.
5. **Speed and memory are not the obstacle**: one decision of the Spanish-capable candidates takes
   15 to 360 ms on 16 CPU threads
   and 17 to 80 ms on the workstation GPU (fp32). In fp32, only the baseline (0.75 GiB) and Quyet-Tiny
   (0.90 GiB, just) fit the 1.0 GiB left on the GTX 1060; Laya multilingual and Quyet-Small (1.5 GiB)
   do not. A CPU on `prusik` would do.

**Recommendation:** do not add a zero-shot decision stage to Home Assistant. If the owner wants a
local decider, it has to be **trained** on Spanish home commands (the [decider PoC](kivi-decider-poc.md)
has the data, and custom sentences cover much of the same room more cheaply), then re-measured with
this harness. The candidate to fine-tune is Quyet-Tiny, or a small classifier on the embedding model,
run on the `prusik` CPU (no risk to Whisper's VRAM, about 0.15 s per decision) rather than on the 1060.

## Candidates (documented from the model cards and package source, 2026-11-05)

| Candidate | Checkpoint (pinned revision) | Encoder, parameters (measured) | Licence | Languages (card) | Input and limits | Runtime used |
| --- | --- | --- | --- | --- | --- | --- |
| **Laya multilingual** | `convaiinnovations/laya`, subfolder `multilingual` (`7b928d8`) | mmBERT-base + decision head, **321.9 M** | Apache-2.0 | "100+ languages"; MASSIVE intent in 13 non-English languages 0.451 (card) | state text plus typed questions (`choice`, `score`, `noul`); option text shares a 256-token head budget; up to 1024 tokens | `laya` 0.4.9 (PyPI, 2026-10-17) |
| Laya (English root) | same repository, root (`7b928d8`) | ModernBERT-large + head, 421 M (card) | Apache-2.0 | English ("English only on root", card) | as above, 512 tokens | `laya` 0.4.9 |
| **Quyet-1.0-Tiny** | `chinhnc/Quyet-1.0-Tiny` (`fba8ca9`) | first 16 layers of mmBERT-small + wide option-query head, **182.9 M** | Apache-2.0 (keep NOTICE) | "English; also tuned for Vietnamese. Other languages work, with lower accuracy" | same question format; **at most 10 options per question**; 8192 tokens; temperature-calibrated per type and option count | `quyet` 1.0.2 |
| Quyet-1.0-Small | `chinhnc/Quyet-1.0-Small` (`f007dfe`) | SEA-LION-ModernBERT-300M + 2-layer head, **327.6 M** | Apache-2.0 | as Tiny | as Tiny | `quyet` 1.0.2 |
| **Baseline (no new model)** | `ibm-granite/granite-embedding-278m-multilingual` (`a9cb533`) | XLM-R type encoder, CLS pooling, **278.0 M** | Apache-2.0 | 12 languages including Spanish (card) | 3 nearest neighbours over synthetic training phrases for request type and action; fuzzy string matching (rapidfuzz) of area, device kind and device against names and aliases; numbers by our parser | `transformers` 5.22 |

The owner's description of Laya ("ModernBERT-large, about 421 M, multilingual") mixes two
checkpoints: the 421 M root checkpoint is English-only by its card, the multilingual one is
mmBERT-base with 322 M parameters. Both were run; the multilingual one is the relevant candidate.

Card caveats that matter here (documented): Laya's base checkpoints are "near chance on
typed-decisions zero-shot ... a fast base to specialise, not a zero-shot decision engine", they ship
over-confident and `laya-multilingual` has no fitted temperatures (all 1.0 in its config);
`action.act_probability` "carries no usable signal yet", so the gate uses the answer probability.
Quyet ships fitted temperatures but names only English and Vietnamese as tuned languages.

Code review before running (documented): both packages load only `safetensors` weights, JSON
configs and a local tokenizer (`snapshot_download` restricted to those files; no repository `.py`,
no `trust_remote_code`, no pickle). Quyet's manifest SHA-256 sums matched. The newest `laya` (0.4.24,
uploaded the same morning) adds a dependency on the same author's new `vegaml` package, so the
19-day-old 0.4.9 without it was used; the card says the checkpoints did not change. On CUDA both
packages autocast (Laya fp16 on pre-Ampere GPUs, Quyet bf16); for the GTX 1060 (Pascal `sm_61`:
no bf16, very slow fp16) the evaluation turns autocast off and runs **fp32** everywhere.

## Method

- **The decision stage** (`decide.py`, identical for every candidate): one forward pass with five
  `choice` questions: request type (home command, timer, state question, anything else), action (ten
  options: on, off, set a value, volume up and down, mute, unmute, pause or stop, resume, next),
  timer action, area (every area of the home plus "no area named" and "the whole home") and device
  kind (lights, blinds, awnings, gates, climate, TV, speakers, fans, plugs, everything; only kinds
  the home has, each with up to six device names in its description). When the area and kind leave
  several devices, a second question asks which one (or all of them). Questions with more than ten
  options are split into knockout rounds. Our code parses the value (digits, Spanish number words,
  "y medio", "a la mitad", "al máximo") and the timer length, resolves the entities and maps the
  decision to a Home Assistant intent call. Invalid combinations (a volume action on a light, a set
  command without a number, a kind with no device in the area) hand off.
- **Gate:** the minimum, over the questions the decision used, of the probability of the chosen
  option. Swept from 0 to 0.999. The **chosen threshold** is the lowest one whose wrong actions are
  at most 1 % of the items that reach the stage; it is fitted in-sample, so a phrase-split check
  (fit on half of the phrases, apply to the other half) is reported as well.
- **Instruction variants** for the decision models: Spanish (`es`) or English (`en`) question and
  option texts (the state is always the Spanish transcript), and neutral option keys `A`, `B`, ...
  with the meaning only in the description (`esN`, `enN`; Laya's card suggests this when label keys
  dominate).
- **Outcome** of an item that reaches the stage: **rescued** (the call matches the phrase's
  expectation from `agent_eval`: intent, exact target entities, value), **wrong action** (any executed
  call that does not, and any call on a general, live or state question, an unknown target or a
  two-action request), **handed off** to Kiais.
- **Test sets:**
  1. **Corpus:** Whisper `large-v3-turbo` transcripts of the 69 graded single-turn phrases of
     `agent_eval` spoken by the 5 intelligible Piper voices, with the **verbs-only Whisper prompt**
     (the shipped one), clean and mixed with TV content at +5 and 0 dB (run `turbo_b5_vonly` of the
     [small local models study](small-local-models.md#5-whisper-prompt-hotwords-beam-and-model-size);
     1035 transcripts). Each transcript first goes through the built-in matcher
     (`agent_eval/builtin_intents.py`, hassil 3.12.1 and home-assistant-intents 2026.10.6 as in Home
     Assistant 2026.10.4); only what it does not answer reaches the stage. Main subset: the graded home
     commands it misses (265); negatives: every general, live and state question (420). Sanity: the
     commands it answers correctly (319), run through the stage as if it came first.
  2. **`kivi-fake-v1`** (built by the decider proof-of-concept, read-only here): four fake homes,
     17 phrasing styles (direct, polite, short, word order, disfluent, relative, Latin American,
     Spanish colloquial, implicit, multi-action, state, missing target, out of domain, general, live,
     chit-chat, TV dialogue with command verbs), template and validated LLM paraphrases, gold intent,
     entities and value; the text of all 2273 items and Whisper transcripts of a 500-item audio subset
     at three noise levels.
  3. **The owner's real names** (private): the exposed entities, areas and aliases read once from Home
     Assistant (read-only), templated commands with gold targets generated from them
     (`real_phrases.py`, text only), plus the general and live questions of `agent_eval`. Only counts
     are published.
- **Paired tests:** exact McNemar on the same items against the baseline (rescued, and wrong
  actions). Items of the same phrase in different voices or noise levels are not independent, so
  p-values are indicative.
- **Latency:** GPU fp32 on the RTX 4060 Ti with `decision_eval.py bench` (warm, one process per
  candidate, 40 distinct clean transcripts twice; the card was shared with another job at 100 %
  utilisation, so an upper bound). CPU fp32 on 16 threads: the per-decision times of the corpus
  accuracy run (Ryzen 9 5950X, load average about 8 to 25 from other jobs). Peak VRAM:
  `torch.cuda.max_memory_allocated` and the process's own `nvidia-smi` figure (includes the CUDA
  context).

## 1. Corpus transcripts (verbs-only Whisper prompt), measured

What reaches the stage, per noise level (the built-in matcher on the same transcripts): clean 67
missed commands and 138 general, live and state questions; +5 dB 87 and 142; 0 dB 111 and 140. The
built-in matcher answered 128, 108 and 83 commands correctly (the sanity set) and acted wrongly on 1
(at 0 dB).

**Without a gate** (threshold 0, pooled over the three noise levels; 265 missed commands, 420
negatives), what each candidate would do on its own:

| Candidate (variant) | Missed commands: rescued / wrong | Negatives acted on |
| --- | --- | --- |
| Baseline | 129 / 92 | 87 |
| Laya multilingual (`es`) | 33 / 60 | 48 |
| Laya English root (`en`) | 20 / 133 | 137 |
| Quyet-Tiny (`es`) | 55 / 38 | 42 |
| Quyet-Tiny (`enN`) | 25 / 52 | 41 |
| Quyet-Small (`es`) | 11 / 44 | 53 |

**At the chosen threshold** (wrong actions at most 1 % of stage inputs; per noise level:
rescued / wrong among the missed commands, negatives acted on; "sanity" = of the commands the built-in
intents already answer, how many the stage would get right / wrong):

| Candidate (variant) | Threshold | Clean (67 / 138) | +5 dB (87 / 142) | 0 dB (111 / 140) | All: rescued, wrong, handed off | Sanity (319): right / wrong |
| --- | --- | --- | --- | --- | --- | --- |
| Baseline | 0.70 | 0 / 0; 0 | 2 / 0; 0 | 1 / 0; 0 | 3 (1 %), 0, 99 % | 41 / 0 |
| Laya multilingual (`es`) | 0.85 | 1 / 0; 0 | 0 / 0; 0 | 2 / 0; 1 | 3 (1 %), 0, 99 % | 60 / **32** |
| Quyet-Tiny (`es`) | 0.65 | 0 / 0; 0 | 1 / 1; 0 | 5 / 0; 0 | 6 (2 %), 1, 97 % | 84 / 8 |
| **Quyet-Tiny (`enN`)** | 0.70 | 5 / 0; 0 | 6 / 1; 0 | 9 / 0; 0 | **20 (8 %)**, 1, 92 % | 47 / 11 |
| Quyet-Small (`es`) | 0.60 | 0 / 0; 0 | 4 / 0; 0 | 6 / 1; 0 | 10 (4 %), 1, 96 % | 60 / **30** |
| Laya English root (`en`) | 0.45 | 0 / 0; 0 | 2 / 0; 1 | 3 / 0; 1 | 5 (2 %), 0, 98 % | 38 / 0 |

Paired against the baseline at each one's threshold (exact McNemar on the 685 stage items): only
Quyet-Tiny `enN` rescues more (20 against 3, p < 0.001; wrong actions 1 against 0, p 1.0);
Quyet-Tiny `en` 10 against 3 (p 0.09), Quyet-Small `es` 10 against 3 (p 0.09), every other variant
p ≥ 0.25. The other variants (`en`, `esN`, `enN` for Laya, `esN` for Quyet-Tiny, `en` for
Quyet-Small, `es` for Laya's English root) rescued 0 to 10 at their thresholds; the full tables are
reproducible with `decision_eval.py report`.

**The threshold does not transfer** (phrase-split check, measured): for Quyet-Tiny `enN`, a threshold
fitted on the odd-numbered phrases (0.6) gives 21 rescued and **17 wrong** on the even ones; fitted
on the even ones (0.7) it gives 0 and 0 on the odd ones. Laya multilingual: 0 rescued and 7 wrong in
one direction. The confidence does not separate right from wrong reliably enough to set a
threshold that holds on new phrases.

**Why** (per-question accuracy against hand-labelled correct answers for 23 control phrases and every
general question, all voices and noise levels, measured):

| Question | Baseline | Laya multilingual `es` | Quyet-Tiny `es` | Quyet-Tiny `en` | Quyet-Small `es` |
| --- | --- | --- | --- | --- | --- |
| Request type (735) | 84 % | 41 % | 70 % | 83 % | 64 % |
| Action (315) | 71 % | 44 % | 50 % | 40 % | 13 % |
| Area (315) | 96 % | 51 % | 47 % | 48 % | 39 % |
| Device kind (315) | 92 % | 84 % | 84 % | 89 % | 80 % |
| Timer action (30) | 47 % | 97 % | 93 % | 93 % | 97 % |

Zero-shot, the decision models are close to chance on the action and the area in Spanish (ten and
seven options), and Laya's median confidence is about the same for its right and wrong answers to the
request type (0.78 and 0.76). They do recognise the device kind and the timer action. English
instructions helped Quyet-Tiny with the request type (70 to 83 %) but not with the action or the
area; neutral option keys did not help either model.

## 2. `kivi-fake-v1`, text (2273 items, four fake homes), measured

The built-in matcher answers 395 of the 1776 single-action commands correctly, acts wrongly on 3 and
misses **1378** (78 %: the set is built from phrasings the built-in sentences do not cover); it also
acts on 7 of the "short" negatives (fragments such as a bare device name). Reaching the stage: 1378
missed commands and 457 negatives (general, live, chit-chat, out of domain, state, missing target,
two-action requests, TV dialogue that contains command verbs).

| Candidate (variant) | Ungated: rescued / wrong; negatives acted on | Threshold | Rescued | Wrong (missed) | Negatives acted on | Sanity (395): right / wrong |
| --- | --- | --- | --- | --- | --- | --- |
| **Baseline** | 765 / 537; 303 | 0.75 | **148 (11 %)** | 9 (0.7 %) | 8 of 457 | 63 / 1 |
| Quyet-Tiny (`es`) | 412 / 471; 141 | 0.85 | 67 (5 %) | 6 | 0 | 47 / 1 |
| Quyet-Tiny (`en`) | 342 / 400; 142 | 0.75 | 78 (6 %) | 16 | 2 | 59 / 2 |
| Quyet-Tiny (`enN`) | 284 / 369; 119 | 0.80 | 57 (4 %) | 13 | 1 | 33 / 0 |
| Laya multilingual (`es`) | 196 / 440; 93 | 0.95 | 53 (4 %) | 9 | 0 | 41 / 2 |

Quyet-Small and the other Laya variants were not run on this set (the corpus already ranked them
lower, and the workstation was saturated by other jobs). Paired against the baseline: the baseline
rescues significantly more than every decision model (Quyet-Tiny `es` 59 only-candidate against 140
only-baseline, p < 1e-8; `en` 67 against 137, p < 1e-6; `enN` 49 against 140, p < 1e-10), with
as many or fewer wrong actions (Quyet-Tiny `es` 6 against 17, p 0.03; `en` 18 against 17 and `enN`
14 against 17, p ≥ 0.7). Phrase-split check
for the baseline: 61 rescued and 5 wrong on one half, 76 and 13 on the other.

Per style at the chosen threshold, rescued share of the missed commands (wrong actions in brackets),
measured:

| Style (missed commands) | Baseline | Quyet-Tiny `en` | Laya multilingual `es` |
| --- | --- | --- | --- |
| direct (187) | 14 % (5) | 4 % (1) | 4 % (0) |
| polite (156) | 14 % (0) | 4 % (5) | 2 % (2) |
| short (161) | 14 % (0) | 4 % (1) | 3 % (0) |
| word order (191) | 14 % (1) | 3 % (4) | 5 % (1) |
| disfluent (181) | 9 % (0) | 6 % (2) | 4 % (0) |
| Latin American (227) | 11 % (0) | 11 % (3) | 4 % (5) |
| Spanish colloquial (163) | 4 % (2) | 8 % (0) | 7 % (0) |
| relative ("un poco más ...", 112) | 1 % (1) | 4 % (0) | 0 % (1) |

Negatives acted on at the chosen threshold (baseline / Quyet-Tiny `en` / Laya): TV dialogue 0 of 48
for all three; general 0 of 40, live 0 of 24, chit-chat 0 of 32, out of domain 0 of 27 for all
three; state questions 2 / 0 / 0 of 45; missing target 3 / 0 / 0 of 60; two-action requests 3 / 2 / 0
of 63. At the thresholds that keep wrong actions near 1 %, all of them hand off almost every negative;
the cost is that they also hand off 89 to 96 % of the missed commands.

### `kivi-fake-v1`, audio subset (500 items × clean, +5 dB, 0 dB; Whisper with the verbs prompt), measured

The built-in matcher answers 56 / 43 / 34 of the 302 commands per noise level (clean / +5 / 0 dB)
and misses 245 / 258 / 267; with the negatives, 1326 transcripts reach the stage.

| Candidate (variant) | Threshold | Clean: rescued / wrong; negatives | +5 dB | 0 dB | All (770 missed, 556 negatives): rescued, wrong, negatives acted on |
| --- | --- | --- | --- | --- | --- |
| Baseline | none reaches 1 % (strictest shown) | 20 / 3; 6 | 20 / 2; 6 | 20 / 1; 3 | 60 (8 %), 6, **15 (2.7 %)** |
| Quyet-Tiny `es` | 0.85 | 10 / 1; 0 | 9 / 1; 0 | 6 / 2; 0 | 25 (3 %), 4, 0 |
| **Quyet-Tiny `en`** | 0.75 | 17 / 2; 0 | 18 / 2; 1 | 12 / 3; 0 | **47 (6 %)**, 7, 1 |
| Quyet-Tiny `enN` | 0.80 | 7 / 3; 0 | 11 / 3; 0 | 6 / 3; 0 | 24 (3 %), 9, 0 |
| Laya multilingual `es` | 0.95 | 11 / 3; 0 | 11 / 1; 0 | 5 / 2; 0 | 27 (4 %), 6, 0 |

Paired against the baseline: Quyet-Tiny `en` rescues about as many (44 only-candidate against 57
only-baseline, p 0.23) with fewer wrong actions (8 against 21, p 0.02); Quyet-Tiny `es` and `enN`
and Laya rescue fewer (p < 0.001) with fewer wrong actions (Laya 6 against 21, p 0.006). The baseline cannot be gated below 1.6 % wrong actions on audio: **Whisper drops the
question marks**, so "¿Está abierta la luz de la terraza?" arrives as "Está abierta la luz de la
terraza." and the 3-NN votes "turn on" unanimously (10 of its 15 negatives acted on are such state
questions; 3 are two-action requests of which it executes the first, 2 name a device the area does
not have). Phrase-split check: Quyet-Tiny `en` 36 rescued / 1 wrong on one half, 11 / 7 on the
other; the baseline 35 / 16 and 28 / 7.

## 3. The owner's real names (text, counts only), measured

Home Assistant exposes 100 entities to Assist in 5 areas (50 switches, 25 of them without an area; 25
sensors; 5 climate, 4 media players, 6 buttons, 3 lights, 3 covers, 2 fans, 1 vacuum, 1 binary sensor;
no entity aliases, Spanish aliases on the areas). The first-pass questions then have 7 areas and 7
device kinds, and "which plug" has up to 50 options (knockout rounds of at most 10). Templated
commands with gold targets (95 items: 65 graded commands, 3 state questions, 1 unknown area and the 26
general and live questions; text only, no audio): the built-in matcher answers **38** commands correctly
and misses **27**; 28 negatives reach the stage.

| Candidate (variant) | Ungated: rescued / wrong; negatives acted on | At 0.5 | At the chosen threshold |
| --- | --- | --- | --- |
| Baseline | 20 / 7; 4 | 13 / 2; 4 | 0.90: 0 rescued, 0 wrong |
| Quyet-Tiny (`es` / `en` / `enN`) | 2 to 4 / 4 to 9; 0 to 1 | 1 to 2 / 0 to 1; 0 | 0 or 1 rescued, 0 wrong |
| Laya multilingual (`es` / `en` / `enN`) | 0 / 6 to 17; 3 to 5 | 0 / 2 to 11; 3 | 0 rescued, 0 wrong |
| Quyet-Small (`es` / `en`) | 1 to 4 / 6 to 10; 1 to 3 | 0 / 0 to 1; 0 | 0 rescued, 0 wrong |

With the real home the decision models rescue almost nothing even ungated. The area names are
English labels with Spanish aliases, the 67 actionable entity names are all distinct but long (median
five words), and the "which device" question over dozens of plugs goes through knockout rounds whose
confidences multiply. Of Quyet-Tiny `en`'s ungated failures, most are a wrong device kind or area
(8 "no target", 4 media actions on a non-media kind) or a wrong cover action (5 wrong calls on
"para ..." and "... a la mitad"). Fuzzy matching of the names does better ungated, but its confidence
is too coarse for a safe gate.
(Quyet-Small `enN` was not run here: stopped for time.)

## 4. Latency and memory

One decision on the fake home is 1.0 to 1.4 forward calls on average (the "which device" question
adds a second call when area and kind leave several devices). Warm, fp32, p50 / p95 per decision
(measured on the workstation; CPU while other jobs ran, load average about 8 to 25 from other jobs; the CPU figures are the per-item times of the corpus accuracy run, as a dedicated CPU benchmark later ran at load 110 and was discarded; 4 threads was not measured for the same reason):

| Candidate | CPU 16 threads | CPU 4 threads | GPU RTX 4060 Ti (shared) | GTX 1060 (inferred, ×4.5) | Process VRAM fp32 (peak tensors) | Fits 1.0 GiB next to Whisper |
| --- | --- | --- | --- | --- | --- | --- |
| Baseline (granite 278 M + fuzzy) | 15 / 22 | not measured | 17 / 20 | about 75 | 748 MiB (567) | yes |
| Quyet-Tiny | 151 / 222 | not measured | 26 / 45 | about 120 | 904 MiB (733) | just (926 MiB with the real home's options) |
| Laya multilingual | 358 / 476 | not measured | 76 / 115 | about 340 | 1466 MiB (1288) | no |
| Quyet-Small | 357 / 494 | not measured | 80 / 97 | about 360 | 1504 MiB (1314) | no |
| Laya English root | 1391 / 1775 | not measured | not measured | | not measured (421 M parameters) | no (inferred) |

On the real home the option lists are longer (up to 50 plugs, names of about five words). That
was not measured cleanly (the workstation was saturated by other jobs from about 17:50 on). In one
contended GPU run, Quyet-Tiny took 206 ms p50 per decision on the real home's lists and the baseline
119 ms in the same conditions, against 26 and 17 ms on the fake home at low load. The longer lists
therefore cost at most a modest factor on top of the contention (inferred). Quyet-Tiny's peak
process VRAM with the real lists was 926 MiB (measured).

GPU figures on the 4060 Ti are upper bounds (another job kept it at 100 % utilisation). The GTX 1060
column scales them by 4.5, the ratio measured for Whisper's encoder between the two cards
([small local models, section 2](small-local-models.md#2-whisper-latency-on-the-1060-observed-from-the-whisper-pods-own-logs));
it is an inference, not a measurement. VRAM is the process's own `nvidia-smi` figure on the 4060 Ti
(weights, activations and the CUDA context; the tensors alone in brackets); on the 1060 the weights
and activations are the same size in fp32, the context size may differ. Only the baseline and
Quyet-Tiny fit the 1.0 GiB budget, and Quyet-Tiny only just. Laya multilingual and Quyet-Small
would need fp16 storage (very slow on Pascal) or the CPU. On `prusik`'s Zen 4 CPU the CPU columns
should be similar or faster (inferred, as in the earlier study).

Against the alternative, every candidate is fast enough: even 0.5 s on a CPU is far below the 3 s
the Kiais round trip costs for a command. Latency is not what rules them out; accuracy is.

## Conclusion and recommendation

- **Zero-shot decision models do not earn a place between the built-in intents and Kiais**
  (confidence high for zero-shot use on this kind of data: three test sets, two model families, four
  instruction variants, and the per-question accuracy shows why: the action and area questions are
  near chance in Spanish). What they would save is small: with the [PoC](kivi-decider-poc.md)'s
  bound of about 3 s saved per rescued command, rescuing 6 % of the missed commands saves about
  0.2 s per missed command on average, at a wrong-action rate of about 1 % of what reaches the
  stage.
- **The baseline (no new model) is a fair comparison and often better**, but its confidence is too
  coarse to gate safely, and it acts on state questions once Whisper has dropped the question marks.
  Not recommended either, as it stands.
- **Where it would run, if a trained version is ever built:** the `prusik` CPU (Quyet-Tiny about
  0.15 s per decision on 16 workstation threads, more knockout rounds with many plugs), not the 1060:
  only Quyet-Tiny and the baseline fit its 1.0 GiB in fp32, and fp16 is slow on Pascal.
- **Wiring** (unchanged from the [design note](../conversation-agent.md#wiring-in-home-assistant-202610-feasibility)):
  Home Assistant has one agent per pipeline, so a stage like this would be a small custom
  conversation agent that calls the intents itself and forwards the rest to Kiais with
  `conversation.async_converse`. Not proposed now.

## Reproduce

```sh
# workstation, throw-away venv (torch, transformers>=5.17, rapidfuzz, hassil==3.12.1,
# home-assistant-intents==2026.10.6, pyyaml, websockets; laya==0.4.9 and quyet==1.0.2 with --no-deps)
export KIVI_PRIVATE=~/webos-voice-satellite/devices/private/kivi DECISION_MODELS=<hf cache>/hub
cd scripts/voice-assistant/decision_models
python3 -m unittest discover -s . -p 'test_decision_models.py'          # 15 tests, no model
python decision_eval.py run --candidates baseline laya-ml quyet-tiny quyet-small laya-en --langs es en esN enN --device cpu
python decision_eval.py report --langs es en esN enN                     # corpus tables
python decision_eval.py run --fake-v1 <kivi-fake-v1 dir> --tag fakev1-text --candidates baseline quyet-tiny laya-ml --langs es en enN
python decision_eval.py run --fake-v1 <dir> --fake-v1-stt <dir>/stt_fake.csv --tag fakev1-audio ...
python decision_eval.py report --tag fakev1-text --by style
# real names (private output only; admin token in a 0600 file, shredded afterwards)
python fetch_inventory.py --token-file <0600 file> --out $KIVI_PRIVATE/decision-models/inventory_real.yaml
python real_phrases.py --inventory .../inventory_real.yaml --out .../phrases_real.jsonl
python decision_eval.py run --phrases .../phrases_real.jsonl --inventory .../inventory_real.yaml --tag real-text ...
python decision_eval.py bench --candidate quyet-tiny --device cuda       # latency and VRAM
```

Per-item results (transcripts, decisions, calls) are written as JSONL to
`$KIVI_PRIVATE/decision-models/` and never committed. The model downloads are pinned to the
revisions in the candidates table.

## Rollback and cleanup

Nothing changed in the cluster or Home Assistant. Home Assistant was read once over the websocket
API with the Whisper name-fetch token (from its Kubernetes Secret into a 0600 file in memory,
shredded afterwards); only list and `get_states` commands were sent. The real inventory and the
phrases generated from it stay in the private directory. Locally, the uv package cache
was deleted; to rerun, the pinned model snapshots (3.9 GB, Hugging Face cache layout) and the
Python environment stay in the workstation's scratch work directory on the NVMe disk (about 9 GB),
not under `/home`.

## Open questions

- **Fine-tuning:** both model cards say the capability comes from fine-tuning on the domain (Laya's
  fine-tuned checkpoint goes from 0.36 to 0.77 on its own benchmark). The [decider PoC](kivi-decider-poc.md)
  has the data (`kivi-fake-v1` train split) to test whether a fine-tuned Quyet-Tiny or Laya (or a small
  classifier on the embedding model) closes the gap; that is a separate, larger experiment.
- **Confidence:** a gate on raw answer probabilities did not transfer between phrase sets. A
  fine-tuned model would need temperatures fitted on held-out data of the household's own phrasing.
- **Question marks:** Whisper's transcripts lose "¿...?", so a stage behind STT cannot tell "¿Está
  encendida la luz?" from an order by punctuation; any stage needs state questions in its training
  or negatives.
- **Real voices and the real phrasing** of the household (workstream c of the plan) decide how much
  room is left after custom sentences; all audio here is synthetic.
