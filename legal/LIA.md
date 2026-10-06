# Legitimate Interest Assessment (LIA)

**Subject:** Internal analysis of publicly available Finnish second-hand listings.
**Controller:** [Company]
**Contact for data subject requests:** timanttiliiga666@gmail.com
**Version:** 0.3, 2 October 2026
**Status:** DRAFT — prepared for controller review and sign-off.
**Signed off by:** [Pending]
**Sign-off date:** [Pending]

This version describes the current local configuration. The previous company,
business ID, street address and research.tori.fi publication claims were
unverified. No controller or counsel signature has been recorded. The contact
matches TORI_CONTACT_EMAIL; mailbox delivery still needs operator verification.

## 1. Purpose and scope

The purpose is an internal reference of asking prices, listing durations and
market conditions to inform second-hand purchasing and resale decisions.

Discovery, detail checks and enrichment focus nationwide on these seven
verticals and their descendants:

| Vertical | Category path |
|---|---|
| Pyöräily | 1.69.3963 |
| Muu urheilu | 1.69.3966 |
| Tietotekniikka | 1.93.3215 |
| Puhelimet ja tarvikkeet | 1.93.3217 |
| Valokuvaus ja video | 1.93.3904 |
| Videopelit ja konsolit | 1.93.3905 |
| Ääni ja kuva | 1.93.3906 |

The public national Atom arrival sampler and sitemap/category metadata have
separate coverage. Out-of-focus feed observations do not authorize out-of-scope
detail checks. The owner's existing collection confirmation is recorded
separately in data/authorizations/registry.json; this LIA does not verify the
underlying letter or establish selling/messaging rights.

Public comparison services, onward dataset sale/licensing, seller contact
automation and real-time seller undercutting are outside this assessment.

## 2. Actual data and processing

Stored records include listing identifiers and titles; asking prices and
currency; category, brand/model and condition; municipality; available
publication/edit timestamps and observation times; public availability/disposal
labels, seller type and image count; derived labels, predictions and history.

**Seller linkage is enabled.** The parser reduces a public ownerId to a keyed
HMAC and discards the raw identifier before storage. Stable keys group listings
for seller concentration, archetype and relist analysis. These keys are
**pseudonymous, not anonymous**; linkage and derived activity patterns require
specific consideration of profiling and its effects on sellers.

Public titles, locations, timestamps and linked observations can relate to an
identifiable person. Public availability does not make those records non-personal.

The listing parser excludes seller names/contact details, precise coordinates,
postal/street addresses and raw account identifiers. Search/item response bodies
are not saved. Retained feed/metadata bodies have separate short retention.
Seller profiles, private messages and login-gated pages are not permitted
collection surfaces. Inference is local and restricted to loopback.

The application is also built for a hosted deployment. `toriresearch/cloud_app.py`
is a WSGI adapter that subclasses the dashboard handler and reaches it through a
Netlify gateway; `netlify.toml` routes `/api/*` to that function. In that
configuration the dashboard's loopback-only refusal is overridden, because the
bind is no longer the control. What replaces it, as implemented:

- Requests reach the application only through the gateway, and each one carries
  an HMAC-SHA256 signature over a timestamp, the method, the route and a hash of
  the body, with a 60-second freshness window and a key of at least 48
  characters. An unsigned or stale request is refused with 403 before routing.
- Every session is an authenticated account. `toriresearch/access.py` classifies
  each method and path into `public`, `account`, `read`, `seller` or `developer`,
  and anything unclassified is denied.
- Roles are `developer`, `seller` and `viewer`. The database holds one
  organisation workspace per deployment, and a membership row must match
  `TORI_WORKSPACE_ID` or the request is refused.
- Mutating requests additionally require a CSRF token compared with
  `hmac.compare_digest`.

This narrows the exposure relative to a public comparison frontend: an account is
required, the viewer role cannot reach operational routes, and one workspace is
isolated per deployment. It does not make the deployment loopback-only, and this
assessment should not be read as saying it is.

Two facts that limit the change and are worth stating plainly. The hosted tier
carries the same database as the local runtime, so retention in §4 applies to
hosted copies as well as local ones. And the agent bridge
(`toriresearch/agent_bridge.py`) is deliberately **not** reachable from the
hosted tier: it binds its own loopback listener, is absent from the route tables
the gateway dispatches through, and `access.capability()` denies anything it has
not classified. It has no accounts and no roles because it is a
machine-to-machine surface, not a user route.

**Not established by the code and required before publication:** the hosting
provider and its processing terms, the deployment region and any resulting
transfer, whether sub-processors are engaged, the retention of provider-side
logs and backups, and whether the controller has entered a data processing
agreement. None of that can be read off a repository.

## 3. Proposed basis, necessity and balancing

The proposed basis is legitimate interests under Article 6(1)(f) GDPR. The
controller must assess the actual processing and record its conclusion.

**Interest:** understand price and liquidity conditions for the controller's own
purchasing and resale decisions.

**Necessity:** repeated observations support duration and price-change analysis.
An adequately covered aggregate feed would reduce listing-level collection.
Market price analysis can operate without seller keys; concentration, archetype
and relist analysis use linkage. The controller must separately justify that
additional processing or disable it and address existing linked records.

**Effects on sellers:** repeated collection and stable linkage can reveal activity
patterns sellers may not expect. Titles may contain incidental personal data.
Pseudonymisation reduces direct identification but preserves linkage and possible
profiling. Public availability and platform permission do not settle the balance.

**Safeguards:** private loopback dashboard and inference; source/action scope
checks; exclusion of direct identifiers from stored records; paced requests;
robots enforcement; persistent refusal/rate-limit pauses; scheduled retention;
and support thresholds/coarsening on aggregate partitions. Coarsening alone is
not proof of anonymisation.

**Decision pending:** the controller must consider the purpose, necessity of
linkage, effects on sellers and sufficiency of safeguards. Technical readiness
and the existing platform confirmation are not evidence of this decision.

## 4. Retention and access

| Data | Current configured retention |
|---|---|
| Retained feed/metadata response bodies | 14 days |
| Unlinked listing records and listing children | 400 days since last observation |
| Request audit records | 400 days |
| Managed automatic recovery snapshots | 7 days; restore rehearsal reapplies listing expiry |
| Listings linked to owned purchases/transactions | Preserved with private ledger; separate schedule needs review |
| Manual backups and aggregate/model artifacts | No general automatic expiry established; separate review needed |

Scheduled maintenance applies configured limits. Defaults are not evidence that
the controller approved those periods. Access is intended for the workspace
operator. Additional recipients, hosting, off-host backups or external inference
need review against actual deployment.

**Update, 2026-10-04.** Hosting is no longer hypothetical: the repository builds
a Netlify deployment that reaches the same database through a signed gateway.
That makes two of the sentences above stale as present-tense fact. Specifically:

- "Access is intended for the workspace operator" now understates it. Access is
  granted to authenticated accounts in three roles, in a single workspace per
  deployment, and every route is allowlisted. The safeguards are described in
  §2 and are real; the single-operator framing is no longer accurate.
- "hosting ... need review against actual deployment" has now happened and the
  outcome is recorded above. What the review cannot settle from the repository is
  the provider relationship itself: processing terms, region, sub-processors,
  provider-side retention and whether a processing agreement exists.

The retention table above describes configured application-level limits. It says
nothing about how long the hosting provider retains its own copies, and no
provider-side figure has been recorded. Treat the table as the application's
schedule only.

Inference remains local and loopback-only in both deployments. The agent bridge
is loopback-only and unreachable from the hosted tier. No external inference
provider is configured, and none is authorised.

## 5. Notice, objections and remaining risks

legal/ART14_NOTICE.md is the matching draft. No public notice URL or delivery
evidence is recorded. A local file or a User-Agent email does not establish
fulfillment of Article 14.

The configured email is the proposed route for access, correction, erasure,
restriction and objections. No automated listing-suppression workflow or
end-to-end request-handling test was found. Before representing an opt-out as
operational, establish and test a procedure to pause affected collection, locate
records, handle applicable deletion/restriction and prevent unwanted recollection.

Remaining risks include unintended profiling, re-identification, an unpublished
notice, untested request handling and retention exceptions. Platform authority
and robots controls do not establish a GDPR lawful basis. This assessment makes
no estimate of legal exposure or claim of completed counsel review.

## 6. Review and sign-off

- [ ] Confirm the controller's actual legal name and monitored contact address.
- [ ] Accept the purpose and seven-vertical scope, including separate Atom coverage.
- [ ] Decide and document necessity and balancing for enabled seller linkage.
- [ ] Approve retention and set schedules for ledger links, backups and artifacts.
- [ ] Record how and when the Article 14 notice will be provided.
- [ ] Test request handling and prevention of recollection.
- [ ] Record any counsel review with the actual reviewer, date and scope.
- [ ] Record the balancing conclusion and acceptance of remaining risks.

After actual review, set the status above to SIGNED OFF, enter the responsible
signer's name and an ISO sign-off date (YYYY-MM-DD), and record the conclusion.
Do not sign for an unconfirmed controller or describe operator approval as
counsel review.

**Balancing conclusion:** [Pending controller decision]
**Counsel review:** Not recorded.
**Notice publication/delivery evidence:** Not recorded.

Reference: [GDPR, Articles 5, 6, 12, 14 and 21](https://eur-lex.europa.eu/eli/reg/2016/679/oj).
