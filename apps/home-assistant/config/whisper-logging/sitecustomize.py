# Temporary (from 2026-11-05, see docs/voice-assistant/data-register.md): make the Whisper STT
# server log transcripts, and nothing else extra, so Vector ships them to Loki.
#
# Why not --debug: the server prints its whole argument namespace at DEBUG, and that includes
# the Home Assistant token read from WYO_WHISPER_HASS_TOKEN_FILE, and it turns on DEBUG for every
# library. Instead the root logger is set up here at INFO and only the dispatch handler logger
# (which logs the transcript at DEBUG in the 3.8.1.x image) is lowered to DEBUG. The server's own
# logging.basicConfig() does nothing once the root logger has a handler, so its level and
# --log-format are ignored while this file is mounted.
#
# Loaded by Python at startup through PYTHONPATH. To stop: remove the PYTHONPATH variable and
# the whisper-logging mount from apps/home-assistant/values.yaml.
import logging

_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("wyoming_faster_whisper.dispatch_handler").setLevel(logging.DEBUG)
