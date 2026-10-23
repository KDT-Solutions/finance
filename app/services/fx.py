"""Exchange rates: Swiss customs (BAZG/ESTV) daily rates, the official rates for imports and VAT.
https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=YYYYMMDD&locale=de returns <devise code="usd"><waehrung>1 USD</waehrung><kurs>0.83456</kurs>."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)
URL = "https://www.backend-rates.bazg.admin.ch/api/xmldaily"


def bazg_rate(currency: str, on: str = "") -> float | None:
    """CHF per 1 unit of currency on the given ISO date (falls back up to 5 days back, e.g. weekends)."""
    code = (currency or "").upper()
    if code in ("", "CHF"):
        return 1.0
    try:
        d = datetime.strptime(on, "%Y-%m-%d").date() if on else date.today()
    except ValueError:
        d = date.today()
    for back in range(6):
        day = d - timedelta(days=back)
        try:
            r = httpx.get(URL, params={"d": day.strftime("%Y%m%d"), "locale": "de"}, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:  # noqa: BLE001
            log.warning("bazg rates %s failed: %s", day, e)
            continue
        for dev in root.iter():
            if dev.tag.split("}")[-1] != "devise" or (dev.get("code") or "").upper() != code:
                continue
            unit = kurs = None
            for child in dev:
                tag = child.tag.split("}")[-1]
                if tag == "waehrung":
                    unit = float((child.text or "1").split()[0])
                elif tag == "kurs":
                    kurs = float(child.text or 0)
            if unit and kurs:
                return round(kurs / unit, 6)
    return None
