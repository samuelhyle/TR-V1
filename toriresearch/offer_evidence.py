"""Comparison admission is independent of a classifier's valid enum output.

Only a current reviewed canonical offer supplies a supported comparison policy.
Explicit accessory evidence also blocks admission when the product label is wrong.
Nothing here writes source facts or infers inspection facts from missing words.
"""

import re

VERSION = "offer-comparison-gate-v1"

_BICYCLE_PART = re.compile(
    r"\b(?:ulkorengas|ulkorenkaat|sisärengas|sisärenkaat|bicycle\s+(?:tyre|tire|tube)|"
    r"bike\s+(?:tyre|tire|tube)|cykeldäck)\b", re.I)
_PART_FOR_BICYCLE = re.compile(
    r"\b(?:rengas|renkaat|satula|satulat|poljin|polkimet|vanteet|kiekko|kiekot)\b"
    r".{0,70}\b(?:polkupyörään|polkupyöriin|pyörään|pyöriin)\b", re.I)


def evaluate(row, identity):
    """Return the current offer's admission and explicit reasons for abstaining."""
    attributes = identity.get("attributes") or {}
    source = identity.get("source_attributes") or row.get("product_attribute_evidence") or {}
    source_values = source.get("values") or {}
    reasons = []
    evidence = []
    automatic = identity.get("listing_analysis") or {}
    if automatic.get("status") in {"pending","stale"} and not identity.get("review"):
        reasons.append("listing_analysis_pending")
    elif automatic.get("status") == "current":
        role = automatic.get("offer_role")
        if role in {"accessory_only","part_only"} or automatic.get("profile") in {"accessory","part"}:
            reasons.append("accessory_offer")
        elif role in {"bundle","multiple_units","wanted","service","unknown"}:
            reasons.append(role+"_offer")
        if automatic.get("blockers"):
            reasons.append("conflicting_offer_evidence")
        fields = automatic.get("fields",{})
        if fields.get("functionality",{}).get("value") is False or fields.get("defects",{}).get("value"):
            reasons.append("incompatible_offer_condition")
    title = str(row.get("title") or "")
    part = _BICYCLE_PART.search(title) or _PART_FOR_BICYCLE.search(title)
    if part:
        reasons.append("accessory_offer")
        evidence.append(dict(field="title", span=list(part.span()),
                             basis="explicit_accessory_text", token=part.group()[:80]))
    role = attributes.get("offer_kind")
    if role in {"accessory", "bundle", "multiple_units"}:
        reasons.append("accessory_offer" if role == "accessory" else role + "_offer")
    quantity = attributes.get("quantity")
    if quantity is not None and quantity != 1:
        reasons.append("multiple_units_offer")
    if (attributes.get("functionality") == "defective" or
            attributes.get("completeness") == "incomplete" or
            attributes.get("defects") == "disclosed" or
            attributes.get("condition") == "for_parts"):
        reasons.append("incompatible_offer_condition")
    if identity.get("conflicts"):
        reasons.append("conflicting_offer_evidence")
    # A review of identity cannot erase an explicit current seller disclosure.
    # Reviews stay attributable; contrary offer evidence needs resolution.
    if source.get("conflicts"):
        reasons.append("conflicting_offer_evidence")
    source_role = source_values.get("offer_kind")
    if source_role in {"accessory", "bundle", "multiple_units"}:
        reasons.append("accessory_offer" if source_role == "accessory" else source_role + "_offer")
    if source_values.get("quantity") is not None and source_values["quantity"] != 1:
        reasons.append("multiple_units_offer")
    if (source_values.get("functionality") == "defective" or
            any(entry.get("attribute") in {"defect_type", "missing_contents"}
                for entry in source.get("assertions", []))):
        reasons.append("incompatible_offer_condition")
    if not identity.get("comparison_key"):
        reasons.append("incomplete_reviewed_offer" if identity.get("pilot_scope")
                       else "unsupported_comparison_policy")
    elif not identity.get("review"):
        reasons.append("unreviewed_offer")
    reasons = list(dict.fromkeys(reasons))
    return dict(eligible=not reasons, reasons=reasons, evidence=evidence,
                policy_version=VERSION,
                comparison_level=identity.get("comparison_level", "unsupported"),
                validation_is_accuracy=False)
