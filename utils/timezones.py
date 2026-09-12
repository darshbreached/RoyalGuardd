"""
utils/timezones.py
--------------------
Maps IP-geolocation country/region data (already collected during Roblox
verification) to a timezone role LABEL - a plain string like "EST" or
"CET" that the bot looks for as an exact role NAME in the guild, same
convention as verified_roles/extra_roles.

This is inherently approximate: IP geolocation only gives country
(and sometimes state/province), not a precise coordinate, and several
countries (US, Canada, Russia, Australia) span multiple real timezones.
US and Canada get a best-effort refinement using the region field already
stored from verification; every other multi-zone country falls back to one
representative label and will be wrong for people outside that zone.
"""

US_STATE_TIMEZONES = {
    "connecticut": "EST", "delaware": "EST", "florida": "EST", "georgia": "EST",
    "maine": "EST", "maryland": "EST", "massachusetts": "EST", "new hampshire": "EST",
    "new jersey": "EST", "new york": "EST", "north carolina": "EST", "ohio": "EST",
    "pennsylvania": "EST", "rhode island": "EST", "south carolina": "EST",
    "vermont": "EST", "virginia": "EST", "west virginia": "EST", "michigan": "EST",
    "indiana": "EST", "district of columbia": "EST",

    "alabama": "CST", "arkansas": "CST", "illinois": "CST", "iowa": "CST",
    "kansas": "CST", "kentucky": "CST", "louisiana": "CST", "minnesota": "CST",
    "mississippi": "CST", "missouri": "CST", "nebraska": "CST", "north dakota": "CST",
    "oklahoma": "CST", "south dakota": "CST", "tennessee": "CST", "texas": "CST",
    "wisconsin": "CST",

    "arizona": "MST", "colorado": "MST", "idaho": "MST", "montana": "MST",
    "new mexico": "MST", "utah": "MST", "wyoming": "MST",

    "california": "PST", "nevada": "PST", "oregon": "PST", "washington": "PST",

    "alaska": "AKST", "hawaii": "HST",
}

CANADA_PROVINCE_TIMEZONES = {
    "newfoundland and labrador": "NST",
    "nova scotia": "AST", "new brunswick": "AST", "prince edward island": "AST",
    "quebec": "EST", "ontario": "EST",
    "manitoba": "CST", "saskatchewan": "CST",
    "alberta": "MST",
    "british columbia": "PST", "yukon": "PST",
    "northwest territories": "MST", "nunavut": "CST",
}

# One representative label per country - see module docstring for the
# accuracy limitation on multi-timezone countries.
COUNTRY_TIMEZONES = {
    "US": "EST", "CA": "EST",  # refined further below using region
    "GB": "GMT", "IE": "GMT", "PT": "GMT",
    "FR": "CET", "DE": "CET", "ES": "CET", "IT": "CET", "NL": "CET",
    "BE": "CET", "CH": "CET", "AT": "CET", "PL": "CET", "SE": "CET",
    "NO": "CET", "DK": "CET",
    "FI": "EET", "GR": "EET", "RO": "EET", "UA": "EET", "TR": "EET",
    "RU": "MSK",
    "AE": "GST", "SA": "AST_ARABIA",
    "IN": "IST", "PK": "PKT", "BD": "BST_BANGLADESH",
    "CN": "CST_CHINA", "SG": "SGT", "MY": "MYT", "PH": "PHT", "ID": "WIB",
    "JP": "JST", "KR": "KST",
    "AU": "AEST", "NZ": "NZST",
    "BR": "BRT", "AR": "ART", "MX": "CST_MEXICO",
    "ZA": "SAST", "EG": "EET", "NG": "WAT", "KE": "EAT",
}


def get_timezone_label(country_code: str, region: str = None) -> str:
    """Returns a timezone role label like 'EST' or 'CET', or None if the
    country isn't in the map."""
    if not country_code:
        return None
    country_code = country_code.upper()

    if country_code == "US" and region:
        refined = US_STATE_TIMEZONES.get(region.strip().lower())
        if refined:
            return refined

    if country_code == "CA" and region:
        refined = CANADA_PROVINCE_TIMEZONES.get(region.strip().lower())
        if refined:
            return refined

    return COUNTRY_TIMEZONES.get(country_code)


def all_timezone_labels() -> set:
    """Every label this module can ever produce - used to know which roles
    to strip from a member when their timezone role changes."""
    labels = set(COUNTRY_TIMEZONES.values())
    labels.update(US_STATE_TIMEZONES.values())
    labels.update(CANADA_PROVINCE_TIMEZONES.values())
    return labels
