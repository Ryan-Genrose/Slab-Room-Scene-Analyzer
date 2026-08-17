
import base64
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
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image

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

GENROSE_INDEX = "https://www.genrose.com/Products/natural-stone-slabs/"
REVIEW_EMAIL = "marketing@genrose.com"

ROOM_TYPES = [
    "Kitchen", "KitchenCounter", "KitchenIsland", "LivingRoom", "Bathroom", "PowderRoom",
    "PrimaryBathroom", "Shower", "Vanity", "Fireplace", "MudRoom", "Entryway", "Foyer",
    "DiningRoom", "Bar", "LaundryRoom", "Bedroom", "Office", "FeatureWall", "Exterior", "Other"
]

# Filename room-language aliases. These are intentionally explicit rather than translating
# the entire filename because many stone names are themselves Italian.
ROOM_ALIASES = {
    "Kitchen": [
        "kitchen", "cucina", "cucine"
    ],
    "KitchenCounter": [
        "kitchen counter", "kitchen countertop", "countertop", "counter top", "piano cucina"
    ],
    "KitchenIsland": [
        "kitchen island", "island", "isola cucina", "isola"
    ],
    "LivingRoom": [
        "living room", "family room", "soggiorno", "salotto", "zona giorno"
    ],
    "Bathroom": [
        "bathroom", "bath", "bagno", "bagni", "stanza da bagno", "vasca"
    ],
    "PowderRoom": [
        "powder room", "powderroom", "half bath", "toilette", "bagno ospiti"
    ],
    "PrimaryBathroom": [
        "primary bathroom", "master bathroom", "primary bath", "master bath", "bagno padronale"
    ],
    "Shower": [
        "shower", "doccia", "box doccia"
    ],
    "Vanity": [
        "vanity", "bath vanity", "mobile bagno", "lavabo"
    ],
    "Fireplace": [
        "fireplace", "hearth", "mantel", "camino", "caminetto"
    ],
    "MudRoom": [
        "mudroom", "mud room", "ingresso di servizio"
    ],
    "Entryway": [
        "entryway", "entry way", "entrance", "ingresso"
    ],
    "Foyer": [
        "foyer", "atrio"
    ],
    "DiningRoom": [
        "dining room", "dining", "sala da pranzo"
    ],
    "Bar": [
        "wet bar", "home bar", "bar"
    ],
    "LaundryRoom": [
        "laundry room", "laundry", "lavanderia"
    ],
    "Bedroom": [
        "bedroom", "camera da letto", "camera letto"
    ],
    "Office": [
        "office", "home office", "ufficio", "studio"
    ],
    "FeatureWall": [
        "feature wall", "accent wall", "wall cladding", "parete", "rivestimento parete"
    ],
    "Exterior": [
        "exterior", "outdoor", "patio", "facade", "facciata", "esterno", "esterni"
    ],
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
            "labels": [], "web_entities": [], "web_pages": [], "text": "",
            "error": "Vision not configured"
        }

    try:
        client = image_client()
        image = vision.Image(content=image_bytes)
        features = [
            vision.Feature(type_=vision.Feature.Type.LABEL_DETECTION, max_results=20),
            vision.Feature(type_=vision.Feature.Type.WEB_DETECTION, max_results=20),
            vision.Feature(type_=vision.Feature.Type.TEXT_DETECTION, max_results=5),
        ]
        req = vision.AnnotateImageRequest(image=image, features=features)
        resp = client.batch_annotate_images(requests=[req]).responses[0]

        if resp.error.message:
            return {
                "labels": [], "web_entities": [], "web_pages": [], "text": "",
                "error": resp.error.message
            }

        labels = [x.description for x in resp.label_annotations]
        web_entities = [x.description for x in resp.web_detection.web_entities if x.description]
        web_pages = [x.url for x in resp.web_detection.pages_with_matching_images if x.url][:10]
        text = resp.text_annotations[0].description if resp.text_annotations else ""
        return {
            "labels": labels,
            "web_entities": web_entities,
            "web_pages": web_pages,
            "text": text,
            "error": ""
        }
    except Exception as e:
        return {
            "labels": [], "web_entities": [], "web_pages": [], "text": "",
            "error": str(e)
        }

def run_product_search(image_bytes, limit=8):
    """Visual similarity against the website-built reference catalog."""
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
            "score": float(result.score),
            "source": "Google Product Search",
            "reference_image": result.image
        })
    return results

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
                vision.Product.KeyValue(key="stone", value=str(record["StoneType"]))
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

def create_reference_for_url(record, image_url, slot):
    if not (storage_ready() and product_search_ready()):
        raise RuntimeError("Google Storage + Product Search must be configured.")

    image_bytes, ctype = download_reference_image(image_url)
    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    bucket_name = secret("GOOGLE_CLOUD_BUCKET", "")
    obj = f"website_references/{record['SKU']}/ref-{slot:02d}{ext}"
    blob = storage_client().bucket(bucket_name).blob(obj)
    blob.upload_from_string(image_bytes, content_type=ctype)

    product_path = ensure_product(record)
    ref_id = f"website-{slot:02d}"
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
        # It may already exist with an older image. Keep the stored reference object for review display.
        if "already exists" not in str(e).lower():
            raise
    return obj

def sync_one_website_record(record, links, build_visual):
    link, link_score = best_collection_link(record, links)
    if not link or link_score < .62:
        return record["SKU"], {
            "status": "NO_PAGE_MATCH",
            "stone": record["StoneType"],
            "sku": record["SKU"],
            "page_url": "",
            "page_match_score": link_score,
            "image_urls": [],
            "reference_objects": [],
            "website_skus": []
        }

    page_html = fetch_html(link["url"])
    images = extract_page_images(link["url"], page_html, record["Source Color Name"])
    website_skus = extract_skus(page_html)
    refs = []

    if build_visual:
        # Use three strongest website images as Product Search references.
        for slot, img in enumerate(images[:3], start=1):
            try:
                refs.append(create_reference_for_url(record, img["url"], slot))
            except Exception as e:
                refs.append(f"ERROR:{str(e)[:140]}")

    return record["SKU"], {
        "status": "OK",
        "stone": record["StoneType"],
        "sku": record["SKU"],
        "page_url": link["url"],
        "page_label": link["label"],
        "page_match_score": link_score,
        "image_urls": [x["url"] for x in images[:3]],
        "reference_objects": refs,
        "website_skus": website_skus
    }

def sync_genrose_website(build_visual=True, max_workers=6):
    index_html = fetch_html(GENROSE_INDEX)
    links = extract_collection_links(index_html)
    cache = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "source": GENROSE_INDEX,
        "materials": {}
    }

    progress = st.progress(0, text="Reading GENROSE slab catalog…")
    status = st.empty()
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(sync_one_website_record, r, links, build_visual): r
            for r in catalog_records
        }
        for future in as_completed(futures):
            r = futures[future]
            try:
                sku, entry = future.result()
            except Exception as e:
                sku = str(r["SKU"])
                entry = {
                    "status": "ERROR", "stone": r["StoneType"], "sku": sku,
                    "page_url": "", "image_urls": [], "reference_objects": [],
                    "website_skus": [], "error": str(e)[:300]
                }
            cache["materials"][sku] = entry
            done += 1
            progress.progress(done / len(catalog_records), text=f"Synced {done}/{len(catalog_records)} materials")
            if done % 12 == 0:
                status.caption(f"Latest: {entry.get('stone')} — {entry.get('status')}")
    save_website_cache(cache)
    progress.empty()
    status.empty()
    return cache

def website_entry_for_sku(sku):
    return load_website_cache().get("materials", {}).get(str(sku), {})

def website_reference_bytes(sku):
    entry = website_entry_for_sku(sku)
    refs = [x for x in entry.get("reference_objects", []) if x and not str(x).startswith("ERROR:")]
    if refs and storage_ready():
        try:
            return storage_client().bucket(secret("GOOGLE_CLOUD_BUCKET", "")).blob(refs[0]).download_as_bytes()
        except Exception:
            pass
    # Fallback to live website image for display.
    urls = entry.get("image_urls", [])
    if urls:
        try:
            return download_reference_image(urls[0])[0]
        except Exception:
            pass
    return None

# ---------------- analysis hierarchy ----------------

def candidate_by_stone(candidates, stone):
    target = compact(stone)
    for c in candidates:
        if compact(c["stone"]) == target:
            return c
    return None

def combine_material_evidence(filename_candidates, web_candidates, product_results, website_cache):
    # Build a union keyed by SKU.
    union = {}
    for c in filename_candidates:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0
        })
        union[c["sku"]]["filename"] = max(union[c["sku"]]["filename"], c["score"])
    for c in web_candidates:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0
        })
        union[c["sku"]]["vision_text"] = max(union[c["sku"]]["vision_text"], c["score"])
    for c in product_results:
        union.setdefault(c["sku"], {
            "stone": c["stone"], "sku": c["sku"], "source_name": c["source_name"],
            "filename": 0.0, "vision_text": 0.0, "visual": 0.0
        })
        union[c["sku"]]["visual"] = max(union[c["sku"]]["visual"], c["score"])

    results = []
    website_materials = website_cache.get("materials", {})
    for sku, x in union.items():
        f = x["filename"]
        vt = x["vision_text"]
        vis = x["visual"]
        web_verified = website_materials.get(str(sku), {}).get("status") == "OK"

        # Hierarchy:
        # filename is authoritative when strong;
        # website confirms canonical product/SKU;
        # Product Search is a fallback/secondary check;
        # Vision web labels/OCR are last-ditch textual evidence.
        if f >= .93:
            final = .88 * f + (.06 if web_verified else 0) + .06 * vt
            method = "Filename + website verification" if web_verified else "Filename"
            if vis >= .55:
                final = min(.995, final + .03)
                method += " + visual confirmation"
        elif f >= .70:
            if vis > 0:
                final = .56 * f + .32 * vis + .07 * vt + (.05 if web_verified else 0)
                method = "Filename + Google visual"
            else:
                final = .78 * f + .14 * vt + (.08 if web_verified else 0)
                method = "Filename + Google Vision clues"
        else:
            if vis > 0:
                final = .72 * vis + .20 * vt + .08 * f
                method = "Google visual fallback"
            else:
                final = max(.62 * vt + .18 * f, f)
                method = "Google Vision fallback"

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
    vision_room = detect_room_from_text(
        " ".join(vision_data.get("labels", []) + vision_data.get("web_entities", []) + [vision_data.get("text", "")])
    )
    vision_materials = vision_text_material_candidates(vision_data, 8)

    top_filename = filename_materials[0]["score"] if filename_materials else 0
    # Visual product matching is expensive/weak on full room scenes, so use it as fallback,
    # or as a confirmation when filename material evidence isn't excellent.
    product_results = []
    if top_filename < .93 and product_search_ready():
        try:
            product_results = run_product_search(image_bytes, 8)
        except Exception as e:
            vision_data["product_search_error"] = str(e)

    website_cache = load_website_cache()
    material_ranked = combine_material_evidence(
        filename_materials, vision_materials, product_results, website_cache
    )
    best = material_ranked[0] if material_ranked else {
        "stone": "", "sku": "", "confidence": 0, "method": "Unmatched",
        "filename": 0, "vision_text": 0, "visual": 0, "website_verified": False
    }

    # Room hierarchy: filename first, Vision only if absent.
    if filename_room["room"] != "Other":
        room = filename_room["room"]
        room_conf = filename_room["score"]
        room_method = f'Filename / translated term: "{filename_room["matched_term"]}"'
    elif vision_room["room"] != "Other":
        room = vision_room["room"]
        room_conf = min(.91, max(.68, vision_room["score"] * .88))
        room_method = "Google Vision room clues"
    else:
        room = "Other"
        room_conf = .22
        room_method = "No confident room clue"

    return {
        "stone": best["stone"],
        "sku": best["sku"],
        "material_confidence": int(round(best["confidence"] * 100)),
        "material_method": best["method"],
        "filename_material_score": int(round(best["filename"] * 100)),
        "vision_text_score": int(round(best["vision_text"] * 100)),
        "visual_score": int(round(best["visual"] * 100)),
        "website_verified": bool(best["website_verified"]),
        "room": room,
        "room_confidence": int(round(room_conf * 100)),
        "room_method": room_method,
        "material_candidates": material_ranked,
        "vision": vision_data,
    }

# ---------------- review persistence ----------------

def app_base_url():
    return str(secret("APP_BASE_URL", "http://localhost:8501")).rstrip("/")

def save_review_batch(items):
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    payload = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
                "material_confidence": item["analysis"]["material_confidence"],
                "room_confidence": item["analysis"]["room_confidence"],
                "material_method": item["analysis"]["material_method"],
                "room_method": item["analysis"]["room_method"],
                "website_url": website_entry_for_sku(item["sku"]).get("page_url", "")
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
                "material_confidence": item["analysis"]["material_confidence"],
                "room_confidence": item["analysis"]["room_confidence"],
                "material_method": item["analysis"]["material_method"],
                "room_method": item["analysis"]["room_method"],
                "website_url": website_entry_for_sku(item["sku"]).get("page_url", "")
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
        (LOCAL_REVIEW_ROOT / batch_id / "submission.json").write_text(
            json.dumps(submission, indent=2), encoding="utf-8"
        )

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

# ---------------- review page ----------------

def proposed_filename(sku, stone, room, ext=".jpg"):
    return safe_filename(f"{sku}-{stone}-{room}") + ext.lower()

def render_review_page(batch_id):
    batch = load_review_batch(batch_id)
    if not batch:
        st.error("That review batch couldn't be found.")
        st.stop()

    st.title("GENROSE Room Scene Approval")
    st.caption("Approve the proposed matches, override a material/room, or add a new material. Then submit once.")

    decisions = []
    stone_options = catalog["StoneType"].astype(str).tolist()

    for i, item in enumerate(batch["items"]):
        with st.container(border=True):
            image_col, info_col, swatch_col = st.columns([1.15, 1.55, .8], gap="large")

            with image_col:
                b = review_image_bytes(batch_id, item)
                if b:
                    st.image(Image.open(io.BytesIO(b)), use_container_width=True)
                st.caption(item["old_name"])

            with info_col:
                conf = int(item["material_confidence"])
                st.markdown(f"### {item['stone']} · `{item['sku']}`")
                st.write(f"**Old filename:** `{item['old_name']}`")
                st.write(f"**Suggested filename:** `{item['new_name']}`")
                st.write(f"**Material confidence:** {conf}%")
                st.write(f"**Room:** {item['room']} ({item['room_confidence']}%)")
                st.caption(f"Material: {item['material_method']} · Room: {item['room_method']}")

                default_approved = conf >= 90 and int(item["room_confidence"]) >= 70
                approved = st.checkbox(
                    "Approve this line item",
                    value=default_approved,
                    key=f"approve_{i}"
                )

                choice = st.selectbox(
                    "Material override",
                    ["— Keep proposed —"] + stone_options + ["➕ Add a new material"],
                    key=f"material_override_{i}"
                )

                new_material = ""
                final_stone = item["stone"]
                final_sku = item["sku"]

                if choice == "➕ Add a new material":
                    new_material = st.text_input(
                        "New material name",
                        key=f"new_material_{i}",
                        placeholder="Exact new material name"
                    )
                    final_stone = new_material.strip() or "NEW-MATERIAL"
                    final_sku = "NEED-SKU"
                elif choice != "— Keep proposed —":
                    rec = row_for_stone(choice)
                    final_stone = rec["StoneType"]
                    final_sku = rec["SKU"]

                room_choice = st.selectbox(
                    "Room override",
                    ROOM_TYPES,
                    index=ROOM_TYPES.index(item["room"]) if item["room"] in ROOM_TYPES else ROOM_TYPES.index("Other"),
                    key=f"room_override_{i}"
                )
                custom_room = ""
                if room_choice == "Other":
                    custom_room = st.text_input(
                        "Custom room type",
                        key=f"custom_room_{i}",
                        placeholder="e.g. WineCellar"
                    )
                final_room = re.sub(r"[^A-Za-z0-9]+", "", custom_room) or "Other" if room_choice == "Other" else room_choice
                ext = Path(item["old_name"]).suffix.lower() or ".jpg"
                final_filename = proposed_filename(final_sku, final_stone, final_room, ext)

                st.markdown("**Final filename if submitted**")
                st.code(final_filename)

                notes = st.text_input(
                    "Notes",
                    key=f"notes_{i}",
                    placeholder="Optional correction / note"
                )

            with swatch_col:
                st.markdown("**Website reference**")
                sw = website_reference_bytes(item["sku"])
                if sw:
                    st.image(Image.open(io.BytesIO(sw)), use_container_width=True)
                else:
                    st.info("No website reference cached.")
                if item.get("website_url"):
                    st.link_button("GENROSE product page", item["website_url"])

            decisions.append({
                "old_filename": item["old_name"],
                "suggested_filename": item["new_name"],
                "final_filename": final_filename,
                "suggested_material": item["stone"],
                "final_material": final_stone,
                "final_sku": final_sku,
                "final_room": final_room,
                "material_confidence": item["material_confidence"],
                "room_confidence": item["room_confidence"],
                "approved": approved,
                "added_new_material": choice == "➕ Add a new material",
                "notes": notes
            })

    reviewer = st.text_input("Reviewer name", placeholder="Name")
    if st.button("SUBMIT REVIEW TO MARKETING", type="primary", use_container_width=True):
        if any(d["added_new_material"] and d["final_material"] in {"", "NEW-MATERIAL"} for d in decisions):
            st.error("Enter a material name for every item marked Add a new material.")
            st.stop()

        submission = {"batch_id": batch_id, "reviewer": reviewer, "decisions": decisions}
        save_submission(batch_id, submission)

        html_rows = []
        text_rows = []
        for d in decisions:
            status = "APPROVED" if d["approved"] else "NOT APPROVED"
            html_rows.append(
                "<tr>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{html.escape(d['old_filename'])}</td>"
                f"<td>{html.escape(d['final_filename'])}</td>"
                f"<td>{html.escape(d['suggested_material'])}</td>"
                f"<td>{html.escape(d['final_material'])}</td>"
                f"<td>{html.escape(d['final_sku'])}</td>"
                f"<td>{html.escape(d['final_room'])}</td>"
                f"<td>{d['material_confidence']}%</td>"
                f"<td>{html.escape(d['notes'])}</td>"
                "</tr>"
            )
            text_rows.append(
                f"{status} | OLD={d['old_filename']} | NEW={d['final_filename']} | "
                f"MATERIAL={d['final_material']} | SKU={d['final_sku']} | ROOM={d['final_room']} | "
                f"CONF={d['material_confidence']}% | NOTES={d['notes']}"
            )

        subject = f"Room Scene Review — {batch_id}"
        html_body = (
            f"<h2>{html.escape(subject)}</h2><p>Reviewer: {html.escape(reviewer or 'Not supplied')}</p>"
            "<table border='1' cellpadding='6' cellspacing='0'><thead><tr>"
            "<th>Status</th><th>Old filename</th><th>Final filename</th><th>Suggested material</th>"
            "<th>Final material</th><th>SKU</th><th>Room</th><th>Confidence</th><th>Notes</th>"
            "</tr></thead><tbody>" + "".join(html_rows) + "</tbody></table>"
        )
        text_body = subject + f"\nReviewer: {reviewer}\n\n" + "\n".join(text_rows)

        try:
            send_review_email(subject, html_body, text_body)
            st.success(f"Submitted and emailed to {REVIEW_EMAIL}.")
        except Exception as e:
            st.warning(f"Review was saved, but email failed: {e}")
        st.stop()

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

st.markdown("""
<style>
:root{
  --ink:#f7f5ef;
  --muted:#a9b3bf;
  --faint:#768392;
  --bg:#080d13;
  --panel:#0f1720;
  --panel2:#121d28;
  --panel3:#172432;
  --line:#26384a;
  --line2:#334b60;
  --acid:#c8ff36;
  --acid-soft:#23320d;
  --blue:#6fc7ff;
  --good:#7be2a8;
  --warn:#ffc857;
  --bad:#ff6b72;
}

html,body,[data-testid="stAppViewContainer"]{
  background:var(--bg)!important;
  color:var(--ink)!important;
}
[data-testid="stAppViewContainer"]>.main{
  background:
    radial-gradient(900px 420px at 72% -10%,rgba(104,198,255,.09),transparent 60%),
    radial-gradient(850px 420px at 20% -15%,rgba(200,255,54,.08),transparent 62%),
    var(--bg)!important;
}
[data-testid="stHeader"]{background:transparent!important;}
#MainMenu{visibility:hidden;}
footer{visibility:hidden;}
.block-container{
  max-width:1760px!important;
  padding:2.0rem 2.25rem 4rem!important;
}
[data-testid="stSidebar"]{
  background:#0a1118!important;
  border-right:1px solid var(--line)!important;
}
[data-testid="stSidebar"] *{color:var(--ink)!important;}

/* Typography */
h1,h2,h3,h4,p,span,label,div{color:var(--ink);}
h1{
  font-size:2.65rem!important;
  line-height:1.03!important;
  letter-spacing:-.045em!important;
  margin:.1rem 0 .55rem!important;
}
h2{
  font-size:1.34rem!important;
  letter-spacing:-.025em!important;
}
h3{font-size:1.03rem!important;}
small,[data-testid="stCaptionContainer"],.stCaption{
  color:var(--muted)!important;
}
[data-testid="stMarkdownContainer"] p{color:#dbe2e9;}
code{
  color:#e9f6ff!important;
  background:#0a1119!important;
}

/* Upload */
[data-testid="stFileUploader"]{
  background:linear-gradient(180deg,#101923 0%,#0c141c 100%)!important;
  border:1px dashed #456078!important;
  border-radius:18px!important;
  padding:.45rem!important;
  box-shadow:0 16px 50px rgba(0,0,0,.15);
}
[data-testid="stFileUploader"]:hover{border-color:#8bad5a!important;}
[data-testid="stFileUploader"] section{
  background:transparent!important;
  border:0!important;
}
[data-testid="stFileUploaderFile"]{
  background:#172432!important;
  border:1px solid #293e53!important;
  border-radius:10px!important;
}
[data-testid="stFileUploaderFile"] *{color:#eef3f7!important;}

/* Metrics */
[data-testid="stMetric"]{
  background:linear-gradient(180deg,#111b25,#0d151e)!important;
  border:1px solid var(--line)!important;
  border-radius:16px!important;
  padding:15px 17px!important;
  min-height:95px;
  box-shadow:0 10px 35px rgba(0,0,0,.12);
}
[data-testid="stMetricLabel"]{
  color:var(--muted)!important;
  font-weight:700!important;
  letter-spacing:.02em;
}
[data-testid="stMetricValue"]{
  color:var(--ink)!important;
  font-size:1.95rem!important;
  font-weight:900!important;
  letter-spacing:-.035em!important;
}

/* Inputs */
[data-testid="stTextInput"] input,
[data-baseweb="select"]>div{
  background:#0b121a!important;
  color:var(--ink)!important;
  border:1px solid var(--line2)!important;
  border-radius:10px!important;
  min-height:43px!important;
}
[data-testid="stTextInput"] input::placeholder{color:#697788!important;}
[data-baseweb="popover"],[role="listbox"]{
  background:#111a24!important;
  color:var(--ink)!important;
}

/* Buttons */
.stButton>button,.stDownloadButton>button,[data-testid="stLinkButton"] a{
  border-radius:10px!important;
  min-height:44px!important;
  font-weight:850!important;
  letter-spacing:.005em!important;
  transition:all .14s ease!important;
}
.stButton>button[kind="primary"]{
  background:var(--acid)!important;
  color:#071008!important;
  border:1px solid var(--acid)!important;
  box-shadow:0 7px 26px rgba(200,255,54,.13)!important;
}
.stButton>button[kind="primary"] p{
  color:#071008!important;
}
.stButton>button[kind="secondary"],
.stDownloadButton>button{
  background:#121c27!important;
  color:var(--ink)!important;
  border:1px solid #304559!important;
}
.stButton>button[kind="secondary"] p,
.stDownloadButton>button p{
  color:var(--ink)!important;
}
.stButton>button:hover,.stDownloadButton>button:hover{
  transform:translateY(-1px);
  border-color:#69809a!important;
}
.stButton>button[kind="primary"]:hover{
  background:#d5ff62!important;
  border-color:#d5ff62!important;
}

/* Bordered containers become dark cards */
[data-testid="stVerticalBlockBorderWrapper"]{
  background:linear-gradient(180deg,#101923,#0c141c)!important;
  border:1px solid var(--line)!important;
  border-radius:16px!important;
  box-shadow:0 14px 40px rgba(0,0,0,.13)!important;
}
[data-testid="stVerticalBlockBorderWrapper"]>div{
  border-radius:16px!important;
}

/* Expanders */
[data-testid="stExpander"]{
  background:#0f1822!important;
  border:1px solid var(--line)!important;
  border-radius:12px!important;
  overflow:hidden!important;
}
[data-testid="stExpander"] summary{
  color:var(--ink)!important;
  font-weight:800!important;
}
[data-testid="stExpander"] svg{fill:var(--muted)!important;}

/* Tables */
[data-testid="stDataFrame"]{
  border:1px solid var(--line)!important;
  border-radius:14px!important;
  overflow:hidden!important;
}

/* Images */
[data-testid="stImage"] img{
  border-radius:16px!important;
  border:1px solid #25384a!important;
  box-shadow:0 18px 60px rgba(0,0,0,.26);
}

/* Custom design classes */
.ds-eyebrow{
  color:var(--acid)!important;
  font-size:.73rem;
  font-weight:950;
  text-transform:uppercase;
  letter-spacing:.20em;
  margin-bottom:.55rem;
}
.ds-lede{
  color:var(--muted)!important;
  font-size:1.02rem;
  max-width:980px;
  margin:0 0 1.55rem;
}
.ds-step{
  color:#91a4b7!important;
  font-size:.72rem;
  font-weight:950;
  text-transform:uppercase;
  letter-spacing:.16em;
  margin:.2rem 0 .7rem;
}
.ds-divider{
  height:1px;
  background:linear-gradient(90deg,var(--line),transparent);
  margin:1.35rem 0 1.4rem;
}
.ds-chip{
  display:inline-flex;
  align-items:center;
  padding:5px 9px;
  margin:0 5px 6px 0;
  border-radius:999px;
  font-size:12px;
  line-height:1;
  font-weight:900;
  border:1px solid transparent;
}
.ds-chip.good{background:#133427;color:#a8f0c4;border-color:#2c674a}
.ds-chip.mid{background:#3d2f13;color:#ffe098;border-color:#6c5225}
.ds-chip.bad{background:#3b1b20;color:#ffb5ba;border-color:#6d333a}
.ds-chip.info{background:#102a3b;color:#b5ddf8;border-color:#27536d}
.ds-titleline{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:16px;
  margin-bottom:12px;
}
.ds-material{
  font-size:1.65rem;
  line-height:1.05;
  font-weight:950;
  letter-spacing:-.035em;
  color:var(--ink)!important;
}
.ds-meta{
  color:var(--muted)!important;
  font-size:.88rem;
  margin-top:5px;
}
.ds-filename{
  color:#e9f5ff!important;
  background:#070c12;
  border:1px solid #2d4154;
  border-radius:10px;
  padding:11px 12px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.94rem;
  word-break:break-word;
}
.ds-label{
  color:#93a8bb!important;
  text-transform:uppercase;
  letter-spacing:.13em;
  font-size:.69rem;
  font-weight:950;
  margin:1rem 0 .45rem;
}
.ds-note{
  color:var(--muted)!important;
  background:#0b131c;
  border:1px solid #223548;
  border-radius:10px;
  padding:10px 11px;
  font-size:.87rem;
}
.ds-empty{
  min-height:150px;
  display:flex;
  align-items:center;
  justify-content:center;
  text-align:center;
  color:#788899!important;
  border:1px dashed #31485d;
  background:#0b131c;
  border-radius:14px;
}
.ds-evidence{
  display:grid;
  grid-template-columns:1fr auto;
  gap:8px 12px;
  align-items:center;
  margin:.35rem 0;
}
.ds-evidence span:first-child{color:#bcc6d0!important;font-size:.9rem;}
.ds-evidence strong{color:var(--ink)!important;}
.ds-statusline{
  display:flex;
  flex-wrap:wrap;
  gap:6px;
  margin:.25rem 0 .4rem;
}

/* Queue buttons: readable dark cards */
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="secondary"]{
  width:100%;
  justify-content:flex-start!important;
  text-align:left!important;
  background:#101a24!important;
  border:1px solid #293e52!important;
  min-height:58px!important;
  padding:.55rem .7rem!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="secondary"] p{
  color:#edf3f8!important;
  font-size:.84rem!important;
  line-height:1.25!important;
  white-space:normal!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="primary"]{
  width:100%;
  justify-content:flex-start!important;
  text-align:left!important;
  background:#1b2a12!important;
  border:1px solid #66872e!important;
  min-height:58px!important;
  padding:.55rem .7rem!important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton>button[kind="primary"] p{
  color:#eaffbd!important;
  font-size:.84rem!important;
  line-height:1.25!important;
  white-space:normal!important;
}

/* progress */
[data-testid="stProgress"]>div>div{background:var(--acid)!important;}

@media(max-width:1100px){
  .block-container{padding:1.2rem!important;}
  h1{font-size:2.05rem!important;}
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="ds-eyebrow">GENROSE · INTERNAL IMAGE OPERATIONS</div>', unsafe_allow_html=True)
st.title("Room Scene Analyzer")
st.markdown(
    '<div class="ds-lede">Upload manufacturer room scenes and get a clean proposed filename, material, SKU, and room type. '
    'Filename intelligence runs first; GENROSE references and Google Vision only step in when needed.</div>',
    unsafe_allow_html=True
)
website_cache = load_website_cache()
website_count = sum(1 for x in website_cache.get("materials", {}).values() if x.get("status") == "OK")

with st.sidebar:
    st.header("Admin")
    if st.button("🧹 Clear Batch", use_container_width=True):
        st.session_state.pending = []
        st.session_state.results = []
        st.session_state.review_url = ""
        st.session_state.selected = 0
        st.session_state.upload_key += 1
        st.rerun()

    st.divider()
    st.subheader("Connections")
    if vision_ready():
        st.success("Cloud Vision credentials: LOADED")
        st.caption("A real API call is tested during analysis. If the API is disabled in that Google project, local filename matching still continues.")
    else:
        st.error("Cloud Vision credentials: NOT CONFIGURED")
    if storage_ready():
        st.success("Cloud Storage: READY")
    else:
        st.warning("Cloud Storage: NOT CONFIGURED")
    if product_search_ready():
        st.success("Visual Product Search: READY")
    else:
        st.warning("Visual Product Search: NOT CONFIGURED")

    st.divider()
    st.subheader("Reference Catalog")
    st.write(f"Website products cached: **{website_count} / {len(catalog_records)}**")
    if website_cache.get("synced_at"):
        st.caption("Last sync: " + website_cache["synced_at"][:19].replace("T", " "))

    build_refs = st.toggle(
        "Also build Google visual references",
        value=True,
        help="Downloads up to 3 GENROSE website images per material and adds them to Google Product Search."
    )
    if st.button("SYNC GENROSE WEBSITE", use_container_width=True):
        if build_refs and not (storage_ready() and product_search_ready()):
            st.error("Configure Google Cloud Storage and Product Search first.")
        else:
            try:
                synced = sync_genrose_website(build_visual=build_refs)
                ok = sum(1 for x in synced["materials"].values() if x.get("status") == "OK")
                st.success(f"Website sync complete: {ok} matched materials.")
                if build_refs:
                    st.info("Google Product Search indexing is asynchronous; visual references may not affect searches immediately.")
                st.rerun()
            except Exception as e:
                st.exception(e)

    st.divider()
    st.caption("You should rarely need this panel after setup.")

st.markdown('<div class="ds-step">01 · Add room scenes</div>', unsafe_allow_html=True)
uploads = st.file_uploader(
    "Drop JPG / PNG / WEBP files here",
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
        f'<div class="ds-shell"><div class="ds-step">02 · Analyze</div>'
        f'<div style="font-size:1.15rem;font-weight:800">{len(pending)} images loaded</div>'
        f'<div class="ds-meta">Filename parsing runs first. Google Vision is enrichment/fallback, not the only matching method.</div></div>',
        unsafe_allow_html=True
    )
    if not vision_ready():
        st.warning("Google Cloud Vision is not configured. Filename + Italian/English room analysis will still run; cloud enrichment will be skipped.")
    if st.button(f"RUN ANALYSIS · {len(pending)} IMAGES", type="primary", use_container_width=True):
        progress = st.progress(0, text="Starting analysis…")
        results = []
        for i, item in enumerate(pending, start=1):
            progress.progress((i-1) / len(pending), text=f"Analyzing {i}/{len(pending)}: {item['name']}")
            try:
                analysis = analyze_image(item["name"], item["bytes"])
            except Exception as e:
                analysis = {
                    "stone": "", "sku": "", "material_confidence": 0, "material_method": f"ERROR: {e}",
                    "filename_material_score": 0, "vision_text_score": 0, "visual_score": 0,
                    "website_verified": False, "room": "Other", "room_confidence": 0,
                    "room_method": "Error", "material_candidates": [], "vision": {"error": str(e)}
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
                "new_name": new_name
            })
        progress.progress(1.0, text="Analysis complete.")
        time.sleep(.25)
        progress.empty()
        st.session_state.results = results
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
    x["new_name"] = safe_filename(base) + x["ext"]

high = sum(x["analysis"]["material_confidence"] >= 90 and x["analysis"]["room_confidence"] >= 70 for x in results)
review = len(results) - high

st.markdown('<div class="ds-step">03 · Results</div>', unsafe_allow_html=True)
m1, m2, m3, m4 = st.columns(4)
m1.metric("Images", len(results))
m2.metric("Ready", high)
m3.metric("Review", review)
m4.metric("Website matched", sum(x["analysis"]["website_verified"] for x in results))

pub1, pub2 = st.columns([1.2, .8])
if pub1.button("CREATE REVIEW LINK", type="primary", use_container_width=True):
    batch_id, url = save_review_batch(results)
    st.session_state.review_url = url
    st.success("Review page created.")

if pub2.button("DOWNLOAD ANALYSIS CSV", use_container_width=True):
    pass

if st.session_state.review_url:
    st.markdown("### Review URL")
    st.code(st.session_state.review_url)
    st.link_button("Open review page", st.session_state.review_url)

st.markdown('<div class="ds-divider"></div>', unsafe_allow_html=True)
left, center, right = st.columns([0.95, 1.55, 1.05], gap="large")

with left:
    with st.container(border=True, height=720):
        st.subheader("Queue")
        q = st.text_input("Search scenes", placeholder="Search filename, material, SKU or room", label_visibility="collapsed")
        for i, x in enumerate(results):
            a = x["analysis"]
            if q and norm(q) not in norm(x["name"] + " " + x["stone"] + " " + x["sku"] + " " + x["room"]):
                continue
            conf = a["material_confidence"]
            icon = "●" if conf >= 90 and a["room_confidence"] >= 70 else "●"
            label = f"{icon} {x['name']}\n{x['stone'] or 'Unknown material'} · {x['room']} · {conf}%"
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
    with st.container(border=True, height=720):
        st.subheader("Preview")
        st.image(Image.open(io.BytesIO(item["bytes"])), use_container_width=True)
        st.caption(item["name"])
        cls = "good" if analysis["material_confidence"] >= 90 else "mid" if analysis["material_confidence"] >= 70 else "bad"
        st.markdown(
            f'<div class="ds-statusline">'
            f'<span class="ds-chip {cls}">Material {analysis["material_confidence"]}%</span>'
            f'<span class="ds-chip info">Room {analysis["room_confidence"]}%</span>'
            f'<span class="ds-chip info">{html.escape(analysis["material_method"])}</span>'
            f'</div>',
            unsafe_allow_html=True
        )

with right:
    with st.container(border=True, height=720):
        st.subheader("Match")
        st.markdown(
            f'<div class="ds-titleline"><div>'
            f'<div class="ds-material">{html.escape(item["stone"] or "Unknown material")}</div>'
            f'<div class="ds-meta">SKU · {html.escape(item["sku"] or "NEED-SKU")} &nbsp;&nbsp; Room · {html.escape(item["room"])}</div>'
            f'</div></div>',
            unsafe_allow_html=True
        )
        st.markdown('<div class="ds-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="ds-label">New filename</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="ds-filename">{html.escape(item["new_name"])}</div>', unsafe_allow_html=True)

        st.markdown('<div class="ds-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="ds-label">Why this match</div>', unsafe_allow_html=True)
        evidence_html = (
            '<div class="ds-evidence"><span>Filename evidence</span><strong>' + str(analysis["filename_material_score"]) + '%</strong></div>'
            '<div class="ds-evidence"><span>Google Vision evidence</span><strong>' + str(analysis["vision_text_score"]) + '%</strong></div>'
        )
        if analysis["visual_score"]:
            evidence_html += '<div class="ds-evidence"><span>Visual similarity</span><strong>' + str(analysis["visual_score"]) + '%</strong></div>'
        evidence_html += (
            '<div class="ds-evidence"><span>GENROSE reference</span><strong>' +
            ('Verified' if analysis["website_verified"] else 'Not cached') + '</strong></div>'
        )
        st.markdown(evidence_html, unsafe_allow_html=True)
        st.markdown(f'<div class="ds-note">{html.escape(analysis["room_method"])}</div>', unsafe_allow_html=True)

        st.markdown("**Website reference**")
        sw = website_reference_bytes(item["sku"])
        if sw:
            st.image(Image.open(io.BytesIO(sw)), width=240)
        else:
            st.markdown('<div class="ds-empty">No GENROSE reference image cached for this material yet.</div>', unsafe_allow_html=True)
        web_entry = website_entry_for_sku(item["sku"])
        if web_entry.get("page_url"):
            st.link_button("Open GENROSE material page", web_entry["page_url"])

        with st.expander("Alternate material matches", expanded=False):
            for c in analysis["material_candidates"][:6]:
                st.write(
                    f"**{c['stone']}** · `{c['sku']}` — {int(round(c['confidence']*100))}%"
                )
                st.caption(
                    f"filename {int(round(c['filename']*100))}% · "
                    f"Vision text {int(round(c['vision_text']*100))}% · "
                    f"visual {int(round(c['visual']*100))}% · {c['method']}"
                )

        with st.expander("Diagnostics · Google Vision", expanded=False):
            v = analysis["vision"]
            if v.get("error"):
                st.warning("Google Cloud Vision did not run for this image. Filename + room-name analysis was preserved.")
                st.code(v["error"][:500])
            st.write("**Labels:**", ", ".join(v.get("labels", [])[:15]) or "—")
            st.write("**Web entities:**", ", ".join(v.get("web_entities", [])[:15]) or "—")
            if v.get("web_pages"):
                st.write("**Matching web pages:**")
                for u in v["web_pages"][:5]:
                    st.caption(u)
            if v.get("text"):
                st.text(v["text"][:1200])

st.markdown('<div class="ds-divider"></div>', unsafe_allow_html=True)
st.markdown('<div class="ds-step">04 · Export</div>', unsafe_allow_html=True)
table = pd.DataFrame([{
    "Old Filename": x["name"],
    "New Filename": x["new_name"],
    "Material": x["stone"],
    "SKU": x["sku"],
    "Room": x["room"],
    "Material Confidence": f'{x["analysis"]["material_confidence"]}%',
    "Room Confidence": f'{x["analysis"]["room_confidence"]}%',
    "Method": x["analysis"]["material_method"],
    "Website Verified": "YES" if x["analysis"]["website_verified"] else "NO"
} for x in results])
st.dataframe(table, use_container_width=True, hide_index=True)
st.download_button(
    "Download Analysis CSV",
    table.to_csv(index=False).encode("utf-8-sig"),
    "room_scene_analysis.csv",
    "text/csv"
)
