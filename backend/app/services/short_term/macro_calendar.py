"""Macro event calendar (FOMC rate decision days) for the short-term pick workflow."""
from datetime import date

# Decision day (second day of each meeting). Source:
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (page updated 2026-11-12).
# Dates are tentative until confirmed at the preceding meeting; extend this table every year.
# 2027-10-27 is listed by the Fed as a Monetary Policy Strategy Meeting (no regular decision guaranteed).
FOMC_DATES: tuple[str, ...] = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-11-17",
)


def events_between(start: date, end: date) -> list[dict]:
    return [
        {"date": d, "event": "FOMC 利率决议", "source": "federalreserve.gov"}
        for d in FOMC_DATES if start <= date.fromisoformat(d) <= end
    ]
