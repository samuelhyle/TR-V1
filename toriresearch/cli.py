"""Command line entry point."""

import argparse
import json
import os
import shutil
import sys

from . import compliance, config, db
from .http import AccessBlocked, PoliteSession

# EX_UNAVAILABLE. Distinct from collect.sh's 78, which means "we are not
# configured to run at all"; this one means "we are configured fine and the
# platform is refusing us anyway".
EXIT_BLOCKED = 69


def cmd_init(args):
    db.init_db()
    print(f"schema initialised ({db.dialect()}) at {config.SQLITE_PATH}")


    return 0


def cmd_repair_evidence(args):
    print(json.dumps(db.repair_disappearance_outcomes(apply=args.apply), indent=2))
    return 0


def cmd_identity(args):
    from pathlib import Path
    from . import product_identity
    filters = {"platform": args.platform, "subplatform": args.subplatform}
    try:
        if args.action == "catalog":
            result = product_identity.catalog_report()
        elif args.action == "backfill":
            result = product_identity.backfill(filters, limit=5000 if args.limit is None else args.limit, after=args.after)
        elif args.action in ("audit-template", "audit-upgrade"):
            if not args.out:
                raise ValueError(f"{args.action} requires --out")
            path = Path(args.out)
            if path.exists():
                raise ValueError("audit output already exists; choose a new file")
            if args.action == "audit-upgrade":
                if not args.input:
                    raise ValueError("audit-upgrade requires --input")
                result = product_identity.upgrade_audit(json.loads(Path(args.input).read_text()))
            else:
                result = product_identity.audit_template(filters, family=args.family, limit=30 if args.limit is None else args.limit,
                                                        purpose=args.purpose)
            with path.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps({"saved": str(path), "n_examples": len(result["reviews"])}))
            return 0
        elif args.action in ("audit-import", "audit-preview"):
            if not args.input:
                raise ValueError(f"{args.action} requires --input")
            document = json.loads(Path(args.input).read_text())
            result = product_identity.preview_audit(document) if args.action == "audit-preview" else product_identity.import_audit(document)
            if args.action == "audit-preview" and not result["valid"]:
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
                return 2
        else:
            result = product_identity.quality_report(filters)
            if not args.json:
                print(product_identity.render_quality(result))
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_listing_analysis(args):
    from pathlib import Path
    from . import listing_analysis_audit as audit, product_profiles
    from .enrich import listing_queue
    try:
        if args.action=="audit-template":
            if not args.out or Path(args.out).exists():
                raise ValueError("audit-template requires a new --out file")
            result = audit.template(args.platform,args.purpose,args.per_family)
            with Path(args.out).open("x",encoding="utf-8") as handle:
                json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
                handle.write("\n")
            print(json.dumps(dict(saved=args.out,examples=len(result["reviews"]),expected_labels="blank")))
            return 0
        if args.action in {"audit-preview","audit-import"}:
            if not args.input:
                raise ValueError("audit import/preview requires --input")
            document = json.loads(Path(args.input).read_text())
            result = audit.preview(document) if args.action=="audit-preview" else audit.import_document(document)
        elif args.action=="status":
            result = listing_queue.report(args.platform)
        elif args.action=="profiles":
            result = {key:value for key,value in product_profiles.load().items() if key!="profiles"}
            result["profiles"] = {key:{name:value for name,value in profile.items() if name!="_hints"}
                                  for key,profile in product_profiles.load()["profiles"].items()}
        else:
            result = audit.report(args.platform)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 2 if result.get("valid") is False else 0
    except (OSError,ValueError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


def cmd_compliance(args):
    """Whether we can answer a data subject who finds this collector."""
    print(json.dumps(compliance.compliance_report(), indent=2, default=str))
    return 0


def cmd_aggregate(args):
    """Maintain local history and telemetry without source or model calls."""
    from . import aggregates, storage_health
    if getattr(args, "status", False):
        print(json.dumps(aggregates.report(), indent=2, ensure_ascii=False))
        return 0
    result = aggregates.run()
    result["storage"] = storage_health.measure()
    aggregates.export()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def cmd_enrich(args):
    from .enrich import runner, vocabulary
    if args.status:
        from .enrich import queue
        with db.read_snapshot():
            from .enrich import listing_queue
            report = {**runner.summary(args.platform), "queue": queue.report(args.platform),
                      "listing_analysis": listing_queue.report(args.platform)}
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    if args.vocabulary:
        print(json.dumps(vocabulary.coverage(), indent=2))
        return 0
    if args.llm_health:
        from .enrich import provider
        print(json.dumps(provider.health(), indent=2, default=str))
        return 0
    if args.panel:
        print(json.dumps(runner.product_type_panel(min_n=args.min_n),
                         indent=2, ensure_ascii=False, default=str))
        return 0
    print(json.dumps(runner.run(platform=args.platform, budget=args.budget,
                                concurrency=args.concurrency, dry_run=args.dry_run,
                                task=getattr(args,"task","product_type")),
                     indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_dashboard(args):
    """Local control and monitoring. Loopback only, by construction."""
    from . import dashboard
    return dashboard.serve(host=args.host, port=args.port, open_browser=args.open,
                           refresh_seconds=args.refresh)


def cmd_agent_bridge(args):
    """Serve frozen evidence to an external agent. Reads only; proposals quarantine."""
    from . import agent_bridge
    return agent_bridge.serve(host=args.host, port=args.port)


def cmd_agent(args):
    """Run a bounded domain agent over a frozen bundle and record the claim."""
    from . import agent_tasks
    if args.agent == "read":
        result = agent_tasks.run(scope=None)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] in ("complete", "unavailable") else 1
    if args.agent == "queue":
        print(json.dumps(dict(pending=agent_tasks.pending(args.limit),
                              stats=agent_tasks.stats()), indent=2, default=str))
        return 0
    # argparse already restricts this to the two verbs above; the guard is here
    # because a branch that returns None here would print nothing and exit 0,
    # which reads as success.
    raise ValueError("unsupported agent verb: " + str(args.agent))


def cmd_inventory(args):
    """Inspect owned inventory or submit the same bounded local actions as the UI."""
    from pathlib import Path
    from . import inventory
    try:
        if args.request:
            raw = Path(args.request).read_bytes()
            if len(raw) > 32768:
                raise ValueError("inventory request file exceeds 32 KiB")
            result = inventory.action(json.loads(raw))
        elif args.sku:
            result = inventory.get(args.sku)
        else:
            result = inventory.listing(search=args.search, state=args.state, page=args.page)
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return 0


def cmd_drafts(args):
    from pathlib import Path
    from . import drafts
    try:
        if args.request:
            raw = Path(args.request).read_bytes()
            if len(raw) > 32768:
                raise ValueError("draft request file exceeds 32 KiB")
            result = drafts.action(json.loads(raw))
        elif args.draft_id:
            result = drafts.get(args.draft_id)
        else:
            result = drafts.listing(sku=args.sku, page=args.page)
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return 0


def cmd_workflow(args):
    from . import workflow
    try:
        result = workflow.report(page=args.page, sku=args.sku)
    except (ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return 0


def cmd_opportunities(args):
    from pathlib import Path
    from . import opportunities
    try:
        scenarios = None
        if args.scenarios:
            path = Path(args.scenarios)
            if path.stat().st_size > 65536:
                raise ValueError("scenario file exceeds 64KB")
            scenarios = json.loads(path.read_text())
        filters = {key:value for key,value in dict(platform=args.platform, subplatform=args.subplatform,
                    category_norm=args.category, municipality_norm=args.municipality,
                    condition_norm=args.condition, seller_type_norm=args.seller_type).items() if value}
        report = opportunities.board(filters, args.days, args.horizon, args.limit, scenarios)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else opportunities.render(report))
    return 0


def cmd_outcomes(args):
    from pathlib import Path
    from . import owned_outcomes
    try:
        if args.request:
            raw = Path(args.request).read_bytes()
            if len(raw) > 32768:
                raise ValueError("owned outcome request exceeds 32 KiB")
            result = owned_outcomes.action(json.loads(raw))
        elif args.purchase_pk:
            result = owned_outcomes.report(args.purchase_pk)
        else:
            result = owned_outcomes.overview(args.page)
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return 0


def cmd_trades(args):
    from pathlib import Path
    from . import trades
    try:
        if args.request:
            raw = Path(args.request).read_bytes()
            if len(raw) > 32768:
                raise ValueError("trade request file exceeds 32 KiB")
            result = trades.action(json.loads(raw))
        elif args.order_id:
            result = trades.get(args.order_id)
        elif args.as_of:
            if not args.sku:
                raise ValueError("--as-of requires --sku")
            result = trades.economics(args.sku, args.as_of)
        else:
            result = trades.listing(sku=args.sku, page=args.page)
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return 0


def cmd_permissions(args):
    from . import permissions
    try:
        result = permissions.import_confirmation() if args.import_confirmation else permissions.report()
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["valid"] else 1


def cmd_connections(args):
    from pathlib import Path
    from . import connections
    from .ebay_api import EbayAPIError
    try:
        if args.request:
            raw = Path(args.request).read_bytes()
            if len(raw) > 16384:
                raise ValueError("connection request file exceeds 16 KiB")
            result = connections.action(json.loads(raw))
        else:
            result = connections.status(args.environment)
    except (ValueError, OSError, EbayAPIError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_dev(args):
    """Dev supervisor: dashboard plus restart-on-change. Loopback only."""
    from . import dev
    return dev.main(["--host", args.host, "--port", str(args.port),
                     "--refresh", str(args.refresh)] + (["--open"] if args.open else []))


def cmd_sweep(args):
    from .ingest import sweep
    session = PoliteSession()
    if args.build:
        result = sweep.build_targets(session, limit_locations=args.limit_locations, budget=args.budget)
    else:
        result = sweep.run(session, budget=args.budget, pages=args.pages,
                           only_municipality=not args.include_regions,
                           dry_run=args.dry_run, deepest_first=args.deepest_first)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 69 if result.get("status") == "blocked" else 75 if result.get("status") == "throttled" else 1 if result.get("status") == "error" else 0


def cmd_coverage(args):
    from . import collection
    from .ingest import sweep
    report = sweep.coverage()
    report["followup"] = collection.coverage()
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_itempage(args):
    from .ingest import itempage
    session = PoliteSession()
    result = itempage.run(session, budget=args.budget,
                          only_stale=not args.all_listings,
                          min_age_hours=args.min_age_hours)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 69 if result.get("status") == "blocked" else 75 if result.get("status") == "throttled" else 1 if result.get("status") == "error" else 0


def cmd_sell(args):
    """Record a completed sale. Closes the loop the buy side was missing.

    Without this the purchases table is a one-way ledger: you can record what you
    bought and never what it cleared for, which is the only number in the project
    nobody else can get.
    """
    from . import purchases
    result = purchases.record_sale(
        source_listing_id=args.source_listing_id,
        sold_listing_id=args.sold_listing_id,
        final_sale_price_eur=args.price,
        sold_at=args.date,
        platform=args.platform,
        platform_fee_eur=args.platform_fee,
        payment_fee_eur=args.payment_fee,
        seller_shipping_cost_eur=args.shipping,
        packaging_cost_eur=args.packaging,
        other_variable_cost_eur=args.other_cost,
        tax_cost_eur=args.tax,
        buyer_fee_eur=args.buyer_fee,
        shipping_method=args.shipping_method,
        outcome_status=args.outcome,
        notes=args.notes,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_economics(args):
    from . import purchases
    print(json.dumps(purchases.summary(), indent=2, default=str))
    return 0


def cmd_retention(args):
    session = PoliteSession(require_contact=False)
    print(json.dumps(session.enforce_retention(), indent=2))
    return 0


def cmd_resume_collection(args):
    path = config.DATA_DIR / "collection_stop.json"
    if path.exists():
        path.unlink()
    print("Tori collection stop cleared. The next scheduled run may request the platform.")
    return 0


def cmd_doctor(args):
    """Everything that has to be true before a scheduled collection can work.

    Run before installing the scheduler, not after. A launchd job that fails
    126 on every fire for a month is invisible in every dashboard this project
    has, because nothing is being written and there is nothing to plot; `doctor`
    exists so the failure is visible before it becomes silent.

    Checks are grouped by whether they stop collection or merely degrade it, and
    the exit code is non-zero if anything in the first group fails -- so this is
    usable as a precondition in the scheduler itself.
    """
    from . import schema
    blocking, warnings, notes = [], [], []

    # -- blocking: collection cannot run at all ---------------------------
    try:
        conn = db.connect()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            if db.dialect() == "sqlite" else
            "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
        missing = [t["name"] for t in schema.TABLES if t["name"] not in tables]
        if missing:
            blocking.append(
                f"database is missing {len(missing)} table(s): "
                f"{', '.join(missing[:5])}"
                + (" ..." if len(missing) > 5 else "")
                + " -- run: python3 -m toriresearch.cli init")
        missing_columns = []
        for table in schema.TABLES:
            if table["name"] not in tables:
                continue
            present = db.existing_columns(conn, table["name"])
            missing_columns.extend(f"{table['name']}.{name}" for name, _decl, _note in table["columns"]
                                   if name not in present)
        if missing_columns:
            blocking.append("database schema is missing columns: " + ", ".join(missing_columns[:5]) +
                            " -- run: python3 -m toriresearch.cli init")
        conn.close()
        if 'listings' in tables:
            from . import backups
            if backups.unexpected_empty():
                blocking.append("saved research corpus is unexpectedly empty -- review the verified backup before collection")
    except Exception as exc:
        blocking.append(f"cannot open the database at {config.SQLITE_PATH}: {exc}")

    if not config.contact_email_ok():
        blocking.append(
            "TORI_CONTACT_EMAIL is not a deliverable address; PoliteSession "
            "refuses to send and collect.sh exits 78")

    if not config.DATA_DIR.exists():
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            blocking.append(f"cannot create data directory "
                            f"{config.DATA_DIR}: {exc}")
    elif not os.access(config.DATA_DIR, os.W_OK):
        blocking.append(f"data directory is not writable: {config.DATA_DIR}")
    # A reports directory that does not exist yet is not a failure: it is a
    # fresh checkout, which is exactly when `doctor` is worth running. Create it
    # rather than reporting a permission problem that does not exist -- and note
    # that os.access() returns False for a *missing* path, so asking first would
    # have called every uninitialised checkout broken.
    if not config.REPORTS_DIR.exists():
        try:
            config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            blocking.append(f"cannot create reports directory "
                            f"{config.REPORTS_DIR}: {exc}")
    elif not os.access(config.REPORTS_DIR, os.W_OK):
        blocking.append(f"reports directory is not writable: {config.REPORTS_DIR}")

    # -- warnings: collection runs, but the result is degraded ------------
    free = shutil.disk_usage(config.DATA_DIR).free
    if free < 500 * 1024 ** 2:
        warnings.append(f"only {free // 1024 ** 2}MB free on the data volume")

    if db.dialect() == "sqlite":
        warnings.append("SQLite backend: single-writer only. Set TORI_PG_DSN "
                        "before running collectors concurrently.")

    for name in db.active_locks():
        warnings.append(f"lock held, a collector may be running: {name}")

    comp = compliance.compliance_report()
    from . import permissions
    authority = permissions.report()
    if not authority["valid"]:
        blocking.append(authority["error"] + " -- import the existing confirmation or review source authority")
    for action, result in authority["actions"].items():
        if not result["allowed"]:
            blocking.append(result["reason"])
    if comp["blocking"]:
        notes.append(
            f"{len(comp['blocking'])} Article 14 / LIA precondition(s) outstanding: "
            + ", ".join(comp["blocking"]))
        notes.append("Document checks do not verify publication, sign-off or authority. "
                     "Covered collection retains the recorded owner confirmation; "
                     "unfinished document requirements need operator review.")

    # Seller-level models group listings under a seller, which needs a key the
    # parser only derives when a salt is configured. Reporting it here means an
    # empty `seller_archetypes` run is explicable from `doctor` alone, instead
    # of only from that model's own error message. The reason comes from the
    # same place the models get theirs, so the two cannot drift apart.
    try:
        from .predict import _linkage
        link = _linkage.status()
        if link["reason"]:
            notes.append(f"seller linkage: unavailable -- {link['reason']}")
        else:
            notes.append(
                f"seller linkage: on, {link['listings_with_key']} of "
                f"{link['listings_total']} listings linked "
                f"({link['sellers']} seller versions, "
                f"{link['listings_with_version']} resolved)")
    except Exception as exc:
        notes.append(f"seller linkage status unavailable: {exc}")

    # -- informational ---------------------------------------------------
    try:
        coverage = sweep_module().coverage()
        notes.append(
            f"grid: {coverage['targets']} targets, "
            f"{coverage['never_crawled']} never crawled, "
            f"coverage {coverage['grid_coverage_pct']}%")
        if coverage.get("median_hours_since_crawl") is not None:
            notes.append(
                f"mean crawl age: {coverage['median_hours_since_crawl']}h")
    except Exception as exc:
        notes.append(f"could not read sweep coverage: {exc}")

    try:
        recent = db.query_one(
            "SELECT throttled_429, status, finished_at FROM sweep_runs "
            "ORDER BY sweep_run_pk DESC LIMIT 1")
        if recent:
            if recent["throttled_429"]:
                warnings.append(
                    f"last sweep was throttled ({recent['throttled_429']}x429) "
                    f"at {recent['finished_at']} -- the platform is asking for a "
                    "lower rate")
            else:
                notes.append(f"last sweep: {recent['status']} at "
                             f"{recent['finished_at']}")
    except Exception:
        pass

    print(f"database:   {config.SQLITE_PATH}")
    print(f"dialect:    {db.dialect()}")
    print(f"contact:    {config.CONTACT_EMAIL or '(unset)'}")
    print()
    for item in blocking:
        print(f"  BLOCKING  {item}")
    for item in warnings:
        print(f"  WARNING   {item}")
    for item in notes:
        print(f"  note      {item}")
    if not blocking:
        print("\n  collection can run")
    return 1 if blocking else 0


def sweep_module():
    from .ingest import sweep
    return sweep


def cmd_schema(args):
    from . import schema
    text = schema.render_postgres() if args.dialect == "postgres" else schema.render_sqlite()
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_taxonomy(args):
    from .ingest import taxonomy
    session = PoliteSession()
    docs = taxonomy.fetch_sitemap_documents(session)
    cats, locs = taxonomy.parse_dimensions(docs)
    n_cats = taxonomy.write_categories(cats)
    cache = taxonomy.resolve_location_labels(session, locs, limit=args.limit_locations)
    n_locs = taxonomy.write_locations(cache)
    print(json.dumps({
        "sitemap_docs": len(docs),
        "categories_discovered": len(cats),
        "categories_written": n_cats,
        "location_ids_discovered": len(locs),
        "location_labels_resolved": sum(1 for v in cache.values() if v),
        "locations_written": n_locs,
    }, indent=2))
    return 0


def cmd_atom(args):
    from .ingest import atom
    session = PoliteSession()
    result = atom.ingest(session)
    print(json.dumps(result, indent=2))
    return 0


def cmd_ebay(args):
    from .ingest import ebay
    if not config.EBAY_CLIENT_ID or not config.EBAY_CLIENT_SECRET:
        print("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET must be set in the environment", file=sys.stderr)
        return 2
    try:
        result = ebay.run_search(config.EBAY_MARKETPLACE_ID, args.keyword, limit=args.limit)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_buy(args):
    from . import purchases
    purchase_pk = purchases.record(
        title=args.title,
        acquisition_cost_eur=args.cost,
        platform=args.platform,
        subplatform=args.subplatform,
        source_listing_id=args.source_listing_id,
        category_norm=args.category,
        brand_norm=args.brand,
        model_norm=args.model,
        condition_norm=args.condition,
        purchased_at=args.date,
        currency=args.currency,
        shipping_cost_eur=args.shipping,
        buyer_fee_eur=args.buyer_fee,
        notes=args.notes,
    )
    print(json.dumps({"purchase_pk": purchase_pk, "recorded": True}, indent=2))
    return 0


def cmd_askpanel(args):
    from .http import PoliteSession
    from .ingest import askpanel
    cache = askpanel.load_location_ids()
    loc_id = args.location_id or next(
        (k for k, v in cache.items() if v and v.lower() == (args.municipality or "").lower()), None
    )
    if not loc_id:
        print("unknown municipality; try: toriresearch taxonomy", file=sys.stderr)
        return 2
    result = askpanel.run(PoliteSession(), args.municipality, loc_id,
                          category=args.category, page_limit=args.pages)
    print(json.dumps(result, indent=2))
    return 0


def cmd_buys(args):
    from . import purchases
    rows = purchases.list_purchases()
    if not rows:
        print("no purchases recorded yet")
        return 0
    print(f"{'pk':>4}  {'purchased':<11} {'cat':<22} {'brand':<14} {'cost EUR':>9}")
    for r in rows:
        print(f"{r['purchase_pk']:>4}  {str(r['purchased_at'])[:10]:<11} "
              f"{str(r['category_norm'] or '-')[:22]:<22} {str(r['brand_norm'] or '-')[:14]:<14} "
              f"{r['acquisition_cost_eur']:>9.2f}")
    return 0


def cmd_panel(args):
    from . import metrics
    report = metrics.ask_price_panel(
        category_norm=args.category,
        region_norm=args.region,
        municipality_norm=args.municipality,
        min_n=config.MIN_N_FOR_REPORT,
        platform=args.platform, subplatform=args.subplatform,
        condition_norm=args.condition, seller_type_norm=args.seller_type,
    )
    print(metrics.render_panel(report))
    return 0


def cmd_liquidity(args):
    from . import metrics
    report = metrics.liquidity(
        horizon_days=args.horizon,
        category_norm=args.category,
        region_norm=args.region,
        municipality_norm=args.municipality,
        min_n=config.MIN_N_FOR_REPORT,
        platform=args.platform, subplatform=args.subplatform,
        condition_norm=args.condition, seller_type_norm=args.seller_type,
    )
    print(metrics.render_liquidity(report))
    return 0


def cmd_focus(args):
    """Each configured focus vertical on a terminal, from the same slice rules.

    A view that only exists in a browser cannot be piped into anything, diffed
    against last week, or read by anything that is not a person looking at a
    screen. This is the same document the dashboard's Focus categories view
    renders.
    """
    from . import analysis
    report = analysis.focus_breakdown(
        {"platform": args.platform, "subplatform": args.subplatform,
         "municipality_norm": args.municipality},
        horizon=args.horizon)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0
    print(render_focus(report))
    return 0


def render_focus(report):
    """Rank the configured verticals by what the evidence actually supports."""
    min_n = report["min_n"]
    lines = [
        f"Focus verticals  ·  generated {report['generated_at']}",
        f"{report['n_configured']} configured  ·  {report['prices_ready']} with a supported "
        f"asking-price median  ·  {report['sale_ready']} with a supported sale rate  ·  "
        f"{report['horizon']}d horizon  ·  minimum {min_n} observations",
        f"market listings in view: {report['n_market']}",
        "",
    ]
    if not report["panels"]:
        lines += ["No focus categories configured. Collection rotates the national grid "
                  "and no vertical is prioritised.", ""]
        return "\n".join(lines)
    for panel in report["panels"]:
        follow, grid = panel["followup"], panel["grid"]
        queue = follow['queue']
        header = panel["name"] if panel["resolved_label"] else f"{panel['path']} (unlabelled)"
        if panel["status"] == "OK":
            ci = panel.get("median_ci95")
            price = (f"median EUR {panel['median_eur']:.0f}"
                     + (f" (95% CI {ci['lo']:.0f}-{ci['hi']:.0f})" if ci else ""))
        else:
            price = f"no supported price (needs {min_n} fresh prices, has {panel['n_priced']})"
        lines += [
            header,
            f"  {panel['path']}  ·  {price}",
            f"  tracked {panel['n_tracked']}, available {panel['n_available']}, "
            f"priced {panel['n_priced']}"
            + (f" ({panel['price_coverage']:.0%} of available)" if panel["price_coverage"] is not None else ""),
            f"  sale markers: {panel['sold_declared']} declared / {panel['sold_verified']} verified; "
            f"{panel['horizon_support']} followed to {report['horizon']}d; "
            f"status {panel['sale_status']}",
            f"  follow-up: {queue['due']} queued in focus "
            + (f"({queue['not_admitted']} not admitted; {queue['awaiting_first']} awaiting first success); "
               if queue['enabled'] else f"({follow['n_never_probed']} never attempted); ")
            + f"{queue['minimum_runs']} "
            f"full-budget run(s) at {follow['item_budget']} items/run",
            f"  grid: {grid['crawled']} of {grid['cells']} cells read "
            f"({grid['pending']} never visited)"
            + (f", median age {grid['median_hours_since_crawl']} h" if grid["median_hours_since_crawl"] is not None else ""),
            "",
        ]
    totals = report["totals"]
    queue = totals['queue']
    lines += [
        "Whole focus scope",
        f"  {queue['due']} status checks queued of {totals['n_tracked']} tracked; "
        f"{queue['minimum_runs']} minimum full-budget runs",
        *([f"  {queue['admitted']} admitted; {queue['not_admitted']} discovered outside admission"] if queue['enabled'] else []),
        f"  condition known {totals['n_known_condition']}, "
        f"accepted classifications {totals['n_classified']}",
        "",
"Each vertical is measured on its own cohort. Asking prices are not clearing "
        "prices, and a sale marker is not a realised price. Every estimate above "
        "carries n.",
        ""]
    return "\n".join(lines)


def cmd_analysis(args):
    """The analyst view, on a terminal, from the same slice rules as the dashboard.

    A view that only exists in a browser cannot be piped into anything, diffed
    against last month, or read by anything that is not a person looking at a
    screen. This is the same document the dashboard renders.
    """
    from . import analysis
    report = analysis.snapshot(
        {"platform": args.platform, "subplatform": args.subplatform,
         "category_norm": args.category, "municipality_norm": args.municipality,
         "condition_norm": args.condition, "seller_type_norm": args.seller_type},
        dimension=args.dimension, days=args.days, horizon=args.horizon,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0
    print(render_analysis(report))
    return 0


def render_analysis(report):
    """The analyst brief, rendered by the module that computes it.

    This used to be a second hand-maintained copy of `analysis.render_analysis`,
    and it had already drifted: the copy here predated the buy/no-buy section,
    so the terminal brief silently stopped reporting a panel the dashboard showed.
    A brief that differs by surface is worse than no brief, because the two get
    compared against each other. There is now one implementation.
    """
    from . import analysis
    return analysis.render_analysis(report)


def cmd_soldextract(args):
    from . import soldextract
    if args.action == "template":
        out = config.REPORTS_DIR / "sold_capture_template.csv"
        out.write_text(soldextract.template_csv())
        print(f"wrote {out}")
        print("Fill it in from eBay Seller Hub -> Research, one row per keyword/condition.")
        print("Then: python3 -m toriresearch.cli soldextract import <file.csv>")
        return 0
    if args.action == "import":
        text = open(args.file).read()
        print(soldextract.import_capture(text, captured_by=args.captured_by))
        return 0
    if args.action == "summary":
        print(json.dumps(soldextract.summary(), indent=2))
        return 0
    if args.action == "ratio":
        print(json.dumps(soldextract.realization_ratio(
            keyword=args.keyword, min_obs=args.min_obs), indent=2, default=str))
        return 0
    return 2


def cmd_realization(args):
    """Expected-realized-price estimation and the buy/no-buy decision on it."""
    from . import realization
    if args.action == "ratios":
        print(json.dumps(realization.own_trade_ratios(min_n=args.min_n), indent=2, default=str))
        return 0
    if args.action == "resale-ratios":
        print(json.dumps(realization.resale_trade_ratios(min_n=args.min_n,
            category_norm=args.category, condition_norm=args.condition,
            platform=args.platform, subplatform=args.subplatform,
            brand_norm=args.brand, model_norm=args.model), indent=2, default=str))
        return 0
    if args.action == "estimate":
        out = realization.realized_price_estimate(
            category_norm=args.category, condition_norm=args.condition, min_n=args.min_n,
            platform=args.platform, subplatform=args.subplatform, brand_norm=args.brand, model_norm=args.model)
        if args.json:
            print(json.dumps(out, indent=2, default=str))
            return 0
        print(render_realization_brief(out))
        return 0
    if args.action == "decide":
        interval = None
        if args.interval:
            parts = [p.strip() for p in args.interval.split(",")]
            if len(parts) != 2:
                print("--interval expects two comma separated numbers", file=sys.stderr)
                return 2
            interval = [float(parts[0]), float(parts[1])]
        print(json.dumps(realization.buy_no_buy(
            args.cost, args.expect, interval_eur=interval,
            selling_costs_eur=args.selling_costs,
            min_margin_pct=args.min_margin_pct), indent=2, default=str))
        return 0
    return 2


def render_realization_brief(estimate):
    """Readable summary of an expected-realized-price estimate."""
    lines = ["== expected realized price ==",
             f"status  {estimate.get('status')}   blocking {estimate.get('blocking', '-')}"]
    filters = estimate.get("filters") or {}
    active = [f"{k}={v}" for k, v in filters.items() if v]
    lines.append("filters " + (" ".join(active) if active else "none"))
    evidence = estimate.get("evidence") or {}
    panel = evidence.get("ask_panel") or {}
    lines.append(f"ask     {panel.get('status')}  n={panel.get('n_with_price')}  "
                 f"median EUR {panel.get('median_eur')}")
    own = evidence.get("resale_trade_ratios") or {}
    lines.append(f"ratio   {own.get('status')}  n={own.get('n_valid_ratios')}  "
                 f"median {own.get('median_ratio')}")
    if estimate.get("note"):
        lines.append(f"note    {estimate['note']}")
    est = estimate.get("estimate")
    if est:
        span = ""
        if est.get("interval_eur"):
            span = f"  interval [{est['interval_eur'][0]}, {est['interval_eur'][1]}]"
        lines.append(f"expect  EUR {est['expected_realized_eur']}  "
                     f"ratio {est['realization_ratio']}{span}")
        lines.append(f"conf    {est.get('confidence')}")
        if est.get("caveat"):
            lines.append(f"caveat  {est['caveat']}")
    lines.append("This is an estimate derived from observed asks and our own measured trades. "
                 "It is not an observed clearing price.")
    return "\n".join(lines)


def cmd_report(args):
    from . import metrics
    metrics.write_reports()
    print(f"reports written to {config.REPORTS_DIR}")
    return 0


def cmd_market_intel(args):
    from .market_intel import run_continuous_intelligence
    result = run_continuous_intelligence()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_internet_research(args):
    from . import internet_research
    try:
        if args.research_action == "status":
            result = internet_research.report(args.listing_pk) if args.listing_pk else internet_research.monitor()
        elif args.research_action == "add":
            result = internet_research.add_source(args.listing_pk,args.url,args.kind,args.label,args.interval_hours,args.watch,args.review_updates)
        elif args.research_action == "run":
            internet_research.recover()
            result = internet_research.start(args.listing_pk,args.operation,background=False)
            result["report"] = internet_research.report(args.listing_pk)
        elif args.research_action == "tick":
            internet_research.recover()
            result = internet_research.tick(background=False) or {"status": "nothing_due_or_busy"}
        elif args.research_action == "watch":
            result = internet_research.configure_source(args.listing_pk,args.source_id,args.enabled)
        else:
            result = internet_research.remove_source(args.listing_pk,args.source_id)
    except ValueError as exc:
        print(f"Invalid internet research request: {exc}",file=sys.stderr)
        return 2
    print(json.dumps(result,indent=2,ensure_ascii=False,default=str,allow_nan=False))
    return 0


def cmd_scenario(args):
    from . import research
    try:
        with open(args.input, encoding="utf-8") as handle:
            assumptions = json.load(handle)
        result = research.scenario(assumptions)
        if result is None:
            raise ValueError("scenario input must be an object of explicit assumptions")
    except (OSError, ValueError) as exc:
        print(f"Invalid scenario: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


def cmd_predict(args):
    """Run optional research models, with readable evidence/dependency failures."""
    try:
        return _cmd_predict(args)
    except ImportError as exc:
        print(f"unavailable: optional predictive dependency missing ({exc}). "
              "Run: python3 -m pip install -r requirements-predict.txt", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"unavailable: {exc}", file=sys.stderr)
        return 1


def _cmd_predict(args):
    import json
    from . import predict

    listing_pks = None
    if args.listing_pks is not None:
        if args.model not in ("sale_probability", "price_trajectory"):
            raise ValueError("--listing-pks applies to sale_probability and price_trajectory")
        listing_pks = [int(x.strip()) for x in args.listing_pks.split(",")]
        if not listing_pks or any(pk <= 0 for pk in listing_pks):
            raise ValueError("--listing-pks must contain positive listing IDs")
    if args.months_back is not None and args.months_back <= 0:
        raise ValueError("--months-back must be positive")
    if (args.months_back is not None or args.region is not None) and args.model not in ("category_trends", "market_concentration"):
        raise ValueError("--months-back and --region apply to category_trends and market_concentration")
    horizons = [args.horizon] if args.horizon is not None else None

    if args.readiness:
        if args.model != "price_trajectory" or args.persist:
            raise ValueError("--readiness applies to price_trajectory and is read-only")
        result = predict.price_trajectory_readiness()
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["ready"] else 1

    if args.validate:
        # The rehearsal is a whole-database check across every model family, not
        # one model, so it runs before the per-model dispatch below and ignores
        # `--model`. `validate_database` copies the source read-only and reports
        # `source_modified`, so it cannot write to the live database.
        from .predict import validation
        from . import config
        report = validation.validate_database(config.SQLITE_PATH)
        print(json.dumps(report, indent=2, allow_nan=False, default=str))
        return 0 if report.get("source_modified") is False else 1

    forecasts = []
    if args.model == "sale_probability":
        if args.train:
            model = predict.train_sale_probability()
            print(json.dumps(dict(model.training_metrics, model_version=model.model_version,
                                  trained_at=model.trained_at), indent=2))
            return 0 if model.activated else 1
        if args.evaluate:
            print(json.dumps(predict.evaluate_model(args.horizon or 30), indent=2))
            return 0
        rows = predict.predict_sale_probability(listing_pks=listing_pks, horizons=horizons,
            **({"experimental":True} if args.experimental else {}))
        if args.persist:
            predict.persist_predictions(rows)
    elif args.model == "price_trajectory":
        if args.train:
            result = predict.train_price_trajectory()
            print(json.dumps(result, indent=2))
            return 0 if result.get("activated") else 1
        if args.evaluate:
            result = predict.evaluate_price_trajectory()
            print(json.dumps(result, indent=2))
            return 0 if result.get("available") else 1
        rows = predict.predict_price_trajectories(listing_pks=listing_pks, horizons=horizons,
            **({"experimental":True} if args.experimental else {}))
        if args.persist:
            predict.persist_trajectories(rows)
    elif args.model == "category_trends":
        if args.evaluate:
            result = predict.trend_summary()
            print(json.dumps(result, indent=2))
            return 0 if result else 1
        rows = predict.compute_category_trends(months_back=args.months_back, region_norm=args.region)
        forecasts = predict.compute_trend_forecasts(months_back=args.months_back, region_norm=args.region,
            **({"experimental":True} if args.experimental else {}))
        if args.persist:
            predict.persist_trends(rows)
            predict.persist_forecasts(forecasts)
    elif args.model == "seller_archetypes":
        if args.evaluate:
            result = predict.archetype_summary()
            print(json.dumps(result, indent=2))
            return 0 if result else 1
        result = predict.train_seller_archetypes(persist=args.persist, days=args.window_days)
        print(json.dumps({k:v for k,v in result.items() if k != "assignments"}, indent=2))
        return 0
    elif args.model == "market_concentration":
        if args.evaluate:
            result = predict.concentration_summary()
            print(json.dumps(result, indent=2, default=str))
            return 0 if result else 1
        rows = predict.compute_category_concentration(months_back=args.months_back or 6, region_norm=args.region)
        if args.persist:
            predict.persist_concentration(rows)
    else:
        raise ValueError(f"Unknown model: {args.model}")
    if not rows:
        print("unavailable: no eligible observations in this scope; trends need completed weeks, "
              "and listing forecasts need fresh active priced listings", file=sys.stderr)
        return 1
    print(f"Generated {len(rows)} {args.model} rows" + ("; persisted to database" if args.persist else ""))
    print(json.dumps(rows[:10], indent=2, default=str))
    if len(rows) > 10:
        print(f"... and {len(rows)-10} more")
    if forecasts:
        print(f"Projected {len(forecasts)} arrival rows; "
              + ("persisted to database" if args.persist else "not persisted"))
        print(json.dumps(forecasts[:5], indent=2, default=str))
    return 0


def cmd_status(args):
    rows = []
    for table in ("dim_category", "dim_location", "listings", "price_history",
                  "events", "media_hashes", "sellers", "transactions",
                  "purchases", "fx_rates", "sold_observations",
                  "sweep_targets", "sweep_runs", "ingest_runs",
                  "predictive_models", "sale_predictions", "price_trajectories", "category_trends",
                  "category_trend_forecasts", "seller_archetypes", "market_concentration",
                  "relist_lineages"):
        try:
            n = db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        except Exception:
            n = "-"
        rows.append((table, n))
    width = max(len(t) for t, _ in rows)
    print(f"database: {config.SQLITE_PATH}")
    print(f"dialect:  {db.dialect()}")
    print()
    for table, n in rows:
        print(f"  {table:<{width}}  {n:>8}")

    from . import compliance
    comp = compliance.compliance_report()
    print()
    print(f"  collection readiness: {'OK' if comp['ready_for_collection'] else 'BLOCKED'}")
    for key in comp["blocking"]:
        print(f"    - {key}")
    return 0


def main(argv=None):
    from . import local_runtime
    delegated = local_runtime.delegate("toriresearch.cli", argv)
    if delegated is not None:
        return delegated
    from . import analysis as _analysis
    parser = argparse.ArgumentParser(prog="toriresearch")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    from . import auth
    p = sub.add_parser('users', help='Manage application login accounts in the owning runtime')
    p.add_argument('action', choices=('create', 'list', 'reset-password'))
    p.add_argument('--username')
    p.add_argument('--role', choices=('developer', 'viewer'), default='viewer')
    p.add_argument('--password-file', help='Private password file, never put passwords in command arguments')
    p.set_defaults(func=auth.cli)
    sub.add_parser("compliance").set_defaults(func=cmd_compliance)

    p = sub.add_parser("identity", help="canonical product pilot, local backfill and operator audit")
    p.add_argument("action", choices=("catalog", "report", "backfill", "audit-template", "audit-upgrade", "audit-preview", "audit-import"))
    p.add_argument("--purpose", default="development", choices=("development", "holdout"), help="freeze diagnostic or independent evaluation predictions")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", default="standard", choices=config.SUBPLATFORMS)
    p.add_argument("--family", help="catalog family for an audit template")
    p.add_argument("--limit", type=int, help="backfill rows or audit examples per family")
    p.add_argument("--after", type=int, default=0, help="resume backfill after this listing key")
    p.add_argument("--out", help="new local audit JSON file")
    p.add_argument("--input", help="completed audit JSON file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("listing-analysis",help="shared listing fields, queue diagnostics and independent semantic audits")
    p.add_argument("action",choices=("profiles","status","report","audit-template","audit-preview","audit-import"))
    p.add_argument("--platform",default="tori")
    p.add_argument("--purpose",default="development",choices=("development","holdout"))
    p.add_argument("--per-family",type=int,default=5)
    p.add_argument("--out",help="new blank audit JSON file")
    p.add_argument("--input",help="independently completed audit JSON file")
    p.set_defaults(func=cmd_listing_analysis)

    p = sub.add_parser("enrich")
    p.add_argument("--task", choices=("product_type","listing_analysis"),
                   default="listing_analysis" if config.LISTING_ANALYSIS_ENABLED else "product_type",
                   help="automatic source analysis or legacy title classification")
    p.add_argument("--platform", default="tori")
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--vocabulary", action="store_true", help="show the vocabulary")
    p.add_argument("--status", action="store_true", help="show classification freshness, recovery queue and measured throughput")
    p.add_argument("--llm-health", action="store_true")
    p.add_argument("--panel", action="store_true",
                   help="ask-price panel by derived product type")
    p.add_argument("--min-n", type=int, default=None)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback only; anything else is refused")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--open", action="store_true", help="open a browser")
    p.add_argument("--refresh", type=int, default=30, help="poll seconds")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("agent-bridge",
                       help="read-only evidence bridge for an external agent runtime")
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback only; anything else is refused")
    p.add_argument("--port", type=int, default=8790)
    p.set_defaults(func=cmd_agent_bridge)

    p = sub.add_parser("agent",
                       help="run a bounded domain agent over a frozen evidence bundle")
    p.add_argument("agent", choices=("read", "queue"),
                   help="read: answer the current bundle locally; queue: show claims awaiting an operator")
    p.add_argument("--limit", type=int, default=50, help="queue rows to show")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("dev")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--refresh", type=int, default=10)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_dev)
    sub.add_parser("coverage").set_defaults(func=cmd_coverage)
    sub.add_parser("economics").set_defaults(func=cmd_economics)
    sub.add_parser("retention").set_defaults(func=cmd_retention)
    p = sub.add_parser("aggregate", help="coarse daily history and local storage measurements")
    p.add_argument("--status", action="store_true", help="read history without reconciling partitions")
    p.set_defaults(func=cmd_aggregate)
    sub.add_parser("resume-collection").set_defaults(func=cmd_resume_collection)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("sweep")
    p.add_argument("--build", action="store_true",
                   help="Advance a resumable sitemap refresh within this run's budget")
    p.add_argument("--limit-locations", type=int, default=None)
    p.add_argument("--budget", type=int, default=None,
                   help="Search requests this run (default TORI_SWEEP_BUDGET)")
    p.add_argument("--pages", type=int, default=None, help="Maximum pages per traversal; resumes across runs")
    p.add_argument("--include-regions", action="store_true")
    p.add_argument("--deepest-first", action="store_true",
                   help="Prefer deep cells over shallow ones (default: national first)")
    p.add_argument("--dry-run", action="store_true", help="Show the queue, fetch nothing")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("itempage")
    p.add_argument("--budget", type=int, default=None,
                   help="Item pages this run (default TORI_ITEM_BUDGET)")
    p.add_argument("--all-listings", action="store_true",
                   help="Ignore staleness and re-probe everything")
    p.add_argument("--min-age-hours", type=float, default=0.0)
    p.set_defaults(func=cmd_itempage)

    p = sub.add_parser("sell")
    p.add_argument("--source-listing-id", required=True,
                   help="The purchase's source_listing_id")
    p.add_argument("--price", type=float, required=True, help="What it actually cleared for")
    p.add_argument("--sold-listing-id", help="Our resale listing id, if any")
    p.add_argument("--date", default=None, help="ISO date, defaults to now")
    p.add_argument("--platform", default="tori")
    p.add_argument("--outcome", default="sold_confirmed",
                   choices=["sold_confirmed", "sold_inferred", "unsold", "returned"])
    p.add_argument("--platform-fee", type=float, default=None)
    p.add_argument("--payment-fee", type=float, default=None)
    p.add_argument("--shipping", type=float, default=None)
    p.add_argument("--packaging", type=float, default=None)
    p.add_argument("--other-cost", type=float, default=None)
    p.add_argument("--tax", type=float, default=None)
    p.add_argument("--buyer-fee", type=float, default=None)
    p.add_argument("--shipping-method", default=None)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("schema")
    p.add_argument("--dialect", choices=["postgres", "sqlite"], default="postgres")
    p.add_argument("--out")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("taxonomy")
    p.add_argument("--limit-locations", type=int, default=None)
    p.set_defaults(func=cmd_taxonomy)

    sub.add_parser("atom").set_defaults(func=cmd_atom)

    p = sub.add_parser("ebay")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_ebay)

    p = sub.add_parser("permissions", help="inspect runtime source/action coverage")
    p.add_argument("--import-confirmation", action="store_true", help="migrate the existing private owner confirmation once")
    p.set_defaults(func=cmd_permissions)

    p = sub.add_parser("connections", help="inspect eBay seller connections or apply a local JSON action")
    p.add_argument("--environment", choices=["sandbox", "production"])
    p.add_argument("--request", help="local JSON action file; do not pass authorization codes as command arguments")
    p.set_defaults(func=cmd_connections)

    p = sub.add_parser("buy")
    p.add_argument("--title", required=True)
    p.add_argument("--cost", type=float, required=True)
    p.add_argument("--currency", default="EUR")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", default="standard")
    p.add_argument("--source-listing-id")
    p.add_argument("--category")
    p.add_argument("--brand")
    p.add_argument("--model")
    p.add_argument("--condition", dest="condition")
    p.add_argument("--date", default=None, help="ISO date, defaults to now")
    p.add_argument("--shipping", type=float, default=None)
    p.add_argument("--notes")
    p.add_argument("--buyer-fee", type=float, default=None)
    p.set_defaults(func=cmd_buy)

    sub.add_parser("buys").set_defaults(func=cmd_buys)

    p = sub.add_parser("inventory", help="owned SKUs, inspections and eligibility previews")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--sku", help="read one unit's facts, inspections and history")
    mode.add_argument("--request", help="apply a JSON action from a local file")
    p.add_argument("--search", default="")
    p.add_argument("--state", default="", choices=["", "in_stock", "quarantined", "retired"])
    p.add_argument("--page", type=int, default=1)
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("drafts", help="Tori listing preparation, review and local publishing queue")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--draft-id", help="read a draft, its reviews and queue history")
    mode.add_argument("--request", help="apply a local JSON action (no platform writes)")
    p.add_argument("--sku", default="", help="filter drafts by owned SKU")
    p.add_argument("--page", type=int, default=1)
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("opportunities", help="canonical product board with separate observed and scenario ranks")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", default="standard")
    p.add_argument("--category")
    p.add_argument("--municipality")
    p.add_argument("--condition")
    p.add_argument("--seller-type")
    p.add_argument("--days", default="30")
    p.add_argument("--horizon", default="30")
    p.add_argument("--limit", default="100")
    p.add_argument("--scenarios", help="complete row scenarios bound to displayed evidence revisions, JSON file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_opportunities)

    p = sub.add_parser("outcomes", help="owned acquisition attempts, work, costs and reconciled cash recovery")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--request", help="local JSON evidence event")
    mode.add_argument("--purchase-pk", type=int)
    p.add_argument("--page", type=int, default=1)
    p.set_defaults(func=cmd_outcomes)

    p = sub.add_parser("trades", help="manual SKU sales, return cases and observed net economics")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--order-id")
    mode.add_argument("--request", help="local JSON event; no external actions")
    p.add_argument("--sku", default="")
    p.add_argument("--as-of", help="history known by this ISO timestamp or Helsinki calendar day")
    p.add_argument("--page", type=int, default=1)
    p.set_defaults(func=cmd_trades)

    p = sub.add_parser("workflow", help="read-only review of owned evidence and Tori workflow gaps")
    p.add_argument("--sku", default="", help="filter the unit table; summary covers all owned stock")
    p.add_argument("--page", type=int, default=1)
    p.set_defaults(func=cmd_workflow)

    p = sub.add_parser("askpanel")
    p.add_argument("--municipality", required=True)
    p.add_argument("--location-id")
    p.add_argument("--category")
    p.add_argument("--pages", type=int, default=1)
    p.set_defaults(func=cmd_askpanel)

    p = sub.add_parser("panel")
    p.add_argument("--category")
    p.add_argument("--region")
    p.add_argument("--municipality")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", choices=config.SUBPLATFORMS, default="standard")
    p.add_argument("--condition")
    p.add_argument("--seller-type", choices=["private", "business", "professional", "unknown"])
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("liquidity")
    p.add_argument("--horizon", type=int, default=30)
    p.add_argument("--category")
    p.add_argument("--region")
    p.add_argument("--municipality")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", choices=config.SUBPLATFORMS, default="standard")
    p.add_argument("--condition")
    p.add_argument("--seller-type", choices=["private", "business", "professional", "unknown"])
    p.set_defaults(func=cmd_liquidity)

    p = sub.add_parser("analysis")
    p.add_argument("--dimension", default="category_root_norm",
                   choices=sorted(_analysis.DIMENSIONS),
                   help="group the market by this field; product_type is model-derived")
    p.add_argument("--days", type=int, default=30, choices=[7, 30, 90])
    p.add_argument("--horizon", type=int, default=30, choices=[7, 14, 30, 60, 90])
    p.add_argument("--category")
    p.add_argument("--municipality")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", choices=config.SUBPLATFORMS, default="standard")
    p.add_argument("--condition")
    p.add_argument("--seller-type", choices=["private", "business", "professional", "unknown"])
    p.add_argument("--json", action="store_true",
                   help="emit the full document instead of a readable brief")
    p.set_defaults(func=cmd_analysis)

    p = sub.add_parser("focus")
    p.add_argument("--horizon", type=int, default=30, choices=[7, 14, 30, 60, 90])
    p.add_argument("--municipality")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", choices=config.SUBPLATFORMS, default="standard")
    p.add_argument("--json", action="store_true",
                   help="emit the full document instead of a readable brief")
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("repair-evidence", help="audit legacy unlabeled expiry guesses")
    p.add_argument("--apply", action="store_true", help="append corrections; default is a read preview")
    p.set_defaults(func=cmd_repair_evidence)

    p = sub.add_parser("predict", help="train and apply predictive models")
    p.add_argument("--model", default="sale_probability",
                   choices=["sale_probability", "price_trajectory",
                            "category_trends", "seller_archetypes",
                            "market_concentration"],
                   help="which model to run")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--train", action="store_true", help="train or compute the model")
    action.add_argument("--predict", action="store_true", help="generate predictions or market summaries")
    action.add_argument("--evaluate", action="store_true", help="chronological validation or latest stored summary")
    action.add_argument("--readiness", action="store_true", help="inspect price-trajectory evidence without fitting")
    action.add_argument("--validate", action="store_true",
                        help="rehearse all five model families against an isolated copy of the source database")
    from .predict._common import HORIZONS
    p.add_argument("--horizon", type=int, choices=list(HORIZONS),
                   help="forecast horizon (default: all supported horizons); evaluation default: 30 days")
    p.add_argument("--listing-pks", help="comma-separated listing PKs to predict (default: all active)")
    p.add_argument("--persist", action="store_true", help="save predictions to database")
    p.add_argument("--experimental", action="store_true", help="inspect the latest candidate; output retains acceptance flags")
    p.add_argument("--months-back", type=int,
                   help="history window: trends default 104 weeks; concentration default 6 calendar months")
    p.add_argument("--region", help="stored region label for trend/concentration summaries")
    p.add_argument("--window-days", type=int, default=180, help="seller behavior observation window")
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser("soldextract")
    p.add_argument("action", choices=["template", "import", "summary", "ratio"])
    p.add_argument("file", nargs="?")
    p.add_argument("--keyword")
    p.add_argument("--min-obs", type=int, default=3)
    p.add_argument("--captured-by", default="operator")
    p.set_defaults(func=cmd_soldextract)

    p = sub.add_parser("realization")
    p.add_argument("action", choices=["ratios", "resale-ratios", "estimate", "decide"])
    p.add_argument("--category", help="normalized category filter for the estimate")
    p.add_argument("--condition", help="normalized condition filter for the estimate")
    p.add_argument("--platform", default="tori")
    p.add_argument("--subplatform", default="standard", choices=config.SUBPLATFORMS)
    p.add_argument("--brand", help="recorded brand filter")
    p.add_argument("--model", help="recorded model filter; variants still require inspection")
    p.add_argument("--min-n", type=int, default=config.MIN_N_FOR_REPORT,
                   help=f"minimum observations (default: {config.MIN_N_FOR_REPORT})")
    p.add_argument("--json", action="store_true", help="emit the full JSON estimate")
    p.add_argument("--cost", type=float, help="acquisition cost in EUR (decide)")
    p.add_argument("--expect", type=float, help="expected realized price in EUR (decide)")
    p.add_argument("--interval", help="comma-separated downside,upside of the estimate (decide)")
    p.add_argument("--selling-costs", type=float,
                   help="explicit total of ALL non-acquisition costs in EUR; omitted means unknown (decide)")
    p.add_argument("--min-margin-pct", type=float, default=0.15,
                   help="minimum acceptable downside margin (decide)")
    p.set_defaults(func=cmd_realization)

    p = sub.add_parser("scenario", help="complete cost and optional monthly operating-capacity assumptions")
    p.add_argument("--input", required=True, help="JSON file containing explicit scenario inputs")
    p.set_defaults(func=cmd_scenario)

    sub.add_parser("report").set_defaults(func=cmd_report)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("market_intel").set_defaults(func=cmd_market_intel)
    internet = sub.add_parser("internet_research", help="Public source evidence, local review and registered URL watches")
    internet.set_defaults(func=cmd_internet_research)
    research_actions = internet.add_subparsers(dest="research_action", required=True)
    research_status = research_actions.add_parser("status")
    research_status.add_argument("--listing-pk", type=int)
    research_add = research_actions.add_parser("add")
    research_add.add_argument("listing_pk", type=int)
    research_add.add_argument("url")
    research_add.add_argument("--kind", choices=("asking_price","product_specs","fee_terms","research"), default="research")
    research_add.add_argument("--label", default="")
    research_add.add_argument("--interval-hours", type=int, default=24)
    research_add.add_argument("--watch", action="store_true")
    research_add.add_argument("--review-updates", action="store_true")
    research_run = research_actions.add_parser("run")
    research_run.add_argument("listing_pk", type=int)
    research_run.add_argument("--operation", choices=("refresh","review","refresh_review"), default="refresh_review")
    research_actions.add_parser("tick")
    research_watch = research_actions.add_parser("watch")
    research_watch.add_argument("listing_pk", type=int)
    research_watch.add_argument("source_id")
    research_watch.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    research_remove = research_actions.add_parser("remove")
    research_remove.add_argument("listing_pk", type=int)
    research_remove.add_argument("source_id")

    args = parser.parse_args(argv)
    from . import collection_events, collection_control
    collection_events.install_logging()
    try:
        if args.command in ('atom','sweep','itempage','taxonomy','askpanel','category') and not getattr(args,'dry_run',False):
            from . import continuous
            try:
                with continuous.source_owner():
                    return args.func(args)
            except continuous.CollectorBusy:
                print(json.dumps({'status':'skipped','reason':'A collection owner is already active'}))
                return 75
        return args.func(args)
    except collection_control.CollectionHalted as exc:
        print(json.dumps({'status': 'paused', 'reason': exc.reason}))
        return 76 if exc.reason == 'operator_cancel' else 75
    except AccessBlocked as exc:
        # One place, so no command can exit on a raw traceback when the platform
        # has refused us. The commands that manage their own budgets catch this
        # themselves and report `status: blocked`; this covers the one-shot
        # commands (atom, taxonomy, askpanel, sweep --build) that have no loop
        # to stop.
        print(f"BLOCKED: {exc}", file=sys.stderr)
        print("Nothing was retried and no observation was guessed at.", file=sys.stderr)
        print("A refusal is not something to work around: defeating an access "
              "control is the line this project does not cross (PROJECT_PLAN.md, "
              "ryzlain 35 luku). Either fix the credentials, or send the Tier 0 "
              "licensing email and let the data come from an agreed channel.",
              file=sys.stderr)
        return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
