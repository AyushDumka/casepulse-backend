import requests
from bs4 import BeautifulSoup
import re
import os
import pdfplumber
import instructor
from pydantic import BaseModel
from openai import OpenAI
from typing import List
from datetime import datetime, timedelta

# 🔥 SELENIUM IMPORTS
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

# ================== CONFIG ==================

BASE_URL = "https://delhihighcourt.nic.in"
PAGE_URL = "https://delhihighcourt.nic.in/web/cause-lists/cause-list"
DOWNLOAD_DIR = "downloaded_causelists"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
}

# ================== NORMALIZATION ==================

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.lower().replace("\n", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def get_first_two_words(name: str) -> str:
    return " ".join(normalize_text(name).split()[:2])

# ================== HELPERS ==================

def is_cause_list_title(text: str, date_str: str) -> bool:
    return "cause list" in text.lower() and date_str in text

def find_pdf_links_with_pagination(date_str: str):
    pdfs = []
    for page in range(0, 4):
        url = f"{PAGE_URL}?page={page}"
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        for a in soup.select("a[href]"):
            img = a.find("img")
            if img and img.get("alt") and is_cause_list_title(img["alt"], date_str):
                href = a["href"].strip()
                pdf_url = href if href.startswith("http") else BASE_URL + href
                pdfs.append((pdf_url, img["alt"]))

    return pdfs

# ================== OPENAI SETUP ==================

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ================== OPENAI RESPONSE MODEL ==================

class CauseListItem(BaseModel):
    case_number: str
    parties: str
    advocate_names: str
    date: str | None = None
    court_number: str | None = None
    judge_name: str | None = None

# ================== PARTY SPLITTER ==================

def split_parties(parties: str):
    if not parties:
        return "N/A", "N/A"

    parts = re.split(r"\s+v(?:s\.?)?\s+", parties, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()

    return parties.strip(), "N/A"

# ================== PUBLIC API FUNCTION ==================

def search(party_name: str, date: str | None) -> List[dict]:
    if not party_name or not date:
        return []

    search_key = get_first_two_words(party_name)

    pdf_links = find_pdf_links_with_pagination(date)
    if not pdf_links:
        return []

    downloaded_paths = []

    for pdf_url, alt_text in pdf_links:
        safe_name = re.sub(r"[^\w\-. ]", "_", alt_text).strip() + ".pdf"
        path = os.path.join(DOWNLOAD_DIR, safe_name)

        if not os.path.exists(path):
            r = requests.get(pdf_url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)

        downloaded_paths.append(path)

    target_pdfs = []
    for p in downloaded_paths:
        name = os.path.basename(p).upper()
        if name.startswith("FINAL MATTERS") or name.startswith("REGULAR MATTERS"):
            target_pdfs.append(p)

    if not target_pdfs:
        return []

    matched_pages = []

    for pdf_path in target_pdfs:
        with pdfplumber.open(pdf_path) as pdf:
            last_court = ""
            last_judges = []

            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue

                page_judges = []
                for line in text.splitlines():
                    l = line.lower()
                    if "court no" in l:
                        last_court = line.strip()
                    elif line.strip().upper().startswith(("HON'BLE", "HON’BLE", "BEFORE")):
                        page_judges.append(line.strip())

                if page_judges:
                    last_judges = page_judges

                if search_key in normalize_text(text):
                    matched_pages.append({
                        "text": text,
                        "court": last_court,
                        "judges": last_judges
                    })

    if not matched_pages:
        return []

    final_results: List[dict] = []

    for m in matched_pages:
        focused_text = m["text"]

        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            response_model=List[CauseListItem],
            messages=[{
                "role": "user",
                "content": f"""
You are extracting data from a Delhi High Court cause list.

STRICT RULES:
1. Searched party: "{party_name}"
2. Find ONLY the matching row.
3. Extract:
   - Case Number
   - Parties
   - Advocate names

Fallback Court No: {m['court']}
Fallback Judge(s): {" | ".join(m['judges'])}

FULL PAGE TEXT:
{focused_text}
"""
            }]
        )

        for r in response:
            petitioner, respondent = split_parties(r.parties)

            final_results.append({
                "case_number": r.case_number,
                "petitioner": petitioner,
                "respondent": respondent,
                "advocates": r.advocate_names or "N/A",
                "court": "Delhi High Court",
                "judge": r.judge_name or " | ".join(m["judges"]),
                "court_no": r.court_number or m["court"],
                "date": r.date or date
            })

    return final_results

# ========================== RANGE SEARCH WRAPPER ==========================

def search_range(party_name: str, start_date: str, end_date: str) -> List[dict]:
    try:
        start_dt = datetime.strptime(start_date, "%d.%m.%Y")
        end_dt = datetime.strptime(end_date, "%d.%m.%Y")
    except ValueError:
        raise Exception("Invalid date format. Use DD.MM.YYYY for Delhi")

    if start_dt > end_dt:
        raise Exception("Start date cannot be after end date")

    all_results: List[dict] = []
    current = start_dt

    while current <= end_dt:
        cause_date = current.strftime("%d.%m.%Y")

        try:
            daily_results = search(party_name, cause_date)
            all_results.extend(daily_results)
        except Exception as e:
            print(f"[DELHI HIGH COURT] Skipping {cause_date}: {e}")

        current += timedelta(days=1)

    return all_results

# ========================== 🔥 CASE STATUS MONITOR (YEAR RANGE ONLY) ==========================

def monitor(keyword: str, year: str, mode: str = "party", headless: bool = True) -> List[dict]:

    URL = "https://delhihighcourt.nic.in/app/party-name-wise-status"

    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 120)

    results = []

    try:
        driver.get(URL)

        party_input = wait.until(
            EC.visibility_of_element_located((By.ID, "party_name"))
        )
        party_input.clear()
        party_input.send_keys(keyword)

        year_select_el = wait.until(
            EC.visibility_of_element_located((By.ID, "case_year"))
        )

        # Select specific year instead of index
        Select(year_select_el).select_by_visible_text(year)

        captcha_code = wait.until(
            EC.visibility_of_element_located((By.ID, "captcha-code"))
        ).text.strip()

        captcha_input = wait.until(
            EC.visibility_of_element_located((By.ID, "captchaInput"))
        )
        captcha_input.clear()
        captcha_input.send_keys(captcha_code)

        submit_btn = wait.until(
            EC.element_to_be_clickable((By.ID, "search"))
        )
        submit_btn.click()

        try:
            wait.until(
                EC.invisibility_of_element_located(
                    (By.ID, "registrarsTable_processing")
                )
            )
        except:
            pass

        wait.until(
            lambda d: len([
                r for r in d.find_elements(By.XPATH, "//table[@id='registrarsTable']//tbody/tr")
                if len(r.find_elements(By.TAG_NAME, "td")) >= 4
                and r.find_elements(By.TAG_NAME, "td")[1].text.strip() != ""
            ]) > 0
        )

        table = driver.find_element(By.ID, "registrarsTable")
        rows = table.find_elements(By.XPATH, ".//tbody/tr")

        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")

            if len(cols) < 4:
                continue
            if cols[1].text.strip() == "":
                continue

            case_info_raw = cols[1].text.strip()

            status = None
            if "[" in case_info_raw and "]" in case_info_raw:
                status = case_info_raw.split("[", 1)[1].split("]")[0].strip()

            case_number = case_info_raw.replace(f"[{status}]", "").strip() if status else case_info_raw

            party_info = cols[2].text.strip()
            listing_info = cols[3].text.strip()

            petitioner, respondent = split_parties(party_info)

            try:
                advocate = cols[2].find_element(By.XPATH, ".//following-sibling::td[1]").text.strip()
            except:
                advocate = "N/A"

            try:
                order_link = cols[1].find_element(
                    By.XPATH, ".//a[.//u[contains(text(),'Order')]]"
                ).get_attribute("href")
            except:
                order_link = None

            try:
                judgment_link = cols[1].find_element(
                    By.XPATH, ".//a[.//u[contains(text(),'Judgment')]]"
                ).get_attribute("href")
            except:
                judgment_link = None

            court_no = None
            if "COURT NO" in listing_info.upper():
                try:
                    court_no = listing_info.split("COURT NO", 1)[1].strip()
                except:
                    court_no = None

            results.append({
                "case_number": case_number,
                "status": status,
                "petitioner": petitioner,
                "respondent": respondent,
                "advocates": advocate,
                "listing_info": listing_info,
                "court": "Delhi High Court",
                "court_no": court_no,
                "order_link": order_link,
                "judgment_link": judgment_link
            })

    except Exception as e:
        print("[DELHI MONITOR ERROR]", e)
        return []

    finally:
        driver.quit()

    return results

