import io
import re
import pdfplumber
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

app = FastAPI()

# Allow Lovable app to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOGIN_URL = "https://dah.skybook.aero/login"
USERNAME  = "dispatcher"
PASSWORD  = "Plan@2018"


class FlightRequest(BaseModel):
    flight: str


def strip_zeros(val):
    return str(int(val)) if val else None

def extract_ofp_data(text):
    data = {}
    m = re.search(r'PLAN\s+\d+\s+(DAH\d+)', text)
    data["flight_number"]      = m.group(1) if m else None
    m = re.search(r'\b([A-Z]{4})\s+TO\s+([A-Z]{4})\b', text)
    data["departure"]          = m.group(1) if m else None
    data["arrival"]            = m.group(2) if m else None
    m = re.search(r'FOR\s+ETD\s+(\d{4}Z?)', text)
    data["etd"]                = m.group(1) if m else None
    m = re.search(r'\b(7T[A-Z]{3})\b', text)
    data["registration"]       = m.group(1) if m else None
    m = re.search(r'^DEST\s+\w+\s+\d+[\s.]+\d+/\d+\s+\d+\s+\d+\s+(\d{3})', text, re.MULTILINE)
    if not m:
        m = re.search(r'\bFL\s+(\d{3})/', text)
    data["flight_level"]       = m.group(1) if m else None
    # Trip fuel = E.FUEL on the DEST line (the number right after the arrival airport code)
    # Estimated time = E.TME on the DEST line (HH/MM after the dots)
    m = re.search(r'^DEST\s+[A-Z]{4}\s+(\d{5,6})[\s.]+(\d{2}/\d{2})', text, re.MULTILINE)
    data["trip_fuel_kg"]       = strip_zeros(m.group(1)) if m else None
    data["estimated_time"]     = m.group(2) if m else None
    m = re.search(r'^ALT\s+(?:[A-Z]{4}\s+)?(\d{5,6})', text, re.MULTILINE)
    data["alternate_fuel_kg"]  = strip_zeros(m.group(1)) if m else None
    m = re.search(r'^F\.R\.\s+(\d{5,6})', text, re.MULTILINE)
    data["final_reserve_kg"]   = strip_zeros(m.group(1)) if m else None
    m = re.search(r'\bEPLD\s+(\d+)', text)
    data["epld_kg"]            = strip_zeros(m.group(1)) if m else None
    m = re.search(r'\bEZFW\s+(\d+)', text)
    data["ezfw_kg"]            = strip_zeros(m.group(1)) if m else None
    m = re.search(r'\bETOW\s+(\d+)', text)
    data["etow_kg"]            = strip_zeros(m.group(1)) if m else None
    m = re.search(r'WIND\s+([PM]\d+)', text)
    data["wind"]               = m.group(1) if m else None
    m = re.search(r'MXSH\s+([\d]+/\w+)', text)
    data["turbulence"]         = m.group(1) if m else None
    return data


@app.post("/ofp")
def get_ofp(req: FlightRequest):
    flight    = req.flight.strip().upper()
    pdf_bytes = []
    ofp_text  = None
    ofp_data  = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page    = context.new_page()

            def handle_response(response):
                ct = response.headers.get("content-type", "")
                if "pdf" in ct and response.status == 200:
                    try:
                        pdf_bytes.append(response.body())
                    except Exception:
                        pass

            page.on("response", handle_response)

            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=20_000)
            page.locator('input[type="text"]').first.fill(USERNAME)
            page.locator('input[type="password"]').first.fill(PASSWORD)
            page.locator('button[type="submit"]').first.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            page.wait_for_timeout(2000)
            page.wait_for_selector("[class*='row']:has-text('DAH')", timeout=30_000)

            search = page.locator("input#searchBar")
            search.wait_for(state="visible", timeout=10_000)
            search.click()
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(150)
            page.keyboard.type(flight, delay=80)
            page.wait_for_timeout(2000)

            # Check flight exists
            try:
                page.locator(f"[class*='row']:has-text('{flight}')").first.wait_for(state="visible", timeout=4000)
            except PlaywrightTimeout:
                browser.close()
                return {"status": "not_found", "message": f"{flight} — THIS FLIGHT NOT YET CREATED"}

            dots_el = None
            for sel in ["[data-testid='icon-button']", "button:has([data-testid='MoreHorizIcon'])",
                        "table tbody tr:first-child td:first-child button",
                        "[class*='row']:first-child button"]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=2000)
                    dots_el = loc
                    break
                except PlaywrightTimeout:
                    continue

            if not dots_el:
                browser.close()
                return {"status": "error", "message": "Could not find ••• button"}

            dots_el.click()
            page.wait_for_timeout(800)

            ofp_el = None
            for sel in ["text=View OFP", "[role='menuitem']:has-text('View OFP')",
                        "li:has-text('View OFP')", "a:has-text('View OFP')"]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3000)
                    ofp_el = loc
                    break
                except PlaywrightTimeout:
                    continue

            if not ofp_el:
                browser.close()
                return {"status": "error", "message": "Could not find View OFP"}

            ofp_el.click()
            page.wait_for_timeout(3000)

            if pdf_bytes:
                full_text = []
                with pdfplumber.open(io.BytesIO(pdf_bytes[-1])) as pdf:
                    for i, pg in enumerate(pdf.pages, 1):
                        t = pg.extract_text()
                        if t:
                            full_text.append(f"--- Page {i} ---\n{t}")
                ofp_text = "\n\n".join(full_text)
                ofp_data = extract_ofp_data(ofp_text)

            browser.close()

        return {
            "status": "ok",
            "flight": flight,
            "data":   ofp_data,
            "text":   ofp_text,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/health")
def health():
    return {"status": "ok"}
