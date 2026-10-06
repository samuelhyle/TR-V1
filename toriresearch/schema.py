"""Single source of truth for the data model.

Renders the same table definitions to both PostgreSQL (production target) and
SQLite (local dev), so the schema exercised locally cannot drift from the one
deployed.

Corrections applied relative to secondhand.pdf are numbered C1..C10 and
labelled inline on the relevant column or table.
"""


_MODIFIER_WORDS = (
    "PRIMARY KEY", "NOT NULL", "UNIQUE", "DEFAULT", "REFERENCES",
    "AUTOINCREMENT", "CHECK",
)


def _validate_tables():
    """Catch declaration/note mixups at import time rather than in generated DDL."""
    for table in TABLES:
        names = set()
        for col, decl, note in table["columns"]:
            if not col or not decl:
                raise AssertionError(f"{table['name']}: column needs name and declaration")
            if col in names:
                raise AssertionError(f"{table['name']}: duplicate column {col}")
            names.add(col)
            if note and any(note.strip().upper().startswith(w) for w in _MODIFIER_WORDS):
                raise AssertionError(
                    f"{table['name']}.{col}: modifier {note!r} in note slot, must be in declaration"
                )
            # db.init_db splits the DDL on ';'. A semicolon inside a comment ends
            # the statement early and the remainder parses as broken SQL, so the
            # failure surfaces at init rather than here. Catch it here instead.
            if note and ";" in note:
                raise AssertionError(f"{table['name']}.{col}: ';' in note breaks DDL splitting")
            if table.get("comment") and ";" in table["comment"]:
                raise AssertionError(f"{table['name']}: ';' in table comment breaks DDL splitting")
        for uname, columns in table["uniques"]:
            missing = [c for c in columns if c not in names]
            if missing:
                raise AssertionError(f"{table['name']} {uname}: unknown columns {missing}")
        for cname, column, values in table.get("checks", []):
            if column not in names:
                raise AssertionError(f"{table['name']} {cname}: unknown column {column}")
            if not values:
                raise AssertionError(f"{table['name']} {cname}: empty check list")

    _validate_indexes()


def _validate_indexes():
    """An index naming a table or column that does not exist is caught here.

    These used to be bare strings inside `db.init_db`, where a typo surfaces as
    a database error partway through initialisation and names the wrong thing.
    Declaring them beside the tables they cover makes the whole set greppable
    against its schema and checkable at import time.
    """
    declared = {table["name"]: {c for c, _d, _n in table["columns"]} for table in TABLES}
    seen = set()
    for iname, itable, unique, columns in INDEXES:
        if iname in seen:
            raise AssertionError(f"duplicate index name {iname}")
        seen.add(iname)
        if itable not in declared:
            raise AssertionError(f"{iname}: unknown table {itable}")
        missing = [c for c in columns if c not in declared[itable]]
        if missing:
            raise AssertionError(f"{iname}: unknown columns {missing}")
        if not columns:
            raise AssertionError(f"{iname}: index needs at least one column")


def _validate_migrations():
    """A migration list that skips a version is one that will be applied twice.

    Versions must be a dense ascending run from 1. A gap means an entry was
    deleted or renumbered after a database had already applied it, and the
    running database would then be permanently behind the declared shape with
    nothing able to explain the difference.
    """
    for expected, (version, name, _fn) in enumerate(MIGRATIONS, start=1):
        if version != expected:
            raise AssertionError(
                f"MIGRATIONS must be a dense run from 1: expected version {expected}, "
                f"found {version} ({name}). Renumbering an applied migration leaves "
                f"existing databases unable to reach the declared shape."
            )
    if MIGRATIONS and MIGRATIONS[-1][0] != SCHEMA_VERSION:
        raise AssertionError(
            f"SCHEMA_VERSION is {SCHEMA_VERSION} but the last migration is "
            f"{MIGRATIONS[-1][0]}; bump SCHEMA_VERSION when adding a migration"
        )


ENUM_CONDITION = [
    "new_unused", "like_new", "very_good", "good", "fair", "poor",
    "for_parts", "refurbished", "unknown",
]
ENUM_STATUS = ["active", "inactive", "withdrawn", "expired", "sold", "unknown"]
ENUM_OUTCOME = [
    "sold_confirmed", "sold_inferred", "withdrawn", "expired", "active", "unknown",
]
ENUM_SELLER_TYPE = ["private", "professional", "business", "unknown"]
ENUM_MEDIA_KIND = ["image", "video"]
ENUM_EVENT = [
    "first_seen", "published", "price_changed", "status_changed", "relisted",
    "promotion_started", "promotion_ended", "sold_confirmed", "withdrawn",
    "expired", "view_snapshot", "watcher_snapshot", "message_snapshot",
    "description_changed", "attribute_changed", "media_changed",
]
ENUM_COVERAGE = ["collecting", "gone_uncertain"]
ENUM_MISSING_REASON = [
    "not_exposed", "not_observed", "not_applicable", "ambiguous",
    "parse_failed", "redacted", "unknown",
]

TABLES = [{'name': 'schema_meta',
  'comment': 'Applied schema version. One row per key. The collector refuses to run against a database it '
             'cannot prove is at the declared shape.',
  'columns': [('meta_key', 'TEXT PRIMARY KEY', None),
              ('meta_value', 'TEXT NOT NULL', None),
              ('applied_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', 'UTC time this key was last written')],
  'uniques': []},
 {'name': 'collection_cycles',
  'comment': 'Durable schedule slots: policy changes cannot duplicate a collector slot.',
  'columns': [('run_id', 'TEXT PRIMARY KEY', None),
              ('collector_key', 'TEXT NOT NULL', None),
              ('schedule_slot', 'TIMESTAMPTZ NOT NULL', None),
              ('policy_key', 'TEXT NOT NULL', None),
              ('origin', 'TEXT NOT NULL', None),
              ('started_at', 'TIMESTAMPTZ NOT NULL', None),
              ('finished_at', 'TIMESTAMPTZ', None),
              ('heartbeat_at', 'TIMESTAMPTZ NOT NULL', None),
              ('deadline_at', 'TIMESTAMPTZ NOT NULL', None),
              ('status', 'TEXT NOT NULL', None),
              ('record_json', 'TEXT NOT NULL', 'Bounded aggregate evidence, no listing text or credentials')],
  'uniques': [('uq_collection_slot', ['collector_key', 'schedule_slot'])]},
 {'name': 'classification_repair',
  'comment': 'Resumable bounded reconciliation by classification identity.',
  'columns': [('platform', 'TEXT NOT NULL', None),
              ('task', 'TEXT NOT NULL', None),
              ('identity_key', 'TEXT NOT NULL', None),
              ('cursor_pk', 'BIGINT NOT NULL DEFAULT 0', None),
              ('completed_at', 'TIMESTAMPTZ', None)],
  'uniques': [('uq_classification_repair', ['platform', 'task'])]},
 {'name': 'aggregate_partitions',
  'comment': 'Versioned coarse research history with no listing or seller references. Frozen partitions survive row '
             'retention.',
  'columns': [('kind', 'TEXT NOT NULL', None),
              ('period_day', 'TEXT NOT NULL', 'UTC snapshot day or admission-cohort day'),
              ('scope_key', 'TEXT NOT NULL', 'Configuration fingerprint, never a listing fingerprint'),
              ('policy_version', 'TEXT NOT NULL', None),
              ('taxonomy_version', 'TEXT NOT NULL', None),
              ('state', 'TEXT NOT NULL', None),
              ('captured_at', 'TIMESTAMPTZ NOT NULL', None),
              ('freeze_cutoff', 'TIMESTAMPTZ', None),
              ('source_watermark_json',
               'TEXT NOT NULL',
               'Operational counters and timestamps, omitted from history exports'),
              ('aggregates_json', 'TEXT NOT NULL', 'Closed dimensions, rounded counts and coarse asking-price bands')],
  'uniques': [('uq_aggregate_partition', ['kind', 'period_day', 'scope_key', 'policy_version', 'taxonomy_version'])],
  'checks': [('ck_aggregate_kind', 'kind', ['market', 'cohort']), ('ck_aggregate_state', 'state', ['open', 'frozen'])]},
 {'name': 'storage_checks',
  'comment': 'Daily local database latency and lock samples. No source identities or database paths.',
  'columns': [('day', 'TEXT PRIMARY KEY', None),
              ('checks', 'INTEGER NOT NULL', None),
              ('busy_checks', 'INTEGER NOT NULL', None),
              ('read_ms_sum', 'REAL NOT NULL', None),
              ('read_ms_max', 'REAL NOT NULL', None),
              ('lock_ms_sum', 'REAL NOT NULL', None),
              ('lock_ms_max', 'REAL NOT NULL', None),
              ('database_bytes', 'BIGINT', None),
              ('wal_bytes', 'BIGINT', None)],
  'uniques': []},
 {'name': 'dim_category',
  'comment': "Platform taxonomy mapped to a canonical, versioned analysis category. Tori's sitemap enumerates category "
             'ids with no labels, and only the top level carries a label on search pages, so deeper labels are learned '
             'from item pages. An id that has not been resolved stays unresolved rather than being presented as if it '
             'were a name.',
  'columns': [('category_version_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('platform', 'TEXT NOT NULL', None),
              ('platform_category_id', 'TEXT NOT NULL', None),
              ('platform_category_path', "TEXT NOT NULL DEFAULT ''", None),
              ('taxonomy_version', 'TEXT NOT NULL', None),
              ('platform_label', 'TEXT', "The platform's own label, once observed"),
              ('platform_label_depth', 'INTEGER', None),
              ('label_source', 'TEXT', 'facet_tree or item_page'),
              ('first_seen_at', 'TIMESTAMPTZ', None),
              ('canonical_category_id', 'TEXT', None),
              ('mapping_version', 'TEXT', None),
              ('mapping_confidence', 'REAL', None),
              ('valid_from', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('valid_to', 'TIMESTAMPTZ', None)],
  'uniques': [('uq_dim_category', ['platform', 'platform_category_id', 'taxonomy_version'])]},
 {'name': 'dim_product',
  'comment': 'Canonical product dimension: brand, model, category normalization with versioning. Separates observed '
             'raw values from normalized, analysis-ready values. Per PROJECT_PLAN.md §4 and secondhand.pdf §21.',
  'columns': [('product_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('canonical_brand_id', 'TEXT', 'Internal stable brand key'),
              ('brand_raw', 'TEXT', 'Original brand string from listing'),
              ('brand_norm', 'TEXT', "Normalized brand (e.g., 'Marimekko')"),
              ('canonical_model_id', 'TEXT', 'Internal stable model key'),
              ('model_raw', 'TEXT', 'Original model string from listing'),
              ('model_norm', 'TEXT', 'Normalized model preserving variants (storage, generation, etc.)'),
              ('canonical_category_id', 'TEXT', 'Internal stable category key'),
              ('category_raw_id', 'TEXT', "Platform's original category path"),
              ('category_norm', 'TEXT', 'Normalized category label chain'),
              ('mapping_version', "TEXT NOT NULL DEFAULT 'map-2026-10-01'", 'Version of normalization rules applied'),
              ('mapping_confidence', 'REAL', 'Confidence in brand/model/category normalization'),
              ('valid_from', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('valid_to', 'TIMESTAMPTZ', None)],
  'uniques': [('uq_dim_product', ['canonical_brand_id', 'canonical_model_id', 'canonical_category_id', 'valid_from'])]},
 {'name': 'platform_enums',
  'comment': "The platform's own enumerated values and how often sellers use them, captured from the facet tree "
             'already present in every search page. condition, trade_type and dealer_segment are published '
             'distinctions that identify nobody, and they are what separates a giveaway from a sale and a private '
             'seller from a business.',
  'columns': [('enum_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('platform', 'TEXT NOT NULL', None),
              ('facet', 'TEXT NOT NULL', None),
              ('value', 'TEXT NOT NULL', None),
              ('label', 'TEXT', "The platform's own label, untranslated"),
              ('hits', 'INTEGER', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_platform_enum', ['platform', 'facet', 'value', 'observed_at'])]},
 {'name': 'dim_location',
  'comment': 'Geography. Postal code is deliberately absent (see PROJECT_PLAN.md 3, mitigation 1).',
  'columns': [('location_pk', 'BIGINT PRIMARY KEY', None),
              ('platform', "TEXT NOT NULL DEFAULT 'tori'", None),
              ('municipality_norm', 'TEXT NOT NULL', None),
              ('region_norm', "TEXT NOT NULL DEFAULT ''", 'C-NULL: no NULL in a UNIQUE key'),
              ('country_code', "TEXT NOT NULL DEFAULT 'FI'", None)],
  'uniques': [('uq_dim_location', ['platform', 'municipality_norm', 'region_norm'])]},
 {'name': 'dim_date',
  'comment': '',
  'columns': [('d', 'TEXT PRIMARY KEY', None),
              ('year', 'INTEGER', None),
              ('quarter', 'INTEGER', None),
              ('month', 'INTEGER', None),
              ('iso_week', 'INTEGER', None),
              ('day_of_week', 'INTEGER', None)],
  'uniques': []},
 {'name': 'sellers',
  'comment': 'C6 SCD-2. listings.seller_id is resolved through seller_version_id. Keys are pseudonymous (see '
             'listings.seller_id): no platform owner identifier is held in this table.',
  'columns': [('seller_version_id', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('platform', 'TEXT NOT NULL', None),
              ('seller_id', 'TEXT NOT NULL', None),
              ('valid_from', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('valid_to', 'TIMESTAMPTZ', None),
              ('seller_type_raw', 'TEXT', None),
              ('seller_type_norm', 'TEXT', 'C1'),
              ('municipality_norm', 'TEXT', None),
              ('region_norm', 'TEXT', None),
              ('rating', 'NUMERIC(8,3)', None),
              ('rating_count', 'INTEGER', None),
              ('active_listing_count', 'INTEGER', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_sellers', ['platform', 'seller_id', 'valid_from'])],
  'checks': [('ck_sellers_type', 'seller_type_norm', ['private', 'professional', 'business', 'unknown'])]},
 {'name': 'fx_rates',
  'comment': 'Provenance for every conversion. ECB rates are informational only.',
  'columns': [('fx_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('rate_date', 'DATE NOT NULL', None),
              ('currency_from', 'TEXT NOT NULL', None),
              ('rate_to_eur', 'NUMERIC(18,8) NOT NULL', None),
              ('fx_source', "TEXT NOT NULL DEFAULT 'ECB'", None),
              ('method_version', 'TEXT', None)],
  'uniques': [('uq_fx', ['rate_date', 'currency_from', 'fx_source'])]},
 {'name': 'sold_observations',
  'comment': 'Human-captured aggregates from official seller-side sold-data views. This is the only sanctioned route '
             'to realized prices: eBay Product Research exposes 3 years of sold aggregates including actual Best Offer '
             'prices, free, with no API. Bounded keyword list, weekly capture.',
  'columns': [('sold_obs_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('captured_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('source', 'TEXT NOT NULL', None),
              ('platform', 'TEXT NOT NULL', None),
              ('keyword', 'TEXT NOT NULL', None),
              ('category_norm', 'TEXT', None),
              ('condition_norm', 'TEXT', 'C1'),
              ('query_url', 'TEXT', None),
              ('window_start', 'DATE', None),
              ('window_end', 'DATE', None),
              ('n_sold', 'INTEGER', None),
              ('avg_sold_price_eur', 'NUMERIC(14,2)', None),
              ('sold_price_min_eur', 'NUMERIC(14,2)', None),
              ('sold_price_max_eur', 'NUMERIC(14,2)', None),
              ('sell_through_rate', 'NUMERIC(6,4)', None),
              ('avg_shipping_eur', 'NUMERIC(14,2)', None),
              ('captured_by', 'TEXT', None),
              ('notes', 'TEXT', None)],
  'uniques': [('uq_sold_obs', ['platform', 'keyword', 'captured_at'])],
  'checks': [('ck_sold_obs_condition',
              'condition_norm',
              ['new_unused', 'like_new', 'very_good', 'good', 'fair', 'poor', 'for_parts', 'refurbished', 'unknown'])]},
 {'name': 'sweep_targets',
  'comment': 'Published municipality/category search cells and synthetic parents. Scoped fair rotation and durable '
             'page checkpoints use a bounded budget. coverage reports disclose partial traversals and infeasible '
             'revisit targets.',
  'columns': [('target_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('platform', 'TEXT NOT NULL', None),
              ('location_id', 'TEXT NOT NULL', "Tori's own opaque location identifier"),
              ('location_label', 'TEXT', 'Municipality label Tori publishes'),
              ('location_grain', 'TEXT', "'municipality', 'region' or 'national'"),
              ('category_path', 'TEXT NOT NULL', 'Tori category id, e.g. 2.78.5222.240'),
              ('category_depth', 'INTEGER', None),
              ('search_url', 'TEXT NOT NULL', None),
              ('first_crawled_at', 'TIMESTAMPTZ', None),
              ('last_crawled_at', 'TIMESTAMPTZ', 'Latest completed bounded traversal, including depth caps'),
              ('crawl_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('last_listing_count', 'INTEGER', None),
              ('last_status', 'TEXT', None),
              ('last_attempt_at', 'TIMESTAMPTZ', None),
              ('last_page1_at', 'TIMESTAMPTZ', 'First-page freshness is independent of traversal completion'),
              ('next_due_at', 'TIMESTAMPTZ', None),
              ('next_page', 'INTEGER NOT NULL DEFAULT 1', None),
              ('traversal_id', 'TEXT', None),
              ('traversal_started_at', 'TIMESTAMPTZ', None),
              ('traversal_depth', 'INTEGER', None),
              ('traversal_pages', 'INTEGER NOT NULL DEFAULT 0', None),
              ('traversal_seen', 'INTEGER NOT NULL DEFAULT 0', 'Distinct listing identities in the current traversal'),
              ('traversal_new', 'INTEGER NOT NULL DEFAULT 0', None),
              ('last_page_new', 'INTEGER', None),
              ('last_page_seen', 'INTEGER', None),
              ('coverage_state',
               "TEXT NOT NULL DEFAULT 'unmeasured'",
               'partial, capped or observed_empty_end never promise a complete moving inventory'),
              ('error_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('empty_streak', 'INTEGER NOT NULL DEFAULT 0', None),
              ('http404_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('grid_origin', 'TEXT', None),
              ('grid_version', 'TEXT', None),
              ('missing_snapshots', 'INTEGER NOT NULL DEFAULT 0', None),
              ('active', 'BOOLEAN NOT NULL DEFAULT TRUE', 'False when Tori no longer serves the page')],
  'uniques': [('uq_sweep_target', ['platform', 'location_id', 'category_path'])]},
 {'name': 'discovery_state',
  'comment': 'Durable scheduler phase and weekly sitemap frontier. Contains taxonomy metadata only.',
  'columns': [('platform', 'TEXT PRIMARY KEY', None),
              ('dispatch_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('refresh_id', 'TEXT', None),
              ('refresh_started_at', 'TIMESTAMPTZ', None),
              ('refresh_location_limit', 'INTEGER', None),
              ('last_refresh_at', 'TIMESTAMPTZ', None),
              ('last_refresh_version', 'TEXT', None),
              ('frontier_json', 'TEXT', None),
              ('visited_json', 'TEXT', None),
              ('retry_at', 'TIMESTAMPTZ', None),
              ('last_error_class', 'TEXT', None)],
  'uniques': []},
 {'name': 'sitemap_cells',
  'comment': 'Staging keys for one complete sitemap reconciliation, no listing payloads.',
  'columns': [('refresh_id', 'TEXT NOT NULL', None),
              ('location_id', 'TEXT NOT NULL', None),
              ('category_path', 'TEXT NOT NULL', None)],
  'uniques': [('uq_sitemap_cell', ['refresh_id', 'location_id', 'category_path'])]},
 {'name': 'parser_checks',
  'comment': 'Short-lived daily structural diagnostics. Contains counts only, no listing or seller identifiers, URLs '
             'or page bodies.',
  'columns': [('day', 'TEXT NOT NULL', 'UTC calendar date'),
              ('platform', 'TEXT NOT NULL', None),
              ('source', 'TEXT NOT NULL', None),
              ('parser', 'TEXT NOT NULL', None),
              ('result', 'TEXT NOT NULL', None),
              ('checks', 'INTEGER NOT NULL DEFAULT 0', None),
              ('raw_rows', 'INTEGER NOT NULL DEFAULT 0', None),
              ('parsed_rows', 'INTEGER NOT NULL DEFAULT 0', None),
              ('missing_id', 'INTEGER NOT NULL DEFAULT 0', None),
              ('missing_title', 'INTEGER NOT NULL DEFAULT 0', None),
              ('missing_price', 'INTEGER NOT NULL DEFAULT 0', None),
              ('missing_category', 'INTEGER NOT NULL DEFAULT 0', None)],
  'uniques': [('uq_parser_check', ['day', 'platform', 'source', 'parser', 'result'])]},
 {'name': 'action_log',
  'comment': 'Actions triggered from the dashboard. Accountability for Article 5(2) covers operator-initiated '
             'collection, not just scheduled collection.',
  'columns': [('action_log_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('started_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('action', 'TEXT NOT NULL', None),
              ('returncode', 'INTEGER', None),
              ('seconds', 'REAL', None),
              ('stdout_tail', 'TEXT', 'Bounded command output for the local operator'),
              ('stderr_tail', 'TEXT', 'Bounded failure details for the local operator'),
              ('client_token', 'TEXT', 'Idempotency key for a retried operator action, NULL for scheduled runs'),
              ('request_sha256', 'TEXT', 'Fingerprint of the submitted request, so a reused token cannot change it')],
  'uniques': [('uq_action_log_token', ['client_token'])]},
 {'name': 'sweep_runs',
  'comment': 'One row per sweep invocation: what was requested, what it cost, what it found.',
  'columns': [('sweep_run_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('started_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('finished_at', 'TIMESTAMPTZ', None),
              ('budget', 'INTEGER', None),
              ('requests_made', 'INTEGER', None),
              ('listings_seen', 'INTEGER', None),
              ('listings_new', 'INTEGER', None),
              ('targets_crawled', 'INTEGER', None),
              ('targets_remaining_stale', 'INTEGER', None),
              ('throttled_429', 'INTEGER', "Non-zero means we are above the platform's tolerance"),
              ('blocked_403',
               'INTEGER',
               'Non-zero means the platform refused us. Never retried, needs a human and Tier 0 licensing'),
              ('status', 'TEXT', None),
              ('error', 'TEXT', None)],
  'uniques': []},
 {'name': 'ingest_runs',
  'comment': 'Every collection run is recorded for Article 5(2) accountability.',
  'columns': [('run_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('started_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('finished_at', 'TIMESTAMPTZ', None),
              ('source', 'TEXT NOT NULL', None),
              ('endpoint', 'TEXT', None),
              ('status', 'TEXT', None),
              ('rows_ingested', 'INTEGER', None),
              ('requests_made', 'INTEGER', None),
              ('error', 'TEXT', None)],
  'uniques': []},
 {'name': 'research_segments',
  'comment': 'Named analyst cohorts saved locally. These filters never trigger collection.',
  'columns': [('segment_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('name', 'TEXT NOT NULL', None),
              ('filters_json', 'TEXT NOT NULL', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_research_segment_name', ['name'])]},
 {'name': 'research_sessions',
   'comment': "Saved price-research desk state: the filter slice, the listings chosen from it, the operator's notes "
              'and their own cost assumptions. Operator notes only -- it asserts no marketplace outcome, records no '
              'asking price of its own, and never changes collection scope.',
   'columns': [('session_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
               ('name', 'TEXT NOT NULL', None),
               ('filters_json', 'TEXT NOT NULL', 'The market slice the shortlist was read from'),
               ('selected_pks_json', "TEXT NOT NULL DEFAULT '[]'",
                'Listing keys chosen at save time. Listing retention may remove them later, but the note survives'),
               ('notes', "TEXT NOT NULL DEFAULT ''", 'Operator notes. Not observed listing attributes'),
               ('scenarios_json', "TEXT NOT NULL DEFAULT '{}'",
                'User cost assumptions keyed by listing_pk, unevaluated. Margin arithmetic is calculated server-side'),
               ('created_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
               ('updated_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
   'uniques': [('uq_research_session_name', ['name'])]},
  {'name': 'analyst_runs',
  'comment': 'Frozen analyst evidence and validated local model selections. Independent of observations and trades.',
  'columns': [('run_id', 'TEXT PRIMARY KEY', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None),
              ('completed_at', 'TIMESTAMPTZ', None),
              ('status', 'TEXT NOT NULL', None),
              ('slot_key', 'TEXT', 'A unique local inference slot, released after work ends'),
              ('owner_pid', 'INTEGER', None),
              ('options_json', 'TEXT NOT NULL', None),
              ('model_json', 'TEXT NOT NULL', None),
              ('evidence_json', 'TEXT', None),
              ('evidence_sha256', 'TEXT', None),
              ('input_sha256', 'TEXT', None),
              ('output_json', 'TEXT', None),
              ('validation_json', 'TEXT', None),
              ('error', 'TEXT', None),
              ('latency_ms', 'INTEGER', None),
              ('cached_from_run_id', 'TEXT', None)],
  'uniques': [('uq_analyst_slot', ['slot_key'])],
  'checks': [('ck_analyst_status',
              'status',
              ['queued', 'building', 'running', 'complete', 'unavailable', 'invalid', 'error', 'cancelled'])]},
 {'name': 'predictive_models',
  'comment': 'One immutable registry row per fit, with feature/method versions, training period, diagnostics and '
             'artifact provenance.',
  'columns': [('model_version', 'TEXT PRIMARY KEY', None),
              ('model_family', 'TEXT NOT NULL', None),
              ('feature_version', 'TEXT NOT NULL', None),
              ('trained_at', 'TIMESTAMPTZ NOT NULL', None),
              ('train_start_at', 'TIMESTAMPTZ', None),
              ('train_end_at', 'TIMESTAMPTZ', None),
              ('n_observations', 'INTEGER NOT NULL', None),
              ('n_entities', 'INTEGER NOT NULL', None),
              ('metrics_json', 'TEXT NOT NULL', None),
              ('method_json', 'TEXT NOT NULL', None),
              ('artifact_path', 'TEXT NOT NULL', 'Artifact basename in configured data/models')],
  'uniques': []},
 {'name': 'category_trends',
  'comment': 'Weekly category trend state with changepoint detection and seasonality index.',
  'columns': [('trend_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('category_norm', 'TEXT NOT NULL', None),
              ('region_norm', 'TEXT', 'Empty string = national aggregate, legacy NULL accepted'),
              ('week_start', 'DATE NOT NULL', None),
              ('arrivals', 'INTEGER NOT NULL', 'New listings observed in week'),
              ('median_ask_eur', 'NUMERIC(14,2)', None),
              ('trend_state', 'TEXT NOT NULL', 'emerging|growing|mature|declining|volatile'),
              ('changepoint_prob',
               'NUMERIC(6,4)',
               'Recency/strength score, not a calibrated break probability, NULL = insufficient history'),
              ('seasonality_idx', 'NUMERIC(6,4)', '1.0 = average seasonal level'),
              ('trend_slope',
               'NUMERIC(12,6)',
               'Arrivals per week implied by the reported state, on the arrivals scale'),
('state_confidence',
                'NUMERIC(6,4)',
                'Share of pairwise slopes agreeing with the reported direction, '
                'where 0.5 = no consistent direction and NULL = not estimated'),
              ('evidence_weeks', 'INTEGER', 'Completed weeks supporting this prefix estimate'),
              ('evidence_json', 'TEXT', 'Nullable field reasons and prospective direction evaluation, legacy NULL = unrecorded'),
              ('model_version', 'TEXT NOT NULL', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_category_trends', ['category_norm', 'region_norm', 'week_start', 'model_version'])],
  'checks': [('ck_trend_state', 'trend_state', ['emerging', 'growing', 'mature', 'declining', 'volatile'])]},
 {'name': 'category_trend_forecasts',
  'comment': 'Projected weekly arrivals from the origin week, with a negative-binomial predictive '
             'interval. Absent rows and absent intervals mean the evidence did not support a projection.',
  'columns': [('forecast_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('category_norm', 'TEXT NOT NULL', None),
              ('region_norm', 'TEXT', 'Empty string = national aggregate, legacy NULL accepted'),
              ('origin_week_start', 'DATE NOT NULL', 'Last completed week the projection starts from'),
              ('horizon_weeks', 'INTEGER NOT NULL', 'Weeks ahead of origin_week_start'),
              ('target_week_start', 'DATE NOT NULL', 'Calendar Monday being projected'),
              ('expected_arrivals', 'NUMERIC(12,3)', 'Mean projected arrivals in the target week'),
              ('arrivals_lo', 'NUMERIC(12,3)', 'Lower bound of the predictive interval'),
              ('arrivals_hi', 'NUMERIC(12,3)', 'Upper bound of the predictive interval'),
              ('interval_level', 'NUMERIC(5,4)', 'Nominal coverage of the predictive interval'),
              ('overdispersion', 'NUMERIC(9,6)', 'Poisson-Gamma dispersion phi >= 1'),
              ('method', 'TEXT NOT NULL', 'seasonal-negative-binomial|negative-binomial'),
              ('evidence_weeks', 'INTEGER', 'Completed weeks supporting this projection'),
              ('is_accepted', 'BOOLEAN', 'Horizon cleared chronological arrival validation, legacy NULL = unverified'),
              ('validation_json', 'TEXT', 'Chronological evaluation and versioned acceptance, including failed checks'),
              ('model_version', 'TEXT NOT NULL', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_category_trend_forecasts',
               ['category_norm', 'region_norm', 'origin_week_start', 'horizon_weeks', 'model_version'])]},
 {'name': 'relist_lineages',
  'comment': 'Reconstructed relist chains with price decay and outcome tracking.',
  'columns': [('lineage_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('lineage_id', 'TEXT NOT NULL', 'Relist lineage identifier'),
              ('platform', 'TEXT NOT NULL', None),
              ('n_relists', 'INTEGER NOT NULL', None),
              ('initial_price_eur', 'NUMERIC(14,2)', None),
              ('final_price_eur', 'NUMERIC(14,2)', None),
              ('final_outcome', 'TEXT NOT NULL', None),
              ('total_days', 'NUMERIC(6,2)', None),
              ('price_decay_pct', 'NUMERIC(6,4)', 'Total price reduction from first to last'),
              ('media_hash_cluster', 'TEXT', 'Perceptual hash group for cross-listing matching'),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_relist_lineages', ['platform', 'lineage_id'])],
  'checks': [('ck_relist_outcome',
              'final_outcome',
              ['sold_confirmed', 'sold_inferred', 'withdrawn', 'expired', 'active', 'unknown'])]},
 {'name': 'market_price_estimates',
  'comment': 'Cached ask price distributions per category/condition/municipality grain. Used by the buy/no-buy engine.',
  'columns': [('estimate_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('category_norm', 'TEXT NOT NULL', None),
              ('condition_norm', "TEXT NOT NULL DEFAULT 'unknown'", None),
              ('municipality_norm', "TEXT NOT NULL DEFAULT ''", None),
              ('region_norm', "TEXT NOT NULL DEFAULT ''", None),
              ('price_quartile', 'NUMERIC(5,2) NOT NULL', '0=min, 0.25=P25, 0.5=median, 0.75=P75, 1=max'),
              ('price_eur', 'NUMERIC(14,2) NOT NULL', None),
              ('n_listings', 'INTEGER NOT NULL DEFAULT 0', None),
              ('n_fresh', 'INTEGER NOT NULL DEFAULT 0', None),
              ('cutoff_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('source', "TEXT NOT NULL DEFAULT 'tori_sitemap'", None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_market_price_estimate',
               ['category_norm', 'condition_norm', 'municipality_norm', 'region_norm', 'price_quartile'])]},
 {'name': 'collection_job_runs',
  'comment': 'Bounded job attempts within an owned collection cycle.',
  'columns': [('job_run_id', 'TEXT PRIMARY KEY', None),
              ('run_id', 'TEXT NOT NULL REFERENCES collection_cycles(run_id) ON DELETE CASCADE', None),
              ('job', 'TEXT NOT NULL', None),
              ('attempt', 'INTEGER NOT NULL', None),
              ('started_at', 'TIMESTAMPTZ NOT NULL', None),
              ('finished_at', 'TIMESTAMPTZ', None),
              ('lease_token', 'TEXT', None),
              ('lease_until', 'TIMESTAMPTZ', None),
              ('status', 'TEXT NOT NULL', None),
              ('error_class', 'TEXT', None),
              ('result_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_collection_job_attempt', ['run_id', 'job', 'attempt'])]},
 {'name': 'listings',
  'comment': 'Grain: one marketplace listing. C1 real CHECK-constrained enums. C10 subplatform splits Diili.',
  'columns': [('listing_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('platform', 'TEXT NOT NULL', None),
              ('subplatform', "TEXT NOT NULL DEFAULT 'standard'", 'C10'),
              ('listing_id', 'TEXT NOT NULL', None),
              ('title', 'TEXT', None),
              ('seller_id',
               'TEXT',
               'Pseudonymous: HMAC of the platform ownerId, keyed by TORI_SELLER_SALT. Raw identifier never stored.'),
              ('seller_version_id', 'BIGINT REFERENCES sellers(seller_version_id)', 'C6: FK to seller SCD-2 version'),
              ('product_pk', 'BIGINT REFERENCES dim_product(product_pk)', 'FK to canonical product dimension'),
              ('seller_type_norm', 'TEXT', 'C1'),
              ('category_raw_id', 'TEXT', "Platform's own category path, e.g. 2.78.5222.240. Always the id."),
              ('category_norm',
               'TEXT',
               'Human label chain, top>leaf. NULL where the label is unknown: an id is not a label.'),
              ('category_root_norm', 'TEXT', 'Top-level label. NULL unless resolved.'),
              ('category_label_confidence',
               'NUMERIC(5,4)',
               '1.0 from an item page, 0.0 when the value is an unresolved id'),
              ('brand_norm', 'TEXT', None),
              ('model_norm', 'TEXT', None),
              ('condition_raw', 'TEXT', None),
              ('condition_norm', 'TEXT', 'C1'),
              ('condition_label_raw', 'TEXT', "Platform's own condition label, untranslated"),
              ('municipality_norm', 'TEXT', None),
              ('region_norm', 'TEXT', None),
              ('listed_price_original', 'NUMERIC(14,2)', None),
              ('currency_original', 'TEXT', None),
              ('listed_price_eur', 'NUMERIC(14,2)', None),
              ('price_observed_at', 'TIMESTAMPTZ', 'When the currently retained asking price was observed'),
              ('buy_now_price_eur', 'NUMERIC(14,2)', None),
              ('shipping_cost_buyer_eur', 'NUMERIC(14,2)', None),
              ('buyer_fee_eur', 'NUMERIC(14,2)', 'Observed applicable buyer fee, NULL is not zero'),
              ('total_buyer_cost_eur',
               'NUMERIC(14,2)',
               'Derived only when ask, shipping and applicable buyer fee are known'),
              ('fx_rate_to_eur', 'NUMERIC(18,8)', None),
              ('fx_rate_date', 'DATE', None),
              ('fx_source', 'TEXT', None),
              ('platform_published_at', 'TIMESTAMPTZ', None),
              ('first_seen_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_seen_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_live_at', 'TIMESTAMPTZ', 'Last observation confirming the listing was not terminal'),
              ('last_probed_at', 'TIMESTAMPTZ', 'Last item-page attempt, including parse failures'),
              ('is_reserved', 'BOOLEAN', 'Reserved is observable and is not a sale'),
              ('field_sources_json', 'TEXT', 'Source of each populated observation field'),
              ('field_missing_reasons_json',
               'TEXT',
               'Missing reason per field: '
               'not_exposed|not_observed|not_applicable|ambiguous|parse_failed|redacted|unknown'),
              ('platform_touched_at', 'TIMESTAMPTZ', 'Search refresh marker, not publication time'),
              ('has_buy_now', 'BOOLEAN', None),
              ('has_shipping', 'BOOLEAN', None),
              ('observation_coverage_end', 'TIMESTAMPTZ', None),
              ('status_raw', 'TEXT', None),
              ('status_norm', "TEXT NOT NULL DEFAULT 'active'", 'C1'),
              ('coverage', "TEXT NOT NULL DEFAULT 'collecting'", 'C1'),
              ('outcome_status', "TEXT NOT NULL DEFAULT 'unknown'", 'C1'),
              ('outcome_confidence', 'NUMERIC(5,4)', None),
              ('outcome_observed_at', 'TIMESTAMPTZ', 'When the terminal state was last checked'),
              ('outcome_label_raw', 'TEXT', "Platform's own outcome word, untranslated (e.g. Myyty)"),
              ('platform_edited_at', 'TIMESTAMPTZ', None),
              ('promotion_active', 'BOOLEAN', None),
              ('promotion_type', 'TEXT', None),
              ('image_count', 'INTEGER', None),
              ('is_giveaway', 'BOOLEAN', 'Platform trade_type is giveaway, or price is zero'),
              ('relist_lineage_id', 'TEXT', None),
              ('canonical_item_id', 'TEXT', None),
              ('match_confidence', 'NUMERIC(5,4)', None),
              ('match_method_version', 'TEXT', None),
              ('normalization_version', "TEXT NOT NULL DEFAULT 'norm-2026-10-01'", None),
              ('source_observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_listings', ['platform', 'listing_id'])],
  'checks': [('ck_listings_condition',
              'condition_norm',
              ['new_unused', 'like_new', 'very_good', 'good', 'fair', 'poor', 'for_parts', 'refurbished', 'unknown']),
             ('ck_listings_status', 'status_norm', ['active', 'inactive', 'withdrawn', 'expired', 'sold', 'unknown']),
             ('ck_listings_outcome',
              'outcome_status',
              ['sold_confirmed', 'sold_inferred', 'withdrawn', 'expired', 'active', 'unknown']),
             ('ck_listings_coverage', 'coverage', ['collecting', 'gone_uncertain']),
             ('ck_listings_seller_type', 'seller_type_norm', ['private', 'professional', 'business', 'unknown']),
             ('ck_listings_subplatform', 'subplatform', ['standard', 'diili'])]},
 {'name': 'sweep_page_commits',
  'comment': 'One committed page per traversal. Page counts and checkpoints share the observation transaction.',
  'columns': [('target_pk', 'BIGINT NOT NULL REFERENCES sweep_targets(target_pk) ON DELETE CASCADE', None),
              ('traversal_id', 'TEXT NOT NULL', None),
              ('page_number', 'INTEGER NOT NULL', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None),
              ('seen', 'INTEGER NOT NULL', None),
              ('new', 'INTEGER NOT NULL', None),
              ('overlap', 'INTEGER NOT NULL', None),
              ('parser', 'TEXT NOT NULL', None),
              ('end_reason', 'TEXT', None)],
  'uniques': [('uq_sweep_page', ['target_pk', 'traversal_id', 'page_number'])]},
 {'name': 'seller_archetypes',
  'comment': 'Behavioral seller segmentation. One row per seller version per model version.',
  'columns': [('archetype_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('seller_version_id', 'BIGINT NOT NULL REFERENCES sellers(seller_version_id)', None),
              ('archetype_id', 'TEXT NOT NULL', 'e.g. aggressive_repricer, patient_holder'),
              ('cluster_id', 'INTEGER', 'HDBSCAN label, -1 = noise'),
              ('confidence', 'NUMERIC(5,4)', 'HDBSCAN membership strength 0-1, noise = 0'),
              ('features_json', 'TEXT NOT NULL', 'Observed features and standardized clustering vector'),
              ('model_version', 'TEXT NOT NULL', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_seller_archetypes', ['seller_version_id', 'model_version'])]},
 {'name': 'market_concentration',
  'comment': 'Observed monthly listing-arrival cohorts: count and initial asking-value concentration with seller '
             'linkage coverage.',
  'columns': [('concentration_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('category_norm', 'TEXT NOT NULL', None),
              ('region_norm', 'TEXT', 'Empty string = national, legacy NULL accepted'),
              ('month_start', 'DATE NOT NULL', None),
              ('hhi', 'NUMERIC(8,6)', 'Herfindahl-Hirschman Index 0-1'),
              ('top3_share', 'NUMERIC(6,4)', 'Combined share of top 3 sellers'),
              ('hhi_count', 'NUMERIC(8,6)', 'HHI based on observed listing counts'),
              ('top3_count_share', 'NUMERIC(6,4)', 'Top three sellers by listing count'),
              ('n_active_sellers', 'INTEGER', 'Distinct linked sellers observed in the arrival cohort'),
              ('n_observed_listings', 'INTEGER', None),
              ('n_linked_listings', 'INTEGER', None),
              ('linkage_coverage', 'NUMERIC(9,6)', 'Linked listings / observed listings'),
              ('price_coverage', 'NUMERIC(9,6)', 'Linked listings with known entry prices / linked listings'),
              ('is_complete_month', 'BOOLEAN', None),
              ('price_leader_seller_version_id',
               'BIGINT REFERENCES sellers(seller_version_id)',
               'Seller who moves prices first'),
              ('model_version', 'TEXT NOT NULL', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_market_concentration', ['category_norm', 'region_norm', 'month_start', 'model_version'])]},
 {'name': 'classification_changes',
  'comment': 'Atomic observation change markers, independent of model configuration.',
  'columns': [('listing_pk', 'BIGINT PRIMARY KEY REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('changed_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': []},
 {'name': 'followup_enrollments',
  'comment': 'Conditional admission provenance for bounded detail follow-up. Historical batches are not a '
             'representative market panel.',
  'columns': [('enrollment_id', 'TEXT PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('policy_key', 'TEXT NOT NULL', None),
              ('selection_version', 'TEXT NOT NULL', None),
              ('batch_id', 'TEXT NOT NULL', None),
              ('mode', 'TEXT NOT NULL', None),
              ('purpose', 'TEXT', 'Legacy NULL maps sampled to baseline and priority to opportunity'),
              ('vertical', 'TEXT NOT NULL', None),
              ('stratum', 'TEXT NOT NULL', None),
              ('population_size', 'INTEGER NOT NULL', None),
              ('selection_probability', 'REAL', 'Conditional on the eligible admission pool in this stratum and batch'),
              ('enrolled_at', 'TIMESTAMPTZ NOT NULL', None),
              ('ended_at', 'TIMESTAMPTZ', None),
              ('end_reason', 'TEXT', None)],
  'uniques': [],
  'checks': [('ck_followup_mode', 'mode', ['sampled', 'priority']),
             ('ck_followup_purpose', 'purpose', ['baseline', 'opportunity', 'exploration'])]},
 {'name': 'followup_intents',
  'comment': 'Discretionary nominations never replace baseline enrollment or reset its schedule.',
  'columns': [('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('purpose', 'TEXT NOT NULL', None),
              ('reason', 'TEXT NOT NULL', 'Operator decision impact or opportunity rationale'),
              ('nominated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_followup_intent', ['listing_pk', 'purpose'])],
  'checks': [('ck_followup_intent', 'purpose', ['opportunity', 'exploration'])]},
 {'name': 'listing_observations',
  'comment': 'Append-only source facts and accepted state, without raw seller identifiers or descriptions.',
  'columns': [('observation_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None),
              ('source', 'TEXT NOT NULL', None),
              ('normalization_version', 'TEXT NOT NULL', None),
              ('state_at', 'TIMESTAMPTZ', 'Accepted state timestamp, unknown for legacy observation snapshots'),
              ('fields_json', 'TEXT NOT NULL', 'Values actually supplied by this source'),
              ('state_json', 'TEXT NOT NULL', 'Accepted descriptive and lifecycle state following the merge')],
  'uniques': [('uq_listing_observation', ['listing_pk', 'observed_at', 'source'])]},
 {'name': 'price_history',
  'comment': 'Append-only. Grain: one observed asking-price interval.',
  'columns': [('price_history_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('valid_from', 'TIMESTAMPTZ NOT NULL', None),
              ('valid_to', 'TIMESTAMPTZ', None),
              ('currency_original', "TEXT NOT NULL DEFAULT 'EUR'", None),
              ('listed_price_original', 'NUMERIC(14,2) NOT NULL', None),
              ('listed_price_eur', 'NUMERIC(14,2)', None),
              ('shipping_cost_buyer_eur', 'NUMERIC(14,2)', None),
              ('buyer_fee_eur', 'NUMERIC(14,2)', None),
              ('total_buyer_cost_eur', 'NUMERIC(14,2)', None),
              ('promotion_active', 'BOOLEAN', None),
              ('fx_rate_to_eur', 'NUMERIC(18,8)', None),
              ('fx_rate_date', 'DATE', None),
              ('fx_source', 'TEXT', None),
              ('price_observed_at', 'TIMESTAMPTZ', 'Source observation time for this price interval'),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': []},
 {'name': 'price_observations',
  'comment': 'Successful price measurements, including unchanged asks. Sightings without a price are excluded.',
  'columns': [('price_observation_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None),
              ('price_observed_at', 'TIMESTAMPTZ NOT NULL', None),
              ('source', 'TEXT NOT NULL', None),
              ('listed_price_eur', 'NUMERIC(14,2)', None),
              ('listed_price_original', 'NUMERIC(14,2) NOT NULL', None),
              ('currency_original', 'TEXT', None)],
  'uniques': [('uq_price_observation', ['listing_pk', 'observed_at', 'source'])]},
 {'name': 'events',
  'comment': 'C4 idempotency key. Without it, re-observing unchanged state duplicates rows.',
  'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('event_type', 'TEXT NOT NULL', 'C1'),
              ('event_at', 'TIMESTAMPTZ', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('before_value', 'TEXT', None),
              ('after_value', 'TEXT', None),
              ('payload_json', 'TEXT', None),
              ('event_confidence', 'NUMERIC(5,4)', None)],
  'uniques': [('uq_events_idem', ['listing_pk', 'event_type', 'event_at'])],
  'checks': [('ck_events_type',
              'event_type',
              ['first_seen',
               'published',
               'price_changed',
               'status_changed',
               'relisted',
               'promotion_started',
               'promotion_ended',
               'sold_confirmed',
               'withdrawn',
               'expired',
               'view_snapshot',
               'watcher_snapshot',
               'message_snapshot',
               'description_changed',
               'attribute_changed',
               'media_changed'])]},
 {'name': 'media_hashes',
  'comment': 'C-hashes-only. Image pixels are never stored (PROJECT_PLAN.md 3, mitigation 3).',
  'columns': [('media_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('platform_media_id', 'TEXT', None),
              ('media_kind', 'TEXT NOT NULL', 'C1'),
              ('media_position', 'INTEGER', None),
              ('sha256', 'TEXT', None),
              ('perceptual_hash', 'TEXT', None),
              ('first_seen_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_seen_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_media', ['listing_pk', 'platform_media_id'])],
  'checks': [('ck_media_kind', 'media_kind', ['image', 'video'])]},
 {'name': 'purchases',
  'comment': 'The moat. Inbound side has no public equivalent and cannot be revoked. Record from day one.',
  'columns': [('purchase_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('purchased_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('platform', 'TEXT NOT NULL', None),
              ('subplatform', 'TEXT', 'C10'),
              ('source_listing_id', 'TEXT', None),
              ('title_raw', 'TEXT NOT NULL', None),
              ('category_norm', 'TEXT', None),
              ('brand_norm', 'TEXT', None),
              ('model_norm', 'TEXT', None),
              ('condition_norm', 'TEXT', None),
              ('condition_at_buy_norm', 'TEXT', None),
              ('attributes_json', 'TEXT', None),
              ('acquisition_cost_eur', 'NUMERIC(14,2) NOT NULL', None),
              ('shipping_cost_eur', 'NUMERIC(14,2)', None),
              ('buyer_fee_eur', 'NUMERIC(14,2)', None),
              ('fx_rate_to_eur', 'NUMERIC(18,8)', None),
              ('fx_rate_date', 'DATE', None),
              ('fx_source', 'TEXT', None),
              ('notes', 'TEXT', None),
              ('sold_listing_pk', 'BIGINT REFERENCES listings(listing_pk)', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP', None)],
  'uniques': [('uq_purchases', ['platform', 'source_listing_id'])]},
 {'name': 'listing_search_attributions',
  'comment': 'Observed search membership, separate from an item-confirmed leaf category. Deleted with listing '
             'retention.',
  'columns': [('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('target_pk', 'BIGINT NOT NULL REFERENCES sweep_targets(target_pk) ON DELETE CASCADE', None),
              ('first_seen_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_seen_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_traversal_id', 'TEXT NOT NULL', None)],
  'uniques': [('uq_listing_search', ['listing_pk', 'target_pk'])]},
 {'name': 'enrichment_tasks',
  'comment': 'Current local classification work. Input hashes and leases survive interruptions, deleted with listing '
             'retention.',
 'columns': [('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('task', 'TEXT NOT NULL', None),
              ('input_sha256', 'TEXT NOT NULL', None),
              ('research_priority', 'INTEGER NOT NULL DEFAULT 0', 'Pilot-family relevance for the bounded fresh lane only'),
              ('state', 'TEXT NOT NULL', 'ready, leased, complete, quarantined or suspended'),
              ('due_at', 'TIMESTAMPTZ NOT NULL', None),
              ('queued_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_attempt_at', 'TIMESTAMPTZ', None),
              ('completed_at', 'TIMESTAMPTZ', None),
              ('failure_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('error_class', 'TEXT', 'Bounded class only, no model response or listing text'),
              ('lease_token', 'TEXT', None),
              ('lease_until', 'TIMESTAMPTZ', None)],
  'uniques': [('uq_enrichment_task', ['listing_pk', 'task'])]},
 {'name': 'enrichment_vocabularies',
  'comment': 'Observed vocabulary byte hashes mapped to validated semantic identities. Contains no listing text.',
  'columns': [('vocabulary_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('raw_sha256', 'TEXT NOT NULL', None),
              ('semantic_sha256', 'TEXT NOT NULL', None),
              ('identity_version', 'TEXT NOT NULL', None),
              ('legacy_compatible', 'BOOLEAN NOT NULL DEFAULT FALSE', 'Legacy parser and current parser agree on the source bytes'),
              ('semantic_json', 'TEXT NOT NULL', 'Normalized bounded vocabulary, not model output'),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_enrichment_vocabulary', ['raw_sha256', 'semantic_sha256'])]},
 {'name': 'enrichment',
  'comment': 'Model-derived values, kept strictly apart from observation. Nothing here is ever read into a reported '
             'statistic without being labelled as derived, and no enrichment field may hold a price, an outcome or '
             'anything that would resolve a suppressed cell. input_sha256 makes every result traceable to the exact '
             'bytes and prompt version that produced it, which is what makes a bad classification traceable instead of '
             'merely suspected.',
  'columns': [('enrichment_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT REFERENCES listings(listing_pk)', None),
              ('platform', 'TEXT NOT NULL', None),
              ('listing_id', 'TEXT NOT NULL', None),
              ('task', 'TEXT NOT NULL', 'Which derivation produced this'),
              ('input_sha256', 'TEXT NOT NULL', 'hash of prompt version plus exact input'),
              ('model', 'TEXT', None),
              ('provider', 'TEXT', None),
              ('prompt_version', 'TEXT', None),
              ('schema_version', 'TEXT', None),
              ('output_json', 'TEXT NOT NULL', 'Enum-constrained. No free text is stored.'),
              ('model_confidence_raw',
               'REAL',
               "The model's own claim. Recorded for audit and otherwise ignored: it reports 0.95 while being "
               'hijacked.'),
              ('confidence', 'REAL', "Ours, derived from validation. Never the model's."),
              ('confidence_basis', 'TEXT', 'Why we believe it'),
              ('validation', 'TEXT', 'enum_ok, agreement_with_platform_category, ...'),
              ('observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('latency_ms', 'INTEGER', None)],
  'uniques': [('uq_enrichment', ['platform', 'listing_id', 'task', 'input_sha256'])]},
 {'name': 'research_decisions',
  'comment': 'Append-only analyst decisions and assumption snapshots. No marketplace outcome is inferred here.',
  'columns': [('decision_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('client_token', 'TEXT NOT NULL', 'Idempotency key for retrying one submission'),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None),
              ('listing_pk',
               'BIGINT REFERENCES listings(listing_pk) ON DELETE SET NULL',
               'Listing retention does not depend on research interest'),
              ('platform', 'TEXT NOT NULL', None),
              ('source_listing_id', 'TEXT NOT NULL', None),
              ('decision', 'TEXT NOT NULL', None),
              ('notes', "TEXT NOT NULL DEFAULT ''", None),
              ('checks_json', 'TEXT NOT NULL', 'Human inspection checks, not observed listing attributes'),
              ('scenario_json', 'TEXT', 'User assumptions and server-calculated arithmetic at decision time'),
              ('evidence_json', 'TEXT NOT NULL', 'Asking-price context and support at decision time')],
  'uniques': [('uq_research_decision_token', ['client_token'])],
  'checks': [('ck_research_decision', 'decision', ['watch', 'inspect', 'reject', 'pursue'])]},
 {'name': 'predictive_feature_snapshots',
  'comment': 'Immutable entry-time covariates for chronological modelling. Missing historical snapshots are never '
             'backfilled from later listing state.',
  'columns': [('listing_pk', 'BIGINT PRIMARY KEY REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL', None),
              ('feature_version', 'TEXT NOT NULL', None),
              ('features_json', 'TEXT NOT NULL', None)],
  'uniques': []},
 {'name': 'sale_predictions',
  'comment': 'Latest probability snapshot per listing, fitted model version and future horizon. Refits receive '
             'distinct model versions.',
  'columns': [('prediction_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('model_version', 'TEXT NOT NULL', None),
              ('horizon_days', 'INTEGER NOT NULL', None),
              ('p_sale', 'NUMERIC(6,4) NOT NULL', '0-1 probability of sale within horizon'),
              ('is_accepted',
               'BOOLEAN',
               'Fit and horizon cleared versioned guidance acceptance, legacy NULL = unverified'),
              ('p_sale_ci_lo', 'NUMERIC(6,4)', None),
              ('p_sale_ci_hi', 'NUMERIC(6,4)', None),
              ('expected_price_eur', 'NUMERIC(14,2)', 'Conditional expected sale price'),
              ('price_ci_lo', 'NUMERIC(14,2)', None),
              ('price_ci_hi', 'NUMERIC(14,2)', None),
              ('features_json', 'TEXT NOT NULL', 'Input feature snapshot for audit/reproducibility'),
              ('is_extrapolated', 'BOOLEAN', 'Current age plus horizon lacks segment and conditional-age holdout support'),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_sale_predictions', ['listing_pk', 'model_version', 'horizon_days'])],
  'checks': [('ck_sale_pred_horizon', 'horizon_days', ['1', '2', '3', '7', '14', '30', '60', '90'])]},
 {'name': 'price_trajectories',
  'comment': 'Latest asking-price forecast snapshot per listing, fitted model version and future horizon.',
  'columns': [('trajectory_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('model_version', 'TEXT NOT NULL', None),
              ('day', 'INTEGER NOT NULL', 'Future horizon in days from computed_at'),
              ('predicted_price_eur', 'NUMERIC(14,2)', None),
              ('is_accepted',
               'BOOLEAN',
               'Fit and horizon cleared versioned guidance acceptance, legacy NULL = unverified'),
              ('price_ci_lo', 'NUMERIC(14,2)', None),
              ('price_ci_hi', 'NUMERIC(14,2)', None),
              ('is_extrapolated', 'BOOLEAN', 'Horizon lacks holdout duration support in this category and condition'),
              ('observed_duration_days', 'NUMERIC(14,2)', None),
              ('diagnostics_ok', 'BOOLEAN', 'Split R-hat and effective sample size checks passed'),
              ('validation_available', 'BOOLEAN', 'A disjoint chronological holdout was available'),
              ('validation_improves_baseline',
               'BOOLEAN',
               'Holdout ratio MAE beats the no-change baseline, not a profit claim'),
              ('features_json', 'TEXT', 'Frozen forecast category and condition, legacy NULL = unrecorded'),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_price_trajectories', ['listing_pk', 'model_version', 'day'])]},
 {'name': 'price_predictions',
  'comment': 'Predicted sale outcome per listing with confidence intervals. Feeds the buy/no-buy engine.',
  'columns': [('prediction_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('model_version', 'TEXT NOT NULL', None),
              ('horizon_days', 'TEXT NOT NULL', None),
              ('p_sale', 'NUMERIC(6,4) NOT NULL', '0-1 probability of sale within horizon'),
              ('expected_price_eur', 'NUMERIC(14,2)', None),
              ('price_ci_lo', 'NUMERIC(14,2)', None),
              ('price_ci_hi', 'NUMERIC(14,2)', None),
              ('features_json', 'TEXT NOT NULL', None),
              ('is_extrapolated', 'BOOLEAN', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_price_pred', ['listing_pk', 'model_version', 'horizon_days'])],
  'checks': [('ck_price_pred_horizon', 'horizon_days', ['7', '14', '30', '60', '90'])]},
 {'name': 'buy_recommendations',
  'comment': 'Buy/no-buy decisions with expected profit calculations. One per (listing, model_version) pair.',
  'columns': [('recommendation_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk)', None),
              ('model_version', 'TEXT NOT NULL', None),
              ('expected_buy_price_eur', 'NUMERIC(14,2) NOT NULL', None),
              ('expected_sale_price_eur', 'NUMERIC(14,2)', None),
              ('p_sale_within_90d', 'NUMERIC(6,4)', None),
              ('expected_gross_margin_eur', 'NUMERIC(14,2)', None),
              ('expected_gross_margin_pct', 'NUMERIC(9,6)', None),
              ('confidence_tier', "TEXT NOT NULL DEFAULT 'low'", 'low, medium or high'),
              ('risk_flags', "TEXT NOT NULL DEFAULT ''", 'JSON list'),
              ('evidence_json', 'TEXT NOT NULL', None),
              ('computed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_buy_rec_unique', ['listing_pk', 'model_version'])]},
 {'name': 'followup_tasks',
  'comment': 'One durable task per listing. Successful observation and task completion commit together.',
  'columns': [('listing_pk', 'BIGINT PRIMARY KEY REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('enrollment_id', 'TEXT NOT NULL REFERENCES followup_enrollments(enrollment_id)', None),
              ('state', 'TEXT NOT NULL', None),
              ('due_at', 'TIMESTAMPTZ NOT NULL', None),
              ('last_success_at', 'TIMESTAMPTZ', 'New tasks never infer success from historical attempt timestamps'),
              ('last_attempt_at', 'TIMESTAMPTZ', None),
              ('attempt_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('failure_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('error_class', 'TEXT', 'Bounded class only, never raw page diagnostics'),
              ('lease_token', 'TEXT', None),
              ('lease_until', 'TIMESTAMPTZ', None)],
  'uniques': [],
  'checks': [('ck_followup_state', 'state', ['ready', 'leased', 'quarantined', 'complete', 'suspended'])]},
 {'name': 'transactions',
  'comment': 'A completed sale of our own inventory. Keyed to the purchase, not to a listing: most of what we sell was '
             'never scraped as a listing, and a sale that must be joined to a marketplace row is a sale we will forget '
             'to record.',
  'columns': [('transaction_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('purchase_pk', 'BIGINT REFERENCES purchases(purchase_pk)', 'The item we sold. The asset lives here.'),
              ('listing_pk',
               'BIGINT REFERENCES listings(listing_pk)',
               'Our resale listing, when the sale went through one'),
              ('platform', 'TEXT NOT NULL', 'C2'),
              ('platform_transaction_id', 'TEXT', None),
              ('outcome_status', "TEXT NOT NULL DEFAULT 'unknown'", 'C1'),
              ('sale_at', 'TIMESTAMPTZ', None),
              ('paid_at', 'TIMESTAMPTZ', None),
              ('shipped_at', 'TIMESTAMPTZ', None),
              ('currency_original', 'TEXT', None),
              ('final_sale_price_original', 'NUMERIC(14,2)', None),
              ('final_sale_price_eur', 'NUMERIC(14,2)', None),
              ('accepted_offer_eur', 'NUMERIC(14,2)', None),
              ('platform_fee_eur', 'NUMERIC(14,2)', None),
              ('payment_fee_eur', 'NUMERIC(14,2)', None),
              ('seller_shipping_cost_eur', 'NUMERIC(14,2)', None),
              ('packaging_cost_eur', 'NUMERIC(14,2)', None),
              ('other_variable_cost_eur', 'NUMERIC(14,2)', None),
              ('tax_cost_eur', 'NUMERIC(14,2)', None),
              ('gross_margin_eur', 'NUMERIC(14,2)', None),
              ('gross_margin_pct', 'NUMERIC(9,6)', None),
              ('margin_known', 'BOOLEAN', None),
              ('margin_missing_reason', 'TEXT', 'C1'),
              ('shipping_method', 'TEXT', None),
              ('carrier', 'TEXT', None),
              ('transaction_confidence', 'NUMERIC(5,4)', None),
              ('observed_at', 'TIMESTAMPTZ NOT NULL DEFAULT now()', None)],
  'uniques': [('uq_transactions', ['platform', 'platform_transaction_id'])],
  'checks': [('ck_transactions_outcome',
              'outcome_status',
              ['sold_confirmed', 'sold_inferred', 'withdrawn', 'expired', 'active', 'unknown']),
             ('ck_transactions_margin_reason',
              'margin_missing_reason',
              ['not_exposed', 'not_observed', 'not_applicable', 'ambiguous', 'parse_failed', 'redacted', 'unknown'])]},
 {'name': 'inventory_units',
  'comment': 'One owned physical unit per purchase, with an immutable SKU and versioned facts.',
  'columns': [('sku', 'TEXT PRIMARY KEY', None),
              ('purchase_pk', 'BIGINT NOT NULL REFERENCES purchases(purchase_pk)', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('title', 'TEXT NOT NULL', None),
              ('kind', 'TEXT NOT NULL', None),
              ('state', "TEXT NOT NULL DEFAULT 'in_stock'", None),
              ('facts_json', 'TEXT NOT NULL', 'Owned facts, never copied automatically from observations'),
              ('version', 'INTEGER NOT NULL DEFAULT 1', None),
              ('facts_version', 'INTEGER NOT NULL DEFAULT 1', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_inventory_purchase', ['purchase_pk']), ('uq_inventory_token', ['client_token'])],
  'checks': [('ck_inventory_kind', 'kind', ['technology', 'bicycle', 'scooter']),
             ('ck_inventory_state', 'state', ['in_stock', 'quarantined', 'retired'])]},
 {'name': 'listing_relationship_events',
  'comment': 'Append-only relationship reviews and reversals. Suggestions never establish sales or unit identity.',
  'columns': [('event_id', 'TEXT PRIMARY KEY', None),
              ('claim_id', 'TEXT NOT NULL', None),
              ('revision', 'INTEGER NOT NULL', None),
              ('left_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('right_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
              ('kind', 'TEXT NOT NULL', None),
              ('state', 'TEXT NOT NULL', None),
              ('basis', 'TEXT NOT NULL', None),
              ('sku', 'TEXT REFERENCES inventory_units(sku)', None),
              ('reason', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_relationship_revision', ['claim_id', 'revision'])],
  'checks': [('ck_relationship_kind', 'kind', ['duplicate', 'possible_relist', 'listing_version']),
             ('ck_relationship_state', 'state', ['accepted', 'retracted']),
             ('ck_relationship_basis', 'basis', ['operator_review', 'owned_inventory'])]},
 {'name': 'inventory_events',
  'comment': 'Append-only unit history, including fact revisions and state changes.',
  'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('unit_version', 'INTEGER NOT NULL', None),
              ('event_type', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('reason', 'TEXT NOT NULL', None),
              ('snapshot_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_inventory_event_version', ['sku', 'unit_version'])]},
 {'name': 'inspections',
  'comment': 'Append-only human inspection snapshots, bound to the unit facts at inspection time.',
  'columns': [('inspection_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('facts_version', 'INTEGER NOT NULL', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('inspector', 'TEXT NOT NULL', None),
              ('competent', 'BOOLEAN NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', 'Private owned evidence reference, never fetched by the server'),
              ('checks_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_inspection_token', ['client_token'])]},
 {'name': 'platform_eligibility',
  'comment': 'Frozen preparation previews with rule and input versions, never a publication grant.',
  'columns': [('evaluation_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('unit_version', 'INTEGER NOT NULL', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('channel', 'TEXT NOT NULL', None),
              ('status', 'TEXT NOT NULL', None),
              ('rule_version', 'TEXT NOT NULL', None),
              ('rules_sha256', 'TEXT NOT NULL', None),
              ('input_sha256', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('result_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_eligibility_token', ['client_token'])],
  'checks': [('ck_eligibility_status', 'status', ['allowed', 'blocked', 'requires_review'])]},
 {'name': 'listing_drafts',
  'comment': 'Versioned local listing preparation from owned facts and inspection evidence.',
  'columns': [('draft_id', 'TEXT PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('channel', 'TEXT NOT NULL', None),
              ('environment', 'TEXT NOT NULL', None),
              ('version', 'INTEGER NOT NULL', None),
              ('unit_version', 'INTEGER NOT NULL', None),
              ('content_json', 'TEXT NOT NULL', None),
              ('binding_json', 'TEXT NOT NULL', 'Frozen facts, inspection, connection and preparation rules'),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [],
  'checks': [('ck_draft_environment', 'environment', ['local', 'sandbox', 'production'])]},
 {'name': 'order_lines',
  'comment': 'Confirmed manual sale cycles for one owned physical unit, independent of acquisition source.',
  'columns': [('order_id', 'TEXT PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('cycle', 'INTEGER NOT NULL', None),
              ('channel', 'TEXT NOT NULL', None),
              ('external_order_ref', 'TEXT', None),
              ('legacy_transaction_pk', 'BIGINT REFERENCES transactions(transaction_pk)', None),
              ('origin', 'TEXT NOT NULL', None),
              ('state', 'TEXT NOT NULL', None),
              ('fulfilment', 'TEXT NOT NULL', None),
              ('version', 'INTEGER NOT NULL DEFAULT 1', None),
              ('sold_at', 'TIMESTAMPTZ NOT NULL', None),
              ('shipped_at', 'TIMESTAMPTZ', None),
              ('delivered_at', 'TIMESTAMPTZ', None),
              ('seller_context', 'TEXT NOT NULL', None),
              ('buyer_context', 'TEXT NOT NULL', None),
              ('sale_method', 'TEXT NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', None),
              ('source_snapshot_json', 'TEXT NOT NULL', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_order_cycle', ['sku', 'cycle']),
              ('uq_order_external', ['channel', 'external_order_ref', 'sku']),
              ('uq_order_legacy', ['legacy_transaction_pk'])],
  'checks': [('ck_order_state', 'state', ['sold', 'returned']),
             ('ck_order_fulfilment', 'fulfilment', ['pending', 'shipped', 'delivered', 'returned']),
             ('ck_order_origin', 'origin', ['manual', 'legacy'])]},
 {'name': 'draft_events',
  'comment': 'Immutable draft revisions and idempotent mutation receipts.',
  'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('draft_id', 'TEXT NOT NULL REFERENCES listing_drafts(draft_id)', None),
              ('version', 'INTEGER NOT NULL', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('snapshot_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_draft_event_token', ['client_token']), ('uq_draft_event_version', ['draft_id', 'version'])]},
 {'name': 'draft_approvals',
  'comment': 'Human preparation approvals bound to one exact draft and evidence snapshot.',
  'columns': [('approval_id', 'TEXT PRIMARY KEY', None),
              ('draft_id', 'TEXT NOT NULL REFERENCES listing_drafts(draft_id)', None),
              ('draft_version', 'INTEGER NOT NULL', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('reviewer', 'TEXT NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', None),
              ('snapshot_sha256', 'TEXT NOT NULL', None),
              ('snapshot_json', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_draft_approval_token', ['client_token'])]},
 {'name': 'stock_lifecycle',
  'comment': 'Current owned-stock lifecycle, with history preserved in inventory and order events.',
  'columns': [('sku', 'TEXT PRIMARY KEY REFERENCES inventory_units(sku)', None),
              ('phase', 'TEXT NOT NULL', None),
              ('current_order_id', 'TEXT NOT NULL REFERENCES order_lines(order_id)', None),
              ('received_at', 'TIMESTAMPTZ', None),
              ('release_inspection_pk', 'BIGINT REFERENCES inspections(inspection_pk)', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [],
  'checks': [('ck_stock_phase', 'phase', ['available', 'sold', 'return_pending', 'returned_review', 'written_off'])]},
 {'name': 'return_cases',
  'comment': 'Distinct return/claim purposes with evidenced operator deadline assessments.',
  'columns': [('case_id', 'TEXT PRIMARY KEY', None),
              ('order_id', 'TEXT NOT NULL REFERENCES order_lines(order_id)', None),
              ('case_type', 'TEXT NOT NULL', None),
              ('state', 'TEXT NOT NULL', None),
              ('reason', 'TEXT NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', None),
              ('notified_at', 'TIMESTAMPTZ NOT NULL', None),
              ('received_at', 'TIMESTAMPTZ', None),
              ('deadline_json', 'TEXT NOT NULL', None),
              ('resolution', 'TEXT', None),
              ('closed_at', 'TIMESTAMPTZ', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [],
  'checks': [('ck_return_type', 'case_type', ['withdrawal', 'defect', 'platform_protection', 'voluntary']),
             ('ck_return_state', 'state', ['open', 'received', 'closed'])]},
 {'name': 'order_events',
  'comment': 'Append-only order and stock snapshots with global local-action retry receipts.',
  'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('order_id', 'TEXT NOT NULL REFERENCES order_lines(order_id)', None),
              ('client_token', 'TEXT NOT NULL', None),
              ('request_sha256', 'TEXT NOT NULL', None),
              ('event_type', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('effective_at', 'TIMESTAMPTZ NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', None),
              ('result_json', 'TEXT NOT NULL', None),
              ('snapshot_json', 'TEXT NOT NULL', None)],
  'uniques': [('uq_order_event_token', ['client_token'])]},
 {'name': 'action_outbox',
  'comment': 'Durable single-unit publishing intents with reconciliation before uncertain retries.',
  'columns': [('action_id', 'TEXT PRIMARY KEY', None),
              ('approval_id', 'TEXT NOT NULL REFERENCES draft_approvals(approval_id)', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('action', 'TEXT NOT NULL', None),
              ('state', 'TEXT NOT NULL', None),
              ('attempt_count', 'INTEGER NOT NULL DEFAULT 0', None),
              ('lease_token', 'TEXT', None),
              ('lease_until', 'TIMESTAMPTZ', None),
              ('detail', 'TEXT NOT NULL', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None),
              ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_outbox_approval_action', ['approval_id', 'action'])],
  'checks': [('ck_outbox_action', 'action', ['publish']),
             ('ck_outbox_state',
              'state',
              ['blocked', 'queued', 'dispatching', 'uncertain', 'reconciling', 'succeeded', 'cancelled', 'failed'])]},
 {'name': 'financial_entries',
  'comment': 'Immutable integer-cent entries and superseding corrections, including explicit unknown costs.',
  'columns': [('entry_id', 'TEXT PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('order_id', 'TEXT REFERENCES order_lines(order_id)', None),
              ('case_id', 'TEXT REFERENCES return_cases(case_id)', None),
              ('supersedes_id', 'TEXT REFERENCES financial_entries(entry_id)', None),
              ('external_event_ref',
               'TEXT',
               'Opaque observed payment/refund reference, never customer contact details'),
              ('kind', 'TEXT NOT NULL', None),
              ('amount_minor', 'BIGINT', 'NULL means unknown, zero means an evidenced zero cost'),
              ('effective_at', 'TIMESTAMPTZ NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('evidence_ref', 'TEXT NOT NULL', None),
              ('reason', 'TEXT NOT NULL', None)],
  'uniques': [('uq_financial_supersedes', ['supersedes_id']),
              ('uq_financial_external', ['order_id', 'external_event_ref'])]},
 {'name': 'action_events',
  'comment': 'Append-only queue transitions and sanitized external outcomes.',
  'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('action_id', 'TEXT NOT NULL REFERENCES action_outbox(action_id)', None),
              ('state', 'TEXT NOT NULL', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None),
              ('detail', 'TEXT NOT NULL', None)],
  'uniques': []},
 {'name': 'inventory_reservations',
  'comment': 'Exclusive physical-unit reservation held through uncertain or active publication.',
  'columns': [('sku', 'TEXT PRIMARY KEY REFERENCES inventory_units(sku)', None),
              ('action_id', 'TEXT NOT NULL REFERENCES action_outbox(action_id)', None),
              ('created_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_inventory_reservation_action', ['action_id'])]},
 {'name': 'channel_listings',
  'comment': 'Confirmed external listing identities, separate from market observations.',
  'columns': [('listing_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
              ('sku', 'TEXT NOT NULL REFERENCES inventory_units(sku)', None),
              ('action_id', 'TEXT NOT NULL REFERENCES action_outbox(action_id)', None),
              ('channel', 'TEXT NOT NULL', None),
              ('environment', 'TEXT NOT NULL', None),
              ('account_binding', 'TEXT NOT NULL', None),
              ('external_id', 'TEXT NOT NULL', None),
              ('state', "TEXT NOT NULL DEFAULT 'active'", None),
              ('confirmed_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_channel_listing_action', ['action_id']),
              ('uq_channel_listing_external', ['channel', 'environment', 'account_binding', 'external_id'])],
  'checks': [('ck_channel_listing_state', 'state', ['active', 'ended'])]},
 {'name': 'agent_submissions',
  'comment': 'Append-only quarantine for external agent proposals. A row here is a claim, not an '
             'effect: nothing in this table has changed an observation, an owned ledger row or a '
             'publication intent. Disposal is a decision recorded separately, and a proposal that '
             'was never approved leaves no trace in any other table.',
  'columns': [('submission_id', 'TEXT PRIMARY KEY', None),
              ('agent', 'TEXT NOT NULL', 'Domain agent that produced the proposal'),
              ('mission_id', 'TEXT', 'External orchestrator correlation id, when one was supplied'),
              ('kind', 'TEXT NOT NULL', 'What the proposal is about: listing, ranking, scenario or audit'),
              ('evidence_sha256', 'TEXT NOT NULL', 'Hash of the frozen evidence bundle the agent was shown'),
              ('input_sha256', 'TEXT NOT NULL', 'Hash of agent identity plus proposal plus evidence'),
              ('proposal_json', 'TEXT NOT NULL', 'The proposal exactly as submitted'),
              ('validation_json', 'TEXT', 'Why it passed or failed, recorded at submission time'),
              ('state', "TEXT NOT NULL DEFAULT 'pending'", 'pending, approved, rejected, superseded or invalid'),
              ('decided_by', 'TEXT', 'Operator who disposed of it. A model cannot fill this in.'),
              ('decided_at', 'TIMESTAMPTZ', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_agent_submission_input', ['input_sha256'])],
  'checks': [('ck_agent_submission_state', 'state', ['pending', 'approved', 'rejected', 'superseded', 'invalid'])]},
 {'name': 'agent_authorizations',
  'comment': 'Operator-signed records for agent capabilities that transfer data off this machine. '
             'Absent a confirmed row the capability is refused, which is the same shape as the '
             'collection authorization registry: the default is a refusal, not a prompt.',
  'columns': [('authorization_id', 'TEXT PRIMARY KEY', None),
              ('capability', 'TEXT NOT NULL', 'What is being authorised, e.g. cloud_llm'),
              ('scope', 'TEXT NOT NULL', 'Data classes this authorisation covers'),
              ('status', "TEXT NOT NULL DEFAULT 'pending'", 'pending, confirmed or revoked'),
              ('evidence_ref', 'TEXT NOT NULL', 'Written record backing the decision'),
              ('confirmed_at', 'TIMESTAMPTZ', None),
              ('recorded_at', 'TIMESTAMPTZ NOT NULL', None)],
  'uniques': [('uq_agent_authorization_capability', ['capability'])],
  'checks': [('ck_agent_authorization_state', 'status', ['pending', 'confirmed', 'revoked'])]}]


# Declared beside the tables so the whole shape is in one place, and rendered
# into both dialects rather than applied as SQLite-only strings at init time.
#
# Order matters only for readability; `IF NOT EXISTS` makes application
# idempotent. The three `listings_*` entries at the end are the ones the
# dashboard pays for on every poll -- see the note on each.
# Operator control and telemetry are additive to the existing cycle journal.
TABLES.extend([
    {'name': 'collection_profiles', 'comment': 'Immutable operator settings, without credentials.',
     'columns': [('profile_id', 'TEXT PRIMARY KEY', None), ('created_at', 'TIMESTAMPTZ NOT NULL', None),
                 ('settings_json', 'TEXT NOT NULL', None)], 'uniques': []},
    {'name': 'collection_control', 'comment': 'Persistent operator mode and active settings revision.',
     'columns': [('collector_key', 'TEXT PRIMARY KEY', None), ('profile_id', 'TEXT NOT NULL REFERENCES collection_profiles(profile_id)', None),
                 ('revision', 'INTEGER NOT NULL', None), ('collection_enabled', 'BOOLEAN NOT NULL DEFAULT TRUE', None),
                 ('updated_at', 'TIMESTAMPTZ NOT NULL', None)], 'uniques': []},
    {'name': 'collection_commands', 'comment': 'Idempotent accepted commands and worker acknowledgements.',
     'columns': [('command_id', 'TEXT PRIMARY KEY', None), ('client_token', 'TEXT NOT NULL', None),
                 ('collector_key', 'TEXT NOT NULL', None), ('command', 'TEXT NOT NULL', None),
                 ('run_id', 'TEXT REFERENCES collection_cycles(run_id) ON DELETE SET NULL', None),
                 ('accepted_at', 'TIMESTAMPTZ NOT NULL', None), ('applied_at', 'TIMESTAMPTZ', None),
                 ('status', 'TEXT NOT NULL', None), ('reason', 'TEXT', None), ('payload_json', 'TEXT NOT NULL', None)],
     'uniques': [('uq_collection_command_token', ['collector_key', 'client_token'])]},
    {'name': 'collection_events', 'comment': 'Ordered bounded operational evidence. No source text or credentials.',
     'columns': [('event_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
                 ('run_id', 'TEXT NOT NULL REFERENCES collection_cycles(run_id) ON DELETE CASCADE', None),
                 ('job_run_id', 'TEXT REFERENCES collection_job_runs(job_run_id) ON DELETE CASCADE', None),
                 ('at', 'TIMESTAMPTZ NOT NULL', None), ('severity', 'TEXT NOT NULL', None),
                 ('event_type', 'TEXT NOT NULL', None), ('payload_json', 'TEXT NOT NULL', None)], 'uniques': []},
    {'name': 'collection_run_observations', 'comment': 'Immutable normalized evidence attributed atomically to its producing run.',
     'columns': [('attribution_pk', 'BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY', None),
                 ('run_id', 'TEXT NOT NULL REFERENCES collection_cycles(run_id) ON DELETE CASCADE', None),
                 ('job_run_id', 'TEXT NOT NULL REFERENCES collection_job_runs(job_run_id) ON DELETE CASCADE', None),
                 ('listing_pk', 'BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE', None),
                 ('commit_key', 'TEXT NOT NULL', None), ('observed_at', 'TIMESTAMPTZ NOT NULL', None),
                 ('kind', 'TEXT NOT NULL', None), ('snapshot_json', 'TEXT NOT NULL', None)],
     'uniques': [('uq_collection_attribution', ['job_run_id', 'listing_pk', 'commit_key'])]},
])


TABLES.extend([
    {'name': 'auth_users', 'comment': 'Application identities, independent of marketplace seller connections.',
     'columns': [('user_id', 'TEXT PRIMARY KEY', None), ('username', 'TEXT NOT NULL', None),
                 ('password_hash', 'TEXT NOT NULL', None), ('role', 'TEXT NOT NULL', None),
                 ('active', 'BOOLEAN NOT NULL DEFAULT TRUE', None),
                 ('must_change_password', 'BOOLEAN NOT NULL DEFAULT TRUE', None),
                 ('created_at', 'TIMESTAMPTZ NOT NULL', None), ('updated_at', 'TIMESTAMPTZ NOT NULL', None)],
     'uniques': [('uq_auth_username', ['username'])], 'checks': [('ck_auth_role', 'role', ['developer', 'viewer'])]},
    {'name': 'auth_sessions', 'comment': 'Revocable sessions. Only a digest of the browser credential is retained.',
     'columns': [('token_hash', 'TEXT PRIMARY KEY', None), ('user_id', 'TEXT NOT NULL REFERENCES auth_users(user_id)', None),
                 ('csrf_token', 'TEXT NOT NULL', None), ('created_at', 'TIMESTAMPTZ NOT NULL', None),
                 ('last_seen_at', 'TIMESTAMPTZ NOT NULL', None), ('expires_at', 'TIMESTAMPTZ NOT NULL', None),
                 ('revoked_at', 'TIMESTAMPTZ', None)], 'uniques': []},
    {'name': 'auth_login_attempts', 'comment': 'Bounded account login throttling without retaining submitted names or passwords.',
     'columns': [('bucket', 'TEXT PRIMARY KEY', None), ('failures', 'INTEGER NOT NULL', None),
                 ('window_start', 'TIMESTAMPTZ NOT NULL', None)], 'uniques': []},
    {'name': 'auth_events', 'comment': 'Application security activity without passwords, cookies or request bodies.',
     'columns': [('event_id', 'TEXT PRIMARY KEY', None), ('actor_id', 'TEXT', None), ('target_id', 'TEXT', None),
                 ('event_type', 'TEXT NOT NULL', None), ('occurred_at', 'TIMESTAMPTZ NOT NULL', None)], 'uniques': []},
])


TABLES.extend([
    {'name': 'cloud_memberships', 'comment': 'Roles for the single private organisation workspace.',
     'columns': [('user_id', 'TEXT PRIMARY KEY REFERENCES auth_users(user_id)', None),
                 ('workspace_id', "TEXT NOT NULL DEFAULT 'default'", None), ('role', 'TEXT NOT NULL', None)],
     'uniques': [], 'checks': [('ck_cloud_role', 'role', ['developer', 'seller', 'viewer'])]},
    {'name': 'cloud_jobs', 'comment': 'Durable worker requests. Interrupted writes require explicit retry.',
     'columns': [('job_id', 'TEXT PRIMARY KEY', None), ('actor_id', 'TEXT NOT NULL', None),
                 ('kind', 'TEXT NOT NULL', None), ('status', 'TEXT NOT NULL', None),
                 ('payload_json', 'TEXT NOT NULL', None), ('result_json', 'TEXT', None),
                 ('created_at', 'TIMESTAMPTZ NOT NULL', None), ('started_at', 'TIMESTAMPTZ', None),
                 ('finished_at', 'TIMESTAMPTZ', None), ('heartbeat_at', 'TIMESTAMPTZ', None),
                 ('worker_id', 'TEXT', None), ('error', 'TEXT', None)], 'uniques': []},
    {'name': 'cloud_artifacts', 'comment': 'Private model artifacts shared by web and worker. Never served to browsers.',
     'columns': [('artifact_key', 'TEXT PRIMARY KEY', None), ('content_base64', 'TEXT NOT NULL', None),
                 ('sha256', 'TEXT NOT NULL', None), ('updated_at', 'TIMESTAMPTZ NOT NULL', None)], 'uniques': []},
])

INDEXES = [
    ('cloud_job_queue', 'cloud_jobs', False, ['status', 'created_at']),
    ('auth_sessions_user', 'auth_sessions', False, ['user_id']),
    ('auth_events_time', 'auth_events', False, ['occurred_at']),
    ('listing_identity', 'listings', True, ['platform', 'listing_id']),
    ('followup_due', 'followup_tasks', False, ['state', 'due_at']),
    ('relationship_claim', 'listing_relationship_events', False, ['claim_id', 'recorded_at']),
    ('relationship_left', 'listing_relationship_events', False, ['left_pk']),
    ('relationship_right', 'listing_relationship_events', False, ['right_pk']),
    ('followup_listing', 'followup_enrollments', False, ['listing_pk']),
    ('history_enrollments', 'followup_enrollments', False, ['enrolled_at', 'listing_pk']),
    ('sweep_due', 'sweep_targets', False, ['platform', 'active', 'next_due_at']),
    ('search_attribution_listing', 'listing_search_attributions', False, ['listing_pk']),
    ('enrichment_due', 'enrichment_tasks', False, ['state', 'due_at']),
    ('enrichment_input', 'enrichment', False, ['platform', 'task', 'input_sha256']),
    ('enrichment_vocabulary_semantic', 'enrichment_vocabularies', False, ['semantic_sha256', 'identity_version']),
    ('collection_events_run', 'collection_events', False, ['run_id', 'event_pk']),
    ('collection_commands_pending', 'collection_commands', False, ['collector_key', 'status', 'accepted_at']),
    ('collection_data_run', 'collection_run_observations', False, ['run_id', 'attribution_pk']),
    ('collection_recent', 'collection_cycles', False, ['collector_key', 'started_at']),
    ('collection_jobs_active', 'collection_job_runs', False, ['status', 'lease_until']),
    ('collection_jobs_recent', 'collection_job_runs', False, ['job', 'started_at']),
    ('classification_changed', 'classification_changes', False, ['changed_at', 'listing_pk']),
    ('history_events', 'events', False, ['listing_pk', 'observed_at']),
    ('history_open', 'aggregate_partitions', False, ['state', 'kind', 'period_day']),
    ('prediction_price_paths', 'price_history', False, ['listing_pk', 'valid_from']),
    ('measured_price_paths', 'price_observations', False, ['listing_pk', 'price_observed_at']),
    ('prediction_seller_links', 'listings', False, ['seller_version_id', 'first_seen_at']),
    ('prediction_sale_views', 'sale_predictions', False,
     ['model_version', 'horizon_days', 'computed_at', 'listing_pk']),
    ('prediction_price_views', 'price_trajectories', False,
     ['model_version', 'day', 'computed_at', 'listing_pk']),
    ('prediction_trend_views', 'category_trends', False,
     ['category_norm', 'week_start', 'computed_at', 'trend_pk']),
    ('prediction_forecast_retention', 'category_trend_forecasts', False,
     ['category_norm', 'region_norm', 'model_version', 'origin_week_start']),
    ('prediction_trend_forecast_views', 'category_trend_forecasts', False,
     ['category_norm', 'origin_week_start', 'horizon_weeks', 'computed_at']),
    ('prediction_concentration_views', 'market_concentration', False,
     ['category_norm', 'month_start', 'computed_at', 'concentration_pk']),

    # Hot paths for the analysis layer. Measured on a copy of the live database
    # (32k listings): municipality and category grouping 12x, the active-window
    # filter 11x, the outcome filter large enough that it changed the shape of
    # the query. All four were full table scans before.
    #
    # Deliberately NOT indexed: the `_freshness_key` aggregate in
    # `dashboard._freshness_key`. It is COUNT(*)/MAX/SUM over unqualified
    # columns, so it has to read every row and no B-tree can avoid that -- the
    # measured cost is unchanged with these indexes present. Indexing it would
    # have been the plausible-looking fix that does nothing; making it cheap is
    # a different change (see the note on `_ledger_freshness_key`).
    ('listings_active_window', 'listings', False, ['status_norm', 'coverage', 'last_seen_at']),
    ('listings_municipality', 'listings', False, ['municipality_norm']),
    ('listings_category_root', 'listings', False, ['category_root_norm']),
    ('listings_outcome', 'listings', False, ['outcome_status', 'outcome_observed_at']),

    # Covering index for the dashboard's freshness aggregate. This one is not
    # about finding rows -- nothing filters on it -- but about reading them
    # without touching the table. `COUNT`/`MAX`/`SUM` over unqualified columns
    # cannot avoid a full scan, so the only lever is making the scan cheap: with
    # every column the aggregate touches present in one narrow index, SQLite
    # reads the index instead of the (much wider) table.
    #
    # Measured on the live database at 33k rows: 33.3ms -> 8.5ms, and the plan
    # changes from `SCAN listings` to `SCAN listings USING COVERING INDEX`.
    # Column order matches the aggregate rather than any filter, because there
    # is no filter.
    ('listings_freshness_scan', 'listings', False,
     ['last_seen_at', 'outcome_observed_at', 'last_probed_at', 'outcome_status']),

    # Agent quarantine reads. `agent_agent_state` matches the disposal queue
    # exactly: state, then oldest first, so an operator sees the proposals that
    # have been waiting longest rather than the most recent.
    ('agent_submission_state', 'agent_submissions', False, ['state', 'recorded_at']),
    ('agent_submission_mission', 'agent_submissions', False, ['mission_id']),
]


# P4 adds evidence to the existing owned ledger, never another sale ledger.
for _table in TABLES:
    if _table["name"] in ("transactions", "order_lines"):
        _table["columns"].append(("resale_pairing_json", "TEXT", "Frozen pre-sale asking and identity evidence, NULL for unpaired legacy records"))
TABLES.extend([
    {"name": "internet_sources", "comment": "Operator-selected public research URLs, independent of marketplace and financial evidence.",
     "columns": [("source_id", "TEXT PRIMARY KEY", None), ("listing_pk", "BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE", None),
                 ("url", "TEXT NOT NULL", None), ("kind", "TEXT NOT NULL", None), ("label", "TEXT NOT NULL", None),
                 ("interval_hours", "INTEGER NOT NULL", None), ("watch_enabled", "BOOLEAN NOT NULL DEFAULT FALSE", None),
                 ("review_updates", "BOOLEAN NOT NULL DEFAULT FALSE", None),
                 ("next_due_at", "TIMESTAMPTZ NOT NULL", None), ("created_at", "TIMESTAMPTZ NOT NULL", None), ("updated_at", "TIMESTAMPTZ NOT NULL", None),
                 ("last_attempt_at", "TIMESTAMPTZ", None), ("last_success_at", "TIMESTAMPTZ", None), ("last_status", "TEXT", None), ("last_error", "TEXT", None)],
     "uniques": [("uq_internet_source_url", ["listing_pk", "url"])],
     "checks": [("ck_internet_source_kind", "kind", ["asking_price", "product_specs", "fee_terms", "research"])]},
    {"name": "internet_runs", "comment": "Monitored bounded fetch and local interpretation jobs, retaining frozen evidence and provenance.",
     "columns": [("run_id", "TEXT PRIMARY KEY", None), ("listing_pk", "BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE", None),
                 ("operation", "TEXT NOT NULL", None), ("origin", "TEXT NOT NULL", None), ("status", "TEXT NOT NULL", None),
                 ("owner_pid", "INTEGER NOT NULL", None), ("slot_key", "TEXT", None), ("created_at", "TIMESTAMPTZ NOT NULL", None),
                 ("completed_at", "TIMESTAMPTZ", None), ("request_json", "TEXT NOT NULL", None), ("evidence_json", "TEXT", None),
                 ("evidence_revision", "TEXT", None), ("model_json", "TEXT", None), ("result_json", "TEXT", None), ("review_json", "TEXT", None), ("error", "TEXT", None)],
     "uniques": [("uq_internet_run_slot", ["slot_key"])],
     "checks": [("ck_internet_run_status", "status", ["queued", "running", "complete", "partial", "unavailable", "invalid", "error", "interrupted"])]},
    {"name": "internet_observations", "comment": "Bounded public-page extracts. Published asks and reference material never establish sales.",
     "columns": [("observation_id", "TEXT PRIMARY KEY", None), ("source_id", "TEXT NOT NULL REFERENCES internet_sources(source_id) ON DELETE CASCADE", None),
                 ("run_id", "TEXT NOT NULL REFERENCES internet_runs(run_id) ON DELETE CASCADE", None),
                 ("retrieved_at", "TIMESTAMPTZ NOT NULL", None), ("record_json", "TEXT NOT NULL", None)],
     "uniques": [], "checks": []},
    {"name": "acquisition_attempts", "comment": "Immutable completed acquisition attempts, explicitly linked to an actual owned purchase on success.",
     "columns": [("attempt_id", "TEXT PRIMARY KEY", None), ("client_token", "TEXT NOT NULL", None),
                 ("request_sha256", "TEXT NOT NULL", None),
                 ("decision_pk", "BIGINT NOT NULL REFERENCES research_decisions(decision_pk)", None),
                 ("purchase_pk", "BIGINT REFERENCES purchases(purchase_pk)", None),
                 ("status", "TEXT NOT NULL", None), ("reason", "TEXT NOT NULL", None),
                 ("evidence_ref", "TEXT NOT NULL", None), ("occurred_at", "TIMESTAMPTZ NOT NULL", None),
                 ("recorded_at", "TIMESTAMPTZ NOT NULL", None)],
     "uniques": [("uq_attempt_token", ["client_token"]), ("uq_attempt_purchase", ["purchase_pk"])],
     "checks": [("ck_attempt_status", "status", ["acquired", "declined", "unavailable", "failed"])]},
    {"name": "owned_outcome_events", "comment": "Append-only work, completeness and cash receipts referencing the existing financial ledger.",
     "columns": [("event_pk", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", None),
                 ("client_token", "TEXT NOT NULL", None), ("request_sha256", "TEXT NOT NULL", None),
                 ("purchase_pk", "BIGINT REFERENCES purchases(purchase_pk)", None),
                 ("decision_pk", "BIGINT REFERENCES research_decisions(decision_pk)", None),
                 ("event_type", "TEXT NOT NULL", None), ("payload_json", "TEXT NOT NULL", None),
                 ("evidence_ref", "TEXT NOT NULL", None), ("effective_at", "TIMESTAMPTZ NOT NULL", None),
                 ("recorded_at", "TIMESTAMPTZ NOT NULL", None)],
     "uniques": [("uq_owned_outcome_token", ["client_token"])], "checks": []},
])

INDEXES.extend([
    ("internet_sources_due", "internet_sources", False, ["watch_enabled", "next_due_at"]),
    ("internet_observations_latest", "internet_observations", False, ["source_id", "retrieved_at"]),
    ("internet_runs_listing", "internet_runs", False, ["listing_pk", "created_at"]),
])

TABLES.append({"name":"listing_analysis_inputs",
    "comment":"Short-lived bounded local product text, with durable source revision hashes after expiry.",
    "columns":[("listing_pk","BIGINT NOT NULL REFERENCES listings(listing_pk) ON DELETE CASCADE",None),
               ("source","TEXT NOT NULL",None),("content_sha256","TEXT NOT NULL",None),
               ("input_json","TEXT",None),("observed_at","TIMESTAMPTZ NOT NULL",None),
               ("expires_at","TIMESTAMPTZ NOT NULL",None)],
    "uniques":[("uq_listing_analysis_input",["listing_pk","source"])],"checks":[]})
INDEXES.append(("listing_analysis_expiry","listing_analysis_inputs",False,["expires_at"]))


def index_ddl(dialect):
    """One CREATE INDEX statement per declared index, for either backend.

    The declarations are dialect-neutral already, so both renderers share this
    rather than each maintaining its own list -- which is how they drifted when
    the indexes lived as strings in `db.init_db` and never reached
    `sql/schema.postgres.sql` at all.
    """
    out = []
    for iname, itable, unique, columns in INDEXES:
        kind = "UNIQUE INDEX" if unique else "INDEX"
        cols = ", ".join(columns)
        out.append(f"CREATE {kind} IF NOT EXISTS {iname} ON {itable} ({cols});")
    return "\n".join(out)


# Bumped only when a migration is added below. `db.migrate` walks from the
# version recorded in `schema_meta` up to this number, so a deployment that
# skipped a release applies the steps it missed rather than assuming it is
# current.
SCHEMA_VERSION = 13


def _migration_001_add_column_audit(conn):
    """Baseline. `db.migrate` reconciles ADD COLUMN unconditionally.

    Historical note, kept because it is the reason the mechanism exists: until
    migration 1 there was no version record and no way to tell a database at the
    declared shape from one that merely had not been touched recently. Databases
    created before this shipped are stamped with version 1 on first contact
    after their columns have been reconciled, which is the strongest claim this
    codebase can honestly make about them -- see `db.migrate`.
    """
    return None


def _migration_002_measurement_purposes(conn):
    """Additive shape was reconciled before this step. Preserve legacy history.

    NULL purpose remains interpretable through mode, so no historical selection
    probability, enrollment date or cohort membership is rewritten.
    """
    return None


def _migration_003_owned_outcomes(conn):
    """Additive evidence only. Existing trades and source history stay intact."""
    return None


# (version, name, callable(conn)). Append only. Never renumber, never edit an
# applied entry: `db.migrate` refuses to run when the list is not a dense
# ascending run from 1, because a gap means an already-applied database would
# skip a step forever.
def _migration_004_collection_control(conn):
    """New tables are created by init_db before additive reconciliation."""
    return None


def _migration_005_internet_research(conn):
    """Additive source registry, observations and jobs; existing ledgers untouched."""
    return None


def _migration_006_listing_analysis(conn):
    """Add bounded analysis inputs. Existing source and inspection facts are preserved."""
    return None


def _migration_007_arrival_validation(conn):
    """Add forecast evidence without upgrading legacy projections to validated."""
    return None


def _migration_008_trend_evidence(conn):
    """Add missing-value and direction evidence without rewriting observed history."""
    return None


def _migration_009_price_forecast_inputs(conn):
    """Retain forecast inputs without fabricating provenance for legacy rows."""
    return None


def _migration_010_short_forecast_horizons(conn):
    """Extend the horizon constraint while preserving every stored forecast."""
    table = next(t for t in TABLES if t["name"] == "sale_predictions")
    expression = "horizon_days IN (" + _sql_list(table["checks"][0][2]) + ")"
    if conn.dialect == "postgres":
        with conn.transaction():
            conn.execute("ALTER TABLE sale_predictions DROP CONSTRAINT IF EXISTS ck_sale_pred_horizon")
            conn.execute("ALTER TABLE sale_predictions ADD CONSTRAINT ck_sale_pred_horizon CHECK (" + expression + ")")
        return
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sale_predictions'").fetchone()
    if row is None or "".join(expression.lower().split()) in "".join(row["sql"].lower().split()):
        return
    with conn.transaction():
        objects = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='sale_predictions' "
                               "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='sale_predictions'").fetchone()
        create = next(s for s in render_sqlite(include_indexes=False).split(";")
                      if "CREATE TABLE IF NOT EXISTS sale_predictions (" in s)
        conn.execute(create.replace("CREATE TABLE IF NOT EXISTS sale_predictions (",
                                    "CREATE TABLE sale_predictions_short_horizons (", 1))
        columns = ",".join(c[0] for c in table["columns"])
        conn.execute(f"INSERT INTO sale_predictions_short_horizons ({columns}) SELECT {columns} FROM sale_predictions")
        conn.execute("DROP TABLE sale_predictions")
        conn.execute("ALTER TABLE sale_predictions_short_horizons RENAME TO sale_predictions")
        for obj in objects:
            conn.execute(obj["sql"])
        if sequence is not None:
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='sale_predictions'", (sequence["seq"],))


def _migration_011_application_accounts(conn):
    """Additive account tables are created by init_db before migration recording."""
    conn.execute('SELECT user_id FROM auth_users LIMIT 1')


def _migration_012_agent_bridge(conn):
    """Additive quarantine tables are created by init_db before migration recording."""
    conn.execute('SELECT submission_id FROM agent_submissions LIMIT 1')
    conn.execute('SELECT authorization_id FROM agent_authorizations LIMIT 1')


def _migration_013_cloud_workspace(conn):
    """Add separate cloud roles and durable jobs without changing local viewer accounts."""
    conn.execute('SELECT user_id FROM cloud_memberships LIMIT 1')
    conn.execute('SELECT job_id FROM cloud_jobs LIMIT 1')


MIGRATIONS = [
    (1, 'add_column_audit', _migration_001_add_column_audit),
    (2, 'measurement_purposes', _migration_002_measurement_purposes),
    (3, 'owned_outcomes', _migration_003_owned_outcomes),
    (4, 'collection_control', _migration_004_collection_control),
    (5, 'internet_research', _migration_005_internet_research),
    (6, 'listing_analysis', _migration_006_listing_analysis),
    (7, 'arrival_validation', _migration_007_arrival_validation),
    (8, 'trend_evidence', _migration_008_trend_evidence),
    (9, 'price_forecast_inputs', _migration_009_price_forecast_inputs),
    (10, 'short_forecast_horizons', _migration_010_short_forecast_horizons),
    (11, 'application_accounts', _migration_011_application_accounts),
    (12, 'agent_bridge', _migration_012_agent_bridge),
    (13, 'cloud_workspace', _migration_013_cloud_workspace),
]


def _header(table):
    """Comment header, one '--' per line.

    db.init_db splits the DDL on ';' and drops lines starting with '--'. A
    wrapped comment whose continuation lines lack the marker is not a comment,
    it is a syntax error, so every physical line carries it.
    """
    text = table["name"]
    if table.get("comment"):
        text += f": {table['comment']}"
    return "".join(f"-- {line}\n" for line in text.splitlines())


def _sql_list(values):
    return ", ".join(f"'{v}'" for v in values)


_validate_tables()


def render_postgres(include_indexes=True):
    """Full DDL for one backend.

    `include_indexes=False` gives the CREATE TABLE statements alone, which is
    what `db.init_db` needs: the identity index must not be created until after
    the duplicate-identity check, because creating it first turns the
    reconciliation refusal into an opaque UNIQUE violation.
    """
    out = []
    for table in TABLES:
        cols = []
        notes = {}
        for col, decl, note in table["columns"]:
            cols.append(f"  {col} {decl}")
            if note:
                notes[len(cols) - 1] = note
        for cname, column, values in table.get("checks", []):
            cols.append(f"  CONSTRAINT {cname} CHECK ({column} IN ({_sql_list(values)}))")
        for uname, columns in table["uniques"]:
            cols.append(f"  CONSTRAINT {uname} UNIQUE ({', '.join(columns)})")
        lines = [c + "," for c in cols[:-1]] + [cols[-1]]
        for idx, note in notes.items():
            lines[idx] = lines[idx] + f"  -- {note}"
        out.append(_header(table) + f"CREATE TABLE IF NOT EXISTS {table['name']} (\n"
                   + "\n".join(lines) + "\n);")
    if include_indexes:
        out.append("-- Indexes. Declared in schema.INDEXES so a database created from\n"
                   "-- this file is indexed identically to one created by db.init_db.")
        out.append(index_ddl("postgres"))
    return "\n".join(out)


def render_sqlite(include_indexes=True):
    """See `render_postgres` for what `include_indexes` is for."""
    out = []
    for table in TABLES:
        cols = []
        notes = {}
        for col, decl, note in table["columns"]:
            d = decl
            d = d.replace("BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            d = d.replace("BIGINT", "INTEGER")
            for num in ("14,2", "18,8", "5,4", "8,3", "9,6"):
                d = d.replace(f"NUMERIC({num})", "REAL")
            d = d.replace("TIMESTAMPTZ", "TEXT").replace("BOOLEAN", "INTEGER")
            d = d.replace("CURRENT_TIMESTAMP", "(datetime('now'))")
            d = d.replace("DEFAULT now()", "DEFAULT (datetime('now'))")
            cols.append(f"  {col} {d}")
            if note:
                notes[len(cols) - 1] = note
        for cname, column, values in table.get("checks", []):
            cols.append(f"  CONSTRAINT {cname} CHECK ({column} IN ({_sql_list(values)}))")
        for uname, columns in table["uniques"]:
            cols.append(f"  CONSTRAINT {uname} UNIQUE ({', '.join(columns)})")
        lines = [c + "," for c in cols[:-1]] + [cols[-1]]
        for idx, note in notes.items():
            lines[idx] = lines[idx] + f"  -- {note}"
        out.append(_header(table) + f"CREATE TABLE IF NOT EXISTS {table['name']} (\n"
                   + "\n".join(lines) + "\n);")
    if include_indexes:
        out.append("-- Indexes. Declared in schema.INDEXES so a database created from\n"
                   "-- this file is indexed identically to one created by db.init_db.")
        out.append(index_ddl("sqlite"))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "postgres"
    print(render_postgres() if target == "postgres" else render_sqlite())
