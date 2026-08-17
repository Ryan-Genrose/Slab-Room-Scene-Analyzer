
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

st.set_page_config(page_title="GENROSE Room Scene Analyzer", page_icon="🪨", layout="wide")

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
    """Always-used Cloud Vision pass: labels + web detection + OCR."""
    if not vision_ready():
        return {"labels": [], "web_entities": [], "web_pages": [], "text": "", "error": "Vision not configured"}

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
        return {"labels": [], "web_entities": [], "web_pages": [], "text": "", "error": resp.error.message}

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
.block-container{max-width:1900px;padding-top:1rem}
html,body,[class*="css"]{font-size:18px}
h1{font-size:2.25rem!important}
.rs-chip{display:inline-block;padding:4px 8px;border-radius:999px;font-size:13px;font-weight:800;margin-right:5px}
.rs-good{background:#153c2b;color:#a0e8bd;border:1px solid #327559}
.rs-mid{background:#4c3417;color:#ffd38a;border:1px solid #825e2f}
.rs-bad{background:#4b2024;color:#ffb5bc;border:1px solid #7e383f}
.rs-info{background:#173149;color:#afd3ef;border:1px solid #315e7e}
.rs-path{font-family:monospace;word-break:break-all;padding:10px;background:#09131d;border-radius:8px;border:1px dashed #3a526d}
</style>
""", unsafe_allow_html=True)

top1, top2 = st.columns([4, 1])
with top1:
    st.title("GENROSE Room Scene Analyzer")
    st.caption("Filename first → Italian room normalization → master SKU match → website verification → Google Cloud Vision fallback")
with top2:
    st.metric("Materials", len(catalog_records))

website_cache = load_website_cache()
website_count = sum(1 for x in website_cache.get("materials", {}).values() if x.get("status") == "OK")

with st.sidebar:
    st.header("System")
    if st.button("🧹 Clear Batch", use_container_width=True):
        st.session_state.pending = []
        st.session_state.results = []
        st.session_state.review_url = ""
        st.session_state.selected = 0
        st.session_state.upload_key += 1
        st.rerun()

    st.divider()
    st.subheader("Google Cloud")
    if vision_ready():
        st.success("Cloud Vision: READY")
    else:
        st.error("Cloud Vision: NOT CONFIGURED")
    if storage_ready():
        st.success("Cloud Storage: READY")
    else:
        st.warning("Cloud Storage: NOT CONFIGURED")
    if product_search_ready():
        st.success("Visual Product Search: READY")
    else:
        st.warning("Visual Product Search: NOT CONFIGURED")

    st.divider()
    st.subheader("Website reference catalog")
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
    st.caption("Daily workflow: you only need the main upload + Analyze button. Website sync is an admin/setup task.")

uploads = st.file_uploader(
    "Drag & drop room scene images",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
    key=f"upload_{st.session_state.upload_key}"
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
    st.success(f"{len(pending)} images ready.")
    if not vision_ready():
        st.warning("Google Cloud Vision is not configured. ANALYZE will not run until credentials are added.")
    if st.button(f"ANALYZE {len(pending)} IMAGES", type="primary", use_container_width=True, disabled=not vision_ready()):
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

m1, m2, m3, m4 = st.columns(4)
m1.metric("Images", len(results))
m2.metric("Ready", high)
m3.metric("Needs review", review)
m4.metric("Website verified", sum(x["analysis"]["website_verified"] for x in results))

pub1, pub2 = st.columns([1, 1])
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

st.divider()
left, center, right = st.columns([1.0, 1.55, 1.1], gap="large")

with left:
    st.subheader("Results")
    q = st.text_input("Search", placeholder="filename, material, SKU, room")
    for i, x in enumerate(results):
        a = x["analysis"]
        if q and norm(q) not in norm(x["name"] + " " + x["stone"] + " " + x["sku"] + " " + x["room"]):
            continue
        conf = a["material_confidence"]
        icon = "🟢" if conf >= 90 and a["room_confidence"] >= 70 else "🟡" if conf >= 70 else "🔴"
        if st.button(
            f"{icon} {x['name']}\n{x['stone'] or 'Unknown'} · {x['room']} · {conf}%",
            key=f"result_{i}",
            use_container_width=True
        ):
            st.session_state.selected = i
            st.rerun()

idx = min(st.session_state.selected, len(results)-1)
item = results[idx]
analysis = item["analysis"]

with center:
    st.subheader("Scene")
    st.image(Image.open(io.BytesIO(item["bytes"])), use_container_width=True)
    st.caption(item["name"])
    cls = "rs-good" if analysis["material_confidence"] >= 90 else "rs-mid" if analysis["material_confidence"] >= 70 else "rs-bad"
    st.markdown(
        f'<span class="rs-chip {cls}">Material {analysis["material_confidence"]}%</span>'
        f'<span class="rs-chip rs-info">Room {analysis["room_confidence"]}%</span>'
        f'<span class="rs-chip rs-info">{html.escape(analysis["material_method"])}</span>',
        unsafe_allow_html=True
    )

with right:
    st.subheader("Proposed result")
    st.markdown(f"### {item['stone'] or 'Unknown material'}")
    st.write(f"**SKU:** `{item['sku'] or 'NEED-SKU'}`")
    st.write(f"**Room:** {item['room']}")
    st.markdown("**New filename**")
    st.markdown(f'<div class="rs-path">{html.escape(item["new_name"])}</div>', unsafe_allow_html=True)

    st.markdown("**Why it chose this**")
    st.write(f"Filename material evidence: **{analysis['filename_material_score']}%**")
    st.write(f"Google Vision text/web evidence: **{analysis['vision_text_score']}%**")
    if analysis["visual_score"]:
        st.write(f"Google visual similarity: **{analysis['visual_score']}%**")
    st.write("Website verification: **YES**" if analysis["website_verified"] else "Website verification: **NO / not cached**")
    st.caption(analysis["room_method"])

    st.markdown("**Website reference**")
    sw = website_reference_bytes(item["sku"])
    if sw:
        st.image(Image.open(io.BytesIO(sw)), width=240)
    else:
        st.caption("No cached website reference for this material.")
    web_entry = website_entry_for_sku(item["sku"])
    if web_entry.get("page_url"):
        st.link_button("Open GENROSE material page", web_entry["page_url"])

    with st.expander("Top material candidates", expanded=True):
        for c in analysis["material_candidates"][:6]:
            st.write(
                f"**{c['stone']}** · `{c['sku']}` — {int(round(c['confidence']*100))}%"
            )
            st.caption(
                f"filename {int(round(c['filename']*100))}% · "
                f"Vision text {int(round(c['vision_text']*100))}% · "
                f"visual {int(round(c['visual']*100))}% · {c['method']}"
            )

    with st.expander("Google Vision evidence"):
        v = analysis["vision"]
        if v.get("error"):
            st.warning(v["error"])
        st.write("**Labels:**", ", ".join(v.get("labels", [])[:15]) or "—")
        st.write("**Web entities:**", ", ".join(v.get("web_entities", [])[:15]) or "—")
        if v.get("web_pages"):
            st.write("**Matching web pages:**")
            for u in v["web_pages"][:5]:
                st.caption(u)
        if v.get("text"):
            st.text(v["text"][:1200])

st.divider()
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
