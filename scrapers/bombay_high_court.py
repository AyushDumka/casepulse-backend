import re
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from typing import List, Dict
from datetime import datetime, timedelta

BOMBAY_CAUSELIST_URL = "https://bombayhighcourt.gov.in/bhc/causelistFinal"

# ================== UTIL ==================

def split_parties(parties: str):
    if not parties:
        return "N/A", "N/A"

    parts = re.split(r"\s+v(?:s\.?)?\s+", parties, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()

    return parties.strip(), "N/A"

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()

# ================== CORE EXTRACTION ==================

async def extract_cases_from_table(table, bench, court_time, court_no, date) -> List[Dict]:
    rows = table.locator("tbody tr")

    results: List[Dict] = []
    current_main_case = None
    last_case_no = None

    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = await cells.count()

        if cell_count == 4:
            cl_no_raw = normalize_text(await cells.nth(0).inner_text())
            case_no = normalize_text(await cells.nth(1).inner_text())
            parties = normalize_text(await cells.nth(2).inner_text())
            advocates = normalize_text(await cells.nth(3).inner_text())

            petitioner, respondent = split_parties(parties)

            # 🔥 CL.NO FIX (includes "0")
            is_cl_no = bool(re.fullmatch(r"\d+", cl_no_raw))
            is_new_case = is_cl_no or (case_no and case_no != last_case_no)

            if is_new_case:
                current_main_case = {
                    "case_number": case_no,
                    "petitioner": petitioner,
                    "respondent": respondent,
                    "advocates": advocates or "N/A",
                    "court": "Bombay High Court",
                    "judge": bench,
                    "court_no": court_no,
                    "date": date,
                    "court_time": court_time,
                    "with_cases": [],
                    "remarks": ""
                }
                results.append(current_main_case)
                last_case_no = case_no

            elif cl_no_raw.lower() == "with" and current_main_case:
                current_main_case["with_cases"].append({
                    "case_number": case_no,
                    "details": parties
                })

        elif cell_count == 1 and current_main_case:
            text = normalize_text(await cells.nth(0).inner_text())
            if text:
                current_main_case["remarks"] += (
                    text if not current_main_case["remarks"]
                    else "\n" + text
                )

    return results

# ================== PUBLIC API ==================

async def search(party_name: str, date: str | None) -> List[dict]:
    if not party_name or not date:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(BOMBAY_CAUSELIST_URL, timeout=60000)
            await page.wait_for_load_state("networkidle", timeout=60000)

            # Click Party tab
            await page.get_by_text("Party", exact=True).click()

            # Fill party name
            await page.fill("input[placeholder='Party Name']", party_name)

            # Set date via JS
            await page.evaluate(
                """(d) => {
                    const i = document.querySelector("input.form-control.datepicker");
                    if (i) {
                        i.value = d;
                        i.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                date
            )

            # Click search
            await page.get_by_role("button", name="Search").click()

            try:
                await page.wait_for_selector("#tb_causelist", timeout=60000, state="visible")
            except PlaywrightTimeout:
                html = await page.content()
                with open("bombay_debug.html", "w", encoding="utf-8") as f:
                    f.write(html)
                raise Exception("Bombay HC: #tb_causelist not found.")

            container = page.locator("#tb_causelist")

            # 🔥 WALK DOM IN ORDER: h3 → table → h3 → table
            nodes = container.locator("h3, table")
            node_count = await nodes.count()

            current_bench = "N/A"
            current_time = "N/A"
            current_court_no = "N/A"

            all_results: List[Dict] = []

            for i in range(node_count):
                node = nodes.nth(i)
                tag = await node.evaluate("n => n.tagName.toLowerCase()")

                if tag == "h3":
                    text = normalize_text(await node.inner_text())

                    # Judges
                    if re.search(r"HON'?BLE\s+SHRI|HON'?BLE\s+MS|HON'?BLE\s+JUSTICE", text, re.IGNORECASE):
                        current_bench = text

                elif tag == "table":
                    # Extract time & court no just before this table
                    preceding_text = normalize_text(await container.inner_text())

                    time_match = re.search(
                        r"AT\s+\d{1,2}\.\d{2}\s*[AP]\.M\.",
                        preceding_text,
                        re.IGNORECASE
                    )
                    court_time = time_match.group(0) if time_match else current_time
                    current_time = court_time

                    court_no_match = re.search(r"COURT\s+NO\s*[:\-]?\s*(\d+)", preceding_text, re.IGNORECASE)
                    court_no = court_no_match.group(1) if court_no_match else current_court_no
                    current_court_no = court_no

                    date_match = re.search(r"DATE\s*[:\-]?\s*(\d{2}-\d{2}-\d{4})", preceding_text, re.IGNORECASE)
                    date_val = date_match.group(1) if date_match else date

                    table_results = await extract_cases_from_table(
                        node,
                        current_bench,
                        current_time,
                        current_court_no,
                        date_val
                    )
                    all_results.extend(table_results)

            # 🔥 FINAL NORMALIZED OUTPUT
            final_results: List[dict] = []

            for case in all_results:
                final_results.append({
                    "case_number": case.get("case_number"),
                    "petitioner": case.get("petitioner"),
                    "respondent": case.get("respondent"),
                    "advocates": case.get("advocates"),
                    "court": case.get("court"),
                    "judge": case.get("judge"),
                    "court_no": case.get("court_no"),
                    "date": case.get("date"),
                    "court_time": case.get("court_time"),
                    "remarks": case.get("remarks"),
                    "with_cases": case.get("with_cases", []),
                })

            return final_results

        finally:
            await browser.close()


# ========================== RANGE SEARCH WRAPPER ==========================
async def search_range(party_name: str, start_date: str, end_date: str) -> List[dict]:
    """
    Bombay High Court date-range search.
    Accepts DD-MM-YYYY from UI.
    Calls search() for each date separately.
    Does NOT modify existing logic.
    """

    try:
        # 🔥 BOMBAY FORMAT
        start_dt = datetime.strptime(start_date, "%d-%m-%Y")
        end_dt = datetime.strptime(end_date, "%d-%m-%Y")
    except ValueError:
        raise Exception("Invalid date format. Use DD-MM-YYYY for Bombay")

    if start_dt > end_dt:
        raise Exception("Start date cannot be after end date")

    all_results: List[dict] = []
    current = start_dt

    while current <= end_dt:
        cause_date = current.strftime("%d-%m-%Y")  # Bombay format

        try:
            daily_results = await search(party_name, cause_date)
            all_results.extend(daily_results)
        except Exception as e:
            print(f"[BOMBAY HIGH COURT] Skipping {cause_date}: {e}")

        current += timedelta(days=1)

    return all_results
