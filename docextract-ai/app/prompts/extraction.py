"""LLM prompt templates for document extraction.

Includes per-document-type hint blocks that get prepended when we can
classify the OCR text up-front (EWAY_BILL / DELIVERY_CHALLAN / TAX_INVOICE).
Specialized hints don't change the response schema — they help the LLM
extract the right fields and assign the correct ``document_type`` label.
"""
from __future__ import annotations

import re

SYSTEM_PROMPT = (
    "You are DocExtract AI — a specialized extraction engine for Indian GST "
    "documents. You extract structured data with field-level confidence scores.\n"
    "Rules you ALWAYS follow:\n"
    "1. Return ONLY a valid JSON object. No markdown, no prose, no backticks.\n"
    "2. For every field, return {value: string, confidence: float 0-1}.\n"
    "3. confidence reflects: OCR clarity x field presence certainty x value validity.\n"
    "4. GSTIN is always 15 characters, uppercase alphanumeric. If extracted value "
    "is not 15 chars, set confidence < 0.3.\n"
    "5. Amounts are numeric strings without currency symbols (e.g. '4366.00').\n"
    "6. Dates in YYYY-MM-DD format where possible.\n"
    "7. If a field is not present in the document, return {value: '', confidence: 0.0}.\n"
    "8. NEVER fabricate values. Low confidence + empty value is always better than "
    "a hallucinated value.\n"
    "9. For line items, extract every row in the table. Do not truncate.\n"
    "10. HSN codes are 4-8 digit numeric strings."
)

USER_PROMPT_TEMPLATE = (
    "OCR Text:\n{ocr_text}\n\n"
    "Document hints: {hints}\n\n"
    "Extract all invoice fields as JSON matching schema exactly."
)

SCHEMA_HINT = """
Schema (return EXACTLY this shape — empty string + 0.0 confidence if missing):
{
  "document_type": "TAX_INVOICE|DELIVERY_CHALLAN|PACKING_LIST|PURCHASE_ORDER|CREDIT_NOTE|DEBIT_NOTE|EWAY_BILL|UNKNOWN",
  "overall_confidence": 0.0,
  "amounts_reconciled_flag": true,
  "data": {
    "vendor_name": {"value": "", "confidence": 0.0},
    "vendor_gstin": {"value": "", "confidence": 0.0},
    "customer_name": {"value": "", "confidence": 0.0},
    "customer_gstin": {"value": "", "confidence": 0.0},
    "document_number": {"value": "", "confidence": 0.0},
    "document_date": {"value": "", "confidence": 0.0},
    "subtotal": {"value": "", "confidence": 0.0},
    "cgst": {"value": "", "confidence": 0.0},
    "sgst": {"value": "", "confidence": 0.0},
    "igst": {"value": "", "confidence": 0.0},
    "total_tax": {"value": "", "confidence": 0.0},
    "grand_total": {"value": "", "confidence": 0.0},
    "items": [
      {
        "description": {"value": "", "confidence": 0.0},
        "hsn": {"value": "", "confidence": 0.0},
        "qty": {"value": "", "confidence": 0.0},
        "rate": {"value": "", "confidence": 0.0},
        "amount": {"value": "", "confidence": 0.0}
      }
    ]
  }
}
Rules:
- Amounts: numeric strings without currency symbols.
- Dates: prefer YYYY-MM-DD.
- GSTIN: 15 chars, uppercase.
- confidence in [0,1] reflecting OCR clarity + field certainty.
- Return ONLY the JSON object. No prose, no markdown fences.

CONFIDENCE CALIBRATION GUIDE:
- 0.95-1.0: Field clearly printed, unambiguous, no OCR noise
- 0.80-0.94: Field present but minor formatting variation or partial OCR noise
- 0.60-0.79: Field inferred from context or significant OCR degradation
- 0.30-0.59: Field uncertain, multiple possible interpretations
- 0.00-0.29: Field absent, illegible, or GSTIN length mismatch

AMOUNT RECONCILIATION INSTRUCTION:
Before returning JSON, mentally verify: subtotal + cgst + sgst + igst = grand_total (+/- 1).
If amounts do not reconcile, recheck your extraction and adjust values.
If they still don't reconcile after recheck, set amounts_reconciled_flag: false in the root object.

FEW-SHOT EXAMPLES FOR GSTIN:
Input: '27AABCU9603R1ZX' -> {"value": "27AABCU9603R1ZX", "confidence": 0.98}
Input: '27AABCU9603R1Z' (14 chars) -> {"value": "27AABCU9603R1Z", "confidence": 0.15}
Input: 'GSTIN: 29AAA' (partial) -> {"value": "", "confidence": 0.0}

FEW-SHOT EXAMPLES FOR AMOUNTS:
Input: 'Rs.4,366.00' -> {"value": "4366.00", "confidence": 0.99}
Input: 'Rs. 4366/-' -> {"value": "4366.00", "confidence": 0.95}
Input: unclear smudge -> {"value": "", "confidence": 0.0}
""".strip()


# ---------- Per-document-type specialization ----------

EWAY_BILL_HINT = """
Document-type specialization — E-WAY BILL:
- This is an EWB (E-Way Bill) generated on ewaybillgst.gov.in. Set
  document_type = "EWAY_BILL" with high confidence.
- "vendor_name" / "vendor_gstin" map to the **Generator / Supplier** of the EWB
  (look for "GSTIN of Generator", "From GSTIN", "From:").
- "customer_name" / "customer_gstin" map to the **Recipient / Consignee**
  ("To GSTIN", "To:", "Place of Delivery").
- "document_number" is the **EWB No.** (12-digit numeric, often near "EWB No"
  or "E-Way Bill No"); do NOT confuse with the underlying invoice number.
- "document_date" is the **EWB Generation Date** (not invoice date).
- Amounts: EWBs carry an "Invoice Value" — populate "grand_total" from it. If
  only "Total Value" appears, treat as grand_total. Tax breakdown (CGST/SGST/
  IGST) is usually present; if absent leave those fields empty with 0.0
  confidence (do NOT fabricate).
- DO NOT extract Vehicle Number, Transporter ID, Mode, Distance, or Valid-Upto
  into the standard schema — they have no mapping (would lower data quality).
- If a per-item HSN/qty table is present, populate "items"; otherwise return [].

FEW-SHOT EXAMPLES (E-WAY BILL):
Example 1 — "EWB No: 121000123456 / Generated: 2026-03-14 / GSTIN of Generator:
27AABCU9603R1ZX / To GSTIN: 29AAACG1234A1ZB / Invoice Value: Rs.118000.00"
=> document_type: "EWAY_BILL", document_number.value: "121000123456",
   document_date.value: "2026-03-14", vendor_gstin.value: "27AABCU9603R1ZX",
   customer_gstin.value: "29AAACG1234A1ZB", grand_total.value: "118000.00".
Example 2 — Document shows ONLY "Total Value: 250000" with no CGST/SGST/IGST
breakdown printed. => grand_total.value: "250000.00" (confidence ~0.9),
cgst/sgst/igst values: "" with confidence: 0.0. Never invent the tax split.
""".strip()

DELIVERY_CHALLAN_HINT = """
Document-type specialization — DELIVERY CHALLAN:
- A Delivery Challan accompanies a non-sale movement of goods (job-work,
  branch transfer, sample, sale-on-approval). Set document_type = "DELIVERY_CHALLAN".
- Delivery Challans typically have **no tax amounts** because no sale has
  occurred yet. If CGST/SGST/IGST are absent, return them as empty strings
  with 0.0 confidence — DO NOT infer or compute them.
- "subtotal" and "grand_total" should reflect the **value of goods** declared
  on the challan (often labelled "Value of goods", "Total Value", "Amount").
  If only one total is present, populate both with the same value.
- "document_number" comes from "Challan No.", "DC No.", "Delivery Challan No."
- "vendor_name" / "vendor_gstin" = the consignor / sender (issuer of the challan).
- "customer_name" / "customer_gstin" = the consignee / receiver.
- Items table (description, HSN, qty, rate, amount) MUST be extracted when present.
- Common challan-specific labels: "Despatched through", "Mode of Transport",
  "Place of Supply", "Reason for Transportation" — these don't map to the
  schema; ignore them.

FEW-SHOT EXAMPLES (DELIVERY CHALLAN):
Example 1 — "Delivery Challan No. DC/2026/045 / Date: 14-Mar-2026 / Consignor:
Acme Industries GSTIN 27AABCU9603R1ZX / Consignee: Beta Traders GSTIN
27BBBCE5555F1ZY / Value of goods: 42500"
=> document_type: "DELIVERY_CHALLAN", document_number.value: "DC/2026/045",
   document_date.value: "2026-03-14", vendor_gstin.value: "27AABCU9603R1ZX",
   customer_gstin.value: "27BBBCE5555F1ZY", subtotal.value: "42500.00",
   grand_total.value: "42500.00", cgst/sgst/igst.value: "" with confidence 0.0.
Example 2 — Challan lists 3 items with HSN 8517 each. Return items[] array
with all 3 rows; never collapse or summarise. If qty/rate not printed, return
{"value":"","confidence":0.0} for those subfields.
""".strip()

TAX_INVOICE_HINT = """
Document-type specialization — TAX INVOICE:
- A standard GST Tax Invoice. Set document_type = "TAX_INVOICE".
- Both vendor (supplier) and customer (recipient) sections are usually labelled
  explicitly. Always look for "Bill To" / "Ship To" for the customer block.
- All tax fields (CGST, SGST, IGST) should reconcile against subtotal and
  grand_total within +/- 1 rupee. Prioritise extracting them accurately.

FEW-SHOT EXAMPLES (TAX INVOICE):
Example 1 — "Tax Invoice / Invoice No: INV-2026-0142 / Date: 12/03/2026 /
Supplier: Acme Industries GSTIN 27AABCU9603R1ZX / Bill To: Gamma Ltd GSTIN
29AAACG1234A1ZB / Subtotal 100000 + CGST 9000 + SGST 9000 = Total 118000"
=> document_type: "TAX_INVOICE", document_number.value: "INV-2026-0142",
   document_date.value: "2026-03-12", vendor_gstin.value: "27AABCU9603R1ZX",
   customer_gstin.value: "29AAACG1234A1ZB", subtotal.value: "100000.00",
   cgst.value: "9000.00", sgst.value: "9000.00", igst.value: "",
   grand_total.value: "118000.00", amounts_reconciled_flag: true.
Example 2 — Inter-state invoice with only IGST (no CGST/SGST). Subtotal 50000
+ IGST 9000 = 59000. => cgst.value: "" (conf 0.0), sgst.value: "" (conf 0.0),
igst.value: "9000.00", grand_total.value: "59000.00", reconciled true.
""".strip()


# ---------- Type detection heuristics ----------

_EWAY_KEYWORDS = (
    r"\bE-?\s*Way\s*Bill\b",
    r"\bEWB\s*N?o\b",
    r"\bewaybillgst\b",
    r"\bGSTIN\s+of\s+Generator\b",
    r"\bValid\s*Upto\b",
)

_DELIVERY_CHALLAN_KEYWORDS = (
    r"\bDelivery\s*Challan\b",
    r"\bChallan\s*N?o\b",
    r"\bD\.?C\.?\s*N?o\b",
    r"\bDespatch(?:ed)?\s*through\b",
    r"\bReason\s*for\s*Transportation\b",
)

_TAX_INVOICE_KEYWORDS = (
    r"\bTax\s*Invoice\b",
    r"\bInvoice\s*N?o\b",
    r"\bBill\s*To\b",
)


def _score(text: str, patterns: tuple[str, ...]) -> int:
    return sum(1 for p in patterns if re.search(p, text, flags=re.IGNORECASE))


def detect_document_type(ocr_text: str) -> str:
    """Best-effort classification from raw OCR text. Returns one of:
    "EWAY_BILL" | "DELIVERY_CHALLAN" | "TAX_INVOICE" | "UNKNOWN".

    Used only to pick a specialized prompt — the LLM may override.
    """
    if not ocr_text:
        return "UNKNOWN"
    scores = {
        "EWAY_BILL": _score(ocr_text, _EWAY_KEYWORDS),
        "DELIVERY_CHALLAN": _score(ocr_text, _DELIVERY_CHALLAN_KEYWORDS),
        "TAX_INVOICE": _score(ocr_text, _TAX_INVOICE_KEYWORDS),
    }
    best_type, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "UNKNOWN"
    # Require a clear lead to avoid wrong specialization
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) >= 2 and sorted_scores[0] == sorted_scores[1]:
        return "UNKNOWN"
    return best_type


_TYPE_HINTS = {
    "EWAY_BILL": EWAY_BILL_HINT,
    "DELIVERY_CHALLAN": DELIVERY_CHALLAN_HINT,
    "TAX_INVOICE": TAX_INVOICE_HINT,
}


def build_user_prompt(
    ocr_text: str,
    hints: str = "",
    document_type_hint: str | None = None,
) -> str:
    """Assemble the full user prompt. If ``document_type_hint`` is one of the
    specialized types, its hint block is appended verbatim so the model
    extracts the right fields and labels the document correctly.
    """
    base = USER_PROMPT_TEMPLATE.format(ocr_text=ocr_text, hints=hints or "none")
    type_block = _TYPE_HINTS.get((document_type_hint or "").upper(), "")
    parts = [base]
    if type_block:
        parts.append(type_block)
    parts.append(SCHEMA_HINT)
    return "\n\n".join(parts)


CORRECTION_PROMPT_TEMPLATE = (
    "Your previous extraction had validation errors:\n{errors}\n\n"
    "Re-examine the OCR text and return a corrected JSON object "
    "in the same schema. Focus on reconciling amounts: "
    "subtotal + cgst + sgst + igst should equal grand_total (+/- 1).\n\n"
    "OCR Text:\n{ocr_text}\n\n"
    "Return ONLY the corrected JSON."
)


def build_correction_prompt(ocr_text: str, errors: list[str]) -> str:
    return CORRECTION_PROMPT_TEMPLATE.format(
        errors="\n- " + "\n- ".join(errors) if errors else "(none)",
        ocr_text=ocr_text,
    )
