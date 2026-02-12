import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path
from datetime import datetime
import pdfplumber
import re
from typing import List
from pydantic import BaseModel
import instructor
from openai import OpenAI
import os

# ================= OPENAI =================

client = instructor.from_openai(
    OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
)

class CauseMatch(BaseModel):
    case_number: str
    parties: str
    appellant_counsel: str
    respondent_counsel: str
    court_no: str
    judges: str


# ================= CONFIG =================

BASE = "https://nclat.nic.in"
URL = BASE + "/daily-cause-list"
SAVE_DIR = Path("nclat_pdfs")
SAVE_DIR.mkdir(exist_ok=True)


# ================= NORMALIZE =================

def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ================= COURT NO =================

def extract_court_no_page1(pdf):
    text = pdf.pages[0].extract_text() or ""
    m = re.search(r"COURT\s*[-–]?\s*([IVX\d]+)", text, re.I)
    return m.group(1) if m else ""


# ================= JUDGE BLOCKS =================

def extract_judge_blocks(page_text):
    lines = [l.strip() for l in page_text.split("\n") if l.strip()]
    blocks = []
    current = []

    for line in lines:
        if "Hon" in line:
            if current:
                blocks.append(" ".join(current))
                current = []
            current.append(line)

        elif current:
            if any(k in line for k in ["Member", "Judicial", "Technical"]):
                current.append(line)
            else:
                blocks.append(" ".join(current))
                current = []

    if current:
        blocks.append(" ".join(current))

    return blocks


# ================= OPENAI EXTRACT =================

def ai_extract(page_text, party_name, court_no, judges):
    try:
        return client.chat.completions.create(
            model="gpt-4.1-mini",
            response_model=List[CauseMatch],
            messages=[{
                "role": "user",
                "content": f"""
Extract ONLY the cause list row containing:

TARGET PARTY: "{party_name}"

Return:
- case_number
- parties
- appellant_counsel
- respondent_counsel

Court No: {court_no}
Judges: {judges}

PAGE TEXT:
{page_text}
"""
            }]
        )
    except Exception as e:
        print("❌ OpenAI extract failed:", e)
        return []


# ================= DOWNLOAD PDFs =================

def download_pdfs(start_dt, end_dt):

    session = requests.Session()
    page = 0
    pdf_files = []
    stop_paging = False

    print(f"🔎 NCLAT scanning from {start_dt} to {end_dt}")

    while not stop_paging:

        r = session.get(URL, params={"page": page}, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table.cols-5 tbody tr")

        if not rows:
            break

        for row in rows:

            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            date_text = cols[3].get_text(strip=True)
            print("📅 Found list date:", date_text)

            try:
                row_dt = datetime.strptime(date_text, "%d/%m/%Y")
            except:
                continue

            # ✅ STOP if older than requested range
            if row_dt < start_dt:
                print("⛔ Reached older than start date — stopping scan")
                stop_paging = True
                break

            # skip if newer than end date
            if row_dt > end_dt:
                continue

            a = cols[4].find("a", href=True)
            if not a:
                continue

            pdf_url = urljoin(BASE, a["href"])
            fname = pdf_url.split("/")[-1]
            path = SAVE_DIR / fname

            if not path.exists():
                print("⬇️ Downloading:", fname)
                pdf = session.get(pdf_url, timeout=60)
                pdf.raise_for_status()
                path.write_bytes(pdf.content)

            pdf_files.append(path)

        page += 1

    print("✅ Total PDFs selected:", len(pdf_files))
    return pdf_files


# ================= SEARCH =================

def split_parties(parties: str):
    parts = re.split(r"\s+v(?:s\.?)?\s+", parties, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parties, "N/A"


def search_party_in_pdf(pdf_path, party_name):

    results = []
    party_norm = normalize(party_name)

    print("📄 Scanning PDF:", pdf_path.name)

    with pdfplumber.open(pdf_path) as pdf:

        court_no = extract_court_no_page1(pdf)
        current_judges = ""

        for page in pdf.pages:

            text = page.extract_text()
            if not text:
                continue

            judge_blocks = extract_judge_blocks(text)
            if judge_blocks:
                current_judges = judge_blocks[-1]

            if party_norm not in normalize(text):
                continue

            print("✅ Party match on page — sending to AI")

            ai_rows = ai_extract(text, party_name, court_no, current_judges)

            for r in ai_rows:
                results.append(r.model_dump())

    return results


# ================= FASTAPI WRAPPER =================

def search_range(party_name: str, start_date: str, end_date: str):

    start_dt = datetime.strptime(start_date, "%d/%m/%Y")
    end_dt = datetime.strptime(end_date, "%d/%m/%Y")

    pdfs = download_pdfs(start_dt, end_dt)

    if not pdfs:
        print("⚠️ No PDFs found for date range")
        return []

    all_results = []

    for pdf in pdfs:
        rows = search_party_in_pdf(pdf, party_name)

        for r in rows:
            petitioner, respondent = split_parties(r["parties"])

            all_results.append({
                "case_number": r["case_number"],
                "petitioner": petitioner,
                "respondent": respondent,
                "advocates": f"A: {r['appellant_counsel']} | R: {r['respondent_counsel']}",
                "court": "NCLAT",
                "judge": r.get("judges"),
                "court_no": r.get("court_no"),
                "date": None
            })

    print("🎯 NCLAT matches:", len(all_results))
    return all_results
