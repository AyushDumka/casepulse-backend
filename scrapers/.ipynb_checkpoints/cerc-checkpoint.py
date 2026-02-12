import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path
import pdfplumber
import time
import json
import os
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE_URL = "https://cercind.gov.in/Sched_hear_hin.html"
SITE_ROOT = "https://cercind.gov.in/"
SAVE_DIR = Path("cerc_cause_lists")
SAVE_DIR.mkdir(exist_ok=True)


# ================= SESSION =================

def make_session():
    s = requests.Session()

    retry = Retry(total=5, backoff_factor=2,
                  status_forcelist=[429, 500, 502, 503, 504])

    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)

    s.headers.update({
        "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36"
    })

    return s


# ================= FETCH MONTH =================

def fetch_month_pdfs(month_name):

    session = make_session()
    r = session.get(BASE_URL, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    downloaded = []

    for panel in soup.select("div.panel.panel-primary"):
        title = panel.select_one(".panel-title strong")
        if not title:
            continue

        month = title.get_text(strip=True)
        if month_name.lower() not in month.lower():
            continue

        for a in panel.select("a[href$='.pdf']"):
            href = a.get("href")
            if not href:
                continue

            pdf_url = urljoin(SITE_ROOT, href)
            fname = href.split("/")[-1]
            path = SAVE_DIR / fname

            if not path.exists():
                pdf = session.get(pdf_url, timeout=60)
                pdf.raise_for_status()
                path.write_bytes(pdf.content)

            downloaded.append(path)

    return downloaded


# ================= AI EXTRACT =================

def ai_extract(page_text):

    prompt = """
Extract CERC cause list cases.

Return JSON array:
sno, petition_no, petitioner, subject, hearing_date_if_present
Return ONLY JSON.
"""

    r = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": "Extract structured legal data."},
            {"role": "user", "content": prompt + page_text[:12000]}
        ],
    )

    return r.choices[0].message.content


# ================= SEARCH =================

def search(month: str, party: str):

    pdfs = fetch_month_pdfs(month)
    results = []

    for pdf_path in pdfs:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""

                if party.lower() not in text.lower():
                    continue

                raw = ai_extract(text)

                try:
                    cases = json.loads(raw)
                except:
                    continue

                for c in cases:
                    if party.lower() in c.get("petitioner", "").lower():
                        c["source_pdf"] = pdf_path.name
                        c["page"] = i
                        results.append(c)

    return results
