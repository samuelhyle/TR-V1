"""Single-listing development reviews over the existing source-bound audit.

GET and preview are read-only. Starting freezes a blank local audit; saving
records human evidence without changing observations or holdout labels.
"""
import copy
import inspect
import json
import uuid

from . import db, identity_audit as audit, inventory, listing_analysis, offer_evidence, product_attributes, product_identity as identity, schema

DRAFT_TASK = "product_identity_ui_draft"
RECEIPT_TASK = "product_identity_ui_receipt"
VERSION = "source-review-ui-v1"


def _listing(conn, pk, lock=False):
    inventory._integer(pk, "listing key")
    if lock:
        inventory._token_lock(conn, "identity-review-listing:" + str(pk))
    suffix = " FOR UPDATE" if lock and conn.dialect == "postgres" else ""
    row = conn.execute("SELECT * FROM listings WHERE listing_pk=?" + suffix, (pk,)).fetchone()
    if not row:
        raise ValueError("listing no longer exists locally")
    return listing_analysis.attach(product_attributes.attach([dict(row)], conn=conn), conn=conn)[0]


def _resolved(row, catalog, review=None):
    result = identity.derive(row, catalog)
    if review:
        result = identity._apply_review(result, review, catalog)
    if row["listing_analysis"]["status"] != "not_analyzed":
        result["listing_analysis"] = row["listing_analysis"]
    return result


def _history(conn, row, catalog):
    result = []
    for entry in conn.execute("SELECT enrichment_pk,output_json FROM enrichment WHERE listing_pk=? AND task=? ORDER BY enrichment_pk DESC", (row["listing_pk"], identity.REVIEW_TASK)).fetchall():
        try:
            record = json.loads(entry["output_json"])
            current = (record.get("review_version") == identity.REVIEW_VERSION
                       and record.get("source_input_sha256") == identity.fact_digest(row)
                       and record.get("model_semantics_sha256") == identity.model_semantics(record["expected"], catalog))
            result.append(dict(record, enrichment_pk=entry["enrichment_pk"], current=current))
        except (ValueError, KeyError, TypeError, AttributeError):
            result.append(dict(enrichment_pk=entry["enrichment_pk"], current=False, review_note="Unreadable retained review"))
    return result


def _context(conn, row):
    catalog = identity.load()
    history = _history(conn, row, catalog)
    protected = conn.execute("SELECT output_json FROM enrichment WHERE listing_pk=? AND task IN (?,?,?,?)",
                            (row["listing_pk"], audit.AUDIT_TASK, identity.REVIEW_TASK, "listing_analysis_audit", "listing_analysis_review")).fetchall()
    hint = audit._unit_hint(row)
    if hint:
        protected += conn.execute("SELECT output_json FROM enrichment WHERE task IN (?,?) AND input_sha256=?",
                                  (audit.GROUP_TASK, audit.HINT_TASK, identity._hash(["hint", hint]))).fetchall()
    blocked = []
    for entry in protected:
        try:
            if json.loads(entry[0]).get("purpose", "development") != "development":
                blocked = ["This listing or known physical-unit lineage belongs to an independent holdout. Use its audit workflow."]
                break
        except (ValueError, TypeError, AttributeError):
            blocked = ["Retained audit metadata needs review before a new development label can be recorded."]
            break
    scope = dict(source=identity.fact_digest(row), catalog=catalog["sha256"], rule=identity.RULE_VERSION,
                 policy=identity.COMPARISON_POLICY_VERSION, review=history[0]["enrichment_pk"] if history else 0, hint=hint,
                 listing_analysis=identity._hash(row["listing_analysis"]))
    return catalog, history, blocked, identity._hash(scope)


def view(listing_pk):
    with db.read_snapshot():
        conn = db.connect()
        row = _listing(conn, listing_pk)
        catalog, history, blocked, revision = _context(conn, row)
        current = next((record for record in history if record["current"]), None)
        resolved = _resolved(row, catalog, current)
        fields = {key: dict(type="enum", values=sorted(values)) for key, values in identity.ENUMS.items()}
        fields.update(condition=dict(type="enum", values=sorted(set(schema.ENUM_CONDITION) - {"unknown"})),
                      storage=dict(type="text", max_length=30), hardware_code=dict(type="text", max_length=40),
                      quantity=dict(type="integer", minimum=1, maximum=100), battery_health_pct=dict(type="integer", minimum=0, maximum=100))
        return dict(version=VERSION, listing={key: row.get(key) for key in ("listing_pk", "platform", "subplatform", "listing_id", "title", "category_norm", "condition_norm")},
                    source=audit._source(row), source_assertions=row.get("product_attribute_evidence"),
                    revision=revision, can_review=not blocked, blocked_reasons=blocked,
                    families=[dict(id=key, label=value["label"], models=[{name: model[name] for name in ("id", "label", "brand", "variant_fields")} for model in value["models"]]) for key, value in catalog["families"].items()],
                    fields=fields, outcomes=sorted(audit.OUTCOMES), history=history[:20], history_total=len(history),
                    current_review=current, current_identity=resolved, offer=offer_evidence.evaluate(row, resolved),
                    note="Source statements and physical inspection remain separate. This records a development review, not an independent holdout or an owned inspection.")


def _saved(conn, task, token):
    row = conn.execute("SELECT listing_pk,output_json FROM enrichment WHERE task=? AND input_sha256=?", (task, identity._hash(token))).fetchone()
    return json.loads(row["output_json"]) if row else None


def start(client_token, listing_pk, expected_revision):
    token = audit._uuid(client_token, "review token")
    inventory._integer(listing_pk, "listing key")
    revision = inventory._text(expected_revision, "source review revision", 64, True)
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "source-review:" + token)
            prior = _saved(conn, DRAFT_TASK, token)
            request = dict(listing_pk=listing_pk, revision=revision)
            if prior:
                if prior["request"] != request:
                    raise inventory.Conflict("This review token belongs to a different source snapshot.")
                return dict(client_token=token, review_id=prior["document"]["reviews"][0]["review_id"], replayed=True)
            row = _listing(conn, listing_pk, lock=True)
            catalog, history, blocked, current = _context(conn, row)
            if blocked:
                raise ValueError(blocked[0])
            if current != revision:
                raise inventory.Conflict("Source facts, catalog or a completed review changed. Reload before starting.")
            document = audit.create([row], {}, {"selected_listing": 1}, sampling="operator_selected_single_listing", conn=conn,
                                    audit_id=str(uuid.uuid5(uuid.UUID(token), "source-review-audit")))
            document["reviews"][0]["review_id"] = str(uuid.uuid5(uuid.UUID(token), "source-review-label"))
            draft = dict(request=request, document=document, supersedes_review_id=history[0].get("review_id") if history else None)
            identity._store(row, DRAFT_TASK, identity._hash(token), draft, catalog, conn)
            return dict(client_token=token, review_id=document["reviews"][0]["review_id"], replayed=False)
    finally:
        conn.close()


def _document(draft, answers):
    allowed = {"availability", "checked_at", "review_source", "reviewer", "review_outcome", "expected", "verification", "unit_group", "group_verified", "evidence_ref", "review_note", "confirm_source_review"}
    if not isinstance(answers, dict) or set(answers) - allowed or answers.get("confirm_source_review") is not True or not isinstance(answers.get("expected"), dict):
        raise ValueError("explicit source review confirmation and supported answer fields are required")
    inventory._text(answers.get("evidence_ref"), "evidence reference", 500, True)
    inventory._text(answers.get("review_note"), "review note", 1000, True)
    document = copy.deepcopy(draft["document"])
    example = document["reviews"][0]
    example["source"].update(availability=answers.get("availability"), checked_at=answers.get("checked_at"))
    for key in allowed - {"availability", "checked_at", "confirm_source_review"}:
        example[key] = answers.get(key)
    example["supersedes_review_id"] = draft["supersedes_review_id"]
    return document


def _prepare(conn, token, answers, lock=False):
    draft = _saved(conn, DRAFT_TASK, token)
    if not draft:
        raise ValueError("start a source review in this database first")
    row = _listing(conn, draft["request"]["listing_pk"], lock=lock)
    catalog, _, blocked, revision = _context(conn, row)
    if blocked:
        raise ValueError(blocked[0])
    if revision != draft["request"]["revision"]:
        raise inventory.Conflict("Source facts, catalog or a completed review changed. Reload and start a new review.")
    document = _document(draft, answers)
    ready, result = audit._prepare(document, conn)
    if result["valid"] and ready:
        after = _resolved(row, catalog, ready[0][1])
        result["effect"] = dict(identity_status=after["status"], offer=offer_evidence.evaluate(row, after),
                              missing_fields=sorted(set(after["missing_variant_fields"] + after["missing_comparison_fields"])),
                              requires_physical_inspection=after["requires_physical_inspection"])
    return row, document, result


def preview(client_token, answers):
    token = audit._uuid(client_token, "review token")
    with db.read_snapshot():
        _, _, result = _prepare(db.connect(), token, answers)
        return result


def save(client_token, answers):
    token = audit._uuid(client_token, "review token")
    digest = identity._hash(answers)
    conn = inventory._write_connection()
    try:
        with conn.transaction():
            inventory._token_lock(conn, "source-review:" + token)
            previous = _saved(conn, RECEIPT_TASK, token)
            if previous:
                if previous["request_sha256"] != digest:
                    raise inventory.Conflict("This completed review token was used with different answers.")
                return dict(previous["result"], replayed=True)
            row, document, result = _prepare(conn, token, answers, lock=True)
            if not result["valid"]:
                raise ValueError("review validation failed: " + "; ".join(item["error"] for item in result["errors"]))
            result = audit.import_document(document, conn=conn)
            result.update(review_id=document["reviews"][0]["review_id"], listing_pk=row["listing_pk"])
            identity._store(row, RECEIPT_TASK, identity._hash(token), dict(request_sha256=digest, result=result), identity.load(), conn)
            return dict(result, replayed=False)
    finally:
        conn.close()


def action(payload):
    writers = dict(start=start, preview=preview, save=save)
    if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict) or not isinstance(payload.get("action"), str) or payload["action"] not in writers:
        raise ValueError("supported source review action and params are required")
    writer = writers[payload["action"]]
    try:
        inspect.signature(writer).bind(**payload["params"])
    except TypeError:
        raise ValueError("unsupported or missing source review fields") from None
    return dict(ok=True, result=writer(**payload["params"]))
