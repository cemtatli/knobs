"""English -> Turkish translation for youdub, with OPUS-MT on CTranslate2 (int8, ~230 MB).

YouTube's own Turkish runs 25-60% longer than the English it replaces, so the voice had to
rush or the video had to slow down. This model stays close to the English length, and it
returns several candidate translations: we take the best one that fits the sentence's
time, as long as it is nearly as likely as the best.

Model: Helsinki-NLP/opus-mt-tc-big-en-tr (CC-BY-4.0), converted by install.sh into
models/opus-mt-en-tr. Without it, /translate is off and youdub uses YouTube's translation.
It runs in worker.py's child process, never in the server itself (see there why).
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

MODEL = Path(__file__).parent / "models" / "opus-mt-en-tr"
CANDIDATES = 4
MAX_BATCH_TOKENS = 384
SCORE_MARGIN = 0.12  # a shorter candidate may score this much lower (log-prob per token)
MIN_SAVING = 0.92  # ...and must be at most this fraction of the best one's length

# Keep discourse markers and answers such as "but", "yes" and "so" for the translator.
HESITATIONS = re.compile(r"\b(?:um+|uh+)\b[,.]?", re.I)
NUMBERS = re.compile(r"(?<!\d)[+−-]?\d+(?:[.,]\d+)*")
NEGATION = re.compile(
    r"\b(?:değil\w*|yok(?:tu|muş|tur)?|hayır|asla|\w+(?:madan|meden|madı|medi|mayacak|meyecek)\w*)\b",
    re.I,
)
# OPUS-MT can translate "seek back 30 seconds" as "call back in 30 seconds".
MEDIA_SEEK = re.compile(
    r"\bseek\s+(back(?:wards?)?|forwards?)\s+(?:by\s+)?(\d+(?:[.,]\d+)?\s+(?:seconds?|minutes?|hours?))\b(?: in the video)?",
    re.I,
)

# Characters the model's vocabulary doesn't know come out as "<unk>", and the voice says
# "unk". Symbols that mean something become words first; accents are dropped (café -> cafe);
# anything else unknown (♪, emoji, braces) is removed. See clean().
SYMBOLS = [
    (re.compile(r"\s*°\s*C\b", re.I), " degrees Celsius"),
    (re.compile(r"\s*°\s*F\b", re.I), " degrees Fahrenheit"),
    (re.compile(r"\s*°"), " degrees"),
    (re.compile(r"€\s*([\d.,]+)|([\d.,]+)\s*€"), lambda m: f"{m[1] or m[2]} euros"),
    (re.compile(r"£\s*([\d.,]+)|([\d.,]+)\s*£"), lambda m: f"{m[1] or m[2]} pounds"),
    (re.compile(r"(\d)\s*×"), r"\1x"),
    (re.compile(r"[€]"), " euro"),
    (re.compile(r"[£]"), " pound"),
    (re.compile(r"[×]"), " times "),
    (re.compile(r"⁄"), "/"),
]

translator = None
source_spm = None
target_spm = None


def available():
    return (MODEL / "model.bin").exists()


def load():
    global translator, source_spm, target_spm
    if translator is not None or not available():
        return
    import ctranslate2
    import sentencepiece

    source_spm = sentencepiece.SentencePieceProcessor(model_file=str(MODEL / "source.spm"))
    target_spm = sentencepiece.SentencePieceProcessor(model_file=str(MODEL / "target.spm"))
    translator = ctranslate2.Translator(str(MODEL), device="cpu", compute_type="int8", intra_threads=2)  # leave cores for the video


@lru_cache(maxsize=512)
def known_char(char):
    return source_spm.unk_id() not in source_spm.encode(char)


def clean(text):
    for pattern, repl in SYMBOLS:
        text = pattern.sub(repl, text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    for pattern, repl in SYMBOLS[-1:]:  # NFKD turns ½ into 1⁄2
        text = pattern.sub(repl, text)
    return "".join(c for c in text if c.isspace() or known_char(c))


def trim(text):
    text = clean(text)
    text = HESITATIONS.sub("", text)
    text = MEDIA_SEEK.sub(
        lambda m: f"rewind the video by {m[2]}" if m[1].lower().startswith("back") else f"skip forward {m[2]} in the video",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return text[:1].upper() + text[1:]


def translate(text, chars=None):
    """Turkish for `text`; with `chars`, prefer a translation at most that long."""
    return translate_many([(text, chars)])[0]


def translate_many(items):
    """Turkish for each (text, chars) pair. Together they run ~2x faster than one by one."""
    texts = [trim(text) for text, _ in items]
    todo = [i for i, text in enumerate(texts) if text]
    # Marian models need the end-of-sentence token, or they repeat themselves.
    tokens = [source_spm.encode(texts[i], out_type=str) + ["</s>"] for i in todo]
    results = translator.translate_batch(
        tokens, max_batch_size=MAX_BATCH_TOKENS, batch_type="tokens", beam_size=CANDIDATES,
        num_hypotheses=CANDIDATES, return_scores=True, max_decoding_length=256,
    ) if tokens else []
    out = [""] * len(items)
    for i, result in zip(todo, results):
        # The model can still emit "<unk>" (it copies what it can't translate); never speak it.
        options = [
            (re.sub(r"\s+", " ", target_spm.decode([p for p in h if p != "<unk>"])).strip(), s)
            for h, s in zip(result.hypotheses, result.scores)
        ]
        out[i] = choose(options, items[i][1])
    return out


def choose(options, chars):
    best, best_score = options[0]
    if not chars or len(best) <= chars:
        return best
    # Prefer the first high-scoring candidate that saves time and keeps explicit numbers and negatives.
    limit = min(chars, len(best) * MIN_SAVING)
    numbers = NUMBERS.findall(best)
    negatives = [word.lower() for word in NEGATION.findall(best)]
    for text, score in options[1:]:
        if not text or len(text) > limit or score < best_score - SCORE_MARGIN:
            continue
        if NUMBERS.findall(text) == numbers and [word.lower() for word in NEGATION.findall(text)] == negatives:
            return text
    return best
