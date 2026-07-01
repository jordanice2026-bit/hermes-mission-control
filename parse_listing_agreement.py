#!/usr/bin/env python3
"""
parse_listing_agreement.py
Indiana IAR Standard Exclusive Right-to-Sell Listing Agreement PDF Parser.

Usage:
    python3 parse_listing_agreement.py <pdf_path>

Output:
    JSON to stdout with extracted listing terms + signature detection.
"""
import sys
import os
import re
import json
from datetime import datetime, date
from typing import Optional, Tuple

PYLIBS = '/opt/data/pylibs'
if PYLIBS not in sys.path:
    sys.path.insert(0, PYLIBS)

try:
    import pdfplumber
except ImportError:
    import subprocess
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', 'pdfplumber', '--target', PYLIBS, '-q'],
        check=True,
    )
    import pdfplumber


def safe_json_dump(obj) -> str:
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(f'{type(o)} not serialisable')
    return json.dumps(obj, indent=2, default=default)


def parse_dollar(s: str) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r'[\$,\s]', '', s)
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_pct(s: str) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r'[%\s]', '', s)
    try:
        return float(cleaned)
    except ValueError:
        return None


_MONTH_ABBR = {
    'jan': 'January', 'feb': 'February', 'mar': 'March', 'apr': 'April',
    'may': 'May', 'jun': 'June', 'jul': 'July', 'aug': 'August',
    'sep': 'September', 'oct': 'October', 'nov': 'November', 'dec': 'December',
}
_DATE_PATTERNS = [
    (r'(\d{4})-(\d{1,2})-(\d{1,2})', '%Y-%m-%d'),
    (r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', '%m/%d/%Y'),
    (r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', '%B %d %Y'),
    (r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', '%d %B %Y'),
]


def normalise_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    for abbr, full in _MONTH_ABBR.items():
        raw = re.sub(r'\b' + abbr + r'\.?\b', full, raw, flags=re.IGNORECASE)
    for pattern, fmt in _DATE_PATTERNS:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            cand = m.group(0).replace('-', '/')
            no_comma = cand.replace(',', '')
            for f in [fmt, '%m/%d/%Y', '%m/%d/%y', '%B %d %Y', '%B %d, %Y',
                      '%b %d %Y', '%b %d, %Y', '%d %B %Y', '%Y/%m/%d']:
                for c in (cand, no_comma):
                    try:
                        return datetime.strptime(c.strip(), f).strftime('%Y-%m-%d')
                    except ValueError:
                        continue
    return None


def extract_text(pdf_path: str) -> Tuple[Optional[str], list]:
    warnings = []
    if not os.path.isfile(pdf_path):
        return None, [f'File not found: {pdf_path}']
    try:
        parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for pg in pdf.pages:
                parts.append(pg.extract_text() or '')
        return '\n'.join(parts), warnings
    except Exception as e:
        return None, [f'PDF extract error: {e}']


def _search(text: str, patterns) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def parse_listing(text: str) -> dict:
    out = {}

    # Property address — line after "Property" / "Address" label
    out['property_address'] = _search(text, [
        r'(?:Property Address|Address of Property|Property)[:\s]+([0-9][^\n]{5,80})',
        r'(?:located at|commonly known as)[:\s]+([0-9][^\n]{5,80})',
    ])

    # List price
    lp = _search(text, [
        r'(?:List(?:ing)? Price|Asking Price|Price of)[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)',
        r'listed for[:\s]*\$?\s*([\d,]+)',
    ])
    out['list_price'] = parse_dollar(lp) if lp else None

    # Commission %
    comm = _search(text, [
        r'(?:commission|fee) of\s*([\d.]+)\s*%',
        r'([\d.]+)\s*%\s*(?:commission|of the (?:gross )?sale)',
        r'(?:total commission|compensation)[:\s]*([\d.]+)\s*%',
    ])
    out['commission_pct'] = parse_pct(comm) if comm else None

    # Listing type
    lt = None
    if re.search(r'exclusive right[\s-]*to[\s-]*sell', text, re.IGNORECASE):
        lt = 'Exclusive Right to Sell'
    elif re.search(r'exclusive agency', text, re.IGNORECASE):
        lt = 'Exclusive Agency'
    elif re.search(r'\bopen listing\b', text, re.IGNORECASE):
        lt = 'Open'
    out['listing_type'] = lt

    # Property type (unit count)
    pt = None
    if re.search(r'\bfour[\s-]*plex|4[\s-]*unit|4[\s-]*plex\b', text, re.IGNORECASE):
        pt = 'Fourplex (4 unit)'
    elif re.search(r'\btri[\s-]*plex|3[\s-]*unit|3[\s-]*plex\b', text, re.IGNORECASE):
        pt = 'Triplex (3 unit)'
    elif re.search(r'\bdu[\s-]*plex|2[\s-]*unit|2[\s-]*plex\b', text, re.IGNORECASE):
        pt = 'Duplex (2 unit)'
    elif re.search(r'single[\s-]*family|1[\s-]*unit', text, re.IGNORECASE):
        pt = 'Single Family (1 unit)'
    out['property_type'] = pt

    # Listing start / begin date
    sd = _search(text, [
        r'(?:begins?|commenc\w+|effective|start(?:ing)? date|listing date)[:\s]*'
        r'((?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})|(?:[A-Za-z]+ \d{1,2},? \d{4}))',
    ])
    out['listing_start_date'] = normalise_date(sd) if sd else None

    # Expiration date
    ed = _search(text, [
        r'(?:expir\w+|end(?:ing)? date|terminat\w+|through)[:\s]*'
        r'((?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})|(?:[A-Za-z]+ \d{1,2},? \d{4}))',
    ])
    out['listing_expiration'] = normalise_date(ed) if ed else None

    # Seller names — near "Seller" / "Owner" signature lines
    sellers = _search(text, [
        r'(?:Seller|Owner)(?:\(s\))?(?:\s*name)?[:\s]+([A-Z][A-Za-z.\'\- ]+(?:(?:and|&|,)\s*[A-Z][A-Za-z.\'\- ]+)?)',
    ])
    out['seller_names'] = sellers

    # MLS number
    out['mls_number'] = _search(text, [
        r'MLS\s*#?\s*[:]?\s*([0-9]{5,9})',
    ])

    # Signature detection — heuristic for "fully executed"
    sig_signals = 0
    if re.search(r'/s/|electronically signed|docusign|signature[:\s]*[A-Za-z]', text, re.IGNORECASE):
        sig_signals += 1
    # A date near a "Seller" signature block
    if re.search(r'Seller[^\n]{0,40}(?:sign|date)', text, re.IGNORECASE):
        sig_signals += 1
    if re.search(r'Broker[^\n]{0,40}(?:sign|date)', text, re.IGNORECASE):
        sig_signals += 1
    out['signatures_detected'] = sig_signals
    out['likely_executed'] = sig_signals >= 2

    return out


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'usage: parse_listing_agreement.py <pdf>'}))
        sys.exit(1)
    text, warnings = extract_text(sys.argv[1])
    if text is None:
        print(json.dumps({'error': 'extraction failed', 'warnings': warnings}))
        sys.exit(1)
    parsed = parse_listing(text)
    parsed['_warnings'] = warnings
    parsed['_char_count'] = len(text)
    print(safe_json_dump(parsed))


if __name__ == '__main__':
    main()
