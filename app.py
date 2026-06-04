import streamlit as st
import plotly.express as px
import fpdf2
import pandas as pd
import re
import pdfplumber
import datetime
# ─────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────
def extract_text_from_pdf(pdf_file_path):
    """
    Extracts raw text from a PDF file using pdfplumber.
    """
    text = ""
    try:
        with pdfplumber.open(pdf_file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"Error reading PDF {pdf_file_path}: {e}")
    return text
def clean_amount(amount_str):
    """
    Cleans amount string by removing currency symbols, commas, and other
    non-numeric characters (except the decimal dot).
    """
    if not amount_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(amount_str))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0
# ─────────────────────────────────────────────────────
# FINANCIAL VALUE EXTRACTOR  (keyword-proximity search)
# ─────────────────────────────────────────────────────
def parse_financial_value(text, keywords, fallback_regex=None):
    """
    Returns the first positive numeric value found on the same line as any
    of the supplied keywords. Falls back to a multi-line search if needed.
    """
    lines = text.split("\n")
    # 1. Line-level scan ─ most reliable
    for keyword in keywords:
        pat = re.compile(
            rf"{re.escape(keyword)}\s*[:\-]?\s*([\d,]+(?:\.\d{{1,2}})?)",
            re.IGNORECASE,
        )
        for line in lines:
            m = pat.search(line)
            if m:
                val = clean_amount(m.group(1))
                if val > 0:
                    return val
    # 2. Lookahead across ≤100 chars of the entire text
    for keyword in keywords:
        m = re.search(
            rf"{re.escape(keyword)}[\s\S]{{0,100}}?([\d,]+(?:\.\d{{1,2}})?)",
            text,
            re.IGNORECASE,
        )
        if m:
            val = clean_amount(m.group(1))
            if val > 0:
                return val
    # 3. Explicit fallback regex
    if fallback_regex:
        m = re.search(fallback_regex, text, re.IGNORECASE)
        if m:
            return clean_amount(m.group(1))
    return 0.0
# ─────────────────────────────────────────────────────
# PAN EXTRACTION
# ─────────────────────────────────────────────────────
def parse_pan(text):
    """
    Extracts a PAN card number (10-character alphanumeric) from text.
    Format: 5 letters + 4 digits + 1 letter  (e.g. ABCDE1234F)
    """
    # Look for PAN with optional label
    m = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else None
# ─────────────────────────────────────────────────────
# EMPLOYEE NAME EXTRACTION
# ─────────────────────────────────────────────────────
def parse_employee_name(text):
    """
    Extracts the taxpayer / employee full name from a payslip or offer letter.
    Tries the following patterns in order of reliability:
      1. Labelled field  — "Employee Name : Rahul Sharma"
      2. Salutation      — "Dear Mr. / Ms. / Mrs. <Name>,"
      3. Name in header  — "Name: <Name>"
    Returns the cleaned full name string or None.
    """
    # Priority 1: explicit label patterns (most reliable)
    label_patterns = [
        r"(?:employee\s*name|name\s*of\s*employee|staff\s*name|worker\s*name)\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]{2,50})",
        r"(?:^|\n)name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]{2,50})",
        r"(?:payslip\s+(?:of|for)|salary\s+slip\s+(?:of|for)|issued\s+to)\s*[:\-]?\s*([A-Za-z][A-Za-z\s\.]{2,50})",
    ]
    for pat in label_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = m.group(1).strip().rstrip(",.")
            # Must have at least first + last name (two words) and no digits
            if len(name.split()) >= 2 and not re.search(r"\d", name):
                return name.title()
    # Priority 2: salutation — "Dear Mr. Rahul Sharma,"
    m = re.search(
        r"\bDear\s+(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?)?\s*([A-Za-z][A-Za-z\s\.]{2,50?}),",
        text,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip()
        if len(name.split()) >= 1 and not re.search(r"\d", name):
            return name.title()
    # Priority 3: bare "Name" line anywhere
    m = re.search(
        r"(?:^|\n)\s*Name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]{3,50})",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        name = m.group(1).strip().rstrip(",.")
        if not re.search(r"\d", name):
            return name.title()
    return None
# ─────────────────────────────────────────────────────
# TAN EXTRACTION
# ─────────────────────────────────────────────────────
def parse_tan(text):
    """
    Extracts a TAN (Tax Deduction & Collection Account Number).
    Format: 4 letters + 5 digits + 1 letter  (e.g. MUMB12345T)
    TAN is shorter than PAN and starts with location code letters.
    """
    # Labelled TAN first  ─ e.g. "TAN : MUMB12345T"
    m = re.search(
        r"(?:tan|tax\s+deduction\s+account|tdan)[^\n]{0,30}?\b([A-Z]{4}[0-9]{5}[A-Z])\b",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    # Bare pattern anywhere
    m = re.search(r"\b([A-Z]{4}[0-9]{5}[A-Z])\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else None
# ─────────────────────────────────────────────────────
# EMPLOYER NAME EXTRACTION
# ─────────────────────────────────────────────────────
def parse_employer_name(text, context="old"):
    """
    Tries to detect the company / employer name from typical payslip /
    offer-letter labels:
      Old payslip  → "Employer:", "Company Name:", "Organisation:", header lines
      Offer letter → "Dear [name]," / "Company:" / top letterhead
    Returns the extracted name string or None.
    """
    # Common label patterns
    label_patterns = [
        r"(?:employer|company|organisation|organization|firm|entity)\s*(?:name)?\s*[:\-]\s*([^\n]{3,60})",
        r"(?:issued by|payslip of|payroll of)\s*[:\-]?\s*([^\n]{3,60})",
        r"(?:from|to)\s*[:\-]?\s*([A-Z][a-zA-Z\s&.,()]{5,60}(?:pvt|ltd|llp|limited|inc|corp|technologies|solutions|services|consulting|systems|india|infosys|tata|wipro|accenture|cognizant|hcl|tech mahindra|infra)[^\n]{0,20})",
    ]
    for pat in label_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip(",.")
            if len(name) >= 3:
                return name
    # For offer letters: grab line after "Dear <name>," which is often the company name
    if context == "new":
        m = re.search(r"(?:offer letter|appointment letter|joining letter)[^\n]{0,10}\n([^\n]{5,80})", text, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if len(name) >= 3:
                return name
    # Last resort: find lines that look like company names (contain Ltd / Pvt / LLP / Inc)
    company_pattern = re.compile(
        r"^([A-Z][A-Za-z\s&.,()]{4,60}(?:Pvt\.?\s*Ltd\.?|Limited|LLP|Inc\.?|Corp\.?|Technologies|Solutions|Services|Consulting|Systems))\s*$",
        re.MULTILINE,
    )
    matches = company_pattern.findall(text)
    if matches:
        return matches[0].strip()
    return None
# ─────────────────────────────────────────────────────
# DATE / JOINING MONTH EXTRACTION
# ─────────────────────────────────────────────────────
# Indian fiscal year month order (April → March)
_FY_MONTHS = [
    "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "January", "February", "March",
]
_MONTH_MAP = {
    "jan": "January", "feb": "February", "mar": "March",
    "apr": "April",   "may": "May",       "jun": "June",
    "jul": "July",    "aug": "August",    "sep": "September",
    "oct": "October", "nov": "November",  "dec": "December",
    "january": "January", "february": "February", "march": "March",
    "april": "April",     "june": "June",
    "july": "July",       "august": "August",     "september": "September",
    "october": "October", "november": "November",  "december": "December",
}
def parse_joining_month(text):
    """
    Extracts the joining / date-of-joining month from an offer letter.
    Looks for patterns like:
      - "Date of Joining: 01 Oct 2025"
      - "Joining Date: October 1, 2025"
      - "Effective from 15-11-2025"
      - "commencement date: 2025-10-01"
    Returns the canonical month name (e.g. "October") or None.
    """
    date_label_patterns = [
        # Label + date  "Date of Joining : 01 Oct 2025"
        r"(?:date\s+of\s+joining|joining\s+date|start\s+date|commencement\s+date|effective\s+(?:date|from))\s*[:\-]?\s*"
        r"(?:\d{1,2}[\s\-/])?([A-Za-z]{3,9})[\s\-/,]*(?:\d{1,2}[\s\-/,]*)?\d{2,4}",
        # DD-MM-YYYY or DD/MM/YYYY  (month as number then convert)
        r"(?:date\s+of\s+joining|joining\s+date|start\s+date|effective\s+(?:date|from))\s*[:\-]?\s*"
        r"\d{1,2}[\-/](\d{1,2})[\-/]\d{2,4}",
        # YYYY-MM-DD ISO
        r"(?:date\s+of\s+joining|joining\s+date|start\s+date|effective\s+(?:date|from))\s*[:\-]?\s*"
        r"\d{4}[\-/](\d{1,2})[\-/]\d{1,2}",
    ]
    for pat in date_label_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            # If it's numeric, convert month number → name
            try:
                month_num = int(raw)
                if 1 <= month_num <= 12:
                    return datetime.date(2000, month_num, 1).strftime("%B")
            except ValueError:
                # It's a text month abbreviation/full name
                key = raw.lower()[:3]
                for abbr, full in _MONTH_MAP.items():
                    if abbr.startswith(key) or key.startswith(abbr[:3]):
                        return full
    # Generic date scan — pick up any "Month YYYY" pattern in the whole document
    # and return the earliest one that falls in the current or next FY
    generic = re.findall(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if generic:
        # Return the first detected month name (typically joining date)
        return generic[0][0].capitalize()
    return None
# ─────────────────────────────────────────────────────
# PUBLIC PARSERS
# ─────────────────────────────────────────────────────
def parse_old_payslip(text):
    """
    Parses a Full-and-Final (F&F) settlement or cumulative payslip.
    Returns a dict with:
      employee_name, pan, old_employer_name, old_employer_tan,
      old_gross_salary, old_tds_deducted
    """
    employee_name = parse_employee_name(text)
    pan = parse_pan(text)
    tan = parse_tan(text)
    employer_name = parse_employer_name(text, context="old")
    # Cumulative / annual gross salary keywords
    gross_keywords = [
        "total gross", "gross salary", "gross earnings", "gross pay",
        "total earnings", "gross wages", "annual gross", "earned gross",
        "ctc", "cost to company", "total ctc",
    ]
    gross_salary = parse_financial_value(text, gross_keywords)
    # TDS keywords — ordered from most specific to generic
    tds_keywords = [
        "tds deducted", "tax deducted at source", "income tax deducted",
        "tds amount", "it deduction", "tds", "income tax", "withholding tax",
        "tax deduction",
    ]
    tds_deducted = parse_financial_value(text, tds_keywords)
    return {
        "employee_name": employee_name,
        "pan": pan,
        "old_employer_name": employer_name,
        "old_employer_tan": tan,
        "old_gross_salary": gross_salary if gross_salary > 0 else None,
        "old_tds_deducted": tds_deducted if tds_deducted > 0 else None,
    }
def parse_new_offer_letter(text):
    """
    Parses a new offer / appointment letter.
    Returns a dict with:
      new_employer_name, new_employer_tan,
      new_basic, new_hra, new_special_allowance,
      switch_month
    """
    employer_name = parse_employer_name(text, context="new")
    tan = parse_tan(text)
    joining_month = parse_joining_month(text)
    # Monthly basic salary
    basic_keywords = [
        "basic salary", "monthly basic", "basic pay", "basic",
        "base salary", "base pay",
    ]
    basic = parse_financial_value(text, basic_keywords)
    # HRA
    hra_keywords = [
        "house rent allowance", "hra", "house rent",
    ]
    hra = parse_financial_value(text, hra_keywords)
    # Special allowance — after HRA to avoid double-matching "allowance"
    special_keywords = [
        "special allowance", "other allowance", "personal allowance",
        "flexi allowance", "supplementary allowance",
    ]
    # Avoid matching HRA again by doing a targeted search
    special = parse_financial_value(text, special_keywords)
    if special == hra and special > 0:
        # They probably matched the same line; try broader keyword last
        special = parse_financial_value(text, ["allowance"])
    # If values are unreasonably large they may be annual — divide by 12
    # (Annual basic > 5,00,000 and no explicit "monthly" label signals annual)
    is_annual_signal = bool(re.search(r"\bper\s+annum\b|\bpa\b|\bannual\b|\bp\.a\.\b", text, re.IGNORECASE))
    if is_annual_signal and basic > 0:
        basic = round(basic / 12, 2)
        hra = round(hra / 12, 2)
        special = round(special / 12, 2)
    return {
        "new_employer_name": employer_name,
        "new_employer_tan": tan,
        "new_basic": basic if basic > 0 else None,
        "new_hra": hra if hra > 0 else None,
        "new_special_allowance": special if special > 0 else None,
        "switch_month": joining_month,
    }
def calculate_progressive_tax(taxable_income):
    """
    Computes tax under the Indian FY 2025-26 New Tax Regime slabs:
    - Up to ₹4,00,000: 0%
    - ₹4,00,001 to ₹8,00,000: 5%
    - ₹8,00,001 to ₹12,00,000: 10%
    - ₹12,00,001 to ₹16,00,000: 15%
    - Above ₹16,00,000: 20%
    """
    if taxable_income <= 0:
        return 0.0
    tax = 0.0
    
    # Slab 1: Up to 4,00,000 (0%)
    # Slab 2: 4,00,001 to 8,00,000 (5%)
    if taxable_income > 400000:
        slab_taxable = min(taxable_income, 800000) - 400000
        tax += slab_taxable * 0.05
    # Slab 3: 8,00,001 to 12,00,000 (10%)
    if taxable_income > 800000:
        slab_taxable = min(taxable_income, 1200000) - 800000
        tax += slab_taxable * 0.10
    # Slab 4: 12,00,001 to 16,00,000 (15%)
    if taxable_income > 1200000:
        slab_taxable = min(taxable_income, 1600000) - 1200000
        tax += slab_taxable * 0.15
    # Slab 5: Above 16,00,000 (20%)
    if taxable_income > 1600000:
        slab_taxable = taxable_income - 1600000
        tax += slab_taxable * 0.20
    # Section 87A Rebate for New Tax Regime:
    # Under FY 2025-26, the tax rebate is up to ₹7,00,000 (or if taxable income is <= ₹7,00,000, tax is 0).
    # Let's apply a standard Section 87A rebate: if taxable_income <= 700000, tax = 0.0
    if taxable_income <= 700000:
        tax = 0.0
    return tax
def get_monthly_pt(month_name, monthly_gross):
    """
    Returns Nagpur/Maharashtra Professional Tax (PT) for a single month based on gross salary.
    - Monthly Gross <= ₹7,500: Nil (₹0)
    - Monthly Gross ₹7,501 to ₹10,000: ₹175
    - Monthly Gross > ₹10,000: ₹200 (₹300 in February)
    """
    if monthly_gross <= 7500:
        return 0.0
    elif monthly_gross <= 10000:
        return 175.0
    else:
        month_clean = month_name.lower().strip()
        if month_clean in ["february", "feb"]:
            return 300.0
        else:
            return 200.0
def calculate_professional_tax(months_list, monthly_gross):
    """
    Calculates total Maharashtra Professional Tax (PT) based on active months and gross salary.
    """
    return sum(get_monthly_pt(m, monthly_gross) for m in months_list)
def get_months_remaining(switch_month):
    """
    Returns the remaining months in the Indian Fiscal Year (which ends in March) starting from switch_month.
    """
    all_months = ["April", "May", "June", "July", "August", "September", "October", "November", "December", "January", "February", "March"]
    
    try:
        start_idx = all_months.index(switch_month)
        # Slicing from the switch month through March (which is the last element in our fiscal year list)
        return all_months[start_idx:]
    except ValueError:
        return ["October", "November", "December", "January", "February", "March"] # default
def compute_tax_scenarios(
    old_gross_salary, 
    old_tds_deducted, 
    new_monthly_basic, 
    new_hra, 
    new_special_allowance, 
    switch_month
):
    """
    Performs comprehensive tax math to compute combined liability, 
    standalone TDS by new employer, the deficit, and the impact on in-hand pay.
    """
    # 1. Monthly salary calculations
    new_monthly_gross = new_monthly_basic + new_hra + new_special_allowance
    remaining_months = get_months_remaining(switch_month)
    num_remaining_months = len(remaining_months)
    
    # 2. Total Combined Income
    new_employer_earnings = new_monthly_gross * num_remaining_months
    total_combined_income = old_gross_salary + new_employer_earnings
    
    # 3. Deductions & Net Taxable Income
    standard_deduction = 75000.0
    combined_taxable_income = max(0.0, total_combined_income - standard_deduction)
    
    # 4. True Tax calculations
    true_base_tax = calculate_progressive_tax(combined_taxable_income)
    true_cess = true_base_tax * 0.04
    true_annual_tax_liability = true_base_tax + true_cess
    
    # 5. Standalone Tax (what the new employer thinks the tax is if they don't know the old salary)
    new_standalone_earnings = new_monthly_gross * num_remaining_months
    new_standalone_taxable = max(0.0, new_standalone_earnings - standard_deduction)
    new_standalone_base_tax = calculate_progressive_tax(new_standalone_taxable)
    new_standalone_cess = new_standalone_base_tax * 0.04
    new_standalone_annual_tax = new_standalone_base_tax + new_standalone_cess
    
    # Standalone monthly TDS the new employer will deduct if NOT declared
    # They divide the standalone tax over the remaining months
    unadjusted_monthly_tds = new_standalone_annual_tax / num_remaining_months if num_remaining_months > 0 else 0.0
    
    # 6. Compliance Path: Tax deficit distribution
    # Remaining annual tax to pay after old employer TDS is subtracted
    remaining_tax_to_collect = max(0.0, true_annual_tax_liability - old_tds_deducted)
    
    # Adjusted monthly TDS if they declare (Form No. 122 submitted)
    adjusted_monthly_tds = remaining_tax_to_collect / num_remaining_months if num_remaining_months > 0 else 0.0
    
    # 7. Deficits and Shock
    # Total standalone TDS projected to be deducted under unadjusted path
    total_unadjusted_tds_deducted = old_tds_deducted + (unadjusted_monthly_tds * num_remaining_months)
    # The March Deficit (impending tax shortfall due to tax bracket jumping)
    projected_march_deficit = max(0.0, true_annual_tax_liability - total_unadjusted_tds_deducted)
    
    # PT calculations
    total_pt = calculate_professional_tax(remaining_months, new_monthly_gross)
    
    # Monthly Take-home components
    # Scenario A: Without SwitchTax Compliance (Unadjusted Take-Home)
    # Wait, in March, the tax deficit might be deducted all at once (March TDS Shock)
    # or they pay it at ITR filing time. Let's model both!
    unadjusted_in_hand_normal = max(0.0, new_monthly_gross - (total_pt / num_remaining_months) - unadjusted_monthly_tds)
    
    # In the final month (March), if the employer discovers it or if they have to pay the shortfall:
    # March TDS Shock deduction: unadjusted_monthly_tds + projected_march_deficit
    
    # Scenario B: With SwitchTax Compliance (Adjusted Take-Home - flat and predictable)
    # Adjusted monthly take-home is flat because the deficit is spread evenly over all remaining months
    adjusted_in_hand = [
        max(0.0, new_monthly_gross - get_monthly_pt(m, new_monthly_gross) - adjusted_monthly_tds)
        for m in remaining_months
    ]
    
    # Scenario A actual monthly curve (with shock in March)
    unadjusted_in_hand = []
    for idx, m in enumerate(remaining_months):
        m_pt = get_monthly_pt(m, new_monthly_gross)
        if m.lower() == "march":
            # March TDS shock! They deduct standard TDS plus the entire deficit
            march_tds_charge = unadjusted_monthly_tds + projected_march_deficit
            in_hand = max(0.0, new_monthly_gross - m_pt - march_tds_charge)
        else:
            in_hand = max(0.0, new_monthly_gross - m_pt - unadjusted_monthly_tds)
        unadjusted_in_hand.append(in_hand)
    return {
        "remaining_months": remaining_months,
        "new_monthly_gross": new_monthly_gross,
        "total_combined_income": total_combined_income,
        "combined_taxable_income": combined_taxable_income,
        "true_annual_tax_liability": true_annual_tax_liability,
        "new_standalone_annual_tax": new_standalone_annual_tax,
        "old_tds_deducted": old_tds_deducted,
        "projected_march_deficit": projected_march_deficit,
        "unadjusted_monthly_tds": unadjusted_monthly_tds,
        "adjusted_monthly_tds": adjusted_monthly_tds,
        "unadjusted_in_hand_curve": unadjusted_in_hand,
        "adjusted_in_hand_curve": adjusted_in_hand,
        "total_pt": total_pt
    }
from fpdf import FPDF
import datetime
class Form122PDF(FPDF):
    def header(self):
        # Government Header Design
        self.set_fill_color(240, 240, 240)
        self.rect(5, 5, 200, 287, 'D') # Outer Border
        
        self.set_font('Helvetica', 'B', 12)
        self.cell(0, 8, 'GOVERNMENT OF INDIA', align='C', ln=True)
        self.set_font('Helvetica', 'B', 14)
        self.cell(0, 8, 'FORM NO. 122', align='C', ln=True)
        self.set_font('Helvetica', 'I', 9)
        self.cell(0, 6, '[See Section 192(2) of the Income-tax Act, 1961]', align='C', ln=True)
        self.set_font('Helvetica', 'B', 11)
        self.cell(0, 8, 'STATEMENT OF DETAILS OF INCOME AND TDS ON JOB SWITCH', align='C', ln=True)
        self.ln(5)
    def draw_section_header(self, title):
        self.set_fill_color(220, 230, 242)
        self.set_font('Helvetica', 'B', 10)
        self.cell(0, 7, f"  {title}", border=1, ln=True, fill=True)
    def draw_grid_row(self, col1, col2, border=1, font_style_1='', font_style_2=''):
        self.set_font('Helvetica', font_style_1, 9)
        self.cell(100, 7, f" {col1}", border=border)
        self.set_font('Helvetica', font_style_2, 9)
        self.cell(90, 7, f" {col2}", border=border, ln=True)
    def footer(self):
        # Official disclaimer at bottom
        self.set_y(-18)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 4, 'Generated securely via SwitchTax AI Compliant Platform.', align='C', ln=True)
        self.cell(0, 4, 'This is a legally valid declaration under Section 192(2) of the Income Tax Act, 1961.', align='C')
def generate_form_122(data):
    """
    Generates a print-ready PDF binary stream for Form No. 122
    based on the computed compliance variables.
    """
    pdf = Form122PDF()
    pdf.add_page()
    pdf.set_margins(10, 10, 10)
    
    # --- Employee Info Table ---
    pdf.draw_section_header("PART A: EMPLOYEE PROFILE")
    pdf.draw_grid_row("Employee Full Name", data.get("employee_name", "N/A"))
    pdf.draw_grid_row("Permanent Account Number (PAN)", data.get("pan", "N/A"), font_style_2='B')
    pdf.draw_grid_row("Assessment Year", "2026-27 (FY 2025-26)")
    pdf.draw_grid_row("Declaration Date", datetime.datetime.now().strftime("%d-%b-%Y"))
    pdf.ln(4)
    # --- Previous Employer Table ---
    pdf.draw_section_header("PART B: DETAILS OF INCOME FROM PREVIOUS EMPLOYER(S)")
    pdf.draw_grid_row("Previous Employer Name", data.get("old_employer_name", "N/A"))
    pdf.draw_grid_row("Previous Employer TAN", data.get("old_employer_tan", "N/A"))
    pdf.draw_grid_row("Gross Earnings from Previous Employer (A)", f"Rs. {data.get('old_gross_salary', 0.0):,.2f}", font_style_2='B')
    pdf.draw_grid_row("TDS Deducted by Previous Employer (B)", f"Rs. {data.get('old_tds_deducted', 0.0):,.2f}", font_style_2='B')
    pdf.ln(4)
    # --- Present Employer Table ---
    pdf.draw_section_header("PART C: DETAILS OF INCOME FROM PRESENT EMPLOYER")
    pdf.draw_grid_row("Present Employer Name", data.get("new_employer_name", "N/A"))
    pdf.draw_grid_row("Present Employer TAN", data.get("new_employer_tan", "N/A"))
    pdf.draw_grid_row("New Monthly Basic Salary", f"Rs. {data.get('new_basic', 0.0):,.2f}")
    pdf.draw_grid_row("New Monthly House Rent Allowance (HRA)", f"Rs. {data.get('new_hra', 0.0):,.2f}")
    pdf.draw_grid_row("New Monthly Special Allowance", f"Rs. {data.get('new_special_allowance', 0.0):,.2f}")
    pdf.draw_grid_row("Remaining Months in FY", f"{data.get('remaining_months_count', 0)}")
    pdf.draw_grid_row("Projected Standalone Present Salary (C)", f"Rs. {data.get('new_projected_salary', 0.0):,.2f}", font_style_2='B')
    pdf.ln(4)
    # --- Combined Tax Slabs Math ---
    pdf.draw_section_header("PART D: CONSOLIDATED COMPLIANCE & TAX MATH ENGINE")
    pdf.draw_grid_row("Combined Gross Income (A + C)", f"Rs. {data.get('total_combined_income', 0.0):,.2f}")
    pdf.draw_grid_row("Standard Deduction", "Rs. 75,000.00")
    pdf.draw_grid_row("Net Taxable Income (under New Tax Regime)", f"Rs. {data.get('combined_taxable_income', 0.0):,.2f}", font_style_2='B')
    pdf.draw_grid_row("True Annual Income Tax Liability (incl. Cess)", f"Rs. {data.get('true_annual_tax_liability', 0.0):,.2f}", font_style_2='B')
    pdf.draw_grid_row("Less: TDS Already Paid (B)", f"Rs. {data.get('old_tds_deducted', 0.0):,.2f}")
    pdf.draw_grid_row("Balance Tax to be Deducted by New Employer", f"Rs. {data.get('remaining_tax_to_collect', 0.0):,.2f}", font_style_1='B', font_style_2='B')
    
    # PT
    pdf.draw_grid_row("Maharashtra Professional Tax (PT) in remaining FY", f"Rs. {data.get('total_pt', 0.0):,.2f}")
    pdf.draw_grid_row("Recommended Monthly Tax Compliance Deduction (TDS)", f"Rs. {data.get('adjusted_monthly_tds', 0.0):,.2f}", font_style_1='B', font_style_2='B')
    pdf.ln(6)
    # --- Verification & Declaration ---
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(0, 5, 'VERIFICATION', ln=True)
    pdf.set_font('Helvetica', '', 8.5)
    verification_text = (
        f"I, {data.get('employee_name', 'N/A')}, do hereby declare that to the best of my knowledge and belief, "
        f"the information given above is correct and complete in every respect. I request my new employer, "
        f"{data.get('new_employer_name', 'N/A')}, to deduct tax at source as computed in this statement."
    )
    pdf.multi_cell(0, 4.5, verification_text, border=0)
    pdf.ln(12)
    
    # Signature Lines
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(90, 5, "Date: ____________________", ln=False)
    pdf.cell(100, 5, "Signature: __________________________", ln=True, align='R')
    pdf.cell(90, 5, "Place: Nagpur, India", ln=False)
    pdf.cell(100, 5, f"Name: {data.get('employee_name', 'N/A')}", ln=True, align='R')
    
    return bytes(pdf.output())
import streamlit as st
import pandas as pd
import plotly.express as px
import io
import time
import pdfplumber
from parser import extract_text_from_pdf, parse_old_payslip, parse_new_offer_letter
from tax_engine import compute_tax_scenarios, get_months_remaining
from form_generator import generate_form_122
# Configure page settings
st.set_page_config(
    page_title="SwitchTax AI - Indian Tax Compliance Dashboard",
    page_icon="💸",
    layout="wide",
    initial_sidebar_state="expanded"
)
# Custom High-Fidelity CSS for Dark Modern Theme and Glowing Alerts
st.markdown("""
<style>
    /* Global Background and Text */
    .stApp {
        background-color: #0e1117;
        color: #e2e8f0;
    }
    
    /* Premium Headers */
    h1, h2, h3 {
        color: #ffffff !important;
        font-family: 'Inter', 'Outfit', sans-serif;
        font-weight: 700;
    }
    
    /* Glowing card metric design */
    .metric-card {
        background: rgba(30, 41, 59, 0.45);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        transition: transform 0.2s, box-shadow 0.2s;
    }
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 24px rgba(0, 180, 216, 0.15);
        border-color: rgba(0, 180, 216, 0.3);
    }
    
    /* Red alert metric card */
    .metric-card-danger {
        background: rgba(69, 10, 10, 0.45);
        border: 1px solid rgba(239, 68, 68, 0.25);
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        animation: pulse 3s infinite alternate;
    }
    .metric-card-danger:hover {
        box-shadow: 0 6px 24px rgba(239, 68, 68, 0.3);
        border-color: rgba(239, 68, 68, 0.5);
    }
    
    /* Red Alert Banner styling */
    .alert-banner {
        background: linear-gradient(90deg, #ef4444 0%, #b91c1c 100%);
        color: white;
        border-radius: 8px;
        padding: 18px;
        font-size: 16px;
        font-weight: 600;
        margin-bottom: 25px;
        box-shadow: 0 4px 15px rgba(239, 68, 68, 0.25);
        border: 1px solid rgba(255, 255, 255, 0.15);
        animation: flash-banner 2.5s infinite ease-in-out;
    }
    
    /* Success Alert Banner styling */
    .success-banner {
        background: linear-gradient(90deg, #10b981 0%, #047857 100%);
        color: white;
        border-radius: 8px;
        padding: 15px;
        font-size: 15px;
        font-weight: 600;
        margin-bottom: 25px;
        box-shadow: 0 4px 15px rgba(16, 185, 129, 0.2);
    }
    
    @keyframes pulse {
        0% { box-shadow: 0 0 10px rgba(239, 68, 68, 0.2); }
        100% { box-shadow: 0 0 25px rgba(239, 68, 68, 0.45); }
    }
    @keyframes flash-banner {
        0% { background: linear-gradient(90deg, #ef4444 0%, #b91c1c 100%); box-shadow: 0 4px 15px rgba(239, 68, 68, 0.25); }
        50% { background: linear-gradient(90deg, #f87171 0%, #dc2626 100%); box-shadow: 0 4px 25px rgba(239, 68, 68, 0.5); }
        100% { background: linear-gradient(90deg, #ef4444 0%, #b91c1c 100%); box-shadow: 0 4px 15px rgba(239, 68, 68, 0.25); }
    }
</style>
""", unsafe_allow_html=True)
# App Title & Description
st.title("💸 SwitchTax AI")
st.markdown("##### *Production-grade Tax Compliance Engine & Form No. 122 Generator for Salaried Job Switchers*")
st.markdown("---")
# Session State Initialization
if "employee_name" not in st.session_state:
    st.session_state.employee_name = "Rajesh Kumar"
if "pan" not in st.session_state:
    st.session_state.pan = ""
if "old_gross_salary" not in st.session_state:
    st.session_state.old_gross_salary = 0.0
if "old_tds_deducted" not in st.session_state:
    st.session_state.old_tds_deducted = 0.0
if "old_employer_name" not in st.session_state:
    st.session_state.old_employer_name = ""
if "old_employer_tan" not in st.session_state:
    st.session_state.old_employer_tan = ""
if "new_basic" not in st.session_state:
    st.session_state.new_basic = 0.0
if "new_hra" not in st.session_state:
    st.session_state.new_hra = 0.0
if "new_special_allowance" not in st.session_state:
    st.session_state.new_special_allowance = 0.0
if "new_employer_name" not in st.session_state:
    st.session_state.new_employer_name = ""
if "new_employer_tan" not in st.session_state:
    st.session_state.new_employer_tan = ""
if "switch_month" not in st.session_state:
    st.session_state.switch_month = "October"
# Indian Fiscal Year months (constant used in PDF parsing and form UI)
months_list = [
    "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "January", "February", "March"
]
# Sidebar Section
st.sidebar.markdown("### 🎛️ Dashboard Controls")
# Mock Dataset Switch
load_mock = st.sidebar.toggle("⚡ Load Mock Test Dataset", value=False, help="Click to load dummy data for live compliance demonstration")
if load_mock:
    st.session_state.employee_name = "Rajesh Kumar"
    st.session_state.pan = "APBKP1234F"
    st.session_state.old_gross_salary = 950000.0
    st.session_state.old_tds_deducted = 65000.0
    st.session_state.old_employer_name = "Tech Solutions Ltd"
    st.session_state.old_employer_tan = "MUMB12345T"
    st.session_state.new_basic = 120000.0
    st.session_state.new_hra = 50000.0
    st.session_state.new_special_allowance = 30000.0
    st.session_state.new_employer_name = "Innovate FinTech India"
    st.session_state.new_employer_tan = "NGPR98765A"
    st.session_state.switch_month = "October"
# Sidebar File Upload Zone
st.sidebar.markdown("---")
st.sidebar.markdown("### 📂 Upload Job Shift Documents")
old_file = st.sidebar.file_uploader("Upload Old F&F Payslip (PDF)", type=["pdf"])
new_file = st.sidebar.file_uploader("Upload New Offer Letter (PDF)", type=["pdf"])
# Trigger pdfplumber parse when files are uploaded
if old_file is not None:
    with st.spinner("Extracting parameters from Previous Employer PDF..."):
        try:
            pdf_bytes = old_file.read()
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = ""
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            parsed_old = parse_old_payslip(text)
            if parsed_old.get("employee_name"):
                st.session_state.employee_name = parsed_old["employee_name"]
            if parsed_old.get("pan"):
                st.session_state.pan = parsed_old["pan"]
            if parsed_old.get("old_employer_name"):
                st.session_state.old_employer_name = parsed_old["old_employer_name"]
            if parsed_old.get("old_employer_tan"):
                st.session_state.old_employer_tan = parsed_old["old_employer_tan"]
            if parsed_old.get("old_gross_salary"):
                st.session_state.old_gross_salary = parsed_old["old_gross_salary"]
            if parsed_old.get("old_tds_deducted"):
                st.session_state.old_tds_deducted = parsed_old["old_tds_deducted"]
            extracted = [k for k, v in parsed_old.items() if v]
            st.sidebar.success(f"Old payslip extracted: {', '.join(extracted)}")
        except Exception as e:
            st.sidebar.error(f"Failed to parse old payslip: {e}")
if new_file is not None:
    with st.spinner("Extracting parameters from New Offer Letter PDF..."):
        try:
            pdf_bytes = new_file.read()
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = ""
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            parsed_new = parse_new_offer_letter(text)
            if parsed_new.get("new_employer_name"):
                st.session_state.new_employer_name = parsed_new["new_employer_name"]
            if parsed_new.get("new_employer_tan"):
                st.session_state.new_employer_tan = parsed_new["new_employer_tan"]
            if parsed_new.get("new_basic"):
                st.session_state.new_basic = parsed_new["new_basic"]
            if parsed_new.get("new_hra"):
                st.session_state.new_hra = parsed_new["new_hra"]
            if parsed_new.get("new_special_allowance"):
                st.session_state.new_special_allowance = parsed_new["new_special_allowance"]
            if parsed_new.get("switch_month") and parsed_new["switch_month"] in months_list:
                st.session_state.switch_month = parsed_new["switch_month"]
            extracted = [k for k, v in parsed_new.items() if v]
            st.sidebar.success(f"Offer letter extracted: {', '.join(extracted)}")
        except Exception as e:
            st.sidebar.error(f"Failed to parse offer letter: {e}")
# Layout Columns
left_col, right_col = st.columns([1, 1.2])
with left_col:
    st.markdown("### 📝 Verified Taxpayer Credentials")
    with st.container(border=True):
        st.session_state.employee_name = st.text_input("Taxpayer Full Name", value=st.session_state.employee_name)
        st.session_state.pan = st.text_input("Permanent Account Number (PAN)", value=st.session_state.pan, max_chars=10, help="10-digit Alphanumeric government ID")
    st.markdown("### 💼 Previous Employment Metrics")
    with st.container(border=True):
        st.session_state.old_employer_name = st.text_input("Previous Employer Name", value=st.session_state.old_employer_name, placeholder="e.g. Tech Solutions Ltd")
        st.session_state.old_employer_tan = st.text_input("Previous Employer TAN", value=st.session_state.old_employer_tan, placeholder="e.g. MUMB12345T")
        st.session_state.old_gross_salary = st.number_input("Cumulative Old Gross Salary (₹)", value=float(st.session_state.old_gross_salary), step=10000.0)
        st.session_state.old_tds_deducted = st.number_input("Cumulative Old TDS Paid (₹)", value=float(st.session_state.old_tds_deducted), step=5000.0)
    st.markdown("### 🚀 New Employment Projections")
    with st.container(border=True):
        st.session_state.new_employer_name = st.text_input("New Employer Name", value=st.session_state.new_employer_name, placeholder="e.g. Innovate FinTech India")
        st.session_state.new_employer_tan = st.text_input("New Employer TAN", value=st.session_state.new_employer_tan, placeholder="e.g. NGPR98765A")
        
        # Grid for salary components
        c1, c2, c3 = st.columns(3)
        with c1:
            st.session_state.new_basic = st.number_input("Monthly Basic (₹)", value=float(st.session_state.new_basic), step=5000.0)
        with c2:
            st.session_state.new_hra = st.number_input("Monthly HRA (₹)", value=float(st.session_state.new_hra), step=2000.0)
        with c3:
            st.session_state.new_special_allowance = st.number_input("Special Allowance (₹)", value=float(st.session_state.new_special_allowance), step=2000.0)
        
        # Switch Month Dropdown (months_list is defined globally above)
        default_month_idx = months_list.index(st.session_state.switch_month) if st.session_state.switch_month in months_list else 6
        st.session_state.switch_month = st.selectbox("Job Switching Month", options=months_list, index=default_month_idx)
# Main Compliance Calculation
tax_data = compute_tax_scenarios(
    old_gross_salary=st.session_state.old_gross_salary,
    old_tds_deducted=st.session_state.old_tds_deducted,
    new_monthly_basic=st.session_state.new_basic,
    new_hra=st.session_state.new_hra,
    new_special_allowance=st.session_state.new_special_allowance,
    switch_month=st.session_state.switch_month
)
remaining_months_count = len(tax_data["remaining_months"])
with right_col:
    st.markdown("### 📊 Live Financial Slabs Dashboard")
    
    # 4 Key Metrics Display
    m1, m2 = st.columns(2)
    m3, m4 = st.columns(2)
    
    with m1:
        st.markdown(f"""
        <div class="metric-card">
            <span style="font-size: 13px; color: #94a3b8; font-weight: 600;">TRUE COMBINED INCOME</span>
            <h2 style="margin: 5px 0 0 0; color: #ffffff;">₹ {tax_data["total_combined_income"]:,.2f}</h2>
            <span style="font-size: 11px; color: #38bdf8;">Previous + Projected Remaining</span>
        </div>
        """, unsafe_allow_html=True)
        
    with m2:
        st.markdown(f"""
        <div class="metric-card">
            <span style="font-size: 13px; color: #94a3b8; font-weight: 600;">TAX ALREADY PAID (TDS)</span>
            <h2 style="margin: 5px 0 0 0; color: #34d399;">₹ {tax_data["old_tds_deducted"]:,.2f}</h2>
            <span style="font-size: 11px; color: #34d399;">Deposited by Previous Employer</span>
        </div>
        """, unsafe_allow_html=True)
    # Check for deficit severity
    deficit = tax_data["projected_march_deficit"]
    
    with m3:
        if deficit > 5000:
            st.markdown(f"""
            <div class="metric-card-danger">
                <span style="font-size: 13px; color: #f87171; font-weight: 700;">🚨 PROJECTED MARCH DEFICIT</span>
                <h2 style="margin: 5px 0 0 0; color: #fca5a5;">₹ {deficit:,.2f}</h2>
                <span style="font-size: 11px; color: #fca5a5;">Pending March TDS Shock!</span>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div class="metric-card">
                <span style="font-size: 13px; color: #94a3b8; font-weight: 600;">PROJECTED MARCH DEFICIT</span>
                <h2 style="margin: 5px 0 0 0; color: #ffffff;">₹ {deficit:,.2f}</h2>
                <span style="font-size: 11px; color: #38bdf8;">Tax liability is fully aligned</span>
            </div>
            """, unsafe_allow_html=True)
            
    with m4:
        # Normal monthly take home under compliance (flat)
        avg_adjusted_in_hand = sum(tax_data["adjusted_in_hand_curve"]) / remaining_months_count if remaining_months_count > 0 else 0.0
        st.markdown(f"""
        <div class="metric-card">
            <span style="font-size: 13px; color: #94a3b8; font-weight: 600;">ADJUSTED IN-HAND SALARY</span>
            <h2 style="margin: 5px 0 0 0; color: #38bdf8;">₹ {avg_adjusted_in_hand:,.2f}</h2>
            <span style="font-size: 11px; color: #94a3b8;">Average / month after PT & Slabs</span>
        </div>
        """, unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)
    # Dynamic Alert System
    if deficit > 5000:
        st.markdown(f"""
        <div class="alert-banner">
            ⚠️ WARNING: Critical Tax Shortfall Detected! (March TDS Shock)<br>
            <span style="font-size: 13px; font-weight: normal; opacity: 0.9;">
            Because you jumped brackets mid-year, your new employer will under-calculate your tax by 
            <b>₹ {deficit:,.2f}</b> unless you submit Form No. 122. If left undeclared, your new employer 
            will have to deduct the entire shortfall from your <b>March 2026</b> salary, causing your March take-home to plummet!
            </span>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="success-banner">
            ✅ COMPLIANT PROFILE: No significant March deficit predicted. Ensure you submit Form No. 122 to establish standard compliance.
        </div>
        """, unsafe_allow_html=True)
    # Plotly Line Visualization
    st.markdown("### 📈 Projected True Monthly In-Hand Salary Curve")
    
    # Format curve data for plotting
    months_remaining = tax_data["remaining_months"]
    df_chart = pd.DataFrame({
        "Month": months_remaining * 2,
        "Net In-Hand Salary (₹)": tax_data["unadjusted_in_hand_curve"] + tax_data["adjusted_in_hand_curve"],
        "Compliance Path": ["Without SwitchTax (March TDS Shock)"] * len(months_remaining) + ["With SwitchTax (Balanced Plan)"] * len(months_remaining)
    })
    
    fig = px.line(
        df_chart, 
        x="Month", 
        y="Net In-Hand Salary (₹)", 
        color="Compliance Path",
        markers=True,
        color_discrete_map={
            "Without SwitchTax (March TDS Shock)": "#ef4444",
            "With SwitchTax (Balanced Plan)": "#38bdf8"
        },
        template="plotly_dark"
    )
    
    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.05)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    
    st.plotly_chart(fig, use_container_width=True)
    # Form Generation Download Widget
    st.markdown("### 📜 Pre-filled Form No. 122 Generation")
    
    # Preparing data structure for form generator
    form_data = {
        "employee_name": st.session_state.employee_name,
        "pan": st.session_state.pan,
        "old_employer_name": st.session_state.old_employer_name,
        "old_employer_tan": st.session_state.old_employer_tan,
        "old_gross_salary": st.session_state.old_gross_salary,
        "old_tds_deducted": st.session_state.old_tds_deducted,
        "new_employer_name": st.session_state.new_employer_name,
        "new_employer_tan": st.session_state.new_employer_tan,
        "new_basic": st.session_state.new_basic,
        "new_hra": st.session_state.new_hra,
        "new_special_allowance": st.session_state.new_special_allowance,
        "remaining_months_count": remaining_months_count,
        "new_projected_salary": tax_data["new_monthly_gross"] * remaining_months_count,
        "total_combined_income": tax_data["total_combined_income"],
        "combined_taxable_income": tax_data["combined_taxable_income"],
        "true_annual_tax_liability": tax_data["true_annual_tax_liability"],
        "remaining_tax_to_collect": max(0.0, tax_data["true_annual_tax_liability"] - tax_data["old_tds_deducted"]),
        "total_pt": tax_data["total_pt"],
        "adjusted_monthly_tds": tax_data["adjusted_monthly_tds"]
    }
    
    try:
        # Generate the PDF binary stream
        pdf_bytes = generate_form_122(form_data)
        
        st.markdown("Click the button below to download the pre-filled official compliance PDF. Submit this file to your new employer to stabilize your TDS and prevent any March deductions shock.")
        
        st.download_button(
            label="⬇️ Download Completed Form No. 122",
            data=pdf_bytes,
            file_name=f"Form_122_{st.session_state.employee_name.replace(' ', '_')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    except Exception as e:
        st.error(f"Error compiling legal PDF template: {e}")
    # Footer Disclaimer
    st.markdown("---")
    st.markdown("<p style='text-align: center; color: #64748b; font-size: 11px; margin-top: 20px;'>For educational and information purposes only. SwitchTax AI does not provide official tax consultancy</p>", unsafe_allow_html=True)
import os
from tax_engine import compute_tax_scenarios
from form_generator import generate_form_122
def main():
    print("--- Running SwitchTax Engine Verification ---")
    
    # Mock data equivalent to the UI's mock dataset
    data = {
        "old_gross_salary": 950000.0,
        "old_tds_deducted": 65000.0,
        "new_basic": 120000.0,
        "new_hra": 50000.0,
        "new_special_allowance": 30000.0,
        "switch_month": "October"
    }
    
    # Run calculations
    results = compute_tax_scenarios(
        old_gross_salary=data["old_gross_salary"],
        old_tds_deducted=data["old_tds_deducted"],
        new_monthly_basic=data["new_basic"],
        new_hra=data["new_hra"],
        new_special_allowance=data["new_special_allowance"],
        switch_month=data["switch_month"]
    )
    
    print(f"Total Combined Income: Rs. {results['total_combined_income']:,.2f}")
    print(f"Standard Taxable Income: Rs. {results['combined_taxable_income']:,.2f}")
    print(f"True Annual Tax Liability: Rs. {results['true_annual_tax_liability']:,.2f}")
    print(f"Projected March Deficit: Rs. {results['projected_march_deficit']:,.2f}")
    print(f"Unadjusted Monthly TDS: Rs. {results['unadjusted_monthly_tds']:,.2f}")
    print(f"Adjusted Monthly TDS: Rs. {results['adjusted_monthly_tds']:,.2f}")
    
    assert results['total_combined_income'] == 2150000.0, "Income calculation mismatch"
    assert results['combined_taxable_income'] == 2075000.0, "Taxable income calculation mismatch"
    
    # 2075000.0 progressive tax FY 2025-26 slabs:
    # 0 to 4L: 0
    # 4L to 8L: 400000 * 0.05 = 20000
    # 8L to 12L: 400000 * 0.10 = 40000
    # 12L to 16L: 400000 * 0.15 = 60000
    # Above 16L: 475000 * 0.20 = 95000
    # Total Base Tax = 20000 + 40000 + 60000 + 95000 = 215000
    # Cess = 215000 * 0.04 = 8600
    # Total Tax = 223600
    print(f"Asserting True Annual Tax is Rs. 2,23,600.00... Found: Rs. {results['true_annual_tax_liability']:,.2f}")
    assert results['true_annual_tax_liability'] == 223600.0, "Tax calculation mismatch"
    
    print("Tax calculations matched perfectly.")
    
    # Test PDF generation
    form_data = {
        "employee_name": "Rajesh Kumar",
        "pan": "APBKP1234F",
        "old_employer_name": "Tech Solutions Ltd",
        "old_employer_tan": "MUMB12345T",
        "old_gross_salary": data["old_gross_salary"],
        "old_tds_deducted": data["old_tds_deducted"],
        "new_employer_name": "Innovate FinTech India",
        "new_employer_tan": "NGPR98765A",
        "new_basic": data["new_basic"],
        "new_hra": data["new_hra"],
        "new_special_allowance": data["new_special_allowance"],
        "remaining_months_count": len(results["remaining_months"]),
        "new_projected_salary": results["new_monthly_gross"] * len(results["remaining_months"]),
        "total_combined_income": results["total_combined_income"],
        "combined_taxable_income": results["combined_taxable_income"],
        "true_annual_tax_liability": results["true_annual_tax_liability"],
        "remaining_tax_to_collect": max(0.0, results["true_annual_tax_liability"] - results["old_tds_deducted"]),
        "total_pt": results["total_pt"],
        "adjusted_monthly_tds": results["adjusted_monthly_tds"]
    }
    
    pdf_bytes = generate_form_122(form_data)
    print(f"Generated PDF successfully. Byte length: {len(pdf_bytes)}")
    assert len(pdf_bytes) > 0, "PDF generation resulted in empty bytes"
    
    print("--- Verification Successful! ---")
if __name__ == "__main__":
    main()
