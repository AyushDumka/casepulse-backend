import requests
from pathlib import Path
from datetime import datetime, timedelta
import pdfplumber
import pandas as pd
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time, os

# 🔥 OPENAI IMPORTS
import instructor
from pydantic import BaseModel
from openai import OpenAI
from typing import List

# ========================== CONFIG ==========================

URL = "http://verdictfinder.sci.gov.in/elk_frontend/index.php"
BASE_URL = "https://api.sci.gov.in/jonew/cl/{date}/M_J_1.pdf"
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ========================== OPENAI SETUP ==========================

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ========================== OPENAI RESPONSE MODEL ==========================

class SupremeCauseListItem(BaseModel):
    case_number: str
    parties: str
    advocate_names: str

# ========================== MONITOR ==========================

def monitor(keyword: str, mode: str):

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)

    driver.get(URL)

    search_input = wait.until(EC.element_to_be_clickable((By.NAME, "search")))
    search_input.clear()
    search_input.send_keys(keyword)

    driver.find_element(By.ID, mode).click()

    captcha_text = wait.until(EC.presence_of_element_located((By.ID, "captcha"))).text.strip()
    captcha_input = wait.until(EC.element_to_be_clickable((By.ID, "captcha-input")))
    captcha_input.send_keys(captcha_text)

    wait.until(EC.element_to_be_clickable((By.ID, "landing_submit"))).click()
    time.sleep(5)

    all_results = []

    while True:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                all_results.append(row.text.strip())
            except:
                continue

        record_view = driver.find_element(By.ID, "record-view").text
        parts = list(map(int, re.findall(r"\d+", record_view)))
        start, end, total = parts

        if end >= total:
            break

        try:
            next_btn = driver.find_element(By.ID, "nextBtn")
            if "disabled" in next_btn.get_attribute("class"):
                break
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(3)
        except:
            break

    driver.quit()
    return all_results

# ========================== DOWNLOAD ==========================

def download_pdf(cause_date: str) -> Path:
    pdf_path = DATA_DIR / f"sc_cause_list_{cause_date}.pdf"
    url = BASE_URL.format(date=cause_date)

    if pdf_path.exists():
        return pdf_path

    r = requests.get(url, timeout=40)
    r.raise_for_status()
    pdf_path.write_bytes(r.content)
    return pdf_path

# ========================== NORMALIZATION ==========================

def normalize_name(text: str) -> str:
    if not isinstance(text, str):
        return ""

    text = text.lower()
    text = re.sub(r"\b(ors?|anr|lrs?|&)\b", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text

def split_petitioner_respondent(party_text: str):
    if not party_text:
        return "N/A", "N/A"

    text = re.sub(r"\{.*?\}|\[.*?\]", "", party_text)
    parts = re.split(r"\bversus\b|\bvs\.?\b|\bv\.?\b", text, flags=re.IGNORECASE)

    if len(parts) >= 2:
        return parts[0].strip(), parts[1].strip()

    return text.strip(), "N/A"

# ========================== JUDGE + COURT + TIME ==========================

def extract_judge_court_time(pdf, start_page_index: int):
    judges = []
    court_no = ""
    court_time = ""

    for i in range(start_page_index, -1, -1):
        page_text = pdf.pages[i].extract_text() or ""

        if not court_no:
            court_match = re.search(r"COURT\s*NO\.?\s*[:\-]?\s*(\d+)", page_text, re.IGNORECASE)
            if court_match:
                court_no = court_match.group(1)

        if not court_time:
            time_match = re.search(r"COURT\s*TIME\s*[:\-]?\s*([0-9:\.apmAPM ]+)", page_text)
            if time_match:
                court_time = time_match.group(1).strip()

        judge_matches = re.findall(r"HON['’]BLE\s+[^,\n]+", page_text, re.IGNORECASE)
        if judge_matches:
            judges = list(dict.fromkeys([j.strip() for j in judge_matches]))
            break

    return ", ".join(judges), court_no, court_time

# ========================== MAIN SEARCH ==========================

def search(party_name: str, cause_date: str | None):

    if not cause_date:
        raise Exception("Supreme Court requires a date (YYYY-MM-DD)")

    try:
        datetime.strptime(cause_date, "%Y-%m-%d")
    except ValueError:
        raise Exception("Invalid date format. Use YYYY-MM-DD")

    pdf_path = download_pdf(cause_date)
    name_norm = normalize_name(party_name)

    matched_pages = set()

    # ---------- PHASE 1: PYTHON MATCHING ----------

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            page_norm = normalize_name(text)

            if name_norm in page_norm:
                matched_pages.add(page_index)
                continue

            tokens = name_norm.split()
            if tokens and all(t in page_norm for t in tokens):
                matched_pages.add(page_index)

    if not matched_pages:
        return []

    # ---------- PHASE 2: OPENAI EXTRACTION + METADATA ----------

    final_results: List[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_index in matched_pages:
            page = pdf.pages[page_index]
            focused_text = page.extract_text()
            if not focused_text:
                continue

            judges, court_no, court_time = extract_judge_court_time(pdf, page_index)

            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                response_model=List[SupremeCauseListItem],
                messages=[{
                    "role": "user",
                    "content": f"""
You are extracting data from a Supreme Court cause list.

STRICT RULES:
1. Searched party: "{party_name}"
2. The page already matched this party using Python.
3. Extract ONLY the matching row.
4. Extract:
   - Case Number
   - Parties
   - Advocate names

FULL PAGE TEXT:
{focused_text}
"""
                }]
            )

            for r in response:
                petitioner, respondent = split_petitioner_respondent(r.parties)

                final_results.append({
                    "case_number": r.case_number.strip(),
                    "petitioner": petitioner,
                    "respondent": respondent,
                    "advocates": r.advocate_names or "N/A",
                    "court": "Supreme Court",
                    "judge": judges,
                    "court_no": court_no,
                    "court_time": court_time,
                    "date": cause_date
                })

    return final_results

# ========================== RANGE SEARCH ==========================

def search_range(party_name: str, start_date: str, end_date: str):

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise Exception("Invalid date format. Use YYYY-MM-DD")

    if start_dt > end_dt:
        raise Exception("Start date cannot be after end date")

    all_results = []
    current = start_dt

    while current <= end_dt:
        cause_date = current.strftime("%Y-%m-%d")

        try:
            daily_results = search(party_name, cause_date)
            all_results.extend(daily_results)
        except Exception as e:
            print(f"[SUPREME COURT] Skipping {cause_date}: {e}")

        current += timedelta(days=1)

    return all_results

# ========================== DOWNLOAD BY INDEX ==========================

def download_by_index(keyword: str, mode: str, case_number: int, download_dir: str):

    options = webdriver.ChromeOptions()
    prefs = {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 40)

    try:
        driver.get(URL)

        search_input = wait.until(EC.element_to_be_clickable((By.NAME, "search")))
        search_input.clear()
        search_input.send_keys(keyword)

        driver.find_element(By.ID, mode).click()

        captcha_text = wait.until(EC.presence_of_element_located((By.ID, "captcha"))).text.strip()
        captcha_input = wait.until(EC.element_to_be_clickable((By.ID, "captcha-input")))
        captcha_input.send_keys(captcha_text)

        wait.until(EC.element_to_be_clickable((By.ID, "landing_submit"))).click()

        rows = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr")))

        if case_number < 1 or case_number > len(rows):
            driver.quit()
            return None

        row = rows[case_number - 1]
        button = row.find_element(By.CSS_SELECTOR, "button.show-modal-btn")
        button.click()

        time.sleep(3)

        iframe = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='load_pdf.php']")))
        driver.switch_to.frame(iframe)

        open_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(@href,'load_pdf.php')]")))
        open_link.click()

        time.sleep(10)

        driver.quit()

        files = [f for f in os.listdir(download_dir) if f.lower().endswith(".pdf")]

        if not files:
            return None

        latest = max([os.path.join(download_dir, f) for f in files], key=os.path.getctime)

        return os.path.basename(latest)

    except Exception as e:
        print("DOWNLOAD ERROR:", e)
        try:
            driver.quit()
        except:
            pass
        return None
