"""Independent source labels and frozen semantic predictions; no model calls."""

import json
import uuid
from collections import defaultdict

from . import collection, db, identity_audit, listing_analysis as analysis, product_identity, product_profiles

AUDIT_TASK = "listing_analysis_audit"
REVIEW_TASK = "listing_analysis_review"
VERSION = "listing-analysis-audit-v1"


def _store(conn, row, task, digest, output):
    conn.execute("INSERT INTO enrichment (listing_pk,platform,listing_id,task,input_sha256,model,provider,"
                 "prompt_version,schema_version,output_json,confidence_basis,validation,observed_at) "
                 "VALUES (?,?,?,?,?,'local_audit','local',?,?,?,'Independent source-label workflow','audit_metadata',?) "
                 "ON CONFLICT(platform,listing_id,task,input_sha256) DO NOTHING",
                 (row["listing_pk"],row["platform"],row["listing_id"],task,digest,VERSION,
                  product_profiles.load()["sha256"][:12],analysis._json(output),analysis.now().isoformat()))


def _records(conn, tasks):
    return [dict(item,json=json.loads(item["output_json"])) for item in conn.execute(
        "SELECT listing_pk,task,input_sha256,output_json FROM enrichment WHERE task IN ("+
        ",".join("?" for _ in tasks)+") AND output_json IS NOT NULL AND output_json<>'null'",tasks)]


def _disjoint(conn, row, purpose, group=None):
    """Share split and lineage protections with the earlier identity audits."""
    tasks = [AUDIT_TASK,REVIEW_TASK,identity_audit.AUDIT_TASK,product_identity.REVIEW_TASK]
    entries = conn.execute("SELECT task,output_json FROM enrichment WHERE listing_pk=? AND task IN ("+
                           ",".join("?" for _ in tasks)+")",[row["listing_pk"],*tasks]).fetchall()
    for entry in entries:
        old = json.loads(entry["output_json"])
        if old.get("purpose","development")!=purpose:
            raise ValueError("listing overlaps development and holdout samples")
    hint = identity_audit._unit_hint(row)
    hashes = ([analysis._hash(["hint",hint])] if hint else [])+([analysis._hash(group)] if group else [])
    if hashes:
        entries = conn.execute("SELECT output_json FROM enrichment WHERE task IN (?,?) AND input_sha256 IN ("+
                               ",".join("?" for _ in hashes)+")",
                               [identity_audit.GROUP_TASK,identity_audit.HINT_TASK,*hashes]).fetchall()
        for entry in entries:
            old = json.loads(entry["output_json"])
            if old["purpose"]!=purpose:
                raise ValueError("physical-unit group or lineage overlaps development and holdout")
            if group and hint and old.get("unit_group") not in (None,group):
                raise ValueError("known relists must share one verified physical-unit group")
    return hint


def template(platform="tori", purpose="development", per_family=5):
    """Freeze current predictions, returning blank labels and category schemas."""
    if purpose not in {"development","holdout"} or type(per_family) is not int or not 1<=per_family<=50:
        raise ValueError("choose development/holdout and 1–50 examples per family")
    conditions, values = collection.Scope(platform).conditions()
    rows = db.query("SELECT DISTINCT l.* FROM enrichment e JOIN listings l ON l.listing_pk=e.listing_pk "
                    "WHERE l.platform=? AND e.task=? AND e.validation='source_validated'"+
                    (" AND "+" AND ".join(conditions) if conditions else "")+" ORDER BY l.listing_pk DESC LIMIT 5000",
                    [platform,analysis.TASK,*values])
    return create(analysis.attach(rows),purpose,per_family)


def create(rows, purpose="development", per_family=5):
    from .research import market_url
    if purpose not in {"development","holdout"} or type(per_family) is not int or not 1<=per_family<=50:
        raise ValueError("choose development/holdout and 1–50 examples per family")
    audit_id, reviews, populations = str(uuid.uuid4()), [], defaultdict(int)
    selected = defaultdict(int)
    conn = db.connect()
    try:
        with conn.transaction():
            for row in sorted(rows,key=lambda row:analysis._hash([purpose,row["platform"],row["listing_id"]])):
                prediction = row.get("listing_analysis",{})
                if prediction.get("status")!="current":
                    continue
                family = prediction["family"]
                populations[family] += 1
                if selected[family]>=per_family or len(reviews)>=500:
                    continue
                current = conn.execute("SELECT * FROM listings WHERE listing_pk=?",(row["listing_pk"],)).fetchone()
                if not current or analysis.input_digest(dict(current),analysis.sources_for(conn,row["listing_pk"]))!=prediction["input_sha256"]:
                    raise ValueError("source inputs changed during audit export")
                current = dict(current)
                try:
                    hint = _disjoint(conn,current,purpose)
                except ValueError:
                    continue
                reviewed = conn.execute("SELECT 1 FROM enrichment WHERE listing_pk=? AND task IN (?,?)",
                                        (row["listing_pk"],REVIEW_TASK,product_identity.REVIEW_TASK)).fetchone()
                if purpose=="holdout" and reviewed:
                    continue
                review_id = str(uuid.uuid4())
                source = dict(platform=row["platform"],listing_id=row["listing_id"],
                              public_url=market_url(row["platform"],row["listing_id"]),
                              input_sha256=prediction["input_sha256"],sources=prediction.get("sources",[]))
                manifest = dict(version=VERSION,audit_id=audit_id,review_id=review_id,purpose=purpose,
                                identity=prediction["identity"],source=source,prediction=prediction,unit_group_hint=hint)
                _store(conn,current,AUDIT_TASK,analysis._hash([audit_id,row["listing_pk"]]),manifest)
                if hint:
                    _store(conn,current,identity_audit.GROUP_TASK,analysis._hash(["hint",hint]),dict(purpose=purpose,unit_group=None))
                reviews.append(dict(review_id=review_id,listing_pk=row["listing_pk"],
                                    source=dict(source,availability="not_checked",checked_at=None,version_matches=None),
                                    reviewer=None,labelled_without_predictions=None,unit_group=None,group_verified=False,
                                    unit_group_hint=hint,expected=None))
                selected[family] += 1
    finally:
        conn.close()
    registry = product_profiles.load()
    return dict(version=VERSION,audit_id=audit_id,purpose=purpose,created_at=analysis.now().isoformat(),
                identity=analysis.identity(),populations=dict(populations),sample_counts=dict(selected),
                sampling="hash_order_per_predicted_family_with_unknown_and_accessories; bounded 5000-row candidate frame",
                instructions="Inspect the linked source independently; do not consult stored predictions. Check availability, timestamp and whether the source still matches the recorded revision. Fill expected with profile, offer_role, basis='seller_statement', and ALL fields applicable to that profile. Each field has state, value and basis. Unknown uses null value/basis; explicit_no is boolean false; conflict uses a list of competing values. Known/conflict fields need seller_statement basis. These labels evaluate source interpretation, not physical condition. Holdouts require labelled_without_predictions=true and verified unit_group UUIDs shared by relists. Keep development and holdout groups disjoint. Leave expected=null until reviewed. Unavailable or changed sources cannot provide evaluation labels.",
                profiles={key:{name:value for name,value in profile.items() if name in {"label","family","fields","comparison_required"}}
                          for key,profile in registry["profiles"].items()},
                field_definitions=registry["fields"],reviews=reviews,automatic_comparison_promoted=False)


def _expected(expected):
    registry = product_profiles.load()
    if not isinstance(expected,dict) or set(expected)!={"profile","offer_role","basis","fields"}:
        raise ValueError("expected needs profile, offer_role, basis and all applicable fields")
    if expected["profile"] not in registry["profiles"] or expected["offer_role"] not in analysis.ROLES:
        raise ValueError("invalid expected profile or offer role")
    if expected["basis"]!="seller_statement":
        raise ValueError("semantic labels must describe seller statements; physical inspections remain separate")
    fields = expected["fields"]
    if not isinstance(fields,dict) or set(fields)!=set(registry["profiles"][expected["profile"]]["fields"]):
        raise ValueError("review every applicable field, including unknowns")
    for key,field in fields.items():
        if not isinstance(field,dict) or set(field)!={"state","value","basis"}:
            raise ValueError("each field needs state, value and basis")
        state, value = field["state"],field["value"]
        definition = registry["fields"][key]
        if state=="unknown":
            if value is not None or field["basis"] is not None:
                raise ValueError("unknown fields must leave value and basis null")
            continue
        if state not in {"known","explicit_no","conflict"} or field["basis"]!="seller_statement":
            raise ValueError("invalid field state or missing source basis")
        values = value if state=="conflict" or definition.get("multiple") else [value]
        if not isinstance(values,list) or not values or any(not product_profiles.valid_value(key,item) for item in values):
            raise ValueError("invalid typed field value")
        if state=="conflict" and len({analysis._json(item) for item in values})<2:
            raise ValueError("a conflict needs competing values")
        if state=="explicit_no" and value is not False or state=="known" and value is False:
            raise ValueError("negative boolean claims use explicit_no")
    return expected


def _prepare(document, conn):
    if (not isinstance(document,dict) or document.get("version")!=VERSION or document.get("purpose") not in {"development","holdout"}
            or not isinstance(document.get("reviews"),list) or not 1<=len(document["reviews"])<=500):
        raise ValueError("import needs a bounded listing-analysis audit template")
    audit_id = identity_audit._uuid(document.get("audit_id"),"audit_id")
    ready, errors, seen, groups, hints = [], [], set(), {}, {}
    for entry in _records(conn,[REVIEW_TASK]):
        record = entry["json"]
        if record.get("unit_group"):
            groups[record["unit_group"]] = (record["expected"]["profile"],record["expected"]["fields"].get("model",{}).get("value"))
    cached = blank = 0
    for index,example in enumerate(document["reviews"]):
        try:
            if not isinstance(example,dict):
                raise ValueError("invalid review example")
            if example.get("expected") is None:
                blank += 1
                continue
            pk = example.get("listing_pk")
            if type(pk) is not int or pk<1 or pk in seen:
                raise ValueError("listing keys must be positive and unique")
            seen.add(pk)
            row = conn.execute("SELECT * FROM listings WHERE listing_pk=?",(pk,)).fetchone()
            entry = conn.execute("SELECT output_json FROM enrichment WHERE task=? AND listing_pk=? AND input_sha256=?",
                                 (AUDIT_TASK,pk,analysis._hash([audit_id,pk]))).fetchone()
            if not row or not entry:
                raise ValueError("frozen audit snapshot not found")
            row, manifest = dict(row),json.loads(entry["output_json"])
            token = identity_audit._uuid(example.get("review_id"),"review_id")
            if (manifest["purpose"]!=document["purpose"] or manifest["identity"]!=document.get("identity")
                    or manifest["review_id"]!=token):
                raise ValueError("audit header or review identity differs from snapshot")
            if manifest["identity"]!=analysis.identity():
                raise ValueError("analysis implementation changed; export a new audit")
            if analysis.input_digest(row,analysis.sources_for(conn,pk))!=manifest["source"]["input_sha256"]:
                raise ValueError("source inputs changed; export a new audit")
            source = example.get("source")
            if not isinstance(source,dict) or any(source.get(key)!=value for key,value in manifest["source"].items()):
                raise ValueError("source identity differs from snapshot")
            if source.get("availability")!="available" or source.get("version_matches") is not True:
                raise ValueError("labels require an available source matching the recorded revision")
            checked = identity_audit._checked_at(source.get("checked_at"))
            reviewer = example.get("reviewer")
            if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>80 or any(c in reviewer for c in "\r\n<>"):
                raise ValueError("reviewer needs a short identifier")
            if example.get("labelled_without_predictions") is not True:
                raise ValueError("reviewer must attest labels were supplied without consulting predictions")
            expected = _expected(example["expected"])
            group = example.get("unit_group")
            group = identity_audit._uuid(group,"unit_group") if group else None
            if type(example.get("group_verified")) is not bool or bool(group)!=example["group_verified"]:
                raise ValueError("unit grouping requires explicit verification")
            if document["purpose"]=="holdout" and not group:
                raise ValueError("holdout needs a verified physical-unit group")
            hint = _disjoint(conn,row,document["purpose"],group)
            if hint!=manifest["unit_group_hint"] or example.get("unit_group_hint")!=hint:
                raise ValueError("lineage differs from frozen snapshot")
            labels = (expected["profile"],expected["fields"].get("model",{}).get("value"))
            if group and group in groups and groups[group]!=labels:
                raise ValueError("physical-unit group has conflicting identity labels")
            if hint and hint in hints and hints[hint]!=group:
                raise ValueError("known relists must share one verified group")
            if group:
                groups[group] = labels
            if hint:
                hints[hint] = group
            record = dict(version=VERSION,audit_id=audit_id,review_id=token,purpose=document["purpose"],identity=manifest["identity"],
                          source=dict(manifest["source"],availability="available",checked_at=checked,version_matches=True),
                          reviewer=reviewer.strip(),labelled_without_predictions=True,expected=expected,
                          prediction=manifest["prediction"],unit_group=group,group_verified=bool(group),unit_group_hint=hint)
            old = conn.execute("SELECT listing_pk,output_json FROM enrichment WHERE task=? AND input_sha256=?",
                               (REVIEW_TASK,analysis._hash(token))).fetchone()
            if old:
                if old["listing_pk"]!=pk or json.loads(old["output_json"])!=record:
                    raise ValueError("review_id already belongs to different evidence")
                cached += 1
            else:
                ready.append((row,record))
        except (ValueError,KeyError,TypeError) as exc:
            errors.append(dict(index=index,error=str(exc)))
    return ready,dict(valid=not errors,would_insert=len(ready),cached=cached,blank_examples=blank,errors=errors)


def preview(document):
    with db.read_snapshot():
        conn = db.connect()
        try:
            return _prepare(document,conn)[1]
        except ValueError as exc:
            return dict(valid=False,would_insert=0,errors=[dict(index=None,error=str(exc))])
        finally:
            conn.close()


def import_document(document):
    conn = db.connect()
    try:
        with conn.transaction():
            ready,result = _prepare(document,conn)
            if not result["valid"]:
                raise ValueError("audit validation failed: "+"; ".join(item["error"] for item in result["errors"][:8]))
            for row,record in ready:
                _store(conn,row,REVIEW_TASK,analysis._hash(record["review_id"]),record)
                for digest in ([analysis._hash(record["unit_group"])] if record["unit_group"] else [])+([analysis._hash(["hint",record["unit_group_hint"]])] if record["unit_group_hint"] else []):
                    _store(conn,row,identity_audit.HINT_TASK,digest,dict(purpose=record["purpose"],unit_group=record["unit_group"]))
        return dict(inserted=result["would_insert"],cached=result["cached"],blank_examples=result["blank_examples"],
                    automatic_comparison_promoted=False,note="Source interpretation labels only; no inspection or price approval was created.")
    finally:
        conn.close()


def _equal(left, right):
    def normalized(value):
        if isinstance(value,str):
            return " ".join(value.casefold().split())
        if isinstance(value,list):
            return sorted(analysis._json(normalized(item)) for item in value)
        return value
    return normalized(left)==normalized(right)


def _field_match(predicted, expected):
    if predicted.get("state","unknown")!=expected["state"]:
        return False
    value = predicted.get("value")
    if expected["state"]=="conflict":
        value = list({analysis._json(item["value"]):item["value"] for item in predicted.get("evidence",[])}.values())
    return _equal(value,expected["value"])


def evaluate(records, independent=True):
    """Score each unit once, using the worst relist result for each metric."""
    grouped, excluded = defaultdict(list), defaultdict(int)
    for index,record in enumerate(records):
        if independent and (record.get("purpose")!="holdout" or not record.get("unit_group") or not record.get("group_verified") or not record.get("labelled_without_predictions")):
            excluded["not_independent_holdout"] += 1
            continue
        if record.get("identity")!=analysis.identity():
            excluded["different_analysis_identity"] += 1
            continue
        family = product_profiles.load()["profiles"][record["expected"]["profile"]]["family"]
        grouped[(family,record.get("unit_group") or str(index))].append(record)
    scores = defaultdict(lambda:defaultdict(list))
    false_claims = defaultdict(int)
    for (family,_),group in grouped.items():
        for key in ("profile","offer_role"):
            scores[family][key].append(all(record["prediction"].get(key)==record["expected"][key] for record in group))
        non_product = {"accessory_only","part_only","wanted","service","unknown"}
        if any(record["expected"]["offer_role"] in non_product and record["prediction"].get("offer_role") not in non_product
               and record["prediction"].get("profile") not in {"accessory","part","unknown"} for record in group):
            false_claims[family] += 1
        asserted = [r for r in group if r["prediction"].get("profile")!="unknown"]
        if asserted:
            scores[family]["primary_object_precision"].append(all(r["prediction"].get("profile")==r["expected"]["profile"] for r in asserted))
        scores[family]["no_unsupported_field_claims"].append(all(
            key in record["expected"]["fields"] and _field_match(field,record["expected"]["fields"][key])
            for record in group for key,field in record["prediction"].get("fields",{}).items()
            if field.get("state") in {"known","explicit_no"}))
        keys = {key for record in group for key in record["expected"]["fields"]}
        for key in keys:
            pairs = [(record["prediction"].get("fields",{}).get(key,{}),record["expected"]["fields"][key])
                     for record in group if key in record["expected"]["fields"]]
            scores[family]["field_state:"+key].append(all(_field_match(p,e) for p,e in pairs))
            if any(p.get("state") in {"known","explicit_no"} for p,e in pairs):
                scores[family]["field_precision:"+key].append(all(_field_match(p,e) for p,e in pairs if p.get("state") in {"known","explicit_no"}))
            if any(e["state"] in {"known","explicit_no"} for p,e in pairs):
                scores[family]["field_coverage:"+key].append(all(_field_match(p,e) for p,e in pairs if e["state"] in {"known","explicit_no"}))
        known = [(record["prediction"].get("fields",{}).get(key,{}),field)
                 for record in group for key,field in record["expected"]["fields"].items() if field["state"] in {"known","explicit_no"}]
        if known:
            scores[family]["all_known_fields_correct"].append(all(_field_match(p,e) for p,e in known))
    families = {family:{key:product_identity._precision(sum(values),len(values)) for key,values in metrics.items()}
                for family,metrics in scores.items()}
    return dict(independent_holdout=independent,n_unit_groups=len(grouped),excluded=dict(excluded),families=families,
                false_complete_product_claims=dict(false_claims),
                automatic_comparison_promoted=False,
                note="Per-field precision scores asserted values; coverage scores correct recovery of known labels. Unknowns remain in state accuracy. Unit groups use worst relist outcome. No policy is automatically promoted.")


def report(platform="tori"):
    with db.read_snapshot():
        conn = db.connect()
        try:
            records = [entry["json"] for entry in _records(conn,[REVIEW_TASK]) if entry["json"]["source"]["platform"]==platform]
            templates = conn.execute("SELECT COUNT(*) AS n FROM enrichment WHERE task=? AND platform=?",(AUDIT_TASK,platform)).fetchone()["n"]
            return dict(task=analysis.TASK,identity=analysis.identity(),frozen_examples=templates,reviewed_examples=len(records),
                        holdout=evaluate(records),development=evaluate([r for r in records if r["purpose"]=="development"],independent=False),
                        semantic_accuracy_probability=None,automatic_comparison_promoted=False)
        finally:
            conn.close()
