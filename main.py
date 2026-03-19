from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from homeharvest import scrape_property
from datetime import datetime
import math

app = FastAPI(title="PHD Properties Comps Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return f if not math.isnan(f) else default
    except (TypeError, ValueError):
        return default

def safe_int(val, default=0):
    try:
        f = float(val)
        return int(f) if not math.isnan(f) else default
    except (TypeError, ValueError):
        return default

def safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    return default if s in ("nan", "None", "") else s

@app.get("/health")
def health():
    return {"status": "ok", "service": "PHD Comps", "version": "1.0"}

@app.get("/comps")
def get_comps(
    address: str,
    beds: str = None,
    baths: str = None,
    sqft: str = None,
    radius: float = 0.5,
    days: int = 548
):
    try:
        subject_beds   = safe_float(beds)   if beds   else None
        subject_baths  = safe_float(baths)  if baths  else None
        subject_sqft   = safe_float(sqft)   if sqft   else None

        # Try full address first for radius search, fall back to zip
        location = address
        parts = [p.strip() for p in address.split(',')]
        zipcode = None
        for part in reversed(parts):
            cleaned = part.strip()
            digits = ''.join(c for c in cleaned if c.isdigit())
            if len(digits) == 5:
                zipcode = digits
                break

        results = None

        # Attempt 1: full address + radius (most precise)
        try:
            results = scrape_property(
                location=address,
                listing_type="sold",
                past_days=days,
                radius=radius,
            )
        except Exception:
            results = None

        # Attempt 2: zip code, wider radius
        if results is None or len(results) == 0:
            loc2 = zipcode or (', '.join(parts[-2:]) if len(parts) >= 2 else address)
            try:
                results = scrape_property(
                    location=loc2,
                    listing_type="sold",
                    past_days=730,
                    radius=1.0,
                )
            except Exception:
                results = None

        if results is None or len(results) == 0:
            return {
                "comps": [],
                "total_found": 0,
                "source": "Realtor.com via HomeHarvest",
                "message": "No sold listings found — try widening your search or check the address format"
            }

        comps = []
        for _, row in results.iterrows():
            try:
                # 0.8.x flat pandas columns
                sale_price = safe_float(row.get("sold_price") or row.get("list_price"))
                sqft_val   = safe_float(row.get("sqft"))
                beds_val   = safe_float(row.get("beds"))
                baths_val  = safe_float(row.get("full_baths"))

                if sale_price < 30000 or sqft_val < 200:
                    continue

                # Similarity score
                score = 0
                if subject_beds and beds_val:
                    score += max(0, 10 - abs(beds_val - subject_beds) * 3)
                if subject_sqft and sqft_val:
                    score += max(0, 10 - (abs(sqft_val - subject_sqft) / subject_sqft) * 20)
                if subject_baths and baths_val:
                    score += max(0, 5 - abs(baths_val - subject_baths) * 2)

                # Address — 0.8.x has flat columns: street, city, state, zip_code
                street   = safe_str(row.get("street"))
                city     = safe_str(row.get("city"))
                state    = safe_str(row.get("state"))
                zip_code = safe_str(row.get("zip_code"))
                full_addr = ", ".join(p for p in [street, city, state, zip_code] if p)
                if not full_addr:
                    full_addr = safe_str(row.get("property_url", "Unknown"))

                beds_str  = str(int(beds_val))  if beds_val  else "—"
                baths_str = str(int(baths_val)) if baths_val else "—"
                beds_baths = f"{beds_str}bd/{baths_str}ba" if beds_val else "—"

                sold_date = row.get("sold_date") or row.get("last_sold_date")
                date_str  = str(sold_date)[:10] if sold_date and safe_str(str(sold_date)) else "—"

                psf = round(sale_price / sqft_val) if sqft_val > 0 else 0

                mls    = safe_str(row.get("mls"))
                mls_id = safe_str(row.get("mls_id"))
                source = f"Realtor.com" + (f" · {mls} #{mls_id}" if mls and mls_id else "")

                style = safe_str(row.get("style"))

                comps.append({
                    "address":      full_addr,
                    "bedsBaths":    beds_baths,
                    "sqft":         safe_int(sqft_val) or "—",
                    "salePrice":    int(sale_price),
                    "pricePerSqft": psf,
                    "date":         date_str,
                    "verified":     True,
                    "source":       source,
                    "vsSubject":    "Very similar" if score >= 18 else "Similar" if score >= 10 else "Nearby",
                    "notes":        f"Realtor.com sale" + (f" · {style}" if style else ""),
                    "_score":       score,
                })
            except Exception:
                continue

        comps.sort(key=lambda x: x["_score"], reverse=True)
        for c in comps:
            c.pop("_score", None)

        return {
            "comps":          comps[:8],
            "total_found":    len(comps),
            "source":         "Realtor.com via HomeHarvest",
            "location_searched": location,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/rent")
def get_rent(zipcode: str, beds: int = 3):
    try:
        results = scrape_property(
            location=zipcode,
            listing_type="for_rent",
        )
        if results is None or len(results) == 0:
            raise HTTPException(status_code=404, detail="No rental listings found")

        rents = []
        for _, row in results.iterrows():
            try:
                price  = safe_float(row.get("list_price"))
                r_beds = safe_float(row.get("beds"))
                if price > 500 and r_beds > 0 and abs(r_beds - beds) <= 1:
                    rents.append(price)
            except Exception:
                continue

        if not rents:
            raise HTTPException(status_code=404, detail="No matching rentals found")

        rents.sort()
        n   = len(rents)
        low    = int(rents[max(0, int(n * 0.10))])
        median = int(rents[int(n * 0.50)])
        high   = int(rents[min(n - 1, int(n * 0.90))])

        return {
            "rentLow":     low,
            "rentMedian":  median,
            "rentHigh":    high,
            "samplesUsed": n,
            "source":      "Realtor.com active rentals",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
