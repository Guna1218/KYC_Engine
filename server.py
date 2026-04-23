import os
import re
import json
import sys
import time
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kyc")

# ── Paths ──
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

MODEL_DIR = os.environ.get("KYC_MODEL_DIR", str(BASE_DIR / "model"))

PORT = int(os.environ.get("KYC_PORT", "7523"))

FIELD_ORDER = [
    "RECORDID","KYCNUMBER","NAME","GUARDIANNAME","GENDER","MARITALSTATUS",
    "DOB","ADDRESS","LANDMARK","CITY","ZIP","CITYOFBIRTH","NATIONALITY",
    "PHOTOATTACHMENT","RESIDENTIALSTATUS","OCCUPATION","ACCOUNTTYPE",
    "ANNUALINCOME","RELATEDPERSONNAME","RELATION","RELATEDPERSONADDRESS",
    "ANYPOLICY","PASSPORT","PASSPORTDATE",
]

FIELD_LABELS = {
    "RECORDID":"Record Id","KYCNUMBER":"Kyc Number","NAME":"Name",
    "GUARDIANNAME":"Guardian Name","GENDER":"Gender","MARITALSTATUS":"Marital Status",
    "DOB":"Date Of Birth","ADDRESS":"Address","LANDMARK":"Landmark",
    "CITY":"City","ZIP":"Zip","CITYOFBIRTH":"City Of Birth",
    "NATIONALITY":"Nationality","PHOTOATTACHMENT":"Photo Attachment",
    "RESIDENTIALSTATUS":"Residential Status","OCCUPATION":"Occupation",
    "ACCOUNTTYPE":"Account Type","ANNUALINCOME":"Annual Income",
    "RELATEDPERSONNAME":"Related Person Name","RELATION":"Relation",
    "RELATEDPERSONADDRESS":"Related Person Address","ANYPOLICY":"Any Policy",
    "PASSPORT":"Passport","PASSPORTDATE":"Passport Date",
}

# Fields that confirm a valid record (at least 3 should be present)
_CORE_FIELDS = {"RECORDID", "KYCNUMBER", "NAME", "DOB", "GENDER", "CITY", "ZIP"}
_MIN_CORE_FOR_VALID_RECORD = 3

NA_KEYWORDS = {"none","not available","not apply","na","not applicable","n/a"}

_DOB_WEEKDAY_DOT_RE = re.compile(r'([A-Za-z]+)\.(?=\s*\d)')
_BKI_OCR_NOISE_RE = re.compile(r'(?i)(B[A-Z]{2,3})[\s.\-_]+(?=[#(\dA-Z])')
_KYC_PREFIX_FIX_RE = re.compile(
    r'(?m)'
    r'^(\d+\s+)'
    r'B[A-Z]{2}'
    r'(?=[A-Z(#\d][^\n]*-\d)',
    re.IGNORECASE
)

SPECIAL_CHARS = set('!@#$%^&*()-_=+{[}]:;"\'|\\<,>.?/~`')
MAX_LENGTH = 512

# ── Confidence threshold for B-RECORDID ──
# Below this, a B-RECORDID prediction is likely noise and ignored.
# Genuine B-RECORDID typically has confidence > 0.90.
B_RECORDID_MIN_CONFIDENCE = 0.50


def normalize_kyc_text(text: str) -> str:
    text = _BKI_OCR_NOISE_RE.sub(r'\1', text)
    text = _KYC_PREFIX_FIX_RE.sub(lambda m: m.group(1) + 'BKI', text)
    return text


def is_na(val):
    if not val or not val.strip():
        return True
    return val.strip().lower() in NA_KEYWORDS


def preprocess(text):
    lines = re.split(r'\r\n|\n|\r', text)
    lines = [l.strip() for l in lines if l.strip()]
    if not lines:
        return text
    joined = lines[0]
    for i in range(1, len(lines)):
        pe = joined[-1] if joined else ''
        ns = lines[i][0] if lines[i] else ''
        if pe in SPECIAL_CHARS or ns in SPECIAL_CHARS:
            joined += lines[i]
        else:
            joined += ' ' + lines[i]
    joined = re.sub(r'([a-z]{2,})([A-Z])', r'\1 \2', joined)
    joined = re.sub(r'([A-Z]{3,})([A-Z][a-z])', r'\1 \2', joined)
    return re.sub(r'\s+', ' ', joined).strip()


def decode_bio(tokens, tags, probs=None):
    """BIO tag sequence → entity list with confidence scores."""
    ents, cur = [], None
    def _c():
        nonlocal cur
        if cur:
            ents.append({
                'type': cur['t'],
                'value': ' '.join(cur['w']),
                'confidence': round(sum(cur['c'])/len(cur['c']), 4) if cur['c'] else 0,
            })
            cur = None
    for i, (tok, tag) in enumerate(zip(tokens, tags)):
        c = probs[i] if probs else 0.0
        if tag.startswith('B-'):
            _c()
            cur = {'t': tag[2:], 'w': [tok], 'c': [c]}
        elif tag.startswith('I-'):
            et = tag[2:]
            if cur and cur['t'] == et:
                cur['w'].append(tok)
                cur['c'].append(c)
            else:
                _c()
                cur = {'t': et, 'w': [tok], 'c': [c]}
        else:
            _c()
    _c()
    return ents


_SINGULAR_FIELDS = {
    "RECORDID","KYCNUMBER","NAME","GUARDIANNAME","DOB",
    "NATIONALITY","PASSPORT","PASSPORTDATE","GENDER",
    "MARITALSTATUS","ZIP","PHOTOATTACHMENT","RESIDENTIALSTATUS",
    "OCCUPATION","ACCOUNTTYPE","ANNUALINCOME",
}

def ents_to_dict(ents):
    """Entity list → field dict.  Singular fields keep the highest-confidence value."""
    r = {}
    for e in ents:
        entry = {'value': e['value'], 'confidence': e['confidence']}
        if e['type'] in r:
            if e['type'] in _SINGULAR_FIELDS:
                existing = r[e['type']]
                if isinstance(existing, list):
                    existing = max(existing, key=lambda x: x['confidence'])
                if entry['confidence'] > existing['confidence']:
                    r[e['type']] = entry
            else:
                if not isinstance(r[e['type']], list):
                    r[e['type']] = [r[e['type']]]
                r[e['type']].append(entry)
        else:
            r[e['type']] = entry
    return r


def _normalize_kycnumber(val: str) -> str:
    if val and len(val) >= 3:
        return "BKI" + val[3:]
    return val

def is_keyword_na(val):
    if not val or not val.strip():
        return False
    return val.strip().lower() in NA_KEYWORDS

_PROPER_CASE_FIELDS = {
    "NAME", "GUARDIANNAME", "ADDRESS", "PHOTOATTACHMENT",
    "OCCUPATION", "RELATEDPERSONNAME", "RELATEDPERSONADDRESS",
}

def proper_case(val: str) -> str:
    if not val:
        return val
    result = []
    capitalise_next = True
    for ch in val:
        if ch.isalpha():
            result.append(ch.upper() if capitalise_next else ch.lower())
            capitalise_next = False
        else:
            result.append(ch)
            if ch == ' ':
                capitalise_next = True
            elif ch.isdigit():
                capitalise_next = True
            elif not ch.isalnum():
                capitalise_next = True
    return ''.join(result)


def post_process(result_json):
    missing, values = [], []
    for f in FIELD_ORDER:
        raw = result_json.get(f)
        if raw is None:
            val = ""
        elif isinstance(raw, dict):
            val = raw.get("value", "")
        elif isinstance(raw, list):
            val = " | ".join(x.get("value", "") for x in raw)
        else:
            val = str(raw)

        if f == "KYCNUMBER" and val and not is_keyword_na(val):
            val = _normalize_kycnumber(val)
        if f == "DOB" and val and not is_keyword_na(val):
            val = _DOB_WEEKDAY_DOT_RE.sub(r'\1,', val)
        if f in _PROPER_CASE_FIELDS and val and not is_keyword_na(val):
            val = proper_case(val)

        if is_keyword_na(val):
            values.append("N.A")
            missing.append(FIELD_LABELS.get(f, f))
        else:
            values.append(val)

    if not missing:
        remarks = "N.A."
    elif len(missing) == 1:
        remarks = f"{missing[0]} Is Missing."
    elif len(missing) == 2:
        remarks = f"{missing[0]} And {missing[1]} Are Missing."
    else:
        remarks = ", ".join(missing[:-1]) + f" And {missing[-1]} Are Missing."

    values.append(remarks)
    return values


# ═══════════════════════════════════════════════════════════════════
#  KYCModel — Neural engine with robust record splitting
# ═══════════════════════════════════════════════════════════════════

class KYCModel:
    def __init__(self, model_dir):
        self.model_dir = model_dir
        self.model = None
        self.tokenizer = None
        self.id2label = None
        self.device = None
        self.loaded = False
        self._token_count_cache = {}   # word → subtoken count cache

    def load(self):
        import torch
        from transformers import AutoTokenizer, AutoModelForTokenClassification

        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        lp = os.path.join(self.model_dir, "label_config.json")
        if not os.path.exists(lp):
            raise FileNotFoundError(
                f"label_config.json not found in {self.model_dir}\n"
                "Copy your trained model files into the 'model' folder."
            )

        with open(lp) as f:
            cfg = json.load(f)
        self.id2label = {int(k): v for k, v in cfg["id2label"].items()}

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_dir, use_fast=True, add_prefix_space=True
        )
        self.model = AutoModelForTokenClassification.from_pretrained(
            self.model_dir, torch_dtype=torch.float32
        )
        self.model.to(self.device)
        self.model.eval()
        self._token_count_cache = {}
        self.loaded = True
        log.info(f"Model loaded on {self.device}")

    # ────────────────────────────────────────────────────────────────
    #  Token counting  (cached + batched for speed)
    # ────────────────────────────────────────────────────────────────

    def _count_subtokens(self, words):
        """
        Return a list of subword-token counts for each word.
        Uses a cache so repeated words ('United', 'States', 'Yes',
        'N/A') are only tokenized once.
        Uncached words are batch-tokenized in a single call.
        """
        counts = [None] * len(words)
        uncached_words = []
        uncached_indices = []

        for i, w in enumerate(words):
            cached = self._token_count_cache.get(w)
            if cached is not None:
                counts[i] = cached
            else:
                uncached_words.append(w)
                uncached_indices.append(i)

        if uncached_words:
            encoded = self.tokenizer(
                uncached_words,
                add_special_tokens=False,
                return_attention_mask=False,
            )
            for j, idx in enumerate(uncached_indices):
                n = max(len(encoded['input_ids'][j]), 1)
                self._token_count_cache[uncached_words[j]] = n
                counts[idx] = n

        return counts

    # ────────────────────────────────────────────────────────────────
    #  Model prediction for a word list
    # ────────────────────────────────────────────────────────────────

    def _predict_words(self, words):
        """Run the model on a list of words and return (tags, confidences)."""
        import torch
        if not words:
            return [], []

        enc = self.tokenizer(
            words, is_split_into_words=True, truncation=True,
            max_length=MAX_LENGTH, padding=True, return_tensors='pt'
        )
        enc = {k: v.to(self.device) for k, v in enc.items()}

        with torch.no_grad():
            logits = self.model(**enc).logits[0]

        probs = torch.softmax(logits, dim=-1)
        pids = torch.argmax(logits, dim=-1).cpu().numpy()
        mprobs = probs.max(dim=-1).values.cpu().numpy()

        wids = self.tokenizer(
            words, is_split_into_words=True, truncation=True, max_length=MAX_LENGTH
        ).word_ids()

        wtags, wconfs, prev = [], [], None
        for i, wid in enumerate(wids):
            if wid is None:
                continue
            if wid != prev:
                wtags.append(self.id2label.get(int(pids[i]), 'O'))
                wconfs.append(float(mprobs[i]))
            prev = wid

        wtags = wtags[:len(words)]
        wconfs = wconfs[:len(words)]
        while len(wtags) < len(words):
            wtags.append('O')
            wconfs.append(0.0)

        return wtags, wconfs

    # ────────────────────────────────────────────────────────────────
    #  Token-aware overlapping chunked prediction
    # ────────────────────────────────────────────────────────────────

    def predict_chunked(self, words):
        """
        Run the model over all words in overlapping token-aware chunks.

        Each chunk is packed to fit within MAX_LENGTH actual subword
        tokens (not word counts).  Overlap is ~80 tokens worth of words.
        For each word in multiple chunks, the higher-confidence prediction
        wins (center-of-chunk words have better context).
        """
        if not words:
            return [], []

        word_tok_counts = self._count_subtokens(words)

        token_budget = MAX_LENGTH - 4
        overlap_tokens = 80

        # Build chunks that fit within the token budget
        chunks = []
        start = 0

        while start < len(words):
            token_count = 0
            end = start
            while end < len(words) and token_count + word_tok_counts[end] <= token_budget:
                token_count += word_tok_counts[end]
                end += 1

            if end == start:
                end = start + 1

            chunks.append((start, end))

            if end >= len(words):
                break

            # Walk back ~80 tokens from end for overlap
            overlap_word_count = 0
            overlap_tok_sum = 0
            for oi in range(end - 1, start, -1):
                overlap_tok_sum += word_tok_counts[oi]
                overlap_word_count += 1
                if overlap_tok_sum >= overlap_tokens:
                    break

            start = max(start + 1, end - overlap_word_count)

        log.info(f"Chunked {len(words)} words → {len(chunks)} chunks "
                 f"(~{sum(word_tok_counts)} subtokens)")

        # Run model on each chunk, merge by confidence
        all_tags  = ['O']  * len(words)
        all_confs = [0.0]  * len(words)

        for c_start, c_end in chunks:
            chunk = words[c_start:c_end]
            tags, confs = self._predict_words(chunk)

            for i, (t, c) in enumerate(zip(tags, confs)):
                gi = c_start + i
                if gi >= len(words):
                    break
                if c > all_confs[gi]:
                    all_tags[gi]  = t
                    all_confs[gi] = c

        return all_tags, all_confs

    # ────────────────────────────────────────────────────────────────
    #  Backward-compatible single-record prediction
    # ────────────────────────────────────────────────────────────────

    def predict(self, text):
        """Single-record prediction."""
        words = text.split()
        if not words:
            return {}
        wtags, wconfs = self._predict_words(words)
        return ents_to_dict(decode_bio(words, wtags, wconfs))

    # ────────────────────────────────────────────────────────────────
    #  Record boundary detection  (3-tier hybrid)
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_record_boundaries(words, tags, confs):
        """
        Identify word indices where a new record starts.

        Three-tier strategy:
          Tier 1 — B-RECORDID with confidence ≥ threshold (neural, primary)
          Tier 2 — B-KYCNUMBER not near an existing boundary (neural, fallback)
                   Catches cases where the model missed the record ID
                   but correctly tagged the BKI# number.
          Tier 3 — Regex on raw words (digit + BKI pattern) as safety net
                   for anything the model missed entirely.

        Returns a sorted list of word indices.
        """
        boundaries = set()

        # ── Tier 1: B-RECORDID (high confidence) ──
        for i, (t, c) in enumerate(zip(tags, confs)):
            if t == 'B-RECORDID' and c >= B_RECORDID_MIN_CONFIDENCE:
                boundaries.add(i)

        # ── Tier 2: B-KYCNUMBER not near a B-RECORDID ──
        for i, (t, c) in enumerate(zip(tags, confs)):
            if t == 'B-KYCNUMBER' and c >= 0.70:
                nearby = any(b in boundaries for b in range(max(0, i - 5), i + 1))
                if not nearby:
                    # Look backward for a digit-only word (the record ID the model missed)
                    found = False
                    for back in range(i - 1, max(-1, i - 4), -1):
                        if back >= 0 and words[back].replace('§', '8').isdigit():
                            boundaries.add(back)
                            found = True
                            break
                    if not found:
                        boundaries.add(i)

        # ── Tier 3: Regex safety net ──
        for i, w in enumerate(words):
            cleaned = w.replace('§', '8').replace('B', '8', 1) if w and w[0] in 'B§' and len(w) >= 6 and w[1:].isdigit() else w.replace('§', '8')
            if re.fullmatch(r'\d{5,8}', cleaned):
                for offset in range(1, min(3, len(words) - i)):
                    nw = words[i + offset]
                    if re.match(r'(?i)^B[A-Z]{2}[#(A-Z\d]', nw):
                        nearby = any(b in boundaries for b in range(max(0, i - 3), i + 3))
                        if not nearby:
                            boundaries.add(i)
                        break

        result = sorted(boundaries)
        if result:
            log.info(f"Boundaries: {len(result)} "
                     f"(tier1-neural: {sum(1 for i,(t,c) in enumerate(zip(tags,confs)) if t=='B-RECORDID' and c>=B_RECORDID_MIN_CONFIDENCE)}, "
                     f"tier2-kycnum: {len(result) - sum(1 for i,(t,c) in enumerate(zip(tags,confs)) if t=='B-RECORDID' and c>=B_RECORDID_MIN_CONFIDENCE)})")
        return result

    # ────────────────────────────────────────────────────────────────
    #  Split word arrays at detected boundaries
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _split_at_boundaries(words, tags, confs, boundaries):
        """
        Given boundary indices, split word/tag/conf arrays into
        per-record segments and decode each into an entity dict.
        """
        if not boundaries:
            ents = decode_bio(words, tags, confs)
            return [ents_to_dict(ents)] if ents else [{}]

        records = []

        # Words before the first boundary (rare — usually junk or header)
        if boundaries[0] > 0:
            seg_w = words[:boundaries[0]]
            seg_t = tags[:boundaries[0]]
            seg_c = confs[:boundaries[0]]
            if seg_w:
                ents = decode_bio(seg_w, seg_t, seg_c)
                if ents:
                    records.append(ents_to_dict(ents))

        # Split at each boundary
        for idx, bnd in enumerate(boundaries):
            end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(words)
            seg_w = words[bnd:end]
            seg_t = tags[bnd:end]
            seg_c = confs[bnd:end]
            if seg_w:
                ents = decode_bio(seg_w, seg_t, seg_c)
                records.append(ents_to_dict(ents))

        return records if records else [{}]

    # ────────────────────────────────────────────────────────────────
    #  Record validation and repair
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _count_core_fields(rec):
        """Count how many core fields a record has (non-empty, non-NA)."""
        count = 0
        for f in _CORE_FIELDS:
            val = rec.get(f)
            if val is not None:
                v = val.get('value', '') if isinstance(val, dict) else ''
                if v and v.strip().lower() not in NA_KEYWORDS:
                    count += 1
        return count

    @staticmethod
    def _merge_records(target, source):
        """Merge source record fields into target (source fills gaps)."""
        for key, val in source.items():
            if key not in target:
                target[key] = val
            elif key not in _SINGULAR_FIELDS:
                if not isinstance(target[key], list):
                    target[key] = [target[key]]
                if isinstance(val, list):
                    target[key].extend(val)
                else:
                    target[key].append(val)

    @classmethod
    def _validate_and_repair(cls, records):
        """
        Post-processing pass over the record list:

        Pass 1 — Absorb orphan fragments:
            Records with fewer than MIN_CORE fields are merged into
            the preceding record (they're likely a continuation that
            the model incorrectly split).

        Pass 2 — Split mega-records:
            Records where RECORDID is a list (two records merged because
            the model missed a B-RECORDID) get split at the second
            RECORDID value.
        """
        if len(records) <= 1:
            return records

        # Pass 1: absorb orphan fragments
        repaired = [records[0]]
        orphans_merged = 0
        for rec in records[1:]:
            core_count = cls._count_core_fields(rec)
            if core_count < _MIN_CORE_FOR_VALID_RECORD:
                cls._merge_records(repaired[-1], rec)
                orphans_merged += 1
            else:
                repaired.append(rec)

        if orphans_merged:
            log.info(f"Merged {orphans_merged} orphan fragment(s)")

        # Pass 2: split mega-records
        final = []
        mega_splits = 0
        for rec in repaired:
            rid = rec.get('RECORDID')
            if isinstance(rid, list) and len(rid) >= 2:
                mega_splits += 1
                # Keep first RECORDID in this record, push rest as new records
                rec_copy = dict(rec)
                rec_copy['RECORDID'] = rid[0]
                final.append(rec_copy)
                for extra_rid in rid[1:]:
                    final.append({'RECORDID': extra_rid})
            else:
                final.append(rec)

        if mega_splits:
            log.info(f"Split {mega_splits} mega-record(s)")

        return final

    # ────────────────────────────────────────────────────────────────
    #  Main extraction pipeline
    # ────────────────────────────────────────────────────────────────

    def extract_batch(self, text, max_records=200):
        """
        Full extraction pipeline:
          1. OCR cleanup + preprocessing
          2. Token-aware chunked prediction (no truncation)
          3. Hybrid record boundary detection (neural + fallback + regex)
          4. Record splitting at boundaries
          5. Orphan fragment merging + mega-record splitting
          6. Post-processing (normalization, proper case, missing fields)

        Returns (rows, stats) where rows is a list of value arrays
        and stats is a diagnostic dict.
        """
        text = normalize_kyc_text(text)
        processed = preprocess(text)
        words = processed.split()
        if not words:
            return [], {"records": 0, "words": 0, "chunks": 0}

        t0 = time.time()

        # Run model (token-aware chunking)
        all_tags, all_confs = self.predict_chunked(words)
        t_predict = time.time() - t0

        # Find record boundaries (3-tier hybrid)
        boundaries = self._find_record_boundaries(words, all_tags, all_confs)
        log.info(f"Found {len(boundaries)} boundaries in {len(words)} words "
                 f"({t_predict*1000:.0f}ms prediction)")

        # Split at boundaries
        record_dicts = self._split_at_boundaries(words, all_tags, all_confs, boundaries)

        # Validate and repair
        record_dicts = self._validate_and_repair(record_dicts)
        log.info(f"Final: {len(record_dicts)} records")

        # Post-process
        rows = []
        for rd in record_dicts[:max_records]:
            rows.append(post_process(rd))

        stats = {
            "records": len(rows),
            "words": len(words),
            "boundaries_detected": len(boundaries),
            "predict_ms": int(t_predict * 1000),
        }
        return rows, stats


# ── FastAPI App ──
_kyc_model = KYCModel(MODEL_DIR)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"ok": True}

@app.get("/api/load_model")
async def load_model():
    try:
        _kyc_model.load()
        return {"ok": True, "device": _kyc_model.device}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/extract")
async def extract(request: Request):
    if not _kyc_model.loaded:
        return {"ok": False, "error": "Model not loaded"}
    try:
        body = await request.json()
        text = body.get("text", "")
        t0 = time.time()
        rows, stats = _kyc_model.extract_batch(text, max_records=200)
        elapsed = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "rows": rows,
            "elapsed": elapsed,
            "stats": stats,
        }
    except Exception as e:
        log.exception("Extraction failed")
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import socket, subprocess

    def free_port(port):
        try:
            result = subprocess.check_output(
                f'netstat -ano | findstr :{port}', shell=True
            ).decode()
            for line in result.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and f':{port}' in parts[1]:
                    pid = parts[-1]
                    subprocess.call(
                        f'taskkill /PID {pid} /F', shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
        except Exception:
            pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    in_use = s.connect_ex(('127.0.0.1', PORT)) == 0
    s.close()

    if in_use:
        print(f"[server] Port {PORT} busy — releasing...")
        free_port(PORT)
        time.sleep(1)

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")