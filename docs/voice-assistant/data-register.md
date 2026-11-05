# Kivi data register

What data the voice assistant collects or handles, where it lives, for how long, and how to stop
it. Content is never written here: only kinds, places and rules. Owner decision 2026-11-05: collect
what is needed to improve Kivi, and be explicit about it.

| # | Dataset | Source | Stored | Retention | Read by | Purpose | Stop or delete |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Whisper transcripts and the other DEBUG lines of its dispatch handler (timings, VAD, the names prompt). **Includes speech from false wakes.** | Whisper container `home-assistant/whisper`, via Vector | Loki, stream `{namespace="home-assistant", container="whisper"}` | 180 days (global Loki retention), collected from 2026-11-05. A temporary exception, to be ended by the owner | The owner and local analysis scripts through Grafana or the Loki API. Not any cloud service | Offline replay of real commands against Home Assistant's built-in sentences, miss analysis, hint list and alias proposals ([plan](plan.md)) | Stop: revert the exception commit ([section 8](stt-hint-list-and-learning.md#8-log-policy-voice-transcripts-in-loki-temporary-exception)). Delete earlier than 180 days: Loki delete API (`POST /loki/api/v1/delete`, selector plus line filter, compactor in `filter-and-delete` mode) on the owner's decision |
| 2 | Derived replay datasets (selected or reshaped transcripts, labels, results) | Extracted from dataset 1 on the workstation | The satellite repository's gitignored `devices/private/kivi/` | Deleted at the end of the experiment, at most 30 days | The owner and local scripts. Agents see aggregates only | Run the replay and measure miss rates | Delete the directory. Never committed |
| 3 | Home Assistant names list (areas, entity names, aliases) | Home Assistant API, read by Whisper with its own token (`--hass-api`) | In Whisper's memory only. The token is a Kubernetes Secret from Vault. The names reappear in the DEBUG "initial prompt" lines of dataset 1 | In memory: refreshed every 300 s, gone when the pod restarts. In Loki: as dataset 1 | Whisper, to bias recognition | Better recognition of the home's names | Remove `--hass-api` and the token mount from the Whisper args, or revoke the token in Home Assistant |
| 4 | Kiais requests: commands that Home Assistant's built-in intents miss go to the Kiais gateway (namespace `isidoro`) and from there to its upstream model providers (see [Kiais integration](kiais-integration.md)) | Home Assistant conversation agent `conversation.fast`, `prefer_local_intents` on | At Home Assistant: the Assist chat log in memory. At Kiais (as found 2026-11-05, not re-verified in this change): logs at `RUST_LOG=kiais=debug` with events and timings and no request or response bodies seen, an empty `chat_messages` table, Prometheus `kiais_*` metrics (counts, latencies, labels `model`, `backend`, `status`, no text). Pod logs reach Loki like any pod. **Not yet verified:** Kiais Redis contents and what the upstream providers keep | Kiais logs in Loki: 180 days. Metrics: Prometheus retention. Providers: their own terms (not checked) | The Kiais gateway and its providers (the request text and the home's device names leave the cluster). The owner reads the metrics | Answering requests that need an LLM | Set the pipeline's conversation agent back to Home Assistant's built-in agent in the UI (rollback steps in [Kiais integration](kiais-integration.md)). To verify: Kiais logging and storage after each upgrade |
| 5 | Audio recordings | **Not collected.** Nothing records or stores microphone audio today | Nowhere | None | Nobody | Possible later for AEC tuning, only opt-in, with the consent of the household, after the AEC work | Nothing to stop. Collection needs a new owner decision and a row update here |

## Usage rules

- **Purpose limited** to improving Kivi: speech-to-text, the hint list, aliases, custom sentences
  and the decision between the local and the LLM path. No other use.
- **Local analysis.** Replay and statistics run on the workstation with scripts. Agents work from
  aggregates: counts, miss rates, category totals. Transcript text is never pasted into an AI
  session and never sent to a cloud or LLM API for analysis.
- **Samples** for manual review are shown to the owner only.
- **Nothing private in git.** No transcripts, samples, recordings or tokens in any repository,
  commit message, issue or pull request. Documents carry counts and shapes only.
- **Summary on use.** When the data is used, the owner gets a summary of what was collected (period,
  number of utterances, kinds of sources) and what was done with it.
- **False wakes.** Because the wake word is server-side, speech that did not address Kivi can be
  transcribed and stored. The household should know that the assistant's microphones feed this
  while the exception lasts.
- **Ending.** The exception ends when the replay study is done, or earlier on the owner's decision:
  revert the commit, delete dataset 2, and decide whether to purge dataset 1 before it ages out.
