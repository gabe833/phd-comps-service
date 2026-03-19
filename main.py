from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from homeharvest import scrape_property
from datetime import datetime, timedelta
import math

app = FastAPI(title="PHD Properties Comps Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "service": "PHD Comps"}

@app.get("/comps")
def get_comps(
    address: str,
    beds: str = None,
    baths: str = None,
    sqft: str = None,
    radius: float = 0.5,
    days: int = 365
):
    """
    Fetch real sold comps near an address using HomeHarvest (Realtor.com).
    Returns up to 8 best matching sold properties.
    """
    try:
        # Parse subject property specs
        subject_beds = float(beds) if beds else None
        subject_baths = float(baths) if baths else None
        subject_sqft = float(sqft) if sqft else None

        # Extract zip/city from address for the search
        # HomeHarvest searches by location string
        parts = [p.strip() for p in address.split(',')]
        # Try zip code first (most precise), fall back to city+state
        location = None
        for part in reversed(parts):
            part = part.strip()
            if any(c.isdigit() for c in part) and len(part) >= 5:
                location = part  # zip code
                break
        if not location and len(parts) >= 2:
            location = ', '.join(parts[-2:])  # city, state
        if not location:
            location = address

        # Search sold listings
        past_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        
        results = scrape_property(
            location=location,
            listing_type="sold",
            date_from=past_date,
            radius=radius,
        )

        if results is None or len(results) == 0:
            # Widen search if no results
            results = scrape_property(
                location=location,
                listing_type="sold",
                date_from=(datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d"),
                radius=1.0,
            )

        if results is None or len(results) == 0:
            return {"comps": [], "source": "HomeHarvest/Realtor.com", "message": "No sold listings found in this area"}

        comps = []
        for _, row in results.iterrows():
            try:
                sale_price = float(row.get("sold_price") or row.get("list_price") or 0)
                sqft_val = float(row.get("sqft") or 0)
                if sale_price < 50000 or sqft_val < 200:
                    continue

                # Score similarity to subject property
                score = 0
                if subject_beds and row.get("beds"):
                    bed_diff = abs(float(row["beds"]) - subject_beds)
                    score += max(0, 10 - bed_diff * 3)
                if subject_sqft and sqft_val:
                    sqft_diff = abs(sqft_val - subject_sqft) / subject_sqft
                    score += max(0, 10 - sqft_diff * 20)
                if subject_baths and row.get("full_baths"):
                    bath_diff = abs(float(row["full_baths"]) - subject_baths)
                    score += max(0, 5 - bath_diff * 2)

                # Format address
                addr_parts = [
                    str(row.get("street") or "").strip(),
                    str(row.get("city") or "").strip(),
                    str(row.get("state") or "").strip(),
                    str(row.get("zip_code") or "").strip(),
                ]
                full_addr = ", ".join(p for p in addr_parts if p and p != "nan")

                beds_val = row.get("beds")
                baths_val = row.get("full_baths")
                beds_baths = f"{int(float(beds_val))}bd/{int(float(baths_val))}ba" if beds_val and baths_val and str(beds_val) != "nan" else "—"

                sold_date = row.get("sold_date") or row.get("last_sold_date")
                date_str = str(sold_date)[:10] if sold_date and str(sold_date) != "nan" else "—"

                psf = round(sale_price / sqft_val) if sqft_val > 0 else 0

                style = str(row.get("style") or "")
                property_url = str(row.get("property_url") or "")

                comps.append({
                    "address": full_addr,
                    "bedsBaths": beds_baths,
                    "sqft": int(sqft_val) if sqft_val else "—",
                    "salePrice": int(sale_price),
                    "pricePerSqft": psf,
                    "date": date_str,
                    "verified": True,
                    "source": "Realtor.com",
                    "url": property_url,
                    "vsSubject": "Similar" if score >= 15 else "Nearby",
                    "notes": f"Realtor.com confirmed sale" + (f" · {style}" if style and style != "nan" else ""),
                    "_score": score,
                })
            except Exception:
                continue

        # Sort by similarity score then by date (most recent first)
        comps.sort(key=lambda x: (-x["_score"], x["date"]), reverse=False)
        comps.sort(key=lambda x: x["_score"], reverse=True)

        # Remove internal score field
        for c in comps:
            c.pop("_score", None)

        return {
            "comps": comps[:8],
            "total_found": len(comps),
            "source": "Realtor.com via HomeHarvest",
            "location_searched": location,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/rent")
def get_rent(
    zipcode: str,
    beds: int = 3,
    baths: float = 2,
    sqft: int = None
):
    """
    Fetch active rental listings for rent range estimates.
    """
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
                price = float(row.get("list_price") or 0)
                r_beds = float(row.get("beds") or 0)
                r_sqft = float(row.get("sqft") or 0)

                if price < 500 or r_beds == 0:
                    continue
                # Filter to similar bedroom count
                if abs(r_beds - beds) <= 1:
                    rents.append(price)
            except Exception:
                continue

        if not rents:
            raise HTTPException(status_code=404, detail="No matching rentals found")

        rents.sort()
        n = len(rents)
        low = rents[max(0, int(n * 0.10))]
        median = rents[int(n * 0.50)]
        high = rents[min(n - 1, int(n * 0.90))]

        return {
            "rentLow": int(low),
            "rentMedian": int(median),
            "rentHigh": int(high),
            "samplesUsed": n,
            "source": "Realtor.com active rentals",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
