from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Union, Optional
from datetime import datetime
import os, json

from scrapers import nclat
from scrapers import supreme_court, delhi_high_court, bombay_high_court

app = FastAPI()

# ================== CORS ==================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== MODELS ==================

class SearchRequest(BaseModel):
    partyName: str
    date: str | None = None
    court: str


class SearchRangeRequest(BaseModel):
    partyName: str
    startDate: str
    endDate: str
    court: str


class MonitorRequest(BaseModel):
    keyword: str
    mode: str = "party"
    year: Optional[str] = None


class DownloadRequest(BaseModel):
    filename: str
    case_index: int


class WithCase(BaseModel):
    case_number: str
    details: str


class BaseCaseResult(BaseModel):
    case_number: str
    petitioner: str
    respondent: str
    advocates: str
    court: str
    judge: str | None = None
    court_no: str | None = None
    date: str | None = None


class BombayCaseResult(BaseCaseResult):
    remarks: str | None = None
    court_time: str | None = None
    with_cases: List[WithCase] = []


class DelhiStatusResult(BaseModel):
    case_number: str
    status: str | None = None
    petitioner: str
    respondent: str
    advocates: str
    listing_info: str
    court: str
    court_no: str | None = None
    order_link: str | None = None
    judgment_link: str | None = None


# ================== DIRS ==================

DATA_DIR = "monitor_data"
DOWNLOAD_DIR = "downloaded_judgments"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================== DATE HELPERS ==================

def convert_date_for_delhi(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%d.%m.%Y")


def convert_date_for_bombay(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%d-%m-%Y")


def convert_date_for_nclat(date_str: str) -> str:
    if "/" in date_str:
        return date_str

    if "-" in date_str and date_str[2] == "-":
        d = datetime.strptime(date_str, "%d-%m-%Y")
        return d.strftime("%d/%m/%Y")

    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%d/%m/%Y")


# ================== NORMAL SEARCH (SINGLE DATE) ==================

@app.post("/api/search", response_model=List[Union[BaseCaseResult, BombayCaseResult]])
async def search_cases(request: SearchRequest):

    results: List[dict] = []

    if not request.date:
        return results

    print("SEARCH:", request.court, request.date)

    if request.court in ("supreme", "all"):
        try:
            results.extend(
                supreme_court.search(request.partyName, request.date)
            )
        except Exception as e:
            print("Supreme Court failed:", e)

    if request.court in ("delhi", "all"):
        try:
            delhi_date = (
                convert_date_for_delhi(request.date)
                if request.court == "all"
                else request.date
            )
            results.extend(
                delhi_high_court.search(request.partyName, delhi_date)
            )
        except Exception as e:
            print("Delhi High Court failed:", e)

    if request.court in ("bombay", "all"):
        try:
            bombay_date = (
                convert_date_for_bombay(request.date)
                if request.court == "all"
                else request.date
            )
            bombay_results = await bombay_high_court.search(
                request.partyName, bombay_date
            )
            results.extend(bombay_results)
        except Exception as e:
            print("Bombay High Court failed:", e)

    # ✅ NCLAT SINGLE DATE (range with same day)

    if request.court == "nclat":
        try:
            print("🔥 NCLAT single-date search running")

            results.extend(
                nclat.search_range(
                    request.partyName,
                    convert_date_for_nclat(request.date),
                    convert_date_for_nclat(request.date)
                )
            )

        except Exception as e:
            print("NCLAT single-date failed:", e)

    return results


# ================== RANGE SEARCH ==================

@app.post("/api/search-range", response_model=List[Union[BaseCaseResult, BombayCaseResult]])
async def search_cases_range(request: SearchRangeRequest):

    results: List[dict] = []

    print("SEARCH RANGE:", request.court, request.startDate, request.endDate)

    if request.court in ("supreme", "all"):
        try:
            results.extend(
                supreme_court.search_range(
                    request.partyName,
                    request.startDate,
                    request.endDate
                )
            )
        except Exception as e:
            print("Supreme Court range failed:", e)

    if request.court in ("delhi", "all"):
        try:
            if request.court == "all":
                delhi_start = convert_date_for_delhi(request.startDate)
                delhi_end = convert_date_for_delhi(request.endDate)
            else:
                delhi_start = request.startDate
                delhi_end = request.endDate

            results.extend(
                delhi_high_court.search_range(
                    request.partyName,
                    delhi_start,
                    delhi_end
                )
            )
        except Exception as e:
            print("Delhi High Court range failed:", e)

    if request.court in ("bombay", "all"):
        try:
            if request.court == "all":
                bombay_start = convert_date_for_bombay(request.startDate)
                bombay_end = convert_date_for_bombay(request.endDate)
            else:
                bombay_start = request.startDate
                bombay_end = request.endDate

            bombay_results = await bombay_high_court.search_range(
                request.partyName,
                bombay_start,
                bombay_end
            )
            results.extend(bombay_results)
        except Exception as e:
            print("Bombay High Court range failed:", e)

    # ✅ NCLAT RANGE

    if request.court == "nclat":
        try:
            print("🔥 NCLAT range search running")

            results.extend(
                nclat.search_range(
                    request.partyName,
                    convert_date_for_nclat(request.startDate),
                    convert_date_for_nclat(request.endDate)
                )
            )

        except Exception as e:
            print("NCLAT range failed:", e)

    return results


# ================== SUPREME MONITOR ==================

@app.post("/api/supreme/monitor")
def supreme_monitor(req: MonitorRequest):

    keyword = req.keyword.strip()
    mode = req.mode

    all_results = supreme_court.monitor(keyword, mode)

    filename = f"{keyword.replace(' ', '_')}_{mode}.json"
    path = os.path.join(DATA_DIR, filename)

    old_data = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old_data = json.load(f)

    if old_data == all_results:
        return {
            "status": "no_change",
            "message": "No new updates",
            "count": len(all_results),
            "file": filename
        }

    new_items = [x for x in all_results if x not in old_data]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    return {
        "status": "updated",
        "message": "New judgments found",
        "new_items": new_items,
        "total": len(all_results),
        "file": filename
    }


# ================== DELHI MONITOR ==================

@app.post("/api/delhi/monitor", response_model=List[DelhiStatusResult])
def delhi_monitor(req: MonitorRequest):

    keyword = req.keyword.strip()
    mode = req.mode
    year = req.year

    try:
        results = delhi_high_court.monitor(
            keyword=keyword,
            year=year,
            mode=mode,
            headless=True
        )
    except Exception as e:
        print("Delhi case-status monitor failed:", e)
        return []

    return results


# ================== FILES ==================

@app.get("/api/supreme/monitors")
def list_saved_monitors():
    files = [f for f in os.listdir(DATA_DIR) if f.endswith(".json")]
    return {"files": sorted(files)}


@app.get("/health")
def health():
    return {"status": "ok"}
