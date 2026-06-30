#!/usr/bin/env python3
"""
parse_purchase_agreement.py
Indiana IAR Standard Residential Purchase Agreement PDF Parser

Usage:
    python3 parse_purchase_agreement.py <pdf_path>

Output:
    JSON to stdout with extracted transaction data.
"""

import sys
import os
import re
import json
from datetime import datetime, date, timedelta
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap pdfplumber from local target dir if needed
# ---------------------------------------------------------------------------
PYLIBS = '/opt/data/pylibs'
if PYLIBS not in sys.path:
    sys.path.insert(0, PYLIBS)

try:
    import pdfplumber
except ImportError:
    import subprocess
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', 'pdfplumber', '--target', PYLIBS, '-q'],
        check=True
    )
    import pdfplumber


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def safe_json_dump(obj) -> str:
    """Return a JSON string, serialising datetime/date objects."""
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(f'Object of type {type(o)} is not JSON serialisable')
    return json.dumps(obj, indent=2, default=default)


def parse_dollar(s: str) -> Optional[float]:
    """Strip $, commas, spaces and convert to float. Returns None on failure."""
    if not s:
        return None
    cleaned = re.sub(r'[\$,\s]', '', s)
    try:
        return float(cleaned)
    except ValueError:
        return None


# Date normalisation — handles many common US real-estate formats
_DATE_PATTERNS = [
    (r'(\d{4})-(\d{1,2})-(\d{1,2})', '%Y-%m-%d'),
    (r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', '%m/%d/%Y'),
    (r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', '%B %d %Y'),
    (r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', '%d %B %Y'),
]

_MONTH_ABBR = {
    'jan': 'January', 'feb': 'February', 'mar': 'March', 'apr': 'April',
    'may': 'May', 'jun': 'June', 'jul': 'July', 'aug': 'August',
    'sep': 'September', 'oct': 'October', 'nov': 'November', 'dec': 'December',
}


def normalise_date(raw: str) -> Optional[str]:
    """Return YYYY-MM-DD string or None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    for abbr, full in _MONTH_ABBR.items():
        raw = re.sub(r'\b' + abbr + r'\.?\b', full, raw, flags=re.IGNORECASE)

    for pattern, fmt in _DATE_PATTERNS:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            raw_candidate = m.group(0).replace('-', '/')
            # Strip commas so "July 30, 2026" -> "July 30 2026"
            raw_no_comma = raw_candidate.replace(',', '')
            fmts_to_try = [
                fmt,
                '%m/%d/%Y', '%m/%d/%y',
                '%B %d %Y', '%B %d, %Y',
                '%b %d %Y', '%b %d, %Y',
                '%d %B %Y', '%d %b %Y',
                '%Y/%m/%d',
            ]
            for f in fmts_to_try:
                for candidate in (raw_candidate, raw_no_comma):
                    try:
                        dt = datetime.strptime(candidate.strip(), f)
                        return dt.strftime('%Y-%m-%d')
                    except ValueError:
                        continue
    return None


def days_to_date(days: int, anchor_date: Optional[str]) -> Optional[str]:
    """Convert 'X days after acceptance' to a calendar date."""
    if anchor_date is None:
        return None
    try:
        anchor = datetime.strptime(anchor_date, '%Y-%m-%d').date()
        return (anchor + timedelta(days=days)).strftime('%Y-%m-%d')
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: str) -> Tuple[Optional[str], list]:
    """Open PDF with pdfplumber and return (full_text, warnings)."""
    warnings = []
    if not os.path.isfile(pdf_path):
        warnings.append(f'File not found: {pdf_path}')
        return None, warnings
    try:
        pages_text = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text()
                    if t:
                        pages_text.append(t)
                except Exception as e:
                    warnings.append(f'Page {i+1} extraction error: {e}')
        full_text = '\n'.join(pages_text)
        if not full_text.strip():
            return None, warnings
        return full_text, warnings
    except Exception as e:
        warnings.append(f'pdfplumber open error: {e}')
        return None, warnings


# ---------------------------------------------------------------------------
# Field extractors — each returns (value_or_None, confidence_str)
# ---------------------------------------------------------------------------

def extract_address(text: str) -> Tuple[Optional[str], str]:
    """Extract property address."""
    patterns = [
        r'[Pp]roperty\s+[Aa]ddress[:\s]+([^\n]{5,120})',
        r'[Ll]ocated\s+at[:\s]+([^\n]{5,120})',
        r'[Pp]roperty\s+[Kk]nown\s+as[:\s]+([^\n]{5,120})',
        r'[Pp]roperty\s+commonly\s+known\s+as[:\s]+([^\n]{5,120})',
        r'[Pp]remises\s+(?:located\s+at|known\s+as)[:\s]+([^\n]{5,120})',
        r'(?:purchase|sell|buy)\s+the\s+(?:real\s+property|property)\s+(?:located|known|at)\s+(?:at\s+)?([^\n,]{5,120})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip('.,;')
            if re.search(r'\d', val):
                confidence = 'high' if re.search(r'[Pp]roperty\s+[Aa]ddress|[Ll]ocated\s+at', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_purchase_price(text: str) -> Tuple[Optional[float], str]:
    """Extract purchase price."""
    patterns = [
        r'[Pp]urchase\s+[Pp]rice[:\s]*\$?([\d,]+(?:\.\d{1,2})?)',
        r'[Pp]urchase\s+[Pp]rice\s+of\s+\$\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Aa]grees?\s+to\s+pay[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Tt]otal\s+[Pp]urchase\s+[Pp]rice[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Ss]ale\s+[Pp]rice[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'\$\s*([\d,]+(?:\.\d{1,2})?)\s*(?:is\s+the\s+)?[Pp]urchase\s+[Pp]rice',
        r'\(\$\s*([\d,]+(?:\.\d{1,2})?)\)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_dollar(m.group(1))
            if val and val > 1000:
                confidence = 'high' if re.search(r'[Pp]urchase\s+[Pp]rice', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_earnest_money(text: str) -> Tuple[Optional[float], str]:
    """Extract earnest money deposit amount."""
    patterns = [
        r'[Ee]arnest\s+[Mm]oney[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Ee]arnest\s+[Mm]oney\s+[Dd]eposit[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Ee]scrow\s+[Dd]eposit[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Dd]eposit\s+of\s+\$?\s*([\d,]+(?:\.\d{1,2})?)\s+(?:as\s+)?[Ee]arnest',
        r'[Bb]uyer\s+(?:shall|will|agrees\s+to)\s+deposit[:\s]*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        r'\$\s*([\d,]+(?:\.\d{1,2})?)\s+as\s+earnest\s+money',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_dollar(m.group(1))
            if val is not None and val >= 0:
                confidence = 'high' if re.search(r'[Ee]arnest\s+[Mm]oney', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_closing_date(text: str) -> Tuple[Optional[str], str]:
    """Extract closing/settlement date."""
    date_re = r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})'
    patterns = [
        rf'[Cc]losing\s+[Dd]ate[:\s]+{date_re}',
        rf'[Cc]lose\s+on\s+or\s+before\s+{date_re}',
        rf'[Ss]ettlement\s+[Dd]ate[:\s]+{date_re}',
        rf'[Cc]losing\s+(?:shall|will)\s+(?:occur|take\s+place)\s+on\s+or\s+before\s+{date_re}',
        rf'[Cc]losing\s+(?:shall|will)\s+be\s+(?:held\s+)?on\s+{date_re}',
        rf'[Cc]lose\s+[Ee]scrow\s+on\s+{date_re}',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw_date = m.group(1)
            normalised = normalise_date(raw_date)
            if normalised:
                confidence = 'high' if re.search(r'[Cc]losing\s+[Dd]ate|[Cc]lose\s+on\s+or\s+before', pat) else 'medium'
                return normalised, confidence
    return None, 'low'


def extract_inspection_deadline(
    text: str, contract_date: Optional[str]
) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Extract inspection deadline.
    Returns (date_str_or_None, confidence, note_or_None).
    """
    date_re = r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})'

    direct_patterns = [
        rf'[Ii]nspection\s+[Cc]ontingency\s+[Dd]eadline[:\s]+{date_re}',
        rf'[Ii]nspection\s+[Pp]eriod\s+[Ee]xpires?[:\s]+{date_re}',
        rf'[Ii]nspection\s+[Dd]eadline[:\s]+{date_re}',
        rf'[Ii]nspections?\s+(?:must\s+be\s+)?completed?\s+(?:by|on\s+or\s+before)\s+{date_re}',
        rf'[Ii]nspection\s+[Pp]eriod[:\s]+(?:through\s+|until\s+|expires\s+)?{date_re}',
    ]
    for pat in direct_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            normalised = normalise_date(m.group(1))
            if normalised:
                return normalised, 'high', None

    days_patterns = [
        r'inspection\s+(?:period|contingency)[^\n]{0,80}?(\d+)\s+(?:calendar\s+|business\s+)?days?\s+(?:after|from|following)\s+(?:acceptance|execution|contract)',
        r'(\d+)\s+(?:calendar\s+|business\s+)?days?\s+(?:after|from|following)\s+(?:acceptance|execution)\s+(?:for\s+)?(?:to\s+)?(?:complete\s+)?(?:an?\s+)?inspection',
        # "Buyer shall have 10 calendar days after acceptance to conduct an inspection"
        r'[Bb]uyer\s+shall\s+have\s+(\d+)\s+(?:calendar\s+|business\s+)?days?\s+after\s+acceptance\s+to\s+(?:conduct|complete|perform)',
        r'[Bb]uyer\s+(?:shall\s+have|has|will\s+have)\s+(\d+)\s+(?:calendar\s+|business\s+)?days?\s+(?:to\s+)?(?:conduct|complete|perform)\s+(?:an?\s+)?inspection',
        r'inspection\s+period\s+(?:of\s+)?(\d+)\s+(?:calendar\s+|business\s+)?days?',
    ]
    for pat in days_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            days = int(m.group(1))
            computed = days_to_date(days, contract_date)
            note = f'{days} days after contract/acceptance'
            if computed:
                return computed, 'medium', note
            else:
                return None, 'low', note

    return None, 'low', None


def extract_financing_deadline(
    text: str, contract_date: Optional[str]
) -> Tuple[Optional[str], str, Optional[str]]:
    """Extract financing contingency deadline."""
    date_re = r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})'

    direct_patterns = [
        rf'[Ff]inancing\s+[Cc]ontingency\s+[Dd]eadline[:\s]+{date_re}',
        rf'[Ff]inancing\s+[Cc]ontingency[:\s]+(?:expires?\s+)?{date_re}',
        rf'[Ll]oan\s+[Cc]ommitment\s+[Dd]ate[:\s]+{date_re}',
        rf'[Mm]ortgage\s+[Cc]ommitment\s+[Dd]ate[:\s]+{date_re}',
        rf'[Ff]inancing\s+(?:must\s+be\s+)?(?:obtained|secured|approved)\s+(?:by|on\s+or\s+before)\s+{date_re}',
        rf'[Ll]oan\s+[Aa]pproval\s+[Dd]ate[:\s]+{date_re}',
    ]
    for pat in direct_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            normalised = normalise_date(m.group(1))
            if normalised:
                return normalised, 'high', None

    days_patterns = [
        r'financing\s+contingency[^\n]{0,100}?(\d+)\s+(?:calendar\s+|business\s+)?days?\s+(?:after|from|following)\s+(?:acceptance|execution|contract)',
        r'(\d+)\s+(?:calendar\s+|business\s+)?days?\s+(?:after|from|following)\s+(?:acceptance|execution)\s+to\s+(?:obtain|secure|procure)\s+financing',
        r'[Bb]uyer\s+(?:shall\s+have|has)\s+(\d+)\s+(?:calendar\s+|business\s+)?days?\s+to\s+(?:obtain|secure)\s+(?:a\s+)?(?:loan|mortgage|financing)',
        r'loan\s+commitment[^\n]{0,60}?(\d+)\s+(?:calendar\s+|business\s+)?days?',
    ]
    for pat in days_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            days = int(m.group(1))
            computed = days_to_date(days, contract_date)
            note = f'{days} days after contract/acceptance'
            if computed:
                return computed, 'medium', note
            else:
                return None, 'low', note

    return None, 'low', None


def extract_buyer_name(text: str) -> Tuple[Optional[str], str]:
    """Extract buyer name(s)."""
    patterns = [
        r'[Bb]uyer[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})(?:[ \t]*(?:and|&)[ \t]*[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})?',
        r'[Pp]urchaser[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})',
        r'[Tt]he[ \t]+undersigned[ \t]+[Bb]uyer[,\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})',
        r'[Bb]uyer\(s\)[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip('.,;').split('\n')[0].strip()
            if len(val) > 3 and not val.isupper():
                confidence = 'high' if re.search(r'[Bb]uyer[:\s]|[Pp]urchaser', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_seller_name(text: str) -> Tuple[Optional[str], str]:
    """Extract seller name(s)."""
    patterns = [
        r'[Ss]eller[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})(?:[ \t]*(?:and|&)[ \t]*[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})?',
        r'[Tt]he[ \t]+undersigned[ \t]+[Ss]eller[,\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})',
        r'[Ss]eller\(s\)[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,4})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip('.,;').split('\n')[0].strip()
            if len(val) > 3 and not val.isupper():
                confidence = 'high' if re.search(r'[Ss]eller[:\s]', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_buyer_agent(text: str) -> Tuple[Optional[str], Optional[str], str]:
    """Extract buyer agent name and company. Returns (name, company, confidence)."""
    name, company = None, None
    confidence = 'low'

    # Agent name patterns — use double-quoted strings to avoid apostrophe issues
    name_patterns = [
        r"[Bb]uyer['\u2019]?s?\s+[Aa]gent[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})",
        r'[Ss]elling\s+[Aa]gent[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})',
        r'[Cc]ooperating\s+[Bb]roker[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})',
        r"[Bb]uyer['\u2019]?s?\s+[Rr]epresentative[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})",
        r'[Ss]elling\s+[Bb]roker[:\s]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})',
    ]
    for pat in name_patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip('.,;').split('\n')[0].strip()
            if len(val) > 3 and not val.isupper():
                name = val
                confidence = 'high'
                break

    # Company patterns
    company_patterns = [
        r"[Bb]uyer['\u2019]?s?\s+[Aa]gent\s+[Cc]ompany[:\s]+([^\n]{3,80})",
        r'[Ss]elling\s+(?:Agent\s+)?[Cc]ompany[:\s]+([^\n]{3,80})',
        r'[Cc]ooperating\s+[Bb]roker\s+[Cc]ompany[:\s]+([^\n]{3,80})',
        r'[Cc]ooperating\s+[Ff]irm[:\s]+([^\n]{3,80})',
        r"[Bb]uyer['\u2019]?s?\s+[Bb]rokerage[:\s]+([^\n]{3,80})",
    ]
    for pat in company_patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip('.,;')
            if len(val) > 2:
                company = val
                break

    # Fallback: look for well-known brokerage name near agent context
    if name and not company:
        agent_section_match = re.search(
            r"(?:[Bb]uyer['\u2019]?s?\s+[Aa]gent|[Ss]elling\s+[Aa]gent)[^\n]{0,200}",
            text
        )
        if agent_section_match:
            section = agent_section_match.group(0)
            brok_m = re.search(
                r'\b(?:RE/MAX|Keller\s+Williams|Century\s+21|Coldwell\s+Banker|Trueblood|ERA|EXIT|Compass|eXp|Redfin|Berkshire|Sotheby)[^\n]{0,50}',
                section,
                re.IGNORECASE
            )
            if brok_m:
                company = brok_m.group(0).strip().rstrip('.,;')

    return name, company, confidence


def extract_title_company(text: str) -> Tuple[Optional[str], str]:
    """Extract title company name."""
    patterns = [
        r'[Tt]itle\s+[Cc]ompany[:\s]+([^\n]{3,100})',
        r'[Cc]losing\s+[Aa]gent[:\s]+([^\n]{3,100})',
        r'[Tt]itle\s+[Ii]nsurance\s+(?:[Cc]ompany|[Aa]gent)[:\s]+([^\n]{3,100})',
        r'[Ee]scrow\s+[Cc]ompany[:\s]+([^\n]{3,100})',
        r'[Tt]itle\s+[Ss]ervices?[:\s]+([^\n]{3,100})',
        r'[Cc]losing\s+[Ss]hall\s+be\s+(?:held|conducted)\s+(?:at|by|through)\s+([^\n]{3,100})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip('.,;')
            if len(val) > 2:
                confidence = 'high' if re.search(r'[Tt]itle\s+[Cc]ompany', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_lender(text: str) -> Tuple[Optional[str], str]:
    """Extract lender name."""
    patterns = [
        r'[Ll]ender[:\s]+([^\n]{3,100})',
        r'[Ll]ending\s+[Ii]nstitution[:\s]+([^\n]{3,100})',
        r'[Mm]ortgage\s+[Cc]ompany[:\s]+([^\n]{3,100})',
        r'[Mm]ortgage\s+[Ll]ender[:\s]+([^\n]{3,100})',
        r'[Ll]oan\s+[Ii]nstitution[:\s]+([^\n]{3,100})',
        r'[Bb]ank[:\s]+([^\n]{3,100})',
        r'financed\s+(?:by|through|with)\s+([^\n]{3,100})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip('.,;')
            if len(val) > 2:
                confidence = 'high' if re.search(r'^[Ll]ender', pat) else 'medium'
                return val, confidence
    return None, 'low'


def extract_commission(text: str) -> Tuple[Optional[float], str]:
    """Extract commission percentage or amount."""
    pct_patterns = [
        r'[Cc]ommission[:\s]+(\d+(?:\.\d{1,2})?)\s*%',
        r'(\d+(?:\.\d{1,2})?)\s*%\s+(?:commission|of\s+(?:the\s+)?(?:sale|purchase|gross)\s+price)',
        r'[Cc]ommission\s+(?:of|at|equal\s+to)\s+(\d+(?:\.\d{1,2})?)\s*%',
        r'[Tt]otal\s+[Cc]ommission[:\s]+(\d+(?:\.\d{1,2})?)\s*%',
    ]
    for pat in pct_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0 < val <= 20:
                    return val, 'high'
            except ValueError:
                pass

    dollar_patterns = [
        r'[Cc]ommission[:\s]+\$\s*([\d,]+(?:\.\d{1,2})?)',
        r'[Cc]ommission\s+(?:of|equal\s+to)\s+\$\s*([\d,]+(?:\.\d{1,2})?)',
    ]
    for pat in dollar_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_dollar(m.group(1))
            if val:
                return val, 'medium'

    return None, 'low'


def extract_mls_number(text: str) -> Tuple[Optional[str], str]:
    """Extract MLS number."""
    patterns = [
        r'MLS\s*#\s*(\w+)',
        r'MLS\s+Number[:\s]+(\w+)',
        r'MLS\s+No\.?\s*(\w+)',
        r'MLS\s+ID[:\s]+(\w+)',
        r'Listing\s+(?:Number|No\.?|ID)[:\s]+(\w+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and val.upper() not in ('N/A', 'NA', 'TBD'):
                return val, 'high'
    return None, 'low'


def extract_contract_date(text: str) -> Optional[str]:
    """Try to find the contract/acceptance date (anchor for days-based deadlines)."""
    date_re = r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})'
    patterns = [
        rf'[Cc]ontract\s+[Dd]ate[:\s]+{date_re}',
        rf'[Dd]ate\s+of\s+[Aa]cceptance[:\s]+{date_re}',
        rf'[Aa]ccepted\s+(?:on|this)[:\s]+{date_re}',
        rf'[Ee]ffective\s+[Dd]ate[:\s]+{date_re}',
        rf'[Oo]ffer\s+[Dd]ate[:\s]+{date_re}',
        rf'[Ss]igned\s+(?:on|this)[:\s]+{date_re}',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            normalised = normalise_date(m.group(1))
            if normalised:
                return normalised
    return None


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_purchase_agreement(pdf_path: str) -> dict:
    """Full pipeline: extract text, run all field extractors, build result dict."""
    result = {
        'address': None,
        'purchase_price': None,
        'earnest_money': None,
        'closing_date': None,
        'inspection_deadline': None,
        'financing_deadline': None,
        'buyer_name': None,
        'seller_name': None,
        'buyer_agent_name': None,
        'buyer_agent_company': None,
        'title_company': None,
        'lender': None,
        'commission_pct': None,
        'mls_number': None,
        'confidence': {},
        'raw_text_preview': None,
        'parse_warnings': [],
    }

    # --- Text extraction ---
    text, extraction_warnings = extract_text_from_pdf(pdf_path)
    result['parse_warnings'].extend(extraction_warnings)

    if text is None:
        return {
            'error': (
                'Could not extract text - PDF may be scanned. '
                'Please use OCR or enter details manually.'
            ),
            'parse_warnings': result['parse_warnings'],
        }

    result['raw_text_preview'] = text[:500]

    # --- Contract date (anchor for days-based deadlines) ---
    contract_date = extract_contract_date(text)

    # --- Address ---
    address, addr_conf = extract_address(text)
    result['address'] = address
    result['confidence']['address'] = addr_conf
    if address is None:
        result['parse_warnings'].append('Could not find property address')

    # --- Purchase price ---
    price, price_conf = extract_purchase_price(text)
    result['purchase_price'] = price
    result['confidence']['purchase_price'] = price_conf
    if price is None:
        result['parse_warnings'].append('Could not find purchase price')

    # --- Earnest money ---
    earnest, earnest_conf = extract_earnest_money(text)
    result['earnest_money'] = earnest
    result['confidence']['earnest_money'] = earnest_conf
    if earnest is None:
        result['parse_warnings'].append('Could not find earnest money amount')

    # --- Closing date ---
    closing, closing_conf = extract_closing_date(text)
    result['closing_date'] = closing
    result['confidence']['closing_date'] = closing_conf
    if closing is None:
        result['parse_warnings'].append('Could not find closing date')

    # --- Inspection deadline ---
    insp_date, insp_conf, insp_note = extract_inspection_deadline(text, contract_date)
    result['inspection_deadline'] = insp_date
    result['confidence']['inspection_deadline'] = insp_conf
    if insp_date is None:
        if insp_note:
            result['parse_warnings'].append(
                f'Inspection deadline expressed as "{insp_note}" but no contract date found to compute calendar date'
            )
        else:
            result['parse_warnings'].append('Could not find inspection deadline')
    elif insp_note:
        result['parse_warnings'].append(f'Inspection deadline computed from: {insp_note}')

    # --- Financing deadline ---
    fin_date, fin_conf, fin_note = extract_financing_deadline(text, contract_date)
    result['financing_deadline'] = fin_date
    result['confidence']['financing_deadline'] = fin_conf
    if fin_date is None:
        if fin_note:
            result['parse_warnings'].append(
                f'Financing deadline expressed as "{fin_note}" but no contract date found to compute calendar date'
            )
        else:
            result['parse_warnings'].append('Could not find financing contingency deadline')
    elif fin_note:
        result['parse_warnings'].append(f'Financing deadline computed from: {fin_note}')

    # --- Buyer name ---
    buyer, buyer_conf = extract_buyer_name(text)
    result['buyer_name'] = buyer
    result['confidence']['buyer_name'] = buyer_conf
    if buyer is None:
        result['parse_warnings'].append('Could not find buyer name')

    # --- Seller name ---
    seller, seller_conf = extract_seller_name(text)
    result['seller_name'] = seller
    result['confidence']['seller_name'] = seller_conf
    if seller is None:
        result['parse_warnings'].append('Could not find seller name')

    # --- Buyer agent ---
    agent_name, agent_company, agent_conf = extract_buyer_agent(text)
    result['buyer_agent_name'] = agent_name
    result['buyer_agent_company'] = agent_company
    result['confidence']['buyer_agent'] = agent_conf
    if agent_name is None:
        result['parse_warnings'].append('Could not find buyer agent name')

    # --- Title company ---
    title, title_conf = extract_title_company(text)
    result['title_company'] = title
    result['confidence']['title_company'] = title_conf
    if title is None:
        result['parse_warnings'].append('Could not find title company')

    # --- Lender ---
    lender, lender_conf = extract_lender(text)
    result['lender'] = lender
    result['confidence']['lender'] = lender_conf
    if lender is None:
        result['parse_warnings'].append('Could not find lender')

    # --- Commission ---
    commission, comm_conf = extract_commission(text)
    result['commission_pct'] = commission
    result['confidence']['commission'] = comm_conf
    if commission is None:
        result['parse_warnings'].append('Could not find commission percentage or amount')

    # --- MLS number ---
    mls, mls_conf = extract_mls_number(text)
    result['mls_number'] = mls
    result['confidence']['mls_number'] = mls_conf
    if mls is None:
        result['parse_warnings'].append('Could not find MLS number')

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            'error': 'Usage: python3 parse_purchase_agreement.py <pdf_path>',
            'parse_warnings': [],
        }, indent=2))
        sys.exit(1)

    pdf_path = sys.argv[1]

    try:
        result = parse_purchase_agreement(pdf_path)
    except Exception as exc:
        result = {
            'error': f'Unexpected parser error: {exc}',
            'parse_warnings': [str(exc)],
        }

    print(safe_json_dump(result))


if __name__ == '__main__':
    main()
