# DEPLOYMENT GUIDE — GENROSE Room Scene Analyzer v0.6.2

You do not need to install Python locally. Everything can be done in a browser.

---

## PART 1 — CREATE THE GITHUB REPOSITORY

1. Go to GitHub and sign in.
2. Click the **+** menu at the top-right.
3. Click **New repository**.
4. Repository name:
   `genrose-room-scene-analyzer`
5. Choose **Private** if you do not want the code public.
6. Click **Create repository**.
7. On the empty repository page, click **uploading an existing file**.
8. Unzip the downloaded `Slab_Room_Scene_Manager_v0.6.2.zip`.
9. Open the folder `Slab_Room_Scene_Manager_v0.6.2`.
10. Drag everything INSIDE that folder into GitHub.
    The GitHub repository root should directly contain:
    - `app.py`
    - `requirements.txt`
    - `README.md`
    - `DEPLOYMENT_GUIDE.md`
    - `data/`
    - `.streamlit/`
11. Do NOT upload a real `.streamlit/secrets.toml` file.
12. Click **Commit changes**.

---

## PART 2 — CREATE THE GOOGLE CLOUD PROJECT

1. Open Google Cloud Console.
2. At the top, click the project selector.
3. Click **New Project**.
4. Name it:
   `genrose-room-scene-analyzer`
5. Create/select the project.
6. Make sure **Billing** is enabled for the project.
7. Open **APIs & Services → Library**.
8. Search for **Cloud Vision API**.
9. Open it and click **Enable**.

The app uses:
- Cloud Vision Label Detection
- Cloud Vision Web Detection
- Cloud Vision OCR/Text Detection
- Vision API Product Search for visual material matching

---

## PART 3 — CREATE CLOUD STORAGE

1. In Google Cloud, search for **Cloud Storage**.
2. Open **Buckets**.
3. Click **Create**.
4. Give the bucket a globally unique name, for example:
   `genrose-room-scene-analyzer-YOURINITIALS`
5. Location type: **Region**
6. Region: `us-east1`
7. Keep the bucket **private**.
8. Finish creating it.
9. Copy the bucket name. You will need it for Streamlit Secrets.

The bucket stores:
- scraped GENROSE reference images
- the website reference catalog
- review batches
- review submissions

---

## PART 4 — CREATE THE GOOGLE SERVICE ACCOUNT

1. In Google Cloud, open **IAM & Admin → Service Accounts**.
2. Click **Create Service Account**.
3. Name:
   `room-scene-analyzer`
4. Click **Create and Continue**.

For the FIRST TEST deployment, the simplest path is to grant the service account
**Project → Owner**, which is also the role Google's Product Search setup guide uses
for its quickstart service account. Once the app is proven, reduce the permissions
for production.

5. Finish creating the service account.
6. Click the new service account.
7. Open the **Keys** tab.
8. Click **Add Key → Create new key**.
9. Choose **JSON**.
10. Click **Create**.
11. A JSON key file downloads to your computer.
12. Keep it private. NEVER upload it to GitHub.

---

## PART 5 — DEPLOY ON STREAMLIT COMMUNITY CLOUD

1. Go to Streamlit Community Cloud.
2. Sign in with GitHub.
3. If asked, connect/authorize your GitHub account.
4. Click **Create app**.
5. Choose **Yup, I have an app**.
6. Select:
   - Repository: `genrose-room-scene-analyzer`
   - Branch: `main`
   - Main file path: `app.py`
7. Pick a simple app URL if Streamlit offers the option, for example:
   `genrose-room-scenes`
8. Before clicking Deploy, open **Advanced settings**.
9. Leave Python at the current Streamlit default unless you have a reason to change it.
10. In the **Secrets** box, paste the configuration described in Part 6 below.
11. Save Advanced settings.
12. Click **Deploy**.

Streamlit will install everything listed in `requirements.txt`.
The first deployment can take a few minutes.

---

## PART 6 — STREAMLIT SECRETS

Open the downloaded Google service-account JSON file in a text editor.

In Streamlit's Secrets box, paste this and replace the placeholders:

```toml
APP_BASE_URL = "https://YOUR-ACTUAL-APP.streamlit.app"

GOOGLE_CLOUD_PROJECT = "YOUR_GOOGLE_PROJECT_ID"
GOOGLE_CLOUD_LOCATION = "us-east1"
GOOGLE_CLOUD_PRODUCT_SET_ID = "genrose-slabs"
GOOGLE_CLOUD_BUCKET = "YOUR_BUCKET_NAME"

GOOGLE_SERVICE_ACCOUNT_JSON = '''PASTE_THE_ENTIRE_SERVICE_ACCOUNT_JSON_HERE'''
```

Important:
- `GOOGLE_CLOUD_PROJECT` is the PROJECT ID, not necessarily the friendly project name.
- The service account JSON must stay inside the triple SINGLE quotes. This preserves the JSON key's backslash escapes.
- Do not put this file in GitHub.

For email, add ONE of these:

### Easiest if you already use Formspree

Create/configure a Formspree form whose destination is:
`marketing@genrose.com`

Then add:

```toml
FORMSPREE_ENDPOINT = "https://formspree.io/f/YOUR_FORM_ID"
```

### Or Resend

```toml
RESEND_API_KEY = "re_YOUR_KEY"
EMAIL_FROM = "Room Scene Manager <noreply@YOUR-VERIFIED-DOMAIN.com>"
```

### Or SMTP

```toml
SMTP_HOST = "smtp.example.com"
SMTP_PORT = 587
SMTP_USER = "user@example.com"
SMTP_PASSWORD = "YOUR_PASSWORD"
EMAIL_FROM = "user@example.com"
```

---

## PART 7 — FIRST-TIME WEBSITE / VISUAL REFERENCE SETUP

This is the one admin step you perform after deployment.

1. Open the deployed app.
2. Look at the left sidebar.
3. Confirm it says:
   - `Cloud Vision: READY`
   - `Cloud Storage: READY`
   - `Visual Product Search: READY`
4. Leave **Also build Google visual references** turned ON.
5. Click:
   **SYNC GENROSE WEBSITE**
6. Leave the page open while it works.

The app will:
- read the GENROSE Natural Stone Slabs website
- match website pages against the built-in 403 Stone/SKU records
- collect up to 3 reference images per material
- upload those images to your private Cloud Storage bucket
- create/update the Google Product Search product catalog
- store the matching GENROSE page URL and reference image for later review

IMPORTANT:
Google states that the Product Search index is updated approximately daily.
That means filename + Vision analysis works immediately, but newly added visual
reference images may take until the next Product Search index update before they
affect visual similarity results.

You do NOT need to run website sync for every batch.
Run it when first setting up and occasionally when the GENROSE website changes.

---

## PART 8 — NORMAL WORKFLOW

1. Open the app.
2. Drag in 20 room-scene images.
3. Click:
   **ANALYZE 20 IMAGES**
4. The hierarchy is:
   1. original filename
   2. Italian/English room-term normalization
   3. fuzzy match to the built-in Stone/SKU master
   4. GENROSE website verification
   5. Google Cloud Vision labels/web/OCR
   6. Google Product Search visual similarity as fallback
5. Review the generated results.
6. Click:
   **CREATE REVIEW LINK**
7. Copy the URL and send it to the slab manager.
8. She opens the link.
9. She can:
   - see the room scene
   - see old filename
   - see proposed/final filename
   - see Material + SKU
   - see the website reference image
   - see confidence
   - approve/unapprove
   - override the material
   - override the room
   - add a new material
   - add notes
10. She clicks:
    **SUBMIT REVIEW TO MARKETING**
11. The submission is saved and emailed to:
    `marketing@genrose.com`

---

## NAMING CONVENTION

`SKU-StoneType-RoomType`

Example:

`QUSACBE-AcquaBella-Kitchen.jpg`

If multiple images would produce exactly the same filename, the analyzer adds:
`-01`, `-02`, etc.

---

## TROUBLESHOOTING

### Streamlit says a Python package is missing
Check that `requirements.txt` is in the repository root, then reboot the app.

### Google status says NOT CONFIGURED
Open the Streamlit app:
**Manage app → Settings → Secrets**
and check the Google values.

### Permission denied from Google
For the first test, confirm the service account was actually granted the role and
that the JSON in Streamlit Secrets belongs to that service account.

### Review URL disappears after reboot
Cloud Storage is not configured or the service account cannot write to the bucket.

### Website visual matches are not appearing immediately
Expected. Google Product Search indexing is asynchronous and is approximately daily.

### Email says the review was saved but email failed
Your analysis/review system is working; only the email provider settings need attention.

### A filename contains Italian but room is wrong
The review page lets the reviewer override it. Add the missing term to `ROOM_ALIASES`
in `app.py` for future batches.


## If every result is Unknown / 0%
v0.6.2 prevents this failure mode. If Google Cloud returns a 403 because Vision is
disabled in the service-account project, the app still returns filename + room results.
Enable Cloud Vision in the exact project named by the Google error to restore cloud
enrichment.
