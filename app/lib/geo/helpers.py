from app.utils.dtos.Location import Location



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def country_code_to_flag(code: str) -> str:
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    base = 0x1F1E6 - ord("A")
    return chr(base + ord(code[0])) + chr(base + ord(code[1]))


# ---------------------------------------------------------------------------
# GeoNames row parsing
#
# allCountries.txt columns (tab-separated):
#   0  geonameid
#   1  name
#   4  latitude
#   5  longitude
#   6  feature_class  (P = populated place, what we want)
#   8  country_code
#  15  elevation
#  16  dem
#  17  timezone
# ---------------------------------------------------------------------------

def _parse_int(value: str) -> int | None:
    v = str(value or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def location_from_row(parts: list[str]) -> Location | None:
    if len(parts) < 18:
        return None
    try:
        if parts[6].strip() != "P":
            return None

        country_code = parts[8].strip().upper()
        if not country_code:
            return None

        return Location(
            geoname_id=parts[0].strip(),
            name=parts[1].strip(),
            lat=float(parts[4]),
            lon=float(parts[5]),
            country_code=country_code,
            country_flag_emoji=country_code_to_flag(country_code),
            timezone=parts[17].strip(),
            elevation=_parse_int(parts[15]) or _parse_int(parts[16]),
        )
    except (ValueError, IndexError):
        return None


