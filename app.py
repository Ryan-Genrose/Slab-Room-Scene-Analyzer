
import base64
import collections
import copy
import difflib
import hashlib
import html
import io
import json
import re
import smtplib
import ssl
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

import pandas as pd
import numpy as np
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter, ImageOps

try:
    from google.api_core.exceptions import AlreadyExists, NotFound
    from google.cloud import storage, vision
    from google.oauth2 import service_account
    GOOGLE_LIBS = True
except Exception:
    GOOGLE_LIBS = False

st.set_page_config(page_title="GENROSE Room Scene Analyzer", page_icon="🪨", layout="wide", initial_sidebar_state="collapsed")

ROOT = Path(__file__).parent
CATALOG_PATH = ROOT / "data" / "stone_sku_master.csv"
LOCAL_DATA = ROOT / ".runtime_data"
LOCAL_DATA.mkdir(exist_ok=True)
LOCAL_REVIEW_ROOT = LOCAL_DATA / "review_batches"
LOCAL_REVIEW_ROOT.mkdir(exist_ok=True)
LOCAL_WEBSITE_CACHE = LOCAL_DATA / "genrose_website_catalog.json"
LOCAL_MATERIAL_THUMBNAIL_ROOT = LOCAL_DATA / "material_thumbnails"
LOCAL_MATERIAL_THUMBNAIL_ROOT.mkdir(exist_ok=True)
REFERENCE_CATALOG_PATH = ROOT / "data" / "genrose_reference_catalog.json"
GENROSE_ASSET_BASE = "https://www.genrose.com/Customer-Content/www/Products/TileTypes"
MAX_GOOGLE_REFERENCES_PER_MATERIAL = 2

GENROSE_INDEX = "https://www.genrose.com/Products/natural-stone-slabs/"
REVIEW_EMAIL = "marketing@genrose.com"

ROOM_TYPES = [
    "Kitchen", "KitchenCounter", "KitchenIsland",
    "LivingRoom", "DiningRoom", "Bedroom",
    "Bathroom", "PowderRoom", "PrimaryBathroom", "Shower", "Vanity",
    "Reception", "Lobby", "Office", "ConferenceRoom",
    "Showroom", "Retail", "Restaurant", "Bar",
    "Fireplace", "MudRoom", "Entryway", "Foyer", "Hallway",
    "LaundryRoom", "FeatureWall", "Exterior", "Other"
]

# Filename room-language aliases. These are intentionally explicit rather than translating
# the entire filename because many stone names are themselves Italian.
ROOM_ALIASES = {
    "Kitchen": ["kitchen", "cucina", "cucine"],
    "KitchenCounter": ["kitchen counter", "kitchen countertop", "countertop", "counter top", "piano cucina"],
    "KitchenIsland": ["kitchen island", "island", "isola cucina", "isola"],
    "LivingRoom": ["living room", "family room", "soggiorno", "salotto", "zona giorno"],
    "DiningRoom": ["dining room", "dining", "sala da pranzo"],
    "Bedroom": ["bedroom", "camera da letto", "camera letto"],
    "Bathroom": ["bathroom", "bath", "bagno", "bagni", "stanza da bagno", "vasca"],
    "PowderRoom": ["powder room", "powderroom", "half bath", "toilette", "bagno ospiti"],
    "PrimaryBathroom": ["primary bathroom", "master bathroom", "primary bath", "master bath", "bagno padronale"],
    "Shower": ["shower", "doccia", "box doccia"],
    "Vanity": ["vanity", "bath vanity", "mobile bagno", "lavabo"],
    "Reception": ["reception", "reception desk", "front desk", "frontdesk", "concierge", "reception area", "reception counter"],
    "Lobby": ["lobby", "hotel lobby", "waiting area", "waiting room", "reception lobby"],
    "Office": ["office", "home office", "ufficio", "studio"],
    "ConferenceRoom": ["conference room", "meeting room", "boardroom", "sala riunioni"],
    "Showroom": ["showroom", "display room", "display area", "gallery showroom"],
    "Retail": ["retail", "store", "shop", "boutique", "negozio"],
    "Restaurant": ["restaurant", "ristorante", "dining hall"],
    "Bar": ["wet bar", "home bar", "bar", "cocktail bar"],
    "Fireplace": ["fireplace", "hearth", "mantel", "camino", "caminetto"],
    "MudRoom": ["mudroom", "mud room", "ingresso di servizio"],
    "Entryway": ["entryway", "entry way", "entrance", "ingresso"],
    "Foyer": ["foyer", "atrio"],
    "Hallway": ["hallway", "corridor", "corridoio"],
    "LaundryRoom": ["laundry room", "laundry", "lavanderia"],
    "FeatureWall": ["feature wall", "accent wall", "wall cladding", "parete", "rivestimento parete"],
    "Exterior": ["exterior", "outdoor", "patio", "facade", "facciata", "esterno", "esterni"],
}

FILENAME_NOISE = {
    "1000px","72ppi","300dpi","final","copy","room","scene","render","image","img","photo",
    "pattern","spiga","ambientata","ambientato","ambiente","application","web","hero","new"
}
MATERIAL_GENERIC = {
    "white","black","blue","grey","gray","green","gold","extra","select","new","original",
    "light","dark","slab","slabs","stone","marble","granite","quartzite","onyx","travertine"
}

def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

def ascii_text(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()

def norm(s):
    s = ascii_text(s).lower().replace("_", " ").replace("-", " ")
    # Known recurring source typo.
    s = s.replace("damsco", "damasco")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def compact(s):
    return re.sub(r"[^a-z0-9]+", "", norm(s))

def safe_filename(s):
    s = ascii_text(s)
    s = re.sub(r'[<>:"/\\|?*]+', "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def safe_id(s):
    return re.sub(r"[^a-z0-9_-]+", "-", norm(s)).strip("-")[:120] or uuid.uuid4().hex[:16]

@st.cache_data
def load_catalog():
    return pd.read_csv(CATALOG_PATH).fillna("")

catalog = load_catalog()
catalog_records = catalog.to_dict("records")

MATERIAL_TOKEN_FREQ = collections.Counter()
for _rec in catalog_records:
    _tokens = set(
        norm(_rec.get("StoneType","")).split()
        + norm(_rec.get("Source Color Name","")).split()
    )
    for _tok in _tokens:
        if len(_tok) >= 4 and _tok not in MATERIAL_GENERIC:
            MATERIAL_TOKEN_FREQ[_tok] += 1

def row_for_sku(sku):
    target = str(sku or "").upper().strip()
    for r in catalog_records:
        if str(r["SKU"]).upper().strip() == target:
            return r
    return None

def row_for_stone(stone):
    target = compact(stone)
    for r in catalog_records:
        if compact(r["StoneType"]) == target or compact(r["Source Color Name"]) == target:
            return r
    return None

# ---------------- filename intelligence ----------------

def strip_room_words(text):
    x = " " + norm(text) + " "
    aliases = sorted(
        [a for vals in ROOM_ALIASES.values() for a in vals],
        key=len, reverse=True
    )
    for alias in aliases:
        x = x.replace(" " + norm(alias) + " ", " ")
    for word in FILENAME_NOISE:
        x = x.replace(" " + word + " ", " ")
    x = re.sub(r"\b\d{2,}\b", " ", x)
    return re.sub(r"\s+", " ", x).strip()

def detect_room_from_text(text):
    x = " " + norm(text) + " "
    matches = []
    for room, aliases in ROOM_ALIASES.items():
        for alias in aliases:
            a = " " + norm(alias) + " "
            if a in x:
                # Longer phrases are more trustworthy than one-word terms.
                score = min(.99, .90 + min(len(alias.split()), 3) * .03)
                matches.append((score, room, alias))
    if matches:
        matches.sort(reverse=True)
        score, room, alias = matches[0]
        return {"room": room, "score": score, "matched_term": alias}
    return {"room": "Other", "score": .18, "matched_term": ""}


def _phrase_hits(text, phrases):
    x = " " + norm(text) + " "
    return sum(1 for p in phrases if (" " + norm(p) + " ") in x)

def classify_room_from_vision(vision_data):
    """Classify the semantic room/space from Google Vision evidence.

    Uses labels, web best guesses/entities, localized objects, and OCR. Scores
    are heuristic evidence weights, then converted into a conservative UI
    confidence. This is intentionally separate from exact material recognition.
    """
    labels = vision_data.get("labels", [])
    web = vision_data.get("web_entities", [])
    best = vision_data.get("best_guess", [])
    objects = vision_data.get("objects", [])
    text = vision_data.get("text", "")
    evidence = " ".join(labels + web + best + objects + [text])
    e = norm(evidence)

    scores = {r: 0.0 for r in ROOM_TYPES if r != "Other"}
    reasons = {r: [] for r in scores}

    def add(room, points, reason):
        scores[room] = scores.get(room, 0.0) + points
        reasons.setdefault(room, []).append(reason)

    # Strong scene labels.
    strong = {
        "Reception": ["reception", "reception desk", "front desk", "concierge"],
        "Lobby": ["lobby", "waiting area", "waiting room"],
        "Kitchen": ["kitchen"],
        "KitchenIsland": ["kitchen island"],
        "KitchenCounter": ["kitchen counter", "countertop"],
        "LivingRoom": ["living room", "family room"],
        "DiningRoom": ["dining room"],
        "Bedroom": ["bedroom"],
        "Bathroom": ["bathroom"],
        "PowderRoom": ["powder room"],
        "PrimaryBathroom": ["primary bathroom", "master bathroom"],
        "Shower": ["shower"],
        "Office": ["office"],
        "ConferenceRoom": ["conference room", "meeting room", "boardroom"],
        "Showroom": ["showroom"],
        "Retail": ["retail store", "store interior", "boutique"],
        "Restaurant": ["restaurant"],
        "Bar": ["bar interior", "cocktail bar", "pub"],
        "MudRoom": ["mudroom", "mud room"],
        "LaundryRoom": ["laundry room"],
        "Hallway": ["hallway", "corridor"],
        "Foyer": ["foyer"],
        "Entryway": ["entryway", "entrance hall"],
        "Exterior": ["exterior", "outdoor", "facade"],
    }
    for room, phrases in strong.items():
        hits = _phrase_hits(e, phrases)
        if hits:
            add(room, 6.0 + min(hits - 1, 2), "strong scene label")

    # Distinctive object cues.
    if _phrase_hits(e, ["bed", "mattress", "bed frame"]):
        add("Bedroom", 5.5, "bed detected")
    if _phrase_hits(e, ["toilet", "bathtub", "bath tub"]):
        add("Bathroom", 5.5, "bath fixture detected")
    if _phrase_hits(e, ["shower", "showerhead"]):
        add("Shower", 5.5, "shower fixture detected")
        add("Bathroom", 2.0, "bathroom fixture detected")
    if _phrase_hits(e, ["stove", "oven", "refrigerator", "kitchen appliance"]):
        add("Kitchen", 5.5, "kitchen appliance detected")
    if _phrase_hits(e, ["sofa", "couch"]):
        add("LivingRoom", 4.5, "sofa/couch detected")
    if _phrase_hits(e, ["washing machine", "washer", "dryer"]):
        add("LaundryRoom", 5.5, "laundry appliance detected")
    if _phrase_hits(e, ["fireplace", "hearth"]):
        add("Fireplace", 6.0, "fireplace detected")
    if _phrase_hits(e, ["display case", "display cabinet", "merchandise"]):
        add("Retail", 4.0, "retail/display cue")
        add("Showroom", 3.0, "display cue")

    # Reception/front desk inference. Vision often says "desk/counter/interior"
    # rather than literally "reception desk", so score combinations.
    front_counter = _phrase_hits(e, ["desk", "counter", "countertop", "front desk", "reception desk"])
    commercial = _phrase_hits(e, [
        "hotel", "commercial building", "office building", "business",
        "public space", "lobby", "waiting room", "concierge", "reception"
    ])
    waiting = _phrase_hits(e, ["waiting", "seating", "chair", "sofa"])
    workstation = _phrase_hits(e, ["computer", "monitor", "keyboard", "workstation", "office chair"])

    if front_counter:
        add("Reception", 1.8, "front counter/desk cue")
        add("Office", 1.0, "desk cue")
    if front_counter and commercial:
        add("Reception", 5.5, "counter + commercial/lobby cues")
    if front_counter and commercial and waiting:
        add("Reception", 2.0, "counter + waiting/seating cues")
    if _phrase_hits(e, ["hotel"]) and front_counter:
        add("Reception", 4.0, "hotel + desk/counter")
        add("Lobby", 2.0, "hotel context")
    if _phrase_hits(e, ["lobby"]) and front_counter:
        add("Reception", 4.5, "lobby + desk/counter")
    if _phrase_hits(e, ["lobby"]) and not front_counter:
        add("Lobby", 4.0, "lobby without front desk")
    if workstation >= 2 and commercial == 0:
        add("Office", 4.0, "workstation cues")
    if workstation >= 2 and not front_counter:
        add("Office", 2.0, "office workstation")

    # Dining / restaurant / bar.
    table = _phrase_hits(e, ["dining table", "table"])
    chairs = _phrase_hits(e, ["chair", "chairs", "seating"])
    if _phrase_hits(e, ["restaurant", "food service"]) and table:
        add("Restaurant", 5.0, "restaurant + tables")
    elif table and chairs:
        add("DiningRoom", 2.2, "table + seating")
    if _phrase_hits(e, ["bar stool", "barstool"]):
        add("Bar", 3.5, "bar stools")
        add("KitchenIsland", 1.0, "counter seating")

    # Counter-only commercial scenes should not become kitchens automatically.
    if front_counter and commercial and not _phrase_hits(e, ["stove", "oven", "refrigerator", "kitchen"]):
        scores["KitchenCounter"] *= 0.35
        scores["Kitchen"] *= 0.35

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_room, top_score = ranked[0] if ranked else ("Other", 0.0)
    second = ranked[1][1] if len(ranked) > 1 else 0.0

    if top_score <= 0:
        return {"room": "Other", "score": .22, "reason": "No strong room cues", "candidates": []}

    margin = max(0.0, top_score - second)
    confidence = min(.96, .50 + min(top_score, 10) * .035 + min(margin, 5) * .03)
    if top_score < 3:
        confidence = min(confidence, .63)

    candidates = [
        {"room": room, "weight": score, "reasons": reasons.get(room, [])}
        for room, score in ranked[:4] if score > 0
    ]
    return {
        "room": top_room,
        "score": confidence,
        "reason": "; ".join(reasons.get(top_room, [])[:3]) or "Vision scene cues",
        "candidates": candidates,
    }


def token_material_score(clean_text, material_name):
    a = strip_room_words(clean_text)
    b = norm(material_name)
    if not a or not b:
        return 0.0

    ac = compact(a)
    bc = compact(b)
    if bc and bc in ac:
        return .995

    at = set(a.split())
    bt = set(b.split())
    if not bt:
        return 0.0
    intersection = len(at & bt)
    containment = intersection / len(bt)
    jaccard = intersection / len(at | bt) if at | bt else 0
    seq = difflib.SequenceMatcher(None, ac, bc).ratio()

    distinctive = [t for t in bt if len(t) >= 4 and t not in MATERIAL_GENERIC]
    distinctive_hits = sum(1 for t in distinctive if t in at)
    distinctive_ratio = distinctive_hits / len(distinctive) if distinctive else 0

    score = max(
        containment * .96,
        jaccard * .90,
        seq * .78,
        distinctive_ratio * .90,
    )
    if containment == 1:
        score = max(score, .96)
    if distinctive and distinctive_ratio == 1 and len(distinctive) >= 2:
        score = max(score, .97)

    rare_hits = [
        tok for tok in distinctive
        if tok in at and MATERIAL_TOKEN_FREQ.get(tok, 999) <= 4
    ]
    if rare_hits:
        rarest = min(MATERIAL_TOKEN_FREQ.get(tok, 999) for tok in rare_hits)
        rarity_floor = {1: .88, 2: .82, 3: .76, 4: .71}.get(rarest, .68)
        score = max(score, rarity_floor)
    if len(rare_hits) >= 2:
        score = max(score, .94)

    return min(.995, score)

def filename_material_candidates(filename, limit=8):
    scored = []
    for r in catalog_records:
        score = max(
            token_material_score(filename, r["Source Color Name"]),
            token_material_score(filename, r["StoneType"])
        )
        scored.append({
            "stone": r["StoneType"],
            "sku": r["SKU"],
            "source_name": r["Source Color Name"],
            "score": score,
            "source": "Filename"
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]

# ---------------- Google Cloud ----------------

def google_credentials():
    if not GOOGLE_LIBS:
        return None
    raw = secret("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return None
    try:
        info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return service_account.Credentials.from_service_account_info(info)
    except Exception:
        return None

def vision_ready():
    return bool(GOOGLE_LIBS and google_credentials())

def storage_ready():
    return bool(vision_ready() and secret("GOOGLE_CLOUD_BUCKET", ""))

def product_search_ready():
    return bool(
        vision_ready()
        and secret("GOOGLE_CLOUD_PROJECT", "")
        and secret("GOOGLE_CLOUD_LOCATION", "")
        and secret("GOOGLE_CLOUD_PRODUCT_SET_ID", "")
    )

def storage_client():
    return storage.Client(
        project=secret("GOOGLE_CLOUD_PROJECT", ""),
        credentials=google_credentials()
    )

def image_client():
    return vision.ImageAnnotatorClient(credentials=google_credentials())

def product_client():
    return vision.ProductSearchClient(credentials=google_credentials())

def run_google_vision(image_bytes):
    """Cloud Vision pass: labels + web detection + OCR.

    IMPORTANT: Cloud failure must NEVER destroy filename/room analysis.
    This function always returns a result dictionary, even when Google returns
    a permissions/API/billing error.
    """
    if not vision_ready():
        return {
            "labels": [], "web_entities": [], "best_guess": [], "web_pages": [], "objects": [], "text": "",
            "error": "Vision not configured"
        }

    try:
        client = image_client()
        image = vision.Image(content=image_bytes)
        features = [
            vision.Feature(type_=vision.Feature.Type.LABEL_DETECTION, max_results=30),
            vision.Feature(type_=vision.Feature.Type.WEB_DETECTION, max_results=25),
            vision.Feature(type_=vision.Feature.Type.OBJECT_LOCALIZATION, max_results=30),
            vision.Feature(type_=vision.Feature.Type.TEXT_DETECTION, max_results=8),
        ]
        req = vision.AnnotateImageRequest(image=image, features=features)
        resp = client.batch_annotate_images(requests=[req]).responses[0]

        if resp.error.message:
            return {
                "labels": [], "web_entities": [], "best_guess": [], "web_pages": [], "objects": [], "text": "",
                "error": resp.error.message
            }

        labels = [x.description for x in resp.label_annotations]
        web_entities = [x.description for x in resp.web_detection.web_entities if x.description]
        best_guess = [x.label for x in resp.web_detection.best_guess_labels if x.label]
        web_pages = [x.url for x in resp.web_detection.pages_with_matching_images if x.url][:10]
        objects = [x.name for x in resp.localized_object_annotations if x.name]
        text = resp.text_annotations[0].description if resp.text_annotations else ""
        return {
            "labels": labels,
            "web_entities": web_entities,
            "best_guess": best_guess,
            "web_pages": web_pages,
            "objects": objects,
            "text": text,
            "error": ""
        }
    except Exception as e:
        return {
            "labels": [], "web_entities": [], "best_guess": [], "web_pages": [], "objects": [], "text": "",
            "error": str(e)
        }

def _product_search_single(image_bytes, limit=8):
    if not product_search_ready():
        return []

    pc = product_client()
    ic = image_client()
    set_path = pc.product_set_path(
        project=secret("GOOGLE_CLOUD_PROJECT", ""),
        location=secret("GOOGLE_CLOUD_LOCATION", "us-east1"),
        product_set=secret("GOOGLE_CLOUD_PRODUCT_SET_ID", "genrose-slabs")
    )
    params = vision.ProductSearchParams(
        product_set=set_path,
        product_categories=["general-v1"],
        filter=""
    )
    context = vision.ImageContext(product_search_params=params)
    response = ic.product_search(
        vision.Image(content=image_bytes),
        image_context=context,
        max_results=limit
    )
    results = []
    for result in response.product_search_results.results:
        labels = {kv.key: kv.value for kv in result.product.product_labels}
        sku = labels.get("sku", "")
        rec = row_for_sku(sku) or row_for_stone(result.product.display_name)
        if not rec:
            continue
        results.append({
            "stone": rec["StoneType"],
            "sku": rec["SKU"],
            "source_name": rec["Source Color Name"],
            "material_type": labels.get("material_type",""),
            "score": float(result.score),
            "source": "Google Product Search",
            "reference_image": result.image
        })
    return results

def _jpeg_bytes_for_crop(image, box):
    crop = image.crop(box)
    buf = io.BytesIO()
    if crop.mode not in ("RGB", "L"):
        crop = crop.convert("RGB")
    crop.save(buf, format="JPEG", quality=90)
    return buf.getvalue()

def run_product_search(image_bytes, limit=8, multi_crop=False):
    """Search the GENROSE Product Search catalog.

    When filename evidence is very weak, also search a few large room-scene crops.
    Stone is often only a countertop/wall/floor region, so this can surface a better
    visual candidate than whole-image matching alone.
    """
    if not product_search_ready():
        return []

    queries = [("full", image_bytes)]
    if multi_crop:
        try:
            im = Image.open(io.BytesIO(image_bytes))
            w, h = im.size
            # Large overlapping crops; deliberately few to control API usage.
            boxes = [
                ("center", (int(w*.15), int(h*.12), int(w*.85), int(h*.88))),
                ("lower", (0, int(h*.42), w, h)),
            ]
            for name, box in boxes:
                if box[2] > box[0] + 50 and box[3] > box[1] + 50:
                    queries.append((name, _jpeg_bytes_for_crop(im, box)))
        except Exception:
            pass

    by_sku = {}
    for query_name, qbytes in queries:
        try:
            for result in _product_search_single(qbytes, limit):
                sku = result["sku"]
                if sku not in by_sku or result["score"] > by_sku[sku]["score"]:
                    by_sku[sku] = {**result, "query_region": query_name}
        except Exception:
            continue

    ranked = sorted(by_sku.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]

def vision_text_material_candidates(vision_data, limit=8):
    evidence = " ".join(
        vision_data.get("web_entities", [])
        + vision_data.get("labels", [])
        + [vision_data.get("text", "")]
    )
    scored = []
    for r in catalog_records:
        score = max(
            token_material_score(evidence, r["Source Color Name"]),
            token_material_score(evidence, r["StoneType"])
        )
        scored.append({
            "stone": r["StoneType"],
            "sku": r["SKU"],
            "source_name": r["Source Color Name"],
            "score": score,
            "source": "Google Vision Web/Labels/OCR"
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


# ---------------- bundled GENROSE slab-image references ----------------

@st.cache_data
def load_bundled_reference_catalog():
    try:
        return json.loads(REFERENCE_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"materials": {}}

def bundled_reference_entry(sku):
    return load_bundled_reference_catalog().get("materials", {}).get(str(sku), {})

def bundled_reference_stats():
    materials = load_bundled_reference_catalog().get("materials", {})
    with_refs = sum(1 for e in materials.values() if e.get("references"))
    total_refs = sum(len(e.get("references", [])) for e in materials.values())
    return len(materials), with_refs, total_refs

def reference_url_candidates(entry, reference):
    filename = str(reference.get("filename","")).strip()
    if not filename:
        return []
    filename_q = quote(filename, safe="-_.()")
    urls = []
    for folder in entry.get("folder_slugs", []):
        folder = str(folder or "").strip("/")
        if not folder:
            continue
        urls.append(f"{GENROSE_ASSET_BASE}/{quote(folder, safe='-')}/{filename_q}")
    return list(dict.fromkeys(urls))

def download_reference_candidates(entry, reference):
    headers = {
        "User-Agent": "Mozilla/5.0 GENROSE-room-scene-reference-builder/1.0",
        "Referer": "https://www.genrose.com/"
    }
    failures = []
    for url in reference_url_candidates(entry, reference):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if r.status_code >= 400:
                failures.append(f"{r.status_code} {url}")
                continue
            ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
            if not ctype.startswith("image/"):
                failures.append(f"not-image {url}")
                continue
            return r.content, ctype, url
        except Exception as e:
            failures.append(f"{type(e).__name__}: {url}")
    raise RuntimeError("No working direct GENROSE asset URL. " + " | ".join(failures[:4]))

# ---------------- GENROSE website reference catalog ----------------

def website_cache_path():
    return "config/genrose_website_catalog.json"

def load_website_cache():
    if storage_ready():
        try:
            blob = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(website_cache_path())
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception:
            pass
    if LOCAL_WEBSITE_CACHE.exists():
        try:
            return json.loads(LOCAL_WEBSITE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"synced_at": "", "materials": {}}

def save_website_cache(cache):
    LOCAL_WEBSITE_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    if storage_ready():
        storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(website_cache_path()).upload_from_string(
            json.dumps(cache, indent=2), content_type="application/json"
        )

def fetch_html(url, timeout=20):
    headers = {"User-Agent": "Mozilla/5.0 GENROSE-room-scene-reference-builder/1.0"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

def extract_collection_links(index_html):
    soup = BeautifulSoup(index_html, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(GENROSE_INDEX, a["href"])
        parsed = urlparse(href)
        path = parsed.path.rstrip("/")
        prefix = "/Products/natural-stone-slabs"
        if not path.lower().startswith(prefix.lower() + "/"):
            continue
        remainder = path[len(prefix):].strip("/")
        # Collection pages have one segment after natural-stone-slabs; SKU detail pages have two.
        if not remainder or "/" in remainder:
            continue
        label = " ".join(a.stripped_strings).strip()
        if label:
            links[href] = label
    return [{"url": u, "label": l} for u, l in links.items()]

def best_collection_link(record, links):
    target_names = [record["Source Color Name"], record["StoneType"]]
    best = None
    best_score = 0
    for link in links:
        for target in target_names:
            s = max(
                difflib.SequenceMatcher(None, compact(target), compact(link["label"])).ratio(),
                1.0 if compact(target) and compact(target) in compact(link["label"]) else 0
            )
            if s > best_score:
                best_score = s
                best = link
    return best, best_score


def is_strict_slab_asset(value):
    """True only for image files whose basename ends in -Slab or _Slab."""
    try:
        path = urlparse(str(value)).path
    except Exception:
        path = str(value)
    name = Path(path).name
    return bool(re.search(r"(?:-|_)slab\.(?:png|jpe?g|webp)$", name, re.I))

def _strict_filename_variants(filename):
    """Generate likely strict -Slab filenames from an exported image filename.

    The product export sometimes gives us SlabImage or another product image while
    the same website folder contains the real slab asset. Probe conservative filename
    transforms instead of treating the export filename as the only possibility.
    """
    filename = str(filename or "").strip()
    if not filename:
        return []

    p = Path(filename)
    ext = p.suffix.lower() if p.suffix else ".png"
    stem = p.stem
    candidates = []

    def add(stem_value, extension=ext):
        stem_value = str(stem_value or "").strip()
        if not stem_value:
            return
        # Canonicalize the terminal suffix to exactly -Slab.
        stem_value = re.sub(r"(?i)(?:-|_)slabs?$", "-Slab", stem_value)
        value = stem_value + extension
        if is_strict_slab_asset(value) and value not in candidates:
            candidates.append(value)

    if is_strict_slab_asset(filename):
        candidates.append(filename)

    # Common export convention: ..._SlabImage.png -> ...-Slab.png / ..._Slab.png
    if re.search(r"(?i)slabimage$", stem):
        base = re.sub(r"(?i)(?:-|_)?slabimage$", "", stem).rstrip("-_")
        add(base + "-Slab")
        add(base + "_Slab")

    # Plural slab filenames are occasionally used in exports.
    if re.search(r"(?i)(?:-|_)slabs$", stem):
        base = re.sub(r"(?i)(?:-|_)slabs$", "", stem).rstrip("-_")
        add(base + "-Slab")
        add(base + "_Slab")

    # Some exported product images omit the slab suffix entirely.
    if not re.search(r"(?i)(?:-|_)slab(?:image)?$", stem):
        add(stem.rstrip("-_") + "-Slab")
        add(stem.rstrip("-_") + "_Slab")

    # Try the same strict basename as JPG too; some site folders mix PNG/JPG.
    current = list(candidates)
    for value in current:
        vp = Path(value)
        if vp.suffix.lower() != ".jpg":
            alt = vp.with_suffix(".jpg").name
            if alt not in candidates:
                candidates.append(alt)

    return candidates[:8]


def strict_bundled_references(entry):
    """Spreadsheet references that are already strict -Slab assets."""
    return [
        r for r in entry.get("references", [])
        if is_strict_slab_asset(r.get("filename",""))
    ]


def strict_reference_probe_rows(entry):
    """Reference rows to probe, including derived -Slab filename candidates."""
    rows = []
    seen = set()
    refs = entry.get("references", [])

    # Exact strict references first.
    for ref in refs:
        fn = str(ref.get("filename", ""))
        if is_strict_slab_asset(fn) and fn not in seen:
            rows.append({**ref, "filename": fn, "derived": False})
            seen.add(fn)

    # Then conservative derived filenames from the export rows.
    for ref in refs:
        for fn in _strict_filename_variants(ref.get("filename", "")):
            if fn in seen:
                continue
            rows.append({**ref, "filename": fn, "derived": True, "derived_from": ref.get("filename", "")})
            seen.add(fn)
            if len(rows) >= 12:
                return rows
    return rows

def strict_reference_stats():
    materials = load_bundled_reference_catalog().get("materials", {})
    with_refs = sum(1 for e in materials.values() if strict_reference_probe_rows(e))
    ref_count = sum(len(strict_reference_probe_rows(e)) for e in materials.values())
    return len(materials), with_refs, ref_count

def extract_page_images(page_url, page_html, material_name):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    material_tokens = set(norm(material_name).split())

    # OG/social image often points to a primary material image.
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in {"og:image", "twitter:image"} and meta.get("content"):
            candidates.append((3.0, urljoin(page_url, meta["content"]), "social"))

    for img in soup.find_all("img"):
        raw_urls = []
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            if img.get(attr):
                raw_urls.append(img.get(attr))
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset:
            for part in srcset.split(","):
                raw_urls.append(part.strip().split(" ")[0])

        alt = " ".join([
            img.get("alt") or "",
            img.get("title") or "",
        ])
        alt_norm = norm(alt)
        alt_tokens = set(alt_norm.split())

        for raw in raw_urls:
            if not raw or raw.startswith("data:"):
                continue
            url = urljoin(page_url, raw)
            ul = url.lower()
            if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", ul):
                continue
            if any(x in ul for x in ["logo", "icon", "loading", "facebook", "instagram", "youtube", "sprite"]):
                continue

            filename_norm = norm(Path(urlparse(url).path).stem)
            f_tokens = set(filename_norm.split())
            overlap = len((alt_tokens | f_tokens) & material_tokens)
            score = overlap * 2.0
            if compact(material_name) in compact(alt + " " + filename_norm):
                score += 5
            if "slab" in alt_norm or "slab" in filename_norm:
                score += 1.5
            candidates.append((score, url, alt))

    # De-dupe and prefer strong material-specific images.
    dedup = {}
    for score, url, alt in candidates:
        dedup[url] = max(dedup.get(url, (-999, "")), (score, alt), key=lambda x: x[0])
    ranked = sorted([(v[0], u, v[1]) for u, v in dedup.items()], reverse=True)

    # Never use room scenes, diagrams, Default tiles, SlabImage placeholders, etc.
    # Reference images shown to users and used by the visual matcher must end in -Slab/_Slab.
    ranked = [row for row in ranked if is_strict_slab_asset(row[1])]
    return [{"url": u, "score": s, "alt": a} for s, u, a in ranked[:8]]

def extract_skus(page_html):
    text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
    # Full slab SKUs commonly end in thickness and can contain finish after whitespace.
    return list(dict.fromkeys(re.findall(r"\b[A-Z]{2,}[A-Z0-9_-]*?(?:2CM|3CM|1CM|12MM|20MM|30MM)(?:\s+[A-Z-]+)?\b", text)))

def download_reference_image(url):
    headers = {"User-Agent": "Mozilla/5.0 GENROSE-room-scene-reference-builder/1.0"}
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if not ctype.startswith("image/"):
        raise RuntimeError(f"Not an image: {ctype}")
    return r.content, ctype

def ensure_product_set():
    pc = product_client()
    project = secret("GOOGLE_CLOUD_PROJECT", "")
    location = secret("GOOGLE_CLOUD_LOCATION", "us-east1")
    set_id = secret("GOOGLE_CLOUD_PRODUCT_SET_ID", "genrose-slabs")
    path = pc.product_set_path(project=project, location=location, product_set=set_id)
    try:
        pc.get_product_set(name=path)
    except Exception:
        parent = pc.location_path(project=project, location=location)
        try:
            pc.create_product_set(
                parent=parent,
                product_set=vision.ProductSet(display_name="GENROSE Natural Stone Slabs"),
                product_set_id=set_id
            )
        except AlreadyExists:
            pass
    return path

def ensure_product(record):
    pc = product_client()
    project = secret("GOOGLE_CLOUD_PROJECT", "")
    location = secret("GOOGLE_CLOUD_LOCATION", "us-east1")
    product_id = safe_id(record["SKU"])
    path = pc.product_path(project=project, location=location, product=product_id)
    try:
        pc.get_product(name=path)
    except Exception:
        parent = pc.location_path(project=project, location=location)
        product = vision.Product(
            display_name=str(record["StoneType"]),
            description=str(record["Source Color Name"]),
            product_category="general-v1",
            product_labels=[
                vision.Product.KeyValue(key="sku", value=str(record["SKU"])),
                vision.Product.KeyValue(key="stone", value=str(record["StoneType"])),
                vision.Product.KeyValue(
                    key="material_type",
                    value=str(bundled_reference_entry(record["SKU"]).get("material_type",""))
                )
            ]
        )
        try:
            pc.create_product(parent=parent, product=product, product_id=product_id)
        except AlreadyExists:
            pass

    set_path = ensure_product_set()
    try:
        pc.add_product_to_product_set(name=set_path, product=path)
    except Exception:
        pass
    return path


def cleanup_legacy_product_references(record):
    """Delete only the old `website-*` Product Search references.

    Strict references are staged first. If strict creation fails, the legacy Product
    Search references remain untouched so a refresh can never blank the cloud catalog.
    """
    if not product_search_ready():
        return 0
    pc = product_client()
    product_path = ensure_product(record)
    removed = 0
    try:
        refs = list(pc.list_reference_images(parent=product_path))
        for ref in refs:
            ref_id = str(ref.name).rsplit('/', 1)[-1]
            if not ref_id.startswith('website-'):
                continue
            try:
                pc.delete_reference_image(name=ref.name)
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed

def create_reference_from_bytes(record, image_bytes, ctype, slot, source_url="", ref_prefix="website"):
    if not (storage_ready() and product_search_ready()):
        raise RuntimeError("Google Storage + Product Search must be configured.")

    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    bucket_name = secret("GOOGLE_CLOUD_BUCKET", "")
    obj = f"website_references/{record['SKU']}/ref-{slot:02d}{ext}"
    blob = storage_client().bucket(bucket_name).blob(obj)
    blob.upload_from_string(image_bytes, content_type=ctype)

    product_path = ensure_product(record)
    ref_id = f"{ref_prefix}-{slot:02d}"
    ref = vision.ReferenceImage(uri=f"gs://{bucket_name}/{obj}")
    try:
        product_client().create_reference_image(
            parent=product_path,
            reference_image=ref,
            reference_image_id=ref_id
        )
    except AlreadyExists:
        pass
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
    return obj

def create_reference_for_url(record, image_url, slot):
    image_bytes, ctype = download_reference_image(image_url)
    return create_reference_from_bytes(record, image_bytes, ctype, slot, image_url)


def sync_one_website_record(record, links, build_visual):
    """Resolve strict -Slab references without destroying the existing library.

    The product export is used as a filename seed. If it gives `SlabImage` or a
    non-slab image, likely `-Slab` variants are probed in the same GENROSE folder.
    Product Search strict refs are created before legacy refs are removed.
    """
    bundled = bundled_reference_entry(record["SKU"])
    resolved_assets = []
    errors = []
    website_skus = []
    link = None
    link_score = 0.0

    # 1) Probe exact and derived strict -Slab filenames from the product export.
    for ref_row in strict_reference_probe_rows(bundled):
        if len(resolved_assets) >= MAX_GOOGLE_REFERENCES_PER_MATERIAL:
            break
        try:
            image_bytes, ctype, resolved_url = download_reference_candidates(bundled, ref_row)
            if not is_strict_slab_asset(resolved_url):
                continue
            resolved_assets.append({
                "bytes": image_bytes,
                "ctype": ctype,
                "url": resolved_url,
                "filename": Path(urlparse(resolved_url).path).name,
                "derived": bool(ref_row.get("derived")),
                "derived_from": ref_row.get("derived_from", "")
            })
        except Exception as e:
            errors.append(f"{ref_row.get('filename','')}: {str(e)[:120]}")

    # 2) Page scrape fallback. extract_page_images itself is strict.
    if not resolved_assets and links:
        link, link_score = best_collection_link(record, links)
        if link and link_score >= .62:
            try:
                page_html = fetch_html(link["url"])
                website_skus = extract_skus(page_html)
                images = extract_page_images(link["url"], page_html, record["Source Color Name"])
                for img in images:
                    if len(resolved_assets) >= MAX_GOOGLE_REFERENCES_PER_MATERIAL:
                        break
                    try:
                        image_bytes, ctype = download_reference_image(img["url"])
                        if not is_strict_slab_asset(img["url"]):
                            continue
                        resolved_assets.append({
                            "bytes": image_bytes,
                            "ctype": ctype,
                            "url": img["url"],
                            "filename": Path(urlparse(img["url"]).path).name,
                            "derived": False,
                            "derived_from": ""
                        })
                    except Exception as e:
                        errors.append(f"page:{str(e)[:120]}")
            except Exception as e:
                errors.append(f"page scrape:{str(e)[:120]}")

    resolved_urls = [x["url"] for x in resolved_assets]
    visual_signatures = []
    refs = []

    for asset in resolved_assets:
        sig = _signature_vector(asset["bytes"])
        if sig:
            visual_signatures.append(sig)

    # Stage strict Product Search refs first. Never clear the old catalog up front.
    legacy_removed = 0
    strict_refs_created = 0
    if build_visual and resolved_assets:
        for slot, asset in enumerate(resolved_assets, start=1):
            try:
                refs.append(
                    create_reference_from_bytes(
                        record,
                        asset["bytes"],
                        asset["ctype"],
                        slot,
                        asset["url"],
                        ref_prefix="strict"
                    )
                )
                strict_refs_created += 1
            except Exception as e:
                errors.append(f"Product Search strict ref {slot}: {str(e)[:120]}")

        # Only after at least one strict cloud reference exists do we remove legacy refs.
        if strict_refs_created:
            legacy_removed = cleanup_legacy_product_references(record)

    page_url = bundled.get("page_url","") if bundled else ""
    if not page_url and link:
        page_url = link["url"]

    status = "OK" if resolved_assets else "NO_STRICT_SLAB_REFERENCE"
    return record["SKU"], {
        "status": status,
        "stone": record["StoneType"],
        "sku": record["SKU"],
        "material_type": bundled.get("material_type","") if bundled else "",
        "page_url": page_url,
        "page_label": bundled.get("product_line","") if bundled else (link["label"] if link else ""),
        "page_match_score": 1.0 if bundled else link_score,
        "image_urls": resolved_urls,
        "strict_slab_filenames": [x["filename"] for x in resolved_assets],
        "reference_objects": refs,
        "website_skus": website_skus,
        "reference_source": "STRICT -Slab website asset",
        "reference_policy": "strict-slab",
        "visual_signatures": visual_signatures,
        "strict_product_refs_created": strict_refs_created,
        "legacy_product_refs_removed": legacy_removed,
        "derived_reference_count": sum(1 for x in resolved_assets if x.get("derived")),
        "errors": errors[:8],
    }

def sync_genrose_website(build_visual=True, max_workers=6):
    """Refresh references transactionally.

    Existing successful cache entries are NEVER replaced by an error/empty result.
    New strict successes are merged in as they complete and checkpointed periodically,
    so a timeout/restart cannot erase the previous library.
    """
    links = []
    try:
        index_html = fetch_html(GENROSE_INDEX)
        links = extract_collection_links(index_html)
    except Exception:
        links = []

    previous = load_website_cache()
    cache = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    cache.setdefault("materials", {})
    cache["refresh_started_at"] = datetime.now(timezone.utc).isoformat()
    cache["source"] = GENROSE_INDEX
    cache["refresh_policy"] = "strict-slab-transactional"

    progress = st.progress(0, text="Preparing strict slab refresh…")
    status = st.empty()
    metrics = st.empty()
    done = 0
    strict_ok_this_run = 0
    retained_previous = 0
    no_ref = 0
    errors = 0

    def strict_ready_count():
        return sum(
            1 for x in cache.get("materials", {}).values()
            if x.get("status") == "OK" and x.get("reference_policy") == "strict-slab" and x.get("visual_signatures")
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(sync_one_website_record, r, links, build_visual): r
            for r in catalog_records
        }
        for future in as_completed(futures):
            r = futures[future]
            sku = str(r["SKU"])
            old_entry = cache["materials"].get(sku, {})
            try:
                _sku, fresh = future.result()
            except Exception as e:
                fresh = {
                    "status": "ERROR", "stone": r["StoneType"], "sku": sku,
                    "error": str(e)[:300], "errors": [str(e)[:300]]
                }

            if fresh.get("status") == "OK" and fresh.get("visual_signatures"):
                cache["materials"][sku] = fresh
                strict_ok_this_run += 1
                latest_status = "STRICT SLAB READY"
            else:
                # Preserve any previous usable entry. Do not blank a material because
                # the refresh could not improve it this time.
                if old_entry:
                    kept = copy.deepcopy(old_entry)
                    kept["last_refresh_status"] = fresh.get("status", "ERROR")
                    kept["last_refresh_errors"] = fresh.get("errors", []) or ([fresh.get("error")] if fresh.get("error") else [])
                    kept["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
                    cache["materials"][sku] = kept
                    retained_previous += 1
                    latest_status = f"{fresh.get('status','ERROR')} · previous kept"
                else:
                    cache["materials"][sku] = fresh
                    if fresh.get("status") == "NO_STRICT_SLAB_REFERENCE":
                        no_ref += 1
                    else:
                        errors += 1
                    latest_status = fresh.get("status", "ERROR")

            done += 1
            ready_now = strict_ready_count()
            progress.progress(done / len(catalog_records), text=f"Scanned {done}/{len(catalog_records)} materials")
            status.caption(f"Latest: {r['StoneType']} — {latest_status}")
            metrics.markdown(
                f"**Strict ready:** {ready_now} &nbsp;&nbsp; "
                f"**New/updated this run:** {strict_ok_this_run} &nbsp;&nbsp; "
                f"**Previous preserved:** {retained_previous} &nbsp;&nbsp; "
                f"**No strict slab:** {no_ref} &nbsp;&nbsp; **Errors:** {errors}"
            )

            # Checkpoint successful merged state. A killed Streamlit run still keeps progress.
            if done % 20 == 0:
                cache["refresh_checkpoint_at"] = datetime.now(timezone.utc).isoformat()
                save_website_cache(cache)

    cache["synced_at"] = datetime.now(timezone.utc).isoformat()
    cache["refresh_finished_at"] = cache["synced_at"]
    cache["refresh_summary"] = {
        "scanned": done,
        "strict_updated": strict_ok_this_run,
        "previous_preserved": retained_previous,
        "no_strict_slab": no_ref,
        "errors": errors,
        "strict_ready_total": strict_ready_count(),
    }
    save_website_cache(cache)
    progress.empty()
    status.empty()
    metrics.empty()
    return cache

def website_entry_for_sku(sku):
    return load_website_cache().get("materials", {}).get(str(sku), {})

def website_reference_bytes(sku):
    """Return only a strict -Slab/_Slab reference image."""
    entry = website_entry_for_sku(sku)

    # Old cache entries are intentionally ignored unless the source URL itself
    # proves it is a strict slab image.
    urls = entry.get("image_urls", [])
    refs = [x for x in entry.get("reference_objects", []) if x and not str(x).startswith("ERROR:")]

    for i, url in enumerate(urls):
        if not is_strict_slab_asset(url):
            continue
        if i < len(refs) and storage_ready():
            try:
                return storage_client().bucket(
                    secret("GOOGLE_CLOUD_BUCKET", "")
                ).blob(refs[i]).download_as_bytes()
            except Exception:
                pass
        try:
            return download_reference_image(url)[0]
        except Exception:
            pass

    # Bundled direct fallback, strict only.
    bundled = bundled_reference_entry(sku)
    for ref in strict_reference_probe_rows(bundled):
        try:
            b, _ctype, url = download_reference_candidates(bundled, ref)
            if is_strict_slab_asset(url):
                return b
        except Exception:
            continue
    return None


# ---------------- manual material thumbnails ----------------

def material_thumbnail_object_path(sku):
    return f"material_thumbnails/{safe_id(str(sku or '').upper())}/thumbnail.jpg"

def local_material_thumbnail_path(sku):
    return LOCAL_MATERIAL_THUMBNAIL_ROOT / f"{safe_id(str(sku or '').upper())}.jpg"

def normalize_material_thumbnail(image_bytes):
    """Normalize a user-supplied slab thumbnail to a compact, durable JPEG."""
    im = Image.open(io.BytesIO(image_bytes))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()

def manual_material_thumbnail_bytes(sku):
    sku = str(sku or "").strip()
    if not sku:
        return None
    if storage_ready():
        try:
            blob = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
                material_thumbnail_object_path(sku)
            )
            if blob.exists():
                return blob.download_as_bytes()
        except Exception:
            pass
    local_path = local_material_thumbnail_path(sku)
    try:
        return local_path.read_bytes() if local_path.exists() else None
    except Exception:
        return None

def save_manual_material_thumbnail(sku, image_bytes):
    """Persist a manually supplied material thumbnail by SKU.

    Manual thumbnails are display references only. They are intentionally NOT added
    to the visual-matching signature library, so a quick human reference image cannot
    accidentally change analyzer scoring.
    """
    sku = str(sku or "").strip()
    if not sku:
        raise ValueError("Select a material with a SKU before adding a thumbnail.")
    normalized = normalize_material_thumbnail(image_bytes)
    local_material_thumbnail_path(sku).write_bytes(normalized)
    if storage_ready():
        storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
            material_thumbnail_object_path(sku)
        ).upload_from_string(normalized, content_type="image/jpeg")
    return normalized

def material_thumbnail_bytes(sku):
    """Human-facing thumbnail: manual override first, strict GENROSE slab second."""
    return manual_material_thumbnail_bytes(sku) or website_reference_bytes(sku)


# ---------------- immediate local slab-reference similarity ----------------

def _signature_vector(image_bytes):
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((256, 256))
        hsv = np.asarray(im.convert("HSV"), dtype=np.uint8)
        gray_im = im.convert("L")
        gray = np.asarray(gray_im, dtype=np.uint8)
        edges = np.asarray(gray_im.filter(ImageFilter.FIND_EDGES), dtype=np.uint8)

        parts = []
        for channel, bins in ((0,18), (1,8), (2,8)):
            h, _ = np.histogram(hsv[:, :, channel], bins=bins, range=(0,256))
            h = h.astype(np.float32)
            h /= max(float(h.sum()), 1.0)
            parts.append(h)

        gh, _ = np.histogram(gray, bins=16, range=(0,256))
        gh = gh.astype(np.float32)
        gh /= max(float(gh.sum()), 1.0)
        parts.append(gh)

        eh, _ = np.histogram(edges, bins=16, range=(0,256))
        eh = eh.astype(np.float32)
        eh /= max(float(eh.sum()), 1.0)
        parts.append(eh)

        edge_flat = edges.reshape(-1).astype(np.float32)
        texture = np.array([
            edge_flat.mean()/255.0,
            edge_flat.std()/255.0,
            np.percentile(edge_flat, 75)/255.0,
            np.percentile(edge_flat, 90)/255.0,
        ], dtype=np.float32)
        parts.append(texture)

        vec = np.concatenate(parts).astype(np.float32)
        n = float(np.linalg.norm(vec))
        if n <= 1e-8:
            return []
        return (vec / n).tolist()
    except Exception:
        return []

def _query_region_bytes(image_bytes):
    regions = [("full", image_bytes)]
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = im.size
        boxes = [
            ("center", (int(w*.12), int(h*.08), int(w*.88), int(h*.90))),
            ("upper", (0, 0, w, int(h*.68))),
            ("lower", (0, int(h*.32), w, h)),
            ("left", (0, int(h*.08), int(w*.68), int(h*.92))),
            ("right", (int(w*.32), int(h*.08), w, int(h*.92))),
        ]
        for name, box in boxes:
            if box[2] <= box[0] + 80 or box[3] <= box[1] + 80:
                continue
            crop = im.crop(box)
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=88)
            regions.append((name, buf.getvalue()))
    except Exception:
        pass
    return regions

def _cosine_from_lists(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    av = np.asarray(a, dtype=np.float32)
    bv = np.asarray(b, dtype=np.float32)
    return float(np.clip(np.dot(av, bv), 0.0, 1.0))

def _calibrate_reference_similarity(raw):
    return float(np.clip((raw - .68) / .28, 0.0, 1.0))

def local_reference_search(image_bytes, limit=10):
    cache = load_website_cache()
    materials = cache.get("materials", {})
    if not materials:
        return []

    qvectors = []
    for region, b in _query_region_bytes(image_bytes):
        sig = _signature_vector(b)
        if sig:
            qvectors.append((region, sig))
    if not qvectors:
        return []

    ranked = []
    for sku, entry in materials.items():
        if entry.get("reference_policy") != "strict-slab":
            continue
        sigs = entry.get("visual_signatures", [])
        if not sigs:
            continue
        best_raw = 0.0
        best_region = "full"
        for qregion, qsig in qvectors:
            for rsig in sigs:
                raw = _cosine_from_lists(qsig, rsig)
                if raw > best_raw:
                    best_raw = raw
                    best_region = qregion

        score = _calibrate_reference_similarity(best_raw)
        if score <= 0:
            continue
        rec = row_for_sku(sku)
        if not rec:
            continue
        ranked.append({
            "stone": rec["StoneType"],
            "sku": rec["SKU"],
            "source_name": rec["Source Color Name"],
            "score": score,
            "raw_similarity": best_raw,
            "query_region": best_region,
            "source": "Immediate GENROSE reference similarity",
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:limit]

@st.cache_data(ttl=3600, show_spinner=False)
def candidate_swatch_bytes(sku):
    return material_thumbnail_bytes(sku)


# ---------------- analysis hierarchy ----------------

def candidate_by_stone(candidates, stone):
    target = compact(stone)
    for c in candidates:
        if compact(c["stone"]) == target:
            return c
    return None

def combine_material_evidence(filename_candidates, web_candidates, product_results, local_results, website_cache):
    """Conservative material ranking.

    Rules:
    - Strong filename match wins.
    - Medium filename match can be strengthened by website verification and/or Product Search.
    - Product Search may provide a fallback when filename evidence is weak.
    - Generic Google Vision labels/web entities/OCR may only support an existing candidate.
      They can NEVER create a material identification by themselves.
    - If evidence is not good enough, return Needs Review instead of inventing a slab.
    """
    union = {}

    for c in filename_candidates:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0, "local_visual": 0.0
        })
        union[c["sku"]]["filename"] = max(union[c["sku"]]["filename"], float(c["score"]))

    # Vision text evidence may only attach to SKUs that were already proposed by
    # filename or visual Product Search. Do not let generic Vision create a new SKU.
    web_by_sku = {c["sku"]: c for c in web_candidates}

    for c in product_results:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0, "local_visual": 0.0
        })
        union[c["sku"]]["visual"] = max(union[c["sku"]]["visual"], float(c["score"]))

    for c in local_results:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0, "local_visual": 0.0
        })
        union[c["sku"]]["local_visual"] = max(union[c["sku"]]["local_visual"], float(c["score"]))

    for sku, x in union.items():
        if sku in web_by_sku:
            x["vision_text"] = float(web_by_sku[sku]["score"])

    results = []
    website_materials = website_cache.get("materials", {})

    for sku, x in union.items():
        f = x["filename"]
        vt = x["vision_text"]
        vis = x["visual"]
        local_vis = x.get("local_visual", 0.0)
        best_visual = max(vis, local_vis)
        web_verified = website_materials.get(str(sku), {}).get("status") == "OK"

        # Strong filename: authoritative.
        if f >= .90:
            final = min(.995, .92 * f + (.05 if web_verified else 0) + .03 * min(vt, .9))
            method = "Filename"
            if web_verified:
                method += " + GENROSE reference"
            if best_visual >= .55:
                final = min(.995, final + .02)
                method += " + visual confirmation"

        elif f >= .70:
            if best_visual >= .55:
                final = min(.95, .62 * f + .28 * best_visual + (.07 if web_verified else 0) + .03 * min(vt, .9))
                method = "Filename + GENROSE visual reference"
            elif web_verified:
                final = min(.89, .86 * f + .08 + .03 * min(vt, .9))
                method = "Filename + GENROSE reference"
            else:
                final = min(.81, f)
                method = "Filename · review recommended"

        else:
            if best_visual >= .86:
                final = min(.91, .88 * best_visual + .05 * f + (.04 if web_verified else 0))
                method = "GENROSE visual reference fallback"
            elif best_visual >= .62:
                final = min(.79, .78 * best_visual + .08 * f + (.03 if web_verified else 0))
                method = "GENROSE visual candidate · review required"
            else:
                final = min(.54, max(f, best_visual * .65))
                method = "Insufficient material evidence"

        results.append({
            **x,
            "website_verified": web_verified,
            "confidence": max(0.0, min(.995, final)),
            "method": method
        })

    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results[:8]

def analyze_image(filename, image_bytes):
    filename_room = detect_room_from_text(filename)
    filename_materials = filename_material_candidates(filename, 8)

    # Cloud Vision is intentionally called for every analyzed image.
    vision_data = run_google_vision(image_bytes)
    vision_room = classify_room_from_vision(vision_data)
    vision_materials = vision_text_material_candidates(vision_data, 8)

    top_filename = filename_materials[0]["score"] if filename_materials else 0

    # Immediate comparison against references created by the sync.
    local_results = local_reference_search(image_bytes, 10) if top_filename < .93 else []

    # Google Product Search remains an additional cloud visual signal.
    product_results = []
    if top_filename < .90 and product_search_ready():
        try:
            product_results = run_product_search(image_bytes, 10, multi_crop=(top_filename < .55))
        except Exception as e:
            vision_data["product_search_error"] = str(e)

    website_cache = load_website_cache()
    material_ranked = combine_material_evidence(
        filename_materials, vision_materials, product_results, local_results, website_cache
    )
    best = material_ranked[0] if material_ranked else {
        "stone": "", "sku": "", "confidence": 0, "method": "Unmatched",
        "filename": 0, "vision_text": 0, "visual": 0, "website_verified": False
    }

    # Never invent a material when the evidence is weak.
    # Below 60% the final result is explicitly Needs Review / UnknownMaterial,
    # while candidates remain visible for a human to choose from.
    if best.get("confidence", 0) < .60:
        best = {
            **best,
            "stone": "",
            "sku": "",
            "method": "Needs Review · insufficient material evidence"
        }

    # Room hierarchy: explicit filename room terms win. If the filename has
    # no useful room term, use a weighted Google Vision scene classifier.
    if filename_room["room"] != "Other":
        room = filename_room["room"]
        room_conf = filename_room["score"]
        room_method = f'Filename / translated term: "{filename_room["matched_term"]}"'
        room_candidates = [{"room": room, "weight": 10.0, "reasons": ["filename term"]}]
    elif vision_room["room"] != "Other":
        room = vision_room["room"]
        room_conf = vision_room["score"]
        room_method = "Google Vision scene classifier · " + vision_room.get("reason", "scene cues")
        room_candidates = vision_room.get("candidates", [])
    else:
        room = "Other"
        room_conf = .22
        room_method = "No confident room clue"
        room_candidates = []

    return {
        "stone": best["stone"],
        "sku": best["sku"],
        "material_confidence": int(round(best["confidence"] * 100)),
        "material_method": best["method"],
        "filename_material_score": int(round(best["filename"] * 100)),
        "vision_text_score": int(round(best["vision_text"] * 100)),
        "visual_score": int(round(best["visual"] * 100)),
        "local_visual_score": int(round(best.get("local_visual", 0) * 100)),
        "website_verified": bool(best["website_verified"]),
        "room": room,
        "room_confidence": int(round(room_conf * 100)),
        "room_method": room_method,
        "room_candidates": room_candidates,
        "material_candidates": material_ranked,
        "vision": vision_data,
    }

# ---------------- review persistence ----------------

def app_base_url():
    """Return the URL the current user is actually using.

    st.context.url is preferable to a manually configured APP_BASE_URL because
    Streamlit subdomains can change. It excludes query parameters, which is
    exactly what the review-link builder needs.
    """
    try:
        current = str(st.context.url or "").strip().rstrip("/")
        if current.startswith("http://") or current.startswith("https://"):
            return current
    except Exception:
        pass

    configured = str(secret("APP_BASE_URL", "") or "").strip().rstrip("/")
    if configured.startswith("http://") or configured.startswith("https://"):
        return configured

    return "http://localhost:8501"

def save_review_batch(items, analyst_batch_note=""):
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    payload = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "analyst_batch_note": str(analyst_batch_note or "").strip(),
        "items": []
    }

    if storage_ready():
        bucket = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", ""))
        for i, item in enumerate(items):
            ext = item["ext"]
            obj = f"review_batches/{batch_id}/images/{i:03d}{ext}"
            bucket.blob(obj).upload_from_string(
                item["bytes"],
                content_type="image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
            )
            payload["items"].append({
                "id": f"item-{i:03d}",
                "image_object": obj,
                "old_name": item["name"],
                "new_name": item["new_name"],
                "stone": item["stone"],
                "sku": item["sku"],
                "room": item["room"],
                "decision_status": item.get("decision_status",""),
                "material_confidence": item["analysis"]["material_confidence"],
                "room_confidence": item["analysis"]["room_confidence"],
                "material_method": item["analysis"]["material_method"],
                "room_method": item["analysis"]["room_method"],
                "material_candidates": [
                    {
                        "stone": c.get("stone",""),
                        "sku": c.get("sku",""),
                        "confidence": int(round(float(c.get("confidence",0))*100))
                    }
                    for c in item["analysis"].get("material_candidates", [])[:6]
                ],
                "room_candidates": [
                    {
                        "room": c.get("room",""),
                        "weight": c.get("weight",0),
                        "reasons": c.get("reasons",[])
                    }
                    for c in item["analysis"].get("room_candidates", [])[:4]
                ],
                "website_url": website_entry_for_sku(item["sku"]).get("page_url", ""),
                "analyst_note": str(item.get("analyst_note", "") or "")
            })
        bucket.blob(f"review_batches/{batch_id}/review.json").upload_from_string(
            json.dumps(payload, indent=2), content_type="application/json"
        )
    else:
        folder = LOCAL_REVIEW_ROOT / batch_id
        folder.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(items):
            local_name = f"{i:03d}{item['ext']}"
            (folder / local_name).write_bytes(item["bytes"])
            payload["items"].append({
                "id": f"item-{i:03d}",
                "image_local": local_name,
                "old_name": item["name"],
                "new_name": item["new_name"],
                "stone": item["stone"],
                "sku": item["sku"],
                "room": item["room"],
                "decision_status": item.get("decision_status",""),
                "material_confidence": item["analysis"]["material_confidence"],
                "room_confidence": item["analysis"]["room_confidence"],
                "material_method": item["analysis"]["material_method"],
                "room_method": item["analysis"]["room_method"],
                "material_candidates": [
                    {
                        "stone": c.get("stone",""),
                        "sku": c.get("sku",""),
                        "confidence": int(round(float(c.get("confidence",0))*100))
                    }
                    for c in item["analysis"].get("material_candidates", [])[:6]
                ],
                "room_candidates": [
                    {
                        "room": c.get("room",""),
                        "weight": c.get("weight",0),
                        "reasons": c.get("reasons",[])
                    }
                    for c in item["analysis"].get("room_candidates", [])[:4]
                ],
                "website_url": website_entry_for_sku(item["sku"]).get("page_url", ""),
                "analyst_note": str(item.get("analyst_note", "") or "")
            })
        (folder / "review.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return batch_id, f"{app_base_url()}/?review={batch_id}"

def load_review_batch(batch_id):
    if storage_ready():
        try:
            blob = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
                f"review_batches/{batch_id}/review.json"
            )
            return json.loads(blob.download_as_text())
        except Exception:
            return None
    p = LOCAL_REVIEW_ROOT / batch_id / "review.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

def review_image_bytes(batch_id, item):
    if item.get("image_object") and storage_ready():
        return storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(item["image_object"]).download_as_bytes()
    p = LOCAL_REVIEW_ROOT / batch_id / item.get("image_local", "")
    return p.read_bytes() if p.exists() else None

def save_submission(batch_id, submission):
    submission["submitted_at"] = datetime.now(timezone.utc).isoformat()
    if storage_ready():
        storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
            f"review_batches/{batch_id}/submission.json"
        ).upload_from_string(json.dumps(submission, indent=2), content_type="application/json")
    else:
        folder = LOCAL_REVIEW_ROOT / batch_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "submission.json").write_text(
            json.dumps(submission, indent=2), encoding="utf-8"
        )


def load_submission(batch_id):
    """Load a completed reviewer submission when one exists."""
    if storage_ready():
        try:
            blob = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
                f"review_batches/{batch_id}/submission.json"
            )
            if not blob.exists():
                return None
            return json.loads(blob.download_as_text())
        except Exception:
            return None
    p = LOCAL_REVIEW_ROOT / batch_id / "submission.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:
        return None


def save_review_draft(batch_id, draft):
    """Persist in-progress review choices so browser refreshes do not wipe the batch."""
    payload = copy.deepcopy(draft)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if storage_ready():
        storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
            f"review_batches/{batch_id}/draft.json"
        ).upload_from_string(json.dumps(payload, indent=2), content_type="application/json")
    else:
        folder = LOCAL_REVIEW_ROOT / batch_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "draft.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_review_draft(batch_id):
    if storage_ready():
        try:
            blob = storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(
                f"review_batches/{batch_id}/draft.json"
            )
            if not blob.exists():
                return None
            return json.loads(blob.download_as_text())
        except Exception:
            return None
    p = LOCAL_REVIEW_ROOT / batch_id / "draft.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:
        return None

# ---------------- email ----------------

def send_review_email(subject, html_body, text_body):
    # Option 1: Formspree endpoint configured to deliver to marketing@genrose.com.
    formspree = secret("FORMSPREE_ENDPOINT", "")
    if formspree:
        r = requests.post(
            formspree,
            data={
                "_subject": subject,
                "message": text_body,
                "html": html_body,
                "recipient": REVIEW_EMAIL
            },
            timeout=30,
            headers={"Accept": "application/json"}
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Formspree returned {r.status_code}: {r.text[:250]}")
        return

    # Option 2: Resend
    resend = secret("RESEND_API_KEY", "")
    sender = secret("EMAIL_FROM", "")
    if resend and sender:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend}", "Content-Type": "application/json"},
            json={
                "from": sender,
                "to": [REVIEW_EMAIL],
                "subject": subject,
                "html": html_body,
                "text": text_body
            },
            timeout=30
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Resend returned {r.status_code}: {r.text[:250]}")
        return

    # Option 3: SMTP
    host = secret("SMTP_HOST", "")
    user = secret("SMTP_USER", "")
    password = secret("SMTP_PASSWORD", "")
    port = int(secret("SMTP_PORT", 587) or 587)
    if host and user and password:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender or user
        msg["To"] = REVIEW_EMAIL
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(msg)
        return

    raise RuntimeError(
        "Email isn't configured. Add FORMSPREE_ENDPOINT, or RESEND_API_KEY + EMAIL_FROM, or SMTP secrets."
    )


GENROSE_STYLE = r"""
<style>
:root{
  --gr-ink:#242424;
  --gr-charcoal:#303033;
  --gr-soft-charcoal:#454549;
  --gr-white:#ffffff;
  --gr-page:#f8f7f5;
  --gr-blush:#eee4e1;
  --gr-blush-2:#f4eeeb;
  --gr-line:#ded8d4;
  --gr-line-dark:#cfc7c2;
  --gr-taupe:#7d6b63;
  --gr-rose:#9b7c72;
  --gr-green:#687866;
  --gr-good:#e7efe7;
  --gr-warn:#f6eed7;
  --gr-bad:#f5e3e3;
  --gr-blue:#e5edf2;
}

html, body, [data-testid="stAppViewContainer"]{
  background:var(--gr-page)!important;
  color:var(--gr-ink)!important;
}
[data-testid="stAppViewContainer"]>.main{
  background:
    linear-gradient(180deg,#ffffff 0 250px,var(--gr-page) 250px 100%)!important;
}
[data-testid="stHeader"]{
  background:#fff!important;
  border-bottom:1px solid #eee9e6!important;
}
#MainMenu{visibility:hidden;}
footer{visibility:hidden;}

.block-container{
  max-width:1720px!important;
  padding:1.15rem 2.25rem 4rem!important;
}

/* sidebar */
[data-testid="stSidebar"]{
  background:#f4efec!important;
  border-right:1px solid var(--gr-line)!important;
}
[data-testid="stSidebar"] *{color:var(--gr-ink)!important;}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3{
  font-family:Arial,Helvetica,sans-serif!important;
}

/* master typography */
h1,h2,h3,h4,p,span,label,div{
  color:var(--gr-ink);
}
h1{
  font-family:Georgia,"Times New Roman",serif!important;
  font-weight:400!important;
  font-size:3.05rem!important;
  line-height:1.04!important;
  letter-spacing:-.035em!important;
  margin:.15rem 0 .55rem!important;
}
h2{
  font-family:Arial,Helvetica,sans-serif!important;
  font-size:1.28rem!important;
  font-weight:700!important;
  letter-spacing:.005em!important;
}
h3{
  font-family:Arial,Helvetica,sans-serif!important;
  font-size:1rem!important;
  font-weight:700!important;
}
p,[data-testid="stMarkdownContainer"] p{
  color:#535154!important;
}
small,[data-testid="stCaptionContainer"],.stCaption{
  color:#777176!important;
}
code{
  color:#2f3032!important;
  background:#f4efec!important;
}

/* top brand */
.gr-brandbar{
  display:grid;
  grid-template-columns:1fr auto 1fr;
  align-items:center;
  min-height:86px;
  background:#fff;
  border-bottom:1px solid #e6dfdc;
  margin:-1.15rem -2.25rem 0;
  padding:0 2.25rem;
}
.gr-brand-image{
  grid-column:2;
  text-align:center;
  display:flex;
  align-items:center;
  justify-content:center;
}
.gr-brand-logo{
  display:block;
  width:min(360px,42vw);
  height:auto;
  object-fit:contain;
}
.gr-brand-fallback{
  font-family:Georgia,"Times New Roman",serif;
  font-size:1.55rem;
  letter-spacing:.07em;
  color:#262626!important;
}
.gr-tooltag{
  grid-column:3;
  justify-self:end;
  font-size:.68rem;
  text-transform:uppercase;
  letter-spacing:.14em;
  color:#807572!important;
  font-weight:700;
}
.gr-kicker{
  color:#8c736a!important;
  font-size:.72rem;
  font-weight:700;
  letter-spacing:.18em;
  text-transform:uppercase;
  margin-bottom:.55rem;
}
.gr-lede{
  color:#5d595a!important;
  font-size:1.02rem;
  line-height:1.65;
  max-width:880px;
  margin-bottom:1.65rem;
}
.gr-sectionbar{
  background:var(--gr-charcoal);
  color:white!important;
  padding:10px 14px;
  font-size:.72rem;
  font-weight:700;
  letter-spacing:.16em;
  text-transform:uppercase;
  margin:1.3rem 0 .9rem;
}
.gr-rule{
  height:1px;
  background:var(--gr-line);
  margin:1.45rem 0;
}
.gr-note{
  background:var(--gr-blush-2);
  border-left:3px solid var(--gr-rose);
  padding:10px 12px;
  color:#625b59!important;
  font-size:.86rem;
  line-height:1.45;
}
.gr-result-title{
  font-family:Georgia,"Times New Roman",serif;
  font-size:1.8rem;
  line-height:1.05;
  color:#272727!important;
  margin:.25rem 0 .2rem;
}
.gr-meta{
  color:#756d6b!important;
  font-size:.85rem;
}
.gr-label{
  margin-top:1rem;
  margin-bottom:.4rem;
  text-transform:uppercase;
  letter-spacing:.12em;
  font-size:.66rem;
  font-weight:700;
  color:#7c706c!important;
}
.gr-filename{
  padding:11px 12px;
  background:#f7f3f1;
  border:1px solid var(--gr-line);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.91rem;
  color:#2e2e2e!important;
  word-break:break-word;
}
.gr-evidence{
  display:grid;
  grid-template-columns:1fr auto;
  gap:7px 12px;
  padding:8px 0;
  border-bottom:1px solid #ebe5e2;
}
.gr-evidence span{color:#696364!important;font-size:.88rem;}
.gr-evidence strong{color:#2c2c2c!important;font-size:.88rem;}
.gr-empty{
  min-height:130px;
  display:flex;
  align-items:center;
  justify-content:center;
  text-align:center;
  background:#faf8f7;
  border:1px dashed #ccc2bd;
  color:#817977!important;
  padding:14px;
}
.gr-chip{
  display:inline-flex;
  align-items:center;
  padding:5px 9px;
  margin:7px 5px 0 0;
  border-radius:999px;
  border:1px solid transparent;
  font-size:.72rem;
  line-height:1;
  font-weight:700;
}
.gr-chip.good{background:var(--gr-good);color:#476147;border-color:#cadbca}
.gr-chip.mid{background:var(--gr-warn);color:#77622c;border-color:#e7d8ad}
.gr-chip.bad{background:var(--gr-bad);color:#804b4b;border-color:#e3c4c4}
.gr-chip.info{background:var(--gr-blue);color:#496473;border-color:#cad9e1}

/* uploader */
[data-testid="stFileUploader"]{
  background:#fff!important;
  border:1px dashed #bdb2ad!important;
  border-radius:0!important;
  padding:.5rem!important;
  box-shadow:none!important;
}
[data-testid="stFileUploader"] section{
  background:#fff!important;
  border:0!important;
}
[data-testid="stFileUploaderFile"]{
  background:#f7f4f2!important;
  border:1px solid #e1d9d5!important;
  border-radius:0!important;
}
[data-testid="stFileUploaderFile"] *{color:#393637!important;}

/* metrics */
[data-testid="stMetric"]{
  background:#fff!important;
  border:1px solid var(--gr-line)!important;
  border-radius:0!important;
  padding:15px 17px!important;
  min-height:94px;
  box-shadow:none!important;
}
[data-testid="stMetricLabel"]{
  color:#716a68!important;
  font-weight:600!important;
}
[data-testid="stMetricValue"]{
  color:#272727!important;
  font-family:Georgia,"Times New Roman",serif!important;
  font-size:2rem!important;
  font-weight:400!important;
}

/* input fields */
[data-testid="stTextInput"] input,
[data-baseweb="select"]>div{
  background:#fff!important;
  color:#292929!important;
  border:1px solid #cfc6c2!important;
  border-radius:0!important;
  min-height:42px!important;
}
[data-testid="stTextInput"] input::placeholder{color:#928a87!important;}
[data-baseweb="popover"],[role="listbox"]{
  background:#fff!important;
  color:#292929!important;
}

/* buttons */
.stButton>button,
.stDownloadButton>button,
[data-testid="stLinkButton"] a{
  border-radius:0!important;
  min-height:43px!important;
  font-weight:700!important;
  letter-spacing:.04em!important;
  text-transform:uppercase;
  font-size:.73rem!important;
  transition:all .12s ease!important;
}
.stButton>button[kind="primary"]{
  background:#303033!important;
  color:#fff!important;
  border:1px solid #303033!important;
}
.stButton>button[kind="primary"] p{color:#fff!important;}
.stButton>button[kind="secondary"],
.stDownloadButton>button,
[data-testid="stLinkButton"] a{
  background:#fff!important;
  color:#343234!important;
  border:1px solid #bfb5b0!important;
}
.stButton>button[kind="secondary"] p,
.stDownloadButton>button p{color:#343234!important;}
.stButton>button:hover,
.stDownloadButton>button:hover{
  border-color:#7f706a!important;
}
.stButton>button[kind="primary"]:hover{
  background:#50494a!important;
}

/* cards / bordered containers */
[data-testid="stVerticalBlockBorderWrapper"]{
  background:#fff!important;
  border:1px solid var(--gr-line)!important;
  border-radius:0!important;
  box-shadow:none!important;
}
[data-testid="stVerticalBlockBorderWrapper"]>div{
  border-radius:0!important;
}

/* queue cards */
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="secondary"]{
  width:100%;
  justify-content:flex-start!important;
  text-align:left!important;
  background:#fff!important;
  border:1px solid #e2dcda!important;
  min-height:58px!important;
  padding:.58rem .72rem!important;
  text-transform:none!important;
  letter-spacing:0!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="secondary"] p{
  color:#454143!important;
  font-size:.82rem!important;
  line-height:1.3!important;
  white-space:normal!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="primary"]{
  width:100%;
  justify-content:flex-start!important;
  text-align:left!important;
  background:var(--gr-blush)!important;
  border:1px solid #c8b6af!important;
  min-height:58px!important;
  padding:.58rem .72rem!important;
  text-transform:none!important;
  letter-spacing:0!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="primary"] p{
  color:#342f2f!important;
  font-size:.82rem!important;
  line-height:1.3!important;
  white-space:normal!important;
}

/* expanders */
[data-testid="stExpander"]{
  background:#fff!important;
  border:1px solid var(--gr-line)!important;
  border-radius:0!important;
}
[data-testid="stExpander"] summary{
  color:#3c393a!important;
  font-weight:600!important;
}

/* tables / images */
[data-testid="stDataFrame"]{
  border:1px solid var(--gr-line)!important;
  border-radius:0!important;
  overflow:hidden!important;
}
[data-testid="stImage"] img{
  border-radius:0!important;
  border:1px solid #ded8d4!important;
  box-shadow:none!important;
}

/* status boxes */
[data-testid="stAlert"]{
  border-radius:0!important;
}

/* progress */
[data-testid="stProgress"]>div>div{background:#8c736a!important;}

@media(max-width:1100px){
  .block-container{padding:1rem!important;}
  .gr-brandbar{margin:-1rem -1rem 0;padding:0 1rem;}
  .gr-tooltag{display:none;}
  h1{font-size:2.25rem!important;}
}

.gr-reference-panel{
  background:#fff;border:1px solid var(--gr-line);
  display:grid;grid-template-columns:1.4fr repeat(3,.62fr);
  margin:1.1rem 0 .8rem;
}
.gr-ref-lead,.gr-ref-stat{padding:14px 16px;border-right:1px solid var(--gr-line)}
.gr-ref-stat:last-child{border-right:0}
.gr-ref-lead strong{display:block;font-family:Georgia,serif;font-size:1.15rem;font-weight:400;margin-bottom:3px}
.gr-ref-lead span,.gr-ref-stat span{color:#777176!important;font-size:.76rem}
.gr-ref-stat b{display:block;font-family:Georgia,serif;font-size:1.42rem;font-weight:400;color:#292929}
.gr-minihead{color:#7c706c!important;text-transform:uppercase;letter-spacing:.12em;font-size:.66rem;font-weight:700;margin:.9rem 0 .45rem}
.gr-current-choice{background:#f3ece8;border-left:3px solid #9b7c72;padding:9px 10px;margin:.45rem 0 .7rem}


/* ===== v0.9 editorial workflow refresh ===== */

:root{
  --gr-rose-deep:#8f685d;
  --gr-rose:#a77e72;
  --gr-rose-light:#efe4df;
  --gr-cream:#fbf8f5;
  --gr-warm:#f4eeea;
  --gr-ink2:#252426;
  --gr-shadow:0 18px 55px rgba(66,50,45,.08);
}

[data-testid="stAppViewContainer"]>.main{
  background:
    radial-gradient(900px 360px at 88% 2%, rgba(205,182,171,.22), transparent 62%),
    linear-gradient(180deg,#fff 0 360px,#f9f7f4 360px 100%)!important;
}
.block-container{
  max-width:1540px!important;
  padding:1rem 2.4rem 5rem!important;
}
.gr-brandbar{
  min-height:88px!important;
  margin:-1rem -2.4rem 0!important;
  padding:10px 2.4rem 8px!important;
  background:rgba(255,255,255,.96)!important;
}
.gr-brand-logo{
  width:min(255px,38vw)!important;
  max-height:64px!important;
  object-fit:contain!important;
}
.gr-tooltag{
  border:1px solid #d8ceca;
  padding:6px 10px;
  background:#faf7f5;
  color:#766966!important;
  letter-spacing:.13em!important;
}

/* hero */
.gr-hero-kicker{
  color:var(--gr-rose-deep)!important;
  font-size:.7rem;
  letter-spacing:.19em;
  text-transform:uppercase;
  font-weight:800;
  margin-bottom:.45rem;
}
.gr-hero-title{
  font-family:Georgia,"Times New Roman",serif;
  color:#262426!important;
  font-size:3.55rem;
  line-height:.98;
  letter-spacing:-.045em;
  font-weight:400;
  margin:0 0 .8rem;
}
.gr-hero-copy{
  max-width:760px;
  color:#625c5c!important;
  font-size:1rem;
  line-height:1.7;
}
.gr-feature-row{
  display:flex;
  flex-wrap:wrap;
  gap:7px;
  margin-top:1.15rem;
}
.gr-feature-pill{
  border:1px solid #dfd5d1;
  background:rgba(255,255,255,.8);
  color:#625a58!important;
  padding:6px 10px;
  font-size:.7rem;
  font-weight:700;
  letter-spacing:.04em;
}

/* Reference library hero card */
.gr-library-card{
  background:
    linear-gradient(145deg,rgba(255,255,255,.98),rgba(244,236,232,.96));
  border:1px solid #d9cfcb;
  border-top:4px solid var(--gr-rose);
  padding:20px 20px 16px;
  box-shadow:var(--gr-shadow);
  min-height:205px;
}
.gr-library-eyebrow{
  color:#8d746c!important;
  font-size:.66rem;
  text-transform:uppercase;
  letter-spacing:.16em;
  font-weight:800;
}
.gr-library-big{
  font-family:Georgia,"Times New Roman",serif;
  font-size:2.45rem;
  line-height:1;
  color:#2c2929!important;
  margin:.35rem 0 .25rem;
}
.gr-library-copy{
  color:#716968!important;
  font-size:.82rem;
  line-height:1.45;
}
.gr-library-stats{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  border-top:1px solid #ddd2ce;
  margin-top:14px;
  padding-top:12px;
  gap:8px;
}
.gr-library-stat span{
  display:block;
  color:#918582!important;
  font-size:.63rem;
  text-transform:uppercase;
  letter-spacing:.09em;
}
.gr-library-stat b{
  display:block;
  color:#302d2d!important;
  font-family:Georgia,"Times New Roman",serif;
  font-size:1.16rem;
  font-weight:400;
  margin-top:2px;
}

/* Step headings */
.gr-stephead{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:20px;
  margin:2.1rem 0 .75rem;
  padding-bottom:10px;
  border-bottom:1px solid #d9d0cc;
}
.gr-step-left{
  display:flex;
  align-items:center;
  gap:12px;
}
.gr-step-no{
  width:34px;
  height:34px;
  display:inline-flex;
  align-items:center;
  justify-content:center;
  border-radius:50%;
  background:#2f2e30;
  color:#fff!important;
  font-size:.72rem;
  font-weight:800;
  letter-spacing:.04em;
}
.gr-step-title{
  color:#2d2b2c!important;
  font-family:Georgia,"Times New Roman",serif;
  font-size:1.45rem;
  line-height:1;
}
.gr-step-sub{
  color:#817977!important;
  font-size:.76rem;
  margin-top:3px;
}

/* uploader */
[data-testid="stFileUploader"]{
  background:
    linear-gradient(135deg,rgba(255,255,255,.96),rgba(246,240,237,.92))!important;
  border:1px dashed #bdaaa3!important;
  border-radius:4px!important;
  padding:1rem!important;
  min-height:116px;
  box-shadow:0 10px 32px rgba(70,53,48,.04)!important;
}
[data-testid="stFileUploader"] section{
  min-height:86px!important;
}
[data-testid="stFileUploader"] button{
  background:#fff!important;
  border:1px solid #b8aaa5!important;
  color:#3a3636!important;
}

/* Action cards */
.gr-action-card{
  background:#fff;
  border:1px solid #ddd5d1;
  box-shadow:0 12px 38px rgba(69,52,47,.05);
  padding:17px 18px;
}
.gr-action-count{
  font-family:Georgia,"Times New Roman",serif;
  font-size:1.8rem;
  line-height:1;
  color:#2e2b2c!important;
}
.gr-action-copy{
  color:#786f6d!important;
  font-size:.79rem;
  margin-top:5px;
  line-height:1.4;
}
.gr-inline-status{
  background:#f1e8e4;
  border-left:3px solid var(--gr-rose);
  padding:9px 11px;
  color:#655c5a!important;
  font-size:.78rem;
}

/* buttons: compact, confident, editorial */
.stButton>button,
.stDownloadButton>button,
[data-testid="stLinkButton"] a{
  min-height:40px!important;
  border-radius:2px!important;
  padding:.52rem 1.05rem!important;
  font-size:.69rem!important;
  letter-spacing:.08em!important;
  box-shadow:none!important;
}
.stButton>button[kind="primary"]{
  background:var(--gr-rose-deep)!important;
  border-color:var(--gr-rose-deep)!important;
  color:#fff!important;
}
.stButton>button[kind="primary"]:hover{
  background:#725248!important;
  border-color:#725248!important;
}
.stButton>button[kind="secondary"],
.stDownloadButton>button{
  background:#fff!important;
  border-color:#bfb2ad!important;
}
.stButton>button[kind="secondary"]:hover,
.stDownloadButton>button:hover{
  background:#f7f1ee!important;
  border-color:#8f7f79!important;
}

/* progress */
[data-testid="stProgress"]{
  margin-top:.45rem!important;
}
[data-testid="stProgress"]>div>div{
  background:linear-gradient(90deg,#9f7468,#c3a096)!important;
  height:7px!important;
}
[data-testid="stProgress"] small{
  color:#726966!important;
  font-weight:600!important;
}

/* result metrics */
[data-testid="stMetric"]{
  border-top:3px solid #c6afa7!important;
  background:rgba(255,255,255,.96)!important;
  min-height:100px!important;
  padding:16px 18px!important;
}
[data-testid="stMetricValue"]{
  font-size:2.15rem!important;
}

/* results/action strip */
.gr-results-actions{
  background:#eee5e1;
  border:1px solid #dacdc8;
  padding:13px 15px;
  margin:.75rem 0 1.25rem;
}
.gr-review-link{
  background:#fff;
  border:1px solid #d6cbc7;
  padding:12px 14px;
  margin-top:10px;
}
.gr-review-link code{
  display:block;
  word-break:break-all;
}

/* correction workspace */
[data-testid="stVerticalBlockBorderWrapper"]{
  background:rgba(255,255,255,.98)!important;
  box-shadow:0 12px 36px rgba(66,50,45,.045)!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="primary"]{
  background:#8f685d!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="secondary"]{
  background:#fbfaf9!important;
}

/* remove the old charcoal banner look if any remain */
.gr-sectionbar{
  background:transparent!important;
  color:#756965!important;
  padding:0!important;
  border:0!important;
  margin:0!important;
  font-size:.68rem!important;
  letter-spacing:.14em!important;
}

@media(max-width:950px){
  .block-container{padding:1rem 1.1rem 4rem!important;}
  .gr-brandbar{margin:-1rem -1.1rem 0!important;padding-left:1.1rem!important;padding-right:1.1rem!important;}
  .gr-hero-title{font-size:2.6rem;}
  .gr-library-card{margin-top:1rem;}
}

</style>
"""

def inject_genrose_styles():
    st.markdown(GENROSE_STYLE, unsafe_allow_html=True)

def render_genrose_brand(tool_label="ROOM SCENE ANALYZER"):
    try:
        logo_b64 = base64.b64encode(GENROSE_LOGO_PATH.read_bytes()).decode("ascii")
        logo_html = (
            f'<img class="gr-brand-logo" '
            f'src="data:image/png;base64,{logo_b64}" '
            f'alt="GENROSE">'
        )
    except Exception:
        logo_html = '<div class="gr-brand-fallback">GENROSE</div>'

    st.markdown(
        f"""<div class="gr-brandbar">
            <div></div>
            <div class="gr-brand-image">{logo_html}</div>
            <div class="gr-tooltag">{html.escape(tool_label)}</div>
        </div>""",
        unsafe_allow_html=True
    )

# ---------------- review page ----------------

def proposed_filename(sku, stone, room, ext=".jpg"):
    return safe_filename(f"{sku}-{stone}-{room}") + ext.lower()

def normalize_output_filename(value, default_ext=".jpg"):
    """Sanitize a human-edited output filename without destroying readability."""
    value = str(value or "").strip()
    if not value:
        return ""
    value = re.sub(r'[<>:"/\\|?*]+', "", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value)
    if not Path(value).suffix:
        value += default_ext.lower()
    return value

def clear_manual_filename(item, widget_key=None):
    item["manual_filename"] = False
    item.pop("custom_filename", None)
    if widget_key:
        st.session_state.pop(widget_key, None)

def set_item_material(item, stone_name, source="Manual material selection", filename_key=None):
    """Update material metadata without destroying an intentionally edited filename.

    Auto filenames are synchronized separately. If the user has put the filename in
    MANUAL mode, material/room corrections leave that filename alone until RESET NAME.
    """
    if not stone_name or stone_name == "Needs Review":
        item["stone"] = ""
        item["sku"] = ""
        item["analysis"]["material_confidence"] = 0
        item["analysis"]["material_method"] = "Manual · Needs Review"
        item["manual_material"] = False
    else:
        rec = row_for_stone(stone_name)
        if rec:
            item["stone"] = str(rec["StoneType"])
            item["sku"] = str(rec["SKU"])
            item["analysis"]["material_confidence"] = 100
            item["analysis"]["material_method"] = source
            item["manual_material"] = True
            item["decision_status"] = "CONFIRMED"


def set_item_custom_material(item, stone_name, sku, filename_key=None):
    stone_name = re.sub(r"[^A-Za-z0-9]+", "", str(stone_name or "").strip())
    sku = re.sub(r"[^A-Za-z0-9]+", "", str(sku or "").strip())
    if stone_name:
        item["stone"] = stone_name
        item["sku"] = sku
        item["analysis"]["material_confidence"] = 100
        item["analysis"]["material_method"] = "Manual custom material"
        item["manual_material"] = True
        item["decision_status"] = "NEW_MATERIAL"


def set_item_room(item, room_name, filename_key=None):
    room_name = re.sub(r"[^A-Za-z0-9]+", "", str(room_name or "").strip()) or "Other"
    item["room"] = room_name
    item["analysis"]["room_confidence"] = 100
    item["analysis"]["room_method"] = "Manual room selection"


def mark_item_needs_input(item, filename_key=None):
    item["decision_status"] = "NEEDS_FURTHER_INPUT"
    item["stone"] = ""
    item["sku"] = ""
    item["manual_material"] = False
    item["analysis"]["material_confidence"] = 0
    item["analysis"]["material_method"] = "Needs further input"
    clear_manual_filename(item, filename_key)

def mark_item_new_material(item, filename_key=None):
    item["decision_status"] = "NEW_MATERIAL"


def set_state_value(key, value):
    """Safe Streamlit callback helper. Callbacks run before widgets are recreated."""
    st.session_state[key] = value


def mark_filename_manual(manual_key):
    st.session_state[manual_key] = True


def reset_state_filename(filename_key, generated_key, manual_key):
    st.session_state[manual_key] = False
    st.session_state[filename_key] = st.session_state.get(generated_key, "")


def mark_main_needs_input(item_index, material_key, filename_key):
    results = st.session_state.get("results", [])
    if not (0 <= item_index < len(results)):
        return
    mark_item_needs_input(results[item_index], filename_key)
    st.session_state[material_key] = "Needs Review"


def save_main_custom_material(item_index, material_key, custom_material_key, custom_sku_key, show_key, notice_key):
    results = st.session_state.get("results", [])
    if not (0 <= item_index < len(results)):
        return
    stone = str(st.session_state.get(custom_material_key, "") or "").strip()
    sku = str(st.session_state.get(custom_sku_key, "") or "").strip()
    if not stone:
        st.session_state[notice_key] = "Enter a material name first."
        return
    set_item_custom_material(results[item_index], stone, sku)
    st.session_state[material_key] = "Needs Review"
    st.session_state[show_key] = False
    st.session_state[notice_key] = ""


def mark_main_filename_manual(item_index, filename_key):
    results = st.session_state.get("results", [])
    if not (0 <= item_index < len(results)):
        return
    item = results[item_index]
    ext = item.get("ext") or ".jpg"
    value = normalize_output_filename(st.session_state.get(filename_key, ""), ext)
    item["manual_filename"] = True
    item["custom_filename"] = value
    item["new_name"] = value or item.get("generated_name", "")


def reset_main_filename(item_index, filename_key, generated_key):
    results = st.session_state.get("results", [])
    if not (0 <= item_index < len(results)):
        return
    item = results[item_index]
    generated = st.session_state.get(generated_key) or item.get("generated_name", "")
    item["manual_filename"] = False
    item.pop("custom_filename", None)
    item["new_name"] = generated
    st.session_state[filename_key] = generated


def review_decision_map(payload):
    return {
        str(d.get("old_filename", "")): d
        for d in (payload or {}).get("decisions", [])
        if d.get("old_filename")
    }


def preflight_results(items):
    """Return blocking export issues plus lighter warnings."""
    blockers = []
    warnings = []
    names = collections.defaultdict(list)
    for i, item in enumerate(items):
        final_name = normalize_output_filename(item.get("new_name", ""), item.get("ext") or ".jpg")
        label = item.get("name") or f"Item {i+1}"
        if not final_name:
            blockers.append(f"{label}: final filename is empty.")
        else:
            names[final_name.lower()].append(label)
            original_ext = (item.get("ext") or Path(label).suffix or ".jpg").lower()
            if Path(final_name).suffix.lower() != original_ext:
                blockers.append(f"{label}: final extension must remain {original_ext}; ZIP export renames but does not transcode images.")
        if not item.get("stone"):
            blockers.append(f"{label}: material still needs input.")
        if not item.get("sku"):
            blockers.append(f"{label}: SKU is missing.")
        if (item.get("room") or "Other") == "Other":
            warnings.append(f"{label}: room type is Other.")
        if item.get("decision_status") in {"NEEDS_FURTHER_INPUT", "NEEDS_REVIEW"}:
            blockers.append(f"{label}: decision is still marked {item.get('decision_status')}.")
    for _, labels in names.items():
        if len(labels) > 1:
            blockers.append("Duplicate final filename: " + " / ".join(labels))
    # De-duplicate while preserving order.
    blockers = list(dict.fromkeys(blockers))
    warnings = list(dict.fromkeys(warnings))
    return blockers, warnings


def renamed_images_zip(items):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            name = normalize_output_filename(item.get("new_name", ""), item.get("ext") or ".jpg")
            if name:
                zf.writestr(name, item["bytes"])
    return out.getvalue()


def submission_dataframe(submission):
    rows = []
    for d in (submission or {}).get("decisions", []):
        rows.append({
            "Old Filename": d.get("old_filename", ""),
            "Final Filename": d.get("final_filename", ""),
            "Suggested Material": d.get("suggested_material", ""),
            "Final Material": d.get("final_material", ""),
            "Final SKU": d.get("final_sku", ""),
            "Final Room": d.get("final_room", ""),
            "Decision Status": d.get("decision_status", ""),
            "Approved": "YES" if d.get("approved") else "NO",
            "Analyst Note": d.get("analyst_note", ""),
            "Reviewer Note": d.get("reviewer_note", d.get("notes", "")),
        })
    return pd.DataFrame(rows)


def apply_submission_to_results(submission):
    """Round-trip a completed review back into the currently loaded analyzer batch."""
    by_name = review_decision_map(submission)
    results = st.session_state.get("results", [])
    applied = 0
    for i, item in enumerate(results):
        d = by_name.get(item.get("name", ""))
        if not d:
            continue
        item["stone"] = d.get("final_material", "") or ""
        item["sku"] = d.get("final_sku", "") or ""
        item["room"] = d.get("final_room", "") or "Other"
        item["decision_status"] = d.get("decision_status", "CONFIRMED")
        item["reviewer_note"] = d.get("reviewer_note", d.get("notes", "")) or ""
        item["reviewed_by"] = submission.get("reviewer", "")
        if item.get("analysis") is not None:
            if item["stone"] and item["sku"]:
                item["analysis"]["material_confidence"] = 100
                item["analysis"]["material_method"] = "Reviewer confirmed / corrected"
            else:
                item["analysis"]["material_confidence"] = 0
                item["analysis"]["material_method"] = "Reviewer marked needs input"
            if item["room"]:
                item["analysis"]["room_confidence"] = 100
                item["analysis"]["room_method"] = "Reviewer confirmed / corrected"
        final_filename = normalize_output_filename(d.get("final_filename", ""), item.get("ext") or ".jpg")
        if final_filename:
            item["new_name"] = final_filename
            item["custom_filename"] = final_filename
            item["manual_filename"] = True
        # Clear widget state so the current canonical values rehydrate cleanly on rerun.
        for key in (
            f"main_material_{i}", f"main_room_{i}", f"main_custom_room_{i}",
            f"main_filename_{i}", f"main_generated_{i}", f"main_analyst_note_{i}"
        ):
            st.session_state.pop(key, None)
        applied += 1
    return applied

def render_review_page(batch_id):
    inject_genrose_styles()
    render_genrose_brand("ROOM SCENE APPROVAL")

    back_col, title_col = st.columns([.16, 1])
    with back_col:
        if st.button("← ANALYZER", use_container_width=True):
            st.query_params.clear()
            st.rerun()

    batch = load_review_batch(batch_id)
    if not batch:
        st.error("That review batch could not be loaded.")
        st.caption("Create a fresh review link from the analyzer. If another person is opening it, the Streamlit app must be public or that viewer must be invited.")
        st.stop()

    completed = load_submission(batch_id)
    draft = load_review_draft(batch_id)
    seed_payload = draft or completed or {}
    seed_map = review_decision_map(seed_payload)

    st.markdown('<div class="gr-kicker">ROOM SCENE REVIEW</div>', unsafe_allow_html=True)
    st.title("Approve Room Scenes")
    st.markdown(
        f'<div class="gr-lede">Batch {html.escape(batch_id)} · Compare every original filename against the proposed result. '
        'Material choices update the SKU, GENROSE reference and generated filename automatically. Everything remains editable before submission.</div>',
        unsafe_allow_html=True
    )
    analyst_batch_note = str(batch.get("analyst_batch_note", "") or "").strip()
    if analyst_batch_note:
        st.markdown('<div class="gr-label">ANALYST NOTE FOR THIS REVIEW</div>', unsafe_allow_html=True)
        st.info(analyst_batch_note)
    if completed:
        who = completed.get("reviewer") or "Reviewer"
        st.success(f"This batch was already submitted by {who}. You can still inspect or revise it and submit again if needed.")
    elif draft:
        st.info("An autosaved review draft was restored for this batch.")

    progress_placeholder = st.empty()
    decisions = []
    stone_options = catalog["StoneType"].astype(str).tolist()

    for i, item in enumerate(batch["items"]):
        seed = seed_map.get(item.get("old_name", ""), {})
        with st.container(border=True):
            st.markdown('<div class="gr-label">ORIGINAL FILENAME</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="gr-filename">{html.escape(item["old_name"])}</div>',
                unsafe_allow_html=True
            )

            image_col, info_col, swatch_col = st.columns([1.05, 1.5, .72], gap="large")

            with image_col:
                b = review_image_bytes(batch_id, item)
                if b:
                    st.image(Image.open(io.BytesIO(b)), use_container_width=True)
                st.caption(f"Proposed confidence · Material {item['material_confidence']}% · Room {item['room_confidence']}%")
                analyst_note = str(item.get("analyst_note", "") or "").strip()
                if analyst_note:
                    st.markdown('<div class="gr-label">ANALYST NOTE</div>', unsafe_allow_html=True)
                    st.info(analyst_note)

            with info_col:
                suggested_stone = item.get("stone") or ""
                suggested_sku = item.get("sku") or ""
                suggested_room = item.get("room") or "Other"
                ext = Path(item["old_name"]).suffix.lower() or ".jpg"

                material_options = ["Needs Further Input"] + stone_options + ["➕ New material"]
                material_key = f"review_material_{batch_id}_{i}"
                seed_status = seed.get("decision_status") or item.get("decision_status", "")
                if seed_status == "NEW_MATERIAL":
                    material_default = "➕ New material"
                elif seed_status == "NEEDS_FURTHER_INPUT":
                    material_default = "Needs Further Input"
                else:
                    seeded_material = seed.get("final_material") or suggested_stone
                    material_default = seeded_material if seeded_material in stone_options else "Needs Further Input"
                if material_key not in st.session_state:
                    st.session_state[material_key] = material_default

                material_choice = st.selectbox(
                    "Material",
                    material_options,
                    key=material_key,
                    help="Type into the selector to search all canonical materials. Changing this updates SKU, slab reference and AUTO filename."
                )

                final_stone = suggested_stone
                final_sku = suggested_sku
                added_new_material = False

                if material_choice == "Needs Further Input":
                    final_stone = ""
                    final_sku = ""
                elif material_choice == "➕ New material":
                    added_new_material = True
                    cc1, cc2 = st.columns([1.2, .8])
                    custom_material_key = f"review_custom_material_{batch_id}_{i}"
                    custom_sku_key = f"review_custom_sku_{batch_id}_{i}"
                    if custom_material_key not in st.session_state:
                        st.session_state[custom_material_key] = (seed.get("final_material") or suggested_stone) if seed_status == "NEW_MATERIAL" else ""
                    if custom_sku_key not in st.session_state:
                        st.session_state[custom_sku_key] = (seed.get("final_sku") or suggested_sku) if seed_status == "NEW_MATERIAL" else ""
                    final_stone = re.sub(
                        r"[^A-Za-z0-9]+", "",
                        cc1.text_input(
                            "Custom material name",
                            key=custom_material_key,
                            placeholder="Exact material name"
                        ).strip()
                    )
                    final_sku = re.sub(
                        r"[^A-Za-z0-9]+", "",
                        cc2.text_input(
                            "SKU",
                            key=custom_sku_key,
                            placeholder="Base SKU"
                        ).strip()
                    )
                else:
                    rec = row_for_stone(material_choice)
                    if rec:
                        final_stone = str(rec["StoneType"])
                        final_sku = str(rec["SKU"])

                sku_display = final_sku or "NEED-SKU"
                st.caption(f"Current material · {final_stone or 'Needs Further Input'} · SKU {sku_display}")

                room_options = ROOM_TYPES
                room_key = f"review_room_{batch_id}_{i}"
                seeded_room = seed.get("final_room") or suggested_room
                room_default = seeded_room if seeded_room in room_options else "Other"
                if room_key not in st.session_state:
                    st.session_state[room_key] = room_default
                room_choice = st.selectbox("Room type", room_options, key=room_key)
                if room_choice == "Other":
                    custom_room_key = f"review_custom_room_{batch_id}_{i}"
                    if custom_room_key not in st.session_state:
                        st.session_state[custom_room_key] = seeded_room if seeded_room not in room_options else ""
                    final_room = re.sub(
                        r"[^A-Za-z0-9]+", "",
                        st.text_input(
                            "Custom room type",
                            key=custom_room_key,
                            placeholder="e.g. ReceptionLounge"
                        ).strip()
                    ) or "Other"
                else:
                    final_room = room_choice

                generated_filename = proposed_filename(
                    final_sku or "NEED-SKU",
                    final_stone or "UnknownMaterial",
                    final_room,
                    ext
                )
                filename_key = f"review_filename_{batch_id}_{i}"
                generated_key = f"review_generated_{batch_id}_{i}"
                manual_key = f"review_filename_manual_{batch_id}_{i}"
                previous_generated = st.session_state.get(generated_key)
                if manual_key not in st.session_state:
                    seeded_filename = seed.get("final_filename", "")
                    st.session_state[manual_key] = bool(seeded_filename and seeded_filename != generated_filename)
                if filename_key not in st.session_state:
                    st.session_state[filename_key] = seed.get("final_filename") or item.get("new_name") or generated_filename
                elif previous_generated is not None and previous_generated != generated_filename and not st.session_state.get(manual_key, False):
                    st.session_state[filename_key] = generated_filename
                st.session_state[generated_key] = generated_filename

                st.markdown('<div class="gr-label">FINAL OUTPUT FILENAME · EDITABLE</div>', unsafe_allow_html=True)
                edited_filename = st.text_input(
                    "Final output filename",
                    key=filename_key,
                    label_visibility="collapsed",
                    on_change=mark_filename_manual,
                    args=(manual_key,),
                    help="AUTO mode follows Material/SKU/Room. Editing this field switches it to MANUAL until Reset Generated Name."
                )
                final_filename = normalize_output_filename(edited_filename, ext) or generated_filename
                filename_mode = "MANUAL" if st.session_state.get(manual_key, False) else "AUTO"

                rc1, rc2 = st.columns(2)
                rc1.button(
                    "RESET GENERATED NAME",
                    key=f"review_reset_name_{batch_id}_{i}",
                    use_container_width=True,
                    on_click=reset_state_filename,
                    args=(filename_key, generated_key, manual_key)
                )
                approve_key = f"review_approve_{batch_id}_{i}"
                if approve_key not in st.session_state:
                    # A fresh review must require an explicit human approval click.
                    # Drafts/submitted reviews keep their previously saved state.
                    st.session_state[approve_key] = bool(seed["approved"]) if "approved" in seed else False
                approved = rc2.checkbox("Approved", key=approve_key)
                st.caption(f"Filename mode · {filename_mode}")

                with st.expander("Suggested candidates", expanded=False):
                    mats = item.get("material_candidates", [])
                    rooms = item.get("room_candidates", [])
                    if mats:
                        st.markdown("**Material candidates — click USE to select**")
                        for j, c in enumerate(mats):
                            sw = candidate_swatch_bytes(c.get("sku", ""))
                            with st.container(border=True):
                                ca, cb, cc = st.columns([.22, .56, .22], gap="small")
                                with ca:
                                    if sw:
                                        st.image(Image.open(io.BytesIO(sw)), use_container_width=True)
                                    else:
                                        st.caption("No thumbnail")
                                with cb:
                                    st.markdown(f"**{html.escape(str(c.get('stone','')))}**")
                                    st.caption(f"{c.get('sku','')} · {int(c.get('confidence',0))}% confidence")
                                with cc:
                                    st.button(
                                        "USE",
                                        key=f"review_candidate_{batch_id}_{i}_{j}",
                                        type="primary",
                                        use_container_width=True,
                                        on_click=set_state_value,
                                        args=(material_key, c.get("stone", ""))
                                    )
                    if rooms:
                        st.markdown("**Room candidates**")
                        for c in rooms:
                            st.write(f"{c.get('room')} · evidence {c.get('weight',0):.1f}")

                reviewer_note_key = f"review_notes_{batch_id}_{i}"
                if reviewer_note_key not in st.session_state:
                    st.session_state[reviewer_note_key] = seed.get("reviewer_note", seed.get("notes", "")) or ""
                reviewer_note = st.text_area(
                    "Reviewer note",
                    key=reviewer_note_key,
                    placeholder="Optional correction, alternate candidates, or context for Marketing",
                    height=90
                )

            with swatch_col:
                st.markdown("**GENROSE Reference**")
                sw = candidate_swatch_bytes(final_sku) if final_sku else None
                if sw:
                    st.image(Image.open(io.BytesIO(sw)), use_container_width=True)
                else:
                    st.info("No material thumbnail is available yet.")
                current_website_url = website_entry_for_sku(final_sku).get("page_url", "") if final_sku else ""
                if current_website_url:
                    st.link_button("GENROSE PRODUCT PAGE", current_website_url)

            decisions.append({
                "old_filename": item["old_name"],
                "suggested_filename": item.get("new_name", ""),
                "final_filename": final_filename,
                "suggested_material": suggested_stone,
                "suggested_sku": suggested_sku,
                "final_material": final_stone,
                "final_sku": final_sku,
                "suggested_room": suggested_room,
                "final_room": final_room,
                "decision_status": (
                    "NEEDS_FURTHER_INPUT" if material_choice == "Needs Further Input"
                    else "NEW_MATERIAL" if material_choice == "➕ New material"
                    else "CONFIRMED"
                ),
                "material_confidence": item["material_confidence"],
                "room_confidence": item["room_confidence"],
                "approved": approved,
                "added_new_material": added_new_material,
                "filename_mode": filename_mode,
                "analyst_note": str(item.get("analyst_note", "") or ""),
                "reviewer_note": reviewer_note,
                "notes": reviewer_note,
            })

    approved_count = sum(1 for d in decisions if d["approved"])
    unresolved_count = sum(1 for d in decisions if d["decision_status"] == "NEEDS_FURTHER_INPUT")
    changed_count = sum(
        1 for d in decisions
        if d["final_material"] != d["suggested_material"]
        or d["final_room"] != d["suggested_room"]
        or d["final_filename"] != d["suggested_filename"]
    )
    with progress_placeholder.container():
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Scenes", len(decisions))
        p2.metric("Approved", approved_count)
        p3.metric("Changed", changed_count)
        p4.metric("Needs input", unresolved_count)

    reviewer_key = f"review_reviewer_{batch_id}"
    if reviewer_key not in st.session_state:
        st.session_state[reviewer_key] = seed_payload.get("reviewer", "") or ""
    reviewer = st.text_input("Reviewer name", key=reviewer_key, placeholder="Name")

    # Autosave only when the meaningful review payload changes.
    draft_payload = {
        "batch_id": batch_id,
        "reviewer": reviewer,
        "analyst_batch_note": analyst_batch_note,
        "decisions": decisions
    }
    draft_digest = hashlib.sha256(json.dumps(draft_payload, sort_keys=True).encode("utf-8")).hexdigest()
    digest_key = f"review_draft_digest_{batch_id}"
    if st.session_state.get(digest_key) != draft_digest:
        try:
            save_review_draft(batch_id, draft_payload)
            st.session_state[digest_key] = draft_digest
        except Exception as e:
            st.caption(f"Draft autosave unavailable: {e}")

    st.caption("Review choices are autosaved as you work.")
    if st.button("SUBMIT REVIEW TO MARKETING", type="primary", use_container_width=True):
        missing_custom = [
            d for d in decisions
            if d["added_new_material"] and not d["final_material"]
        ]
        if missing_custom:
            st.error("Enter a material name for every custom-material item.")
            st.stop()

        submission = {
            "batch_id": batch_id,
            "reviewer": reviewer,
            "analyst_batch_note": analyst_batch_note,
            "decisions": decisions
        }
        save_submission(batch_id, submission)

        html_rows = []
        text_rows = []
        for d in decisions:
            status = "APPROVED" if d["approved"] else "NOT APPROVED"
            html_rows.append(
                "<tr>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{html.escape(d.get('decision_status',''))}</td>"
                f"<td>{html.escape(d['old_filename'])}</td>"
                f"<td>{html.escape(d['final_filename'])}</td>"
                f"<td>{html.escape(d['suggested_material'])}</td>"
                f"<td>{html.escape(d['final_material'])}</td>"
                f"<td>{html.escape(d['final_sku'])}</td>"
                f"<td>{html.escape(d['final_room'])}</td>"
                f"<td>{d['material_confidence']}%</td>"
                f"<td>{html.escape(d['analyst_note'])}</td>"
                f"<td>{html.escape(d['reviewer_note'])}</td>"
                "</tr>"
            )
            text_rows.append(
                f"{status} | OLD={d['old_filename']} | NEW={d['final_filename']} | "
                f"MATERIAL={d['final_material']} | SKU={d['final_sku']} | ROOM={d['final_room']} | "
                f"CONF={d['material_confidence']}% | ANALYST={d['analyst_note']} | REVIEWER={d['reviewer_note']}"
            )

        subject = f"Room Scene Review — {batch_id}"
        batch_note_html = (
            f"<p><strong>Analyst review note:</strong> {html.escape(analyst_batch_note)}</p>"
            if analyst_batch_note else ""
        )
        html_body = (
            f"<h2>{html.escape(subject)}</h2><p>Reviewer: {html.escape(reviewer or 'Not supplied')}</p>"
            + batch_note_html
            + "<table border='1' cellpadding='6' cellspacing='0'><thead><tr>"
            "<th>Status</th><th>Decision</th><th>Old filename</th><th>Final filename</th><th>Suggested material</th>"
            "<th>Final material</th><th>SKU</th><th>Room</th><th>Confidence</th><th>Analyst note</th><th>Reviewer note</th>"
            "</tr></thead><tbody>" + "".join(html_rows) + "</tbody></table>"
        )
        batch_note_text = f"\nAnalyst review note: {analyst_batch_note}\n" if analyst_batch_note else ""
        text_body = subject + f"\nReviewer: {reviewer}" + batch_note_text + "\n" + "\n".join(text_rows)

        try:
            send_review_email(subject, html_body, text_body)
            st.success(f"Submitted and emailed to {REVIEW_EMAIL}.")
        except Exception as e:
            st.warning(f"Review was saved, but email failed: {e}")

review_param = st.query_params.get("review", "")
if review_param:
    render_review_page(str(review_param))
    st.stop()

# ---------------- main app ----------------

st.session_state.setdefault("upload_key", 0)
st.session_state.setdefault("pending", [])
st.session_state.setdefault("results", [])
st.session_state.setdefault("selected", 0)
st.session_state.setdefault("review_url", "")
st.session_state.setdefault("review_batch_note", "")

inject_genrose_styles()
render_genrose_brand()


hero_left, hero_right = st.columns([1.6, .72], gap="large")

with hero_left:
    st.markdown('<div class="gr-hero-kicker">INTERNAL IMAGE OPERATIONS</div>', unsafe_allow_html=True)
    st.markdown('<div class="gr-hero-title">Room Scene Analyzer</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="gr-hero-copy">Turn messy manufacturer room scenes into consistent, reviewable production assets. '
        'The analyzer reads the original filename first, identifies the room, compares weak material matches against the synced GENROSE slab library, '
        'and keeps every uncertain decision easy to correct.</div>'
        '<div class="gr-feature-row">'
        '<span class="gr-feature-pill">FILENAME FIRST</span>'
        '<span class="gr-feature-pill">GENROSE VISUAL REFERENCES</span>'
        '<span class="gr-feature-pill">EDITABLE OUTPUT</span>'
        '<span class="gr-feature-pill">HUMAN REVIEW BUILT IN</span>'
        '</div>',
        unsafe_allow_html=True
    )

website_cache = load_website_cache()
website_count = sum(1 for x in website_cache.get("materials", {}).values() if x.get("status") == "OK")
signature_count = sum(
    1 for x in website_cache.get("materials", {}).values()
    if x.get("reference_policy") == "strict-slab" and x.get("visual_signatures")
)
legacy_preserved_count = sum(
    1 for x in website_cache.get("materials", {}).values()
    if x.get("visual_signatures") and x.get("reference_policy") != "strict-slab"
)
bundled_total, bundled_probeable, bundled_probe_files = strict_reference_stats()

with hero_right:
    library_ready = signature_count > 0
    st.markdown(
        f'<div class="gr-library-card">'
        f'<div class="gr-library-eyebrow">GENROSE REFERENCE LIBRARY</div>'
        f'<div class="gr-library-big">{signature_count if signature_count else "Not synced"}</div>'
        f'<div class="gr-library-copy">'
        f'{"materials ready with strict -Slab reference images" if library_ready else "Refresh the library to build strict -Slab references. Existing synced data is preserved until a strict replacement succeeds."}'
        f'</div>'
        f'<div class="gr-library-stats">'
        f'<div class="gr-library-stat"><span>Probeable from export</span><b>{bundled_probeable}/{bundled_total}</b></div>'
        f'<div class="gr-library-stat"><span>Library entries</span><b>{website_count}</b></div>'
        f'<div class="gr-library-stat"><span>Visual index</span><b>{signature_count}</b></div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    if legacy_preserved_count:
        st.caption(f"{legacy_preserved_count} previous reference entries are preserved as backup while strict replacements are built.")
    sync_col, stamp_col = st.columns([.62,.38], gap="small")
    with sync_col:
        if st.button("REFRESH LIBRARY", type="primary", use_container_width=False):
            if not storage_ready():
                st.error("Google Cloud Storage must be configured before references can be synced.")
            else:
                try:
                    synced = sync_genrose_website(build_visual=product_search_ready())
                    summary = synced.get("refresh_summary", {})
                    strict_ready = summary.get("strict_ready_total", 0)
                    preserved = summary.get("previous_preserved", 0)
                    st.success(f"Refresh complete · {strict_ready} strict slab references ready · {preserved} previous entries preserved where no strict replacement was found.")
                    st.rerun()
                except Exception as e:
                    st.exception(e)
    with stamp_col:
        if website_cache.get("synced_at"):
            st.caption("Last sync\n" + website_cache["synced_at"][5:16].replace("T"," · "))
        else:
            st.caption("Never synced")

with st.sidebar:
    st.header("Controls")
    if st.button("CLEAR CURRENT BATCH", use_container_width=True):
        st.session_state.pending = []
        st.session_state.results = []
        st.session_state.review_url = ""
        st.session_state.review_batch_note = ""
        st.session_state.selected = 0
        st.session_state.upload_key += 1
        st.rerun()

    st.divider()
    st.subheader("System Status")
    st.write("Cloud Vision", "✅" if vision_ready() else "⚠️")
    st.write("Cloud Storage", "✅" if storage_ready() else "⚠️")
    st.write("Product Search", "✅" if product_search_ready() else "⚠️")
    st.caption("Reference syncing and daily workflow controls are on the main screen.")

st.markdown(
    '<div class="gr-stephead">'
    '<div class="gr-step-left"><span class="gr-step-no">01</span><div>'
    '<div class="gr-step-title">Add room scenes</div>'
    '<div class="gr-step-sub">Drag in a batch of manufacturer JPG, PNG or WEBP files.</div>'
    '</div></div></div>',
    unsafe_allow_html=True
)

uploads = st.file_uploader(
    "Drop room scene images here",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key=f"upload_{st.session_state.upload_key}",
    label_visibility="collapsed"
)

if uploads:
    existing = {x["name"] for x in st.session_state.pending}
    for u in uploads:
        if u.name not in existing:
            ext = Path(u.name).suffix.lower() or ".jpg"
            st.session_state.pending.append({"name": u.name, "bytes": u.getvalue(), "ext": ext})
            existing.add(u.name)

pending = st.session_state.pending

if pending and not st.session_state.results:
    st.markdown(
        '<div class="gr-stephead">'
        '<div class="gr-step-left"><span class="gr-step-no">02</span><div>'
        '<div class="gr-step-title">Analyze the batch</div>'
        '<div class="gr-step-sub">Filename → room → slab references → correction candidates.</div>'
        '</div></div></div>',
        unsafe_allow_html=True
    )

    analyze_left, analyze_right = st.columns([1.65,.55], gap="large")
    with analyze_left:
        st.markdown(
            f'<div class="gr-action-card">'
            f'<div class="gr-action-count">{len(pending)} scenes ready</div>'
            f'<div class="gr-action-copy">Run the complete naming pass. Nothing is renamed automatically — every result remains editable.</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        if not vision_ready():
            st.markdown(
                '<div class="gr-inline-status">Cloud Vision is not configured. Filename and reference matching will still run, but room identification will be less robust.</div>',
                unsafe_allow_html=True
            )
    with analyze_right:
        st.write("")
        st.write("")
        run_analysis = st.button(
            f"ANALYZE {len(pending)} SCENES",
            type="primary",
            use_container_width=False
        )
        clear_pending = st.button(
            "CLEAR BATCH",
            use_container_width=False
        )
        if clear_pending:
            st.session_state.pending = []
            st.session_state.results = []
            st.session_state.review_url = ""
            st.session_state.review_batch_note = ""
            st.session_state.selected = 0
            st.session_state.upload_key += 1
            st.rerun()

    if run_analysis:
        progress = st.progress(0, text="Preparing analysis…")
        results = []
        for i, item in enumerate(pending, start=1):
            progress.progress(
                (i-1) / len(pending),
                text=f"Analyzing {i} of {len(pending)} · {item['name']}"
            )
            try:
                analysis = analyze_image(item["name"], item["bytes"])
            except Exception as e:
                analysis = {
                    "stone": "", "sku": "", "material_confidence": 0, "material_method": f"ERROR: {e}",
                    "filename_material_score": 0, "vision_text_score": 0, "visual_score": 0, "local_visual_score": 0,
                    "website_verified": False, "room": "Other", "room_confidence": 0,
                    "room_method": "Error", "room_candidates": [], "material_candidates": [], "vision": {"error": str(e)}
                }

            new_name = proposed_filename(
                analysis["sku"] or "NEED-SKU",
                analysis["stone"] or "UnknownMaterial",
                analysis["room"],
                item["ext"]
            )
            results.append({
                **item,
                "analysis": analysis,
                "stone": analysis["stone"],
                "sku": analysis["sku"],
                "room": analysis["room"],
                "new_name": new_name,
                "decision_status": "AUTO_MATCH" if analysis["stone"] else "NEEDS_REVIEW"
            })
        progress.progress(1.0, text="Analysis complete")
        time.sleep(.2)
        progress.empty()
        st.session_state.results = results
        st.session_state.review_url = ""
        st.session_state.review_batch_note = ""
        st.session_state.selected = 0
        st.rerun()

results = st.session_state.results

if not pending:
    st.info("Drop a batch of room scenes above. Then click ANALYZE.")
    st.stop()

if not results:
    st.stop()

# Collision-safe suffixes
counts = {}
for x in results:
    key = (x["sku"], x["stone"], x["room"], x["ext"])
    counts[key] = counts.get(key, 0) + 1
seen = {}
for x in results:
    key = (x["sku"], x["stone"], x["room"], x["ext"])
    seen[key] = seen.get(key, 0) + 1
    base = f"{x['sku'] or 'NEED-SKU'}-{x['stone'] or 'UnknownMaterial'}-{x['room']}"
    if counts[key] > 1:
        base += f"-{seen[key]:02d}"
    generated_name = safe_filename(base) + x["ext"]
    x["generated_name"] = generated_name
    if x.get("manual_filename") and x.get("custom_filename"):
        x["new_name"] = normalize_output_filename(x["custom_filename"], x["ext"]) or generated_name
    else:
        x["new_name"] = generated_name

high = sum(1 for x in results if x["analysis"]["material_confidence"] >= 90 and x["analysis"]["room_confidence"] >= 70 and bool(x.get("stone")) and bool(x.get("sku")))
review = len(results) - high

summary_df = pd.DataFrame([{
    "Old Filename": x["name"],
    "New Filename": x["new_name"],
    "Material": x["stone"] or "Needs Review",
    "SKU": x["sku"] or "NEED-SKU",
    "Room": x["room"],
    "Decision Status": x.get("decision_status",""),
    "Material Confidence": f'{x["analysis"]["material_confidence"]}%',
    "Room Confidence": f'{x["analysis"]["room_confidence"]}%',
    "Method": x["analysis"]["material_method"],
    "Website Verified": "YES" if x["analysis"].get("website_verified") else "NO",
    "Analyst Note": x.get("analyst_note", ""),
    "Reviewer Note": x.get("reviewer_note", "")
} for x in results])

st.markdown(
    '<div class="gr-stephead">'
    '<div class="gr-step-left"><span class="gr-step-no">03</span><div>'
    '<div class="gr-step-title">Review the results</div>'
    '<div class="gr-step-sub">Correct uncertain matches, confirm the room, and edit the final filename.</div>'
    '</div></div></div>',
    unsafe_allow_html=True
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Scenes", len(results))
m2.metric("Ready", high)
m3.metric("Needs review", review)
m4.metric("References matched", sum(1 for x in results if x["analysis"].get("website_verified")))

st.markdown('<div class="gr-minihead">Note for reviewer</div>', unsafe_allow_html=True)
st.text_area(
    "Note for reviewer",
    key="review_batch_note",
    label_visibility="collapsed",
    placeholder="Optional note that will appear at the top of Cyndi's review page — batch-level context, alternate materials to watch for, special instructions, etc.",
    height=100
)
st.caption("This note is saved into the review batch when you create the link. Per-image Analyst Notes still travel with their individual scenes.")

st.markdown('<div class="gr-results-actions">', unsafe_allow_html=True)
act_spacer, act_review, act_csv = st.columns([1.0,.34,.34], gap="small")
with act_review:
    if st.button("CREATE REVIEW LINK", type="primary", use_container_width=False):
        batch_id, url = save_review_batch(results, st.session_state.get("review_batch_note", ""))
        st.session_state.review_url = url
        st.success("Review page created.")
with act_csv:
    st.download_button(
        "DOWNLOAD CSV",
        summary_df.to_csv(index=False).encode("utf-8-sig"),
        "room_scene_analysis.csv",
        "text/csv",
        use_container_width=False
    )
st.markdown('</div>', unsafe_allow_html=True)

if st.session_state.review_url:
    review_id = st.session_state.review_url.split("review=", 1)[-1]
    st.markdown(
        f'<div class="gr-review-link"><div class="gr-library-eyebrow">REVIEW LINK READY</div>'
        f'<code>{html.escape(st.session_state.review_url)}</code></div>',
        unsafe_allow_html=True
    )
    r1, r2, r3 = st.columns([.22,.22,1], gap="small")
    with r1:
        st.markdown(
            f'<a href="?review={html.escape(review_id)}" target="_blank" '
            'style="display:inline-block;padding:10px 14px;background:#8f685d;color:white;text-decoration:none;'
            'font-size:11px;font-weight:700;letter-spacing:.08em">TEST REVIEW PAGE</a>',
            unsafe_allow_html=True
        )
    with r2:
        st.caption("Make the Streamlit app public before sending this link outside your account.")

    submitted_review = load_submission(review_id)
    if submitted_review:
        reviewer_name = submitted_review.get("reviewer") or "Reviewer"
        st.success(f"Review submitted by {reviewer_name}. You can apply the approved decisions back to this analyzer batch.")
        sr1, sr2, sr3 = st.columns([.28, .28, 1], gap="small")
        with sr1:
            if st.button("APPLY SUBMITTED REVIEW", type="primary", use_container_width=True):
                applied = apply_submission_to_results(submitted_review)
                st.success(f"Applied {applied} reviewed scene(s).")
                st.rerun()
        with sr2:
            submitted_df = submission_dataframe(submitted_review)
            st.download_button(
                "DOWNLOAD REVIEW CSV",
                submitted_df.to_csv(index=False).encode("utf-8-sig"),
                f"room_scene_review_{review_id}.csv",
                "text/csv",
                use_container_width=True
            )
    else:
        st.caption("Review status · Awaiting submission. Draft choices are autosaved on the review page.")

st.markdown('<div class="gr-rule"></div>', unsafe_allow_html=True)
left, center, right = st.columns([0.78, 1.38, 1.22], gap="large")

with left:
    with st.container(border=True, height=860):
        st.subheader("Queue")
        q = st.text_input("Search scenes", placeholder="Search filename, material, SKU or room", label_visibility="collapsed")
        for i, x in enumerate(results):
            a = x["analysis"]
            if q and norm(q) not in norm(x["name"] + " " + x["stone"] + " " + x["sku"] + " " + x["room"]):
                continue
            conf = a["material_confidence"]
            icon = "●" if conf >= 90 and a["room_confidence"] >= 70 else "●"
            label = f"{icon} {x['name']}\n{x['stone'] or 'Needs Review'} · {x['room']} · {conf}%"
            if st.button(
                label,
                key=f"result_{i}",
                use_container_width=True,
                type="primary" if i == st.session_state.selected else "secondary"
            ):
                st.session_state.selected = i
                st.rerun()

idx = min(st.session_state.selected, len(results)-1)
item = results[idx]
analysis = item["analysis"]

with center:
    with st.container(border=True, height=860):
        st.subheader("Preview")
        st.image(Image.open(io.BytesIO(item["bytes"])), use_container_width=True)
        st.caption(item["name"])
        cls = "good" if analysis["material_confidence"] >= 90 else "mid" if analysis["material_confidence"] >= 70 else "bad"
        st.markdown(
            f'<div class="gr-statusline">'
            f'<span class="gr-chip {cls}">Material {analysis["material_confidence"]}%</span>'
            f'<span class="gr-chip info">Room {analysis["room_confidence"]}%</span>'
            f'<span class="gr-chip info">Reference {analysis.get("local_visual_score",0)}%</span>'
            f'<span class="gr-chip info">{html.escape(analysis["material_method"])}</span>'
            f'</div>',
            unsafe_allow_html=True
        )

with right:
    with st.container(border=True, height=1120):
        st.subheader("Review + Correct")
        filename_key = f"main_filename_{idx}"

        st.markdown('<div class="gr-minihead">Original filename</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="gr-filename">{html.escape(item["name"])}</div>', unsafe_allow_html=True)

        st.markdown('<div class="gr-minihead">1 · Material</div>', unsafe_allow_html=True)
        stone_options = ["Needs Review"] + catalog["StoneType"].astype(str).tolist()
        current_stone = item.get("stone") if item.get("stone") in stone_options else "Needs Review"
        material_key = f"main_material_{idx}"
        if material_key not in st.session_state:
            st.session_state[material_key] = current_stone

        selected_stone = st.selectbox(
            "Choose material",
            stone_options,
            key=material_key,
            help="Type any part of the material name to search the full master list."
        )
        if selected_stone != current_stone:
            set_item_material(item, selected_stone, "Manual material selection", filename_key)
            st.rerun()

        current_swatch = candidate_swatch_bytes(item.get("sku","")) if item.get("sku") else None
        if current_swatch:
            sw1, sw2 = st.columns([.34,.66])
            with sw1:
                st.image(Image.open(io.BytesIO(current_swatch)), use_container_width=True)
            with sw2:
                st.markdown(
                    f'<div class="gr-current-choice"><strong>{html.escape(item["stone"] or "Needs Review")}</strong><br>'
                    f'<span class="gr-meta">SKU · {html.escape(item["sku"] or "NEED-SKU")} · '
                    f'{analysis["material_confidence"]}% confidence</span></div>',
                    unsafe_allow_html=True
                )
        else:
            st.markdown(
                '<div class="gr-meta" style="margin:.15rem 0 .4rem">No material thumbnail is available for this material yet.</div>',
                unsafe_allow_html=True
            )
            st.markdown(
                f'<div class="gr-current-choice"><strong>{html.escape(item["stone"] or "Needs Review")}</strong><br>'
                f'<span class="gr-meta">SKU · {html.escape(item["sku"] or "NEED-SKU")} · '
                f'{analysis["material_confidence"]}% confidence</span></div>',
                unsafe_allow_html=True
            )
            if item.get("sku"):
                thumb_sku = str(item.get("sku") or "").strip()
                thumb_key = f"material_thumb_upload_{idx}_{safe_id(thumb_sku)}"
                st.markdown('<div class="gr-minihead">Add material thumbnail</div>', unsafe_allow_html=True)
                uploaded_thumb = st.file_uploader(
                    "Add material thumbnail",
                    type=["jpg", "jpeg", "png", "webp"],
                    key=thumb_key,
                    label_visibility="collapsed",
                    help="Upload a slab/reference image for this material. It will appear anywhere this SKU needs a thumbnail, including the review page."
                )
                if uploaded_thumb is not None:
                    try:
                        st.image(uploaded_thumb, width=180)
                    except Exception:
                        pass
                    if st.button(
                        "SAVE MATERIAL THUMBNAIL",
                        key=f"save_material_thumb_{idx}_{safe_id(thumb_sku)}",
                        type="primary",
                        use_container_width=True
                    ):
                        try:
                            save_manual_material_thumbnail(thumb_sku, uploaded_thumb.getvalue())
                            candidate_swatch_bytes.clear()
                            st.success(f"Thumbnail saved for {item.get('stone') or thumb_sku}.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not save thumbnail: {e}")

        decision_a, decision_b = st.columns(2, gap="small")
        with decision_a:
            st.button(
                "CAN'T IDENTIFY · NEED INPUT",
                key=f"needs_input_{idx}",
                use_container_width=True,
                on_click=mark_main_needs_input,
                args=(idx, material_key, filename_key)
            )
        with decision_b:
            if st.button(
                "NEW MATERIAL",
                key=f"new_material_{idx}",
                use_container_width=True
            ):
                mark_item_new_material(item, filename_key)
                st.session_state[f"show_new_material_{idx}"] = True
                st.rerun()

        if item.get("decision_status") == "NEEDS_FURTHER_INPUT":
            st.warning("Marked for further input. Room and filename can still be corrected now.")
        elif item.get("decision_status") == "NEW_MATERIAL":
            st.info("This item is being treated as a new / uncataloged material.")

        material_candidates = analysis.get("material_candidates", [])[:5]
        if material_candidates:
            st.markdown('<div class="gr-minihead">Suggested material alternatives</div>', unsafe_allow_html=True)
            for j, c in enumerate(material_candidates):
                sw = candidate_swatch_bytes(c["sku"])
                with st.container(border=True):
                    c1, c2, c3 = st.columns([.22,.54,.24], gap="small")
                    with c1:
                        if sw:
                            st.image(Image.open(io.BytesIO(sw)), use_container_width=True)
                        else:
                            st.caption("No thumbnail")
                    with c2:
                        st.markdown(f"**{c['stone']}**")
                        st.caption(
                            f"{c['sku']} · {int(round(c['confidence']*100))}% · "
                            f"filename {int(round(c['filename']*100))}% · "
                            f"reference {int(round(c.get('local_visual',0)*100))}%"
                        )
                    with c3:
                        st.button(
                            "USE",
                            key=f"use_material_candidate_{idx}_{j}",
                            type="primary",
                            use_container_width=True,
                            on_click=set_state_value,
                            args=(material_key, c["stone"])
                        )

        show_new_material = st.session_state.get(f"show_new_material_{idx}", False) or item.get("decision_status") == "NEW_MATERIAL"
        with st.expander("New / uncataloged material details", expanded=show_new_material):
            cm1, cm2 = st.columns([1.2,.8])
            custom_material_key = f"custom_material_{idx}"
            custom_sku_key = f"custom_sku_{idx}"
            show_key = f"show_new_material_{idx}"
            notice_key = f"custom_material_notice_{idx}"
            cm1.text_input("New material name", key=custom_material_key, placeholder="Material name")
            cm2.text_input("Base SKU", key=custom_sku_key, placeholder="Leave blank if unknown")
            st.button(
                "SAVE NEW MATERIAL",
                key=f"use_custom_material_{idx}",
                type="primary",
                use_container_width=True,
                on_click=save_main_custom_material,
                args=(idx, material_key, custom_material_key, custom_sku_key, show_key, notice_key)
            )
            if st.session_state.get(notice_key):
                st.warning(st.session_state[notice_key])

        st.markdown('<div class="gr-minihead">2 · Room type</div>', unsafe_allow_html=True)
        room_key = f"main_room_{idx}"
        current_room = item.get("room") if item.get("room") in ROOM_TYPES else "Other"
        if room_key not in st.session_state:
            st.session_state[room_key] = current_room

        selected_room = st.selectbox("Choose room type", ROOM_TYPES, key=room_key)
        if selected_room == "Other":
            custom_room = st.text_input(
                "Custom room type",
                value=item.get("manual_custom_room", ""),
                key=f"main_custom_room_{idx}",
                placeholder="e.g. ReceptionLounge"
            )
            corrected_room = re.sub(r"[^A-Za-z0-9]+", "", custom_room.strip()) or "Other"
        else:
            corrected_room = selected_room

        if corrected_room != item.get("room"):
            item["manual_custom_room"] = custom_room if selected_room == "Other" else ""
            set_item_room(item, corrected_room, filename_key)
            st.rerun()

        room_candidates = [
            rc for rc in analysis.get("room_candidates", [])[:4]
            if rc.get("room") and rc.get("room") != item.get("room")
        ]
        if room_candidates:
            st.markdown('<div class="gr-minihead">Suggested room alternatives</div>', unsafe_allow_html=True)
            rcols = st.columns(min(len(room_candidates),3))
            for j, rc in enumerate(room_candidates):
                with rcols[j % len(rcols)]:
                    st.button(
                        rc["room"],
                        key=f"use_room_candidate_{idx}_{j}",
                        use_container_width=True,
                        on_click=set_state_value,
                        args=(room_key, rc["room"] if rc["room"] in ROOM_TYPES else "Other")
                    )

        st.markdown('<div class="gr-minihead">3 · Output filename</div>', unsafe_allow_html=True)
        if item.get("decision_status") == "NEEDS_FURTHER_INPUT":
            generated_name = safe_filename(f"NEED-INPUT-{item.get('room') or 'Other'}") + (item.get("ext") or ".jpg")
        else:
            generated_name = item.get("generated_name") or proposed_filename(
                item.get("sku") or "NEED-SKU",
                item.get("stone") or "UnknownMaterial",
                item.get("room") or "Other",
                item.get("ext") or ".jpg"
            )
        generated_key = f"main_generated_{idx}"
        previous_generated = st.session_state.get(generated_key)
        if filename_key not in st.session_state:
            st.session_state[filename_key] = item.get("custom_filename") if item.get("manual_filename") else (item.get("new_name") or generated_name)
        elif previous_generated is not None and previous_generated != generated_name and not item.get("manual_filename"):
            st.session_state[filename_key] = generated_name
        st.session_state[generated_key] = generated_name

        edited_filename = st.text_input(
            "Editable output filename",
            key=filename_key,
            label_visibility="collapsed",
            help="AUTO follows Material/SKU/Room. Editing this field switches it to MANUAL until Reset Name.",
            on_change=mark_main_filename_manual,
            args=(idx, filename_key)
        )
        cleaned_filename = normalize_output_filename(edited_filename, item.get("ext") or ".jpg") or generated_name
        if item.get("manual_filename"):
            item["custom_filename"] = cleaned_filename
            item["new_name"] = cleaned_filename
        else:
            item.pop("custom_filename", None)
            item["new_name"] = generated_name

        out1, out2 = st.columns([.62,.38])
        with out1:
            mode = "MANUAL" if item.get("manual_filename") else "AUTO"
            st.caption(f"{mode} · {item['stone'] or 'Needs Review'} / {item['room']}")
        with out2:
            st.button(
                "RESET NAME",
                key=f"reset_filename_{idx}",
                use_container_width=True,
                on_click=reset_main_filename,
                args=(idx, filename_key, generated_key)
            )

        st.markdown('<div class="gr-minihead">4 · Analyst note</div>', unsafe_allow_html=True)
        analyst_note_key = f"main_analyst_note_{idx}"
        if analyst_note_key not in st.session_state:
            st.session_state[analyst_note_key] = str(item.get("analyst_note", "") or "")
        item["analyst_note"] = st.text_area(
            "Analyst note",
            key=analyst_note_key,
            label_visibility="collapsed",
            placeholder="Optional note for Cyndi — alternate slab candidates, uncertainty, context, etc.",
            height=90
        )

        with st.expander("Why did it choose this?", expanded=False):
            st.write(f"Filename evidence: **{analysis['filename_material_score']}%**")
            st.write(f"Immediate GENROSE reference: **{analysis.get('local_visual_score',0)}%**")
            st.write(f"Google Product Search: **{analysis['visual_score']}%**")
            st.write(f"Room confidence: **{analysis['room_confidence']}%**")
            st.caption(analysis["room_method"])

        with st.expander("Advanced diagnostics", expanded=False):
            v = analysis["vision"]
            if v.get("error"):
                st.warning("Google Cloud Vision did not run for this image.")
                st.code(v["error"][:500])
            st.write("Best guess:", ", ".join(v.get("best_guess", [])[:10]) or "—")
            st.write("Labels:", ", ".join(v.get("labels", [])[:20]) or "—")
            st.write("Objects:", ", ".join(v.get("objects", [])[:20]) or "—")

st.markdown('<div class="gr-rule"></div>', unsafe_allow_html=True)
st.markdown('<div class="gr-stephead"><div class="gr-step-left"><span class="gr-step-no">04</span><div><div class="gr-step-title">Export</div><div class="gr-step-sub">Download the corrected naming table for production.</div></div></div></div>', unsafe_allow_html=True)
latest_summary_df = pd.DataFrame([{
    "Old Filename": x["name"],
    "New Filename": x["new_name"],
    "Material": x["stone"] or "Needs Review",
    "SKU": x["sku"] or "NEED-SKU",
    "Room": x["room"],
    "Decision Status": x.get("decision_status",""),
    "Material Confidence": f'{x["analysis"]["material_confidence"]}%',
    "Room Confidence": f'{x["analysis"]["room_confidence"]}%',
    "Method": x["analysis"]["material_method"],
    "Website Verified": "YES" if x["analysis"].get("website_verified") else "NO",
    "Analyst Note": x.get("analyst_note", ""),
    "Reviewer Note": x.get("reviewer_note", "")
} for x in results])
st.dataframe(latest_summary_df, use_container_width=True, hide_index=True)

blockers, preflight_warnings = preflight_results(results)
st.markdown('<div class="gr-minihead">Production preflight</div>', unsafe_allow_html=True)
if blockers:
    st.error(f"{len(blockers)} blocking issue(s) must be resolved before renamed-image export.")
    for issue in blockers[:10]:
        st.caption(f"• {issue}")
    if len(blockers) > 10:
        st.caption(f"…and {len(blockers)-10} more blocker(s).")
else:
    st.success("Preflight passed · filenames are unique and every scene has a material + SKU.")
if preflight_warnings:
    with st.expander(f"Preflight warnings ({len(preflight_warnings)})", expanded=False):
        for issue in preflight_warnings:
            st.write(f"• {issue}")

ex1, ex2 = st.columns(2, gap="small")
with ex1:
    st.download_button(
        "DOWNLOAD CURRENT CSV",
        latest_summary_df.to_csv(index=False).encode("utf-8-sig"),
        "room_scene_analysis_current.csv",
        "text/csv",
        use_container_width=True
    )
with ex2:
    if blockers:
        st.button("DOWNLOAD RENAMED IMAGES ZIP", disabled=True, use_container_width=True, help="Resolve preflight blockers first.")
    else:
        st.download_button(
            "DOWNLOAD RENAMED IMAGES ZIP",
            renamed_images_zip(results),
            "room_scenes_renamed.zip",
            "application/zip",
            use_container_width=True
        )

