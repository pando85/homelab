"""Backends for decide.decide(): each exposes ask(text, questions) -> {qid: {option: probability}}.

  laya         convaiinnovations/laya (subfolder "multilingual" = mmBERT-base, or the English root),
               official `laya` package (0.4.9)
  quyet        chinhnc/Quyet-1.0-Tiny or -Small, official `quyet` package (1.0.2)
  baseline     no decision model: granite-embedding-278m-multilingual 3-NN over synthetic training
               phrases for the request type and action, fuzzy matching (rapidfuzz) of the area,
               device kind and device against the option texts (names and aliases)

fp32=True turns the packages' CUDA autocast off (laya: fp16/bf16, quyet: bf16): the GTX 1060
(Pascal sm_61) has no bf16 and runs fp16 very slowly, so fp32 is what it would run.
Model downloads are untrusted data: load them from a local snapshot directory (pinned revision).
"""
import re

import decide as D


class Laya:
    max_options = 10

    def __init__(self, path, subfolder=None, device="cpu", fp32=True):
        import laya
        import torch
        self.agent = laya.load(path, subfolder=subfolder, device=device)
        if fp32:
            self.agent.amp_enabled = False
            self.agent.dtype = torch.float32
        self.model = self.agent.model

    def ask(self, text, questions):
        r = self.agent.predict(text, questions)
        return {q: {k: float(v) for k, v in a["probabilities"].items()} for q, a in r["answers"].items()}


class Quyet:
    max_options = 10   # the package refuses more than 10 options per question

    def __init__(self, path, device="cpu", fp32=True):
        import quyet
        import torch
        self.m = quyet.load(path, device=device)
        self.model = self.m.model
        if fp32 and device.startswith("cuda"):
            from quyet.encoder import inputs as I
            m = self.m

            def logits_fp32(items):
                out = [None] * len(items)
                for bi in I.length_batches([len(it["ids"]) for it in items], m.max_batch_tokens, m.max_batch_rows):
                    arrays = I.collate([items[i] for i in bi], m.pad_id)
                    b = {k: torch.from_numpy(v).to(m.device) for k, v in arrays.items()}
                    with torch.no_grad():
                        lg = m.model(b).float().cpu().numpy()
                    for j, i in enumerate(bi):
                        out[i] = lg[j, :len(items[i]["markers"])]
                return out
            m._logits = logits_fp32

    def ask(self, text, questions):
        # raw_probabilities: unrounded, aligned with the labels in request order
        probs = self.m.raw_probabilities([{"state": text, "questions": questions}])[0]
        return {q: dict(zip(questions[q]["criteria"].keys(), p)) for q, p in probs.items()}


# ---- baseline ----

# Synthetic training phrases for the 3-NN intent classifier, written for this study; no sentence of
# agent_eval/phrases_es.yaml. "otra" reuses the general phrases of small_models/router_eval.py.
INTENT_TRAIN = {
    "encender": ["enciende las luces de la habitación", "enciende la lámpara", "abre la persiana del baño",
                 "sube la persiana de la cocina", "abre el toldo", "enciende el ventilador", "enciende la tele",
                 "quiero más luz aquí", "prende la luz", "activa el enchufe del calefactor"],
    "apagar": ["apaga la lámpara", "baja las persianas", "cierra el toldo", "apaga el aire", "apaga la cafetera",
               "apágalo todo", "me voy a dormir apaga todo", "cierra la persiana del dormitorio", "apaga la televisión",
               "desconecta el enchufe"],
    "fijar": ["pon la luz al cincuenta por ciento", "pon la calefacción a veinte grados", "sube el brillo al setenta",
              "pon la persiana al treinta por ciento", "pon el volumen al veinte", "ventilador al máximo",
              "baja la temperatura a diecinueve", "deja la luz al diez por ciento"],
    "subir_volumen": ["pon la tele más alta", "sube el volumen del altavoz", "más volumen", "no se oye nada sube"],
    "bajar_volumen": ["baja el volumen del altavoz", "ponme la música más baja", "menos volumen", "está muy alto"],
    "silenciar": ["silencia el televisor", "quita el sonido", "mutea el altavoz"],
    "quitar_silencio": ["activa el sonido de la tele", "vuelve a poner el sonido", "desmutea el altavoz"],
    "pausar": ["pausa la música", "para la persiana", "detén la reproducción", "pausa la serie"],
    "reanudar": ["vuelve a poner la serie", "continúa la música", "dale al play", "sigue reproduciendo"],
    "siguiente": ["siguiente canción", "pasa a la siguiente", "salta esta canción"],
    "temp_iniciar": ["pon un temporizador de cinco minutos", "avísame en veinte minutos",
                     "temporizador de media hora para el horno"],
    "temp_cancelar": ["cancela la alarma", "quita el temporizador", "borra todos los temporizadores"],
    "temp_consultar": ["cuánto queda del temporizador", "cuánto tiempo le queda al temporizador"],
    "estado": ["qué temperatura hay en la cocina", "está encendida la luz del baño", "hay alguna luz encendida",
               "están cerradas las persianas", "qué humedad hay en el salón", "está puesta la calefacción"],
    "otra": ["quién ganó el mundial de dos mil diez", "cuál es el río más largo de europa",
             "cuántos kilómetros hay de madrid a barcelona", "explícame qué es un agujero negro",
             "dime una frase célebre", "cómo se hace una paella", "qué significa la palabra efímero",
             "cuántos habitantes tiene méxico", "quién fue cervantes", "cuéntame algo interesante",
             "cuánto es ciento veinte entre ocho", "traduce perro al alemán", "cuál es la montaña más alta",
             "qué noticias hay de economía", "cómo va la bolsa hoy", "a qué hora juega el barcelona",
             "qué película me recomiendas", "hay huelga de metro mañana", "qué tiempo hará el lunes en sevilla",
             "cuál es el precio de la gasolina hoy", "escribe un poema corto", "por qué el cielo es azul",
             "quién es el presidente de francia", "recomiéndame un libro", "hola qué tal estás",
             "qué hora es en nueva york", "qué hora es", "qué día es hoy", "qué tiempo hace fuera"],
}
# Generic words per device kind (written without looking at the test phrases' wording).
KIND_WORDS = {"luz": ["luz", "luces", "lampara", "lamparita", "bombilla", "flexo", "foco"],
              "persiana": ["persiana", "persianas", "cortina", "cortinas", "estor", "ventana"],
              "toldo": ["toldo", "toldos"], "puerta": ["puerta", "garaje", "cancela", "porton"],
              "clima": ["aire", "aire acondicionado", "calefaccion", "termostato", "temperatura", "grados", "split"],
              "tele": ["tele", "television", "televisor", "tv"],
              "altavoz": ["altavoz", "altavoces", "musica", "cancion", "radio"],
              "ventilador": ["ventilador"], "enchufe": ["enchufe", "interruptor"]}
TODO_RE = re.compile(r"\b(todo|todos los aparatos|apagalo todo)\b")
HOUSE_RE = re.compile(r"\b(toda la casa|de la casa|en casa|de toda la casa)\b")
PLURAL_RE = re.compile(r"\b(todas|todos|luces|persianas|las del|los del)\b")


def _dist(scores, floor=0.02):
    """Pseudo-probabilities: the winner keeps its score in [0, 1], the rest share what is left."""
    best = max(scores, key=scores.get)
    s = max(0.0, min(1.0, scores[best]))
    n = len(scores) - 1
    return {k: (s if k == best else (1 - s) / n if n else 0.0) for k in scores} if s > floor else \
        {k: 1 / len(scores) for k in scores}


def _match(name, text):
    """0..1 fuzzy presence of an option name in a folded transcript (short names: whole word only)."""
    from rapidfuzz import fuzz
    n = D.fold(name)
    if not n:
        return 0.0
    if len(n) <= 4:
        return 1.0 if re.search(r"\b" + re.escape(n) + r"\b", text) else 0.0
    return fuzz.partial_ratio(n, text) / 100


def _names(desc):
    return [x.strip() for x in re.sub(r"\(.*?\)", "", desc).split(",") if x.strip()]


class Baseline:
    max_options = 10

    def __init__(self, path, device="cpu", k=3, fuzzy_min=0.85):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.torch, self.device, self.k, self.fmin = torch, device, k, fuzzy_min
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModel.from_pretrained(path).to(device).eval()
        self.train = [(lab, t) for lab, ts in INTENT_TRAIN.items() for t in ts]
        self.vecs = self.embed([t for _, t in self.train])

    def embed(self, texts):
        torch = self.torch
        b = self.tok(texts, padding=True, truncation=True, max_length=128, return_tensors="pt").to(self.device)
        with torch.no_grad():
            v = self.model(**b).last_hidden_state[:, 0]   # CLS pooling (model card)
        return torch.nn.functional.normalize(v.float(), dim=-1)

    def intent(self, text):
        sims = (self.embed([text]) @ self.vecs.T)[0]
        top = sims.topk(self.k)
        votes = {}
        for s, i in zip(top.values.tolist(), top.indices.tolist()):
            lab = self.train[i][0]
            votes[lab] = votes.get(lab, 0.0) + max(s, 0.0)
        tot = sum(votes.values()) or 1.0
        return {lab: v / tot for lab, v in votes.items()}

    def ask(self, text, questions):
        f = D.fold(text)
        out, votes = {}, None
        for qid, q in questions.items():
            base = qid.split("#")[0]
            opts = q["criteria"]
            if base in ("peticion", "accion", "temporizador"):
                votes = votes or self.intent(text)
                if base == "peticion":
                    m = {"orden": sum(v for k, v in votes.items() if k in D.ACTIONS["es"][1]),
                         "temporizador": sum(v for k, v in votes.items() if k.startswith("temp_")),
                         "estado": votes.get("estado", 0.0), "otra": votes.get("otra", 0.0)}
                elif base == "accion":
                    m = {k: votes.get(k, 0.0) for k in opts}
                    tot = sum(m.values())
                    m = {k: v / tot for k, v in m.items()} if tot else {k: 1 / len(m) for k in m}
                else:
                    m = {k: votes.get("temp_" + k, 0.0) for k in opts}
                    tot = sum(m.values())
                    m = {k: v / tot for k, v in m.items()} if tot else {k: 1 / len(m) for k in m}
                out[qid] = {k: m.get(k, 0.0) for k in opts}
            elif base == "zona":
                sc = {k: max((_match(n, f) for n in _names(v)), default=0.0) if k.startswith("a:") else 0.0
                      for k, v in opts.items()}
                sc = {k: (v if v >= self.fmin else 0.0) for k, v in sc.items()}
                if "casa" in opts and HOUSE_RE.search(f):
                    sc["casa"] = 0.95
                if "aqui" in opts and max(sc.values()) == 0.0:
                    sc["aqui"] = 0.9
                out[qid] = _dist(sc)
            elif base == "aparato":
                sc = {}
                for k, v in opts.items():
                    if k == "todo":
                        sc[k] = 0.9 if TODO_RE.search(f) else 0.0
                        continue
                    words = KIND_WORDS.get(k, []) + _names(v.split(":", 1)[1]) if ":" in v else KIND_WORDS.get(k, [])
                    s = max((_match(w, f) for w in words), default=0.0)
                    if k == "tele" and s < self.fmin and re.search(r"\bvolumen\b", f):
                        s = 0.75
                    sc[k] = s if s >= self.fmin or k == "tele" and s == 0.75 else 0.0
                out[qid] = _dist(sc)
            elif base == "cual":
                sc = {k: (max((_match(n, f) for n in _names(v)), default=0.0) if k.startswith("e:") else 0.0)
                      for k, v in opts.items()}
                best = max(sc.values()) if sc else 0.0
                if best >= self.fmin:
                    ties = [k for k, v in sc.items() if v == best]
                    sc = {k: (best / len(ties) if k in ties else 0.0) for k in sc}
                else:
                    sc = {k: 0.0 for k in sc}
                    sc["todos"] = 0.85 if PLURAL_RE.search(f) or best < 0.6 else 0.5
                out[qid] = _dist(sc)
            else:
                out[qid] = {k: 1 / len(opts) for k in opts}
        return out


def load(name, device="cpu", fp32=True, paths=None):
    """name: laya-ml, laya-en, quyet-tiny, quyet-small, baseline."""
    p = paths or {}
    if name == "laya-ml":
        return Laya(p["laya"], "multilingual", device, fp32)
    if name == "laya-en":
        return Laya(p["laya"], None, device, fp32)
    if name == "quyet-tiny":
        return Quyet(p["quyet-tiny"], device, fp32)
    if name == "quyet-small":
        return Quyet(p["quyet-small"], device, fp32)
    if name == "baseline":
        return Baseline(p["granite"], device)
    raise ValueError(name)


def neutral_keys(ask):
    """Wrap ask() so options are sent as neutral keys "A", "B", ... with the meaning only in the
    description (Laya's card suggests this when label keys dominate the answer); answers are mapped
    back to the original keys. A description-less option gets its key as description."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def wrapped(text, questions):
        sent, back = {}, {}
        for qid, q in questions.items():
            keys = list(q["criteria"])
            m = dict(zip(letters, keys))
            back[qid] = m
            sent[qid] = dict(q, criteria={L: (q["criteria"][k] or k) for L, k in m.items()})
        res = ask(text, sent)
        return {qid: {back[qid][L]: p for L, p in probs.items()} for qid, probs in res.items()}
    return wrapped
