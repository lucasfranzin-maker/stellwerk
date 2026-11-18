# CLAUDE.md – Design & UI Architecture for "Stellwerk"

## 1. Product Context & Visual Identity
**Stellwerk** (German for *interlocking signal box / control tower*) is a high-precision healthcare data integration and interoperability workbench. It automates pipelines from legacy/tabular sources (HL7 v2, SQL databases, CSV/XML) into structured **FHIR Compositions** targeting a **FHIR Clinical Data Repository (CDR)**, supported by an AI assistant for automated schema discovery and terminology mapping ("Datenerschließung").

**Design Archetype:** Modern Control Plane & Data Observability (inspired by Linear, Supabase Studio, Raycast, and electronic interlocking panels).
**Core Vibe:** Authoritative, calm, dense, razor-sharp, deterministic. Like a modern digital signal box, every route, switch, and validation state must be unambiguous at a glance. *(Note: Keep the aesthetic strictly modern software/IDE — never use literal retro railway graphics or skeuomorphic gauges).*

---

## 2. Strict Anti-Slop & Negative Rules (CRITICAL)
NEVER use any of the following patterns:
- **No AI Clichés:** Zero purple/blue/pink gradients, no glowing neon borders, no floating sparkle icons, no conversational chat-bubble UIs taking over the screen.
- **No Healthcare Clichés:** Avoid sterile hospital-teal/cyan themes, medical cross icons, or soft rounded "wellness" aesthetics.
- **No Card Bloat:** Do not wrap every metric or list item in heavy drop-shadow cards (`shadow-md`, `shadow-lg`). Separate content using `1px` hairline borders and background contrast.
- **No Oversized Radii:** Do not use `rounded-2xl` or `rounded-full` for containers or standard buttons. Use crisp technical radii (`4px` to `6px`).
- **No Empty Whitespace Waste:** Interface engineers need context. Favor compact, scannable split-panes and dense data tables over giant padded hero blocks.

---

## 3. Color System (Strict Neutral + Signal Accents)
Use a pure **Zinc/Neutral** grayscale foundation (avoid cold blue-tinted Slate) so syntax highlighting, routing connectors, and mapping states remain readable without eye strain. Support a crisp **Light Mode** as default with an effortless **Dark Mode** equivalent:

### Surface & Border Tokens
- **App Canvas:** `#FAFAFA` (Light) / `#09090B` (Dark)
- **Primary Panel / Editor Surface:** `#FFFFFF` (Light) / `#111113` (Dark)
- **Subtle Elevated Surface (Headers, Sidebars, Table Headers):** `#F4F4F5` (Light) / `#18181B` (Dark)
- **Hairline Borders:** `1px solid #E4E4F7` (Light) / `1px solid #27272A` (Dark)
- **Active/Focused Border:** `#A1A1AA` (Light) / `#52525B` (Dark)

### Text Hierarchy
- **Primary Text:** `#09090B` (Light) / `#FAFAFA` (Dark)
- **Secondary / Metadata Text:** `#52525B` (Light) / `#A1A1AA` (Dark)
- **Muted / Disabled / Line Numbers:** `#A1A1AA` (Light) / `#52525B` (Dark)

### Functional & Signal Accents (Use Sparingly)
- **Primary Brand / Routing Accent:** Industrial Signal Amber (`#D97706` Light / `#F59E0B` Dark) or Deep Technical Cobalt (`#1D4ED8` Light / `#3B82F6` Dark) — use strictly for primary CTAs, active pipeline routing lines, and active tab indicators.
- **Clear / Validated FHIR State:** Emerald (`#059669` text on `#ECFDF5` subtle tint with `1px` border).
- **Unmapped / Warning / Cardinality Mismatch:** Amber (`#B45309` text on `#FFFBEB` subtle tint).
- **Blocked / Validation Failure (FHIR Profile):** Rose (`#BE123C` text on `#FFF1F2` subtle tint).
- **AI Discovery ("Datenerschließung"):** Subtle Indigo (`#4338CA` text on `#EEF2FF` tint, `1px` border) — used exclusively for AI-proposed mappings, confidence scores (e.g., `94%`), and inline diff suggestions.

---

## 4. Typography & Data Formatting
- **UI Sans-Serif:** `Geist` or `Inter` (`font-feature-settings: "cv02", "cv03", "cv04", "cv11"`).
  - Page Titles: `18px`–`20px`, `font-weight: 600`, tight tracking (`-0.02em`).
  - Section / Panel Headers: `13px`, `font-weight: 600`, uppercase or crisp sentence case.
  - Body / Labels: `13px`–`14px`, `font-weight: 400` / `500`.
- **Code & Schema Monospace (Mandatory for Data):** `JetBrains Mono` or `Geist Mono` (`12px`–`13px`).
  - ALWAYS render HL7 segments (`MSH-9`, `OBX-5`), SQL table/column names (`dbo.pat_lab_results.val_num`), FHIR resource paths (`Composition.section[0].entry`), LOINC/SNOMED codes, and JSON payloads in Monospace.
- **Numbers & Metrics:** Enforce `font-variant-numeric: tabular-nums` on all row counts, latency metrics, throughput rates, and AI confidence percentages.

---

## 5. Layout & Component Rules
- **Workbench Architecture:** Build around a resizable **3-Pane IDE Layout**:
  1. *Left Pane:* Source Schema Explorer (HL7 Message Tree / SQL Tables & Columns).
  2. *Center Pane:* Routing Switchboard, Transformation Rules & AI Discovery Workbench.
  3. *Right Pane:* Live FHIR Composition JSON / Resource Validator & CDR Preview.
- **Elevation & Shadows:** Static panels have `box-shadow: none` and rely strictly on `1px` borders. Reserve subtle shadows (`0 4px 12px rgba(0,0,0,0.08)`) exclusively for floating elements: Command Palette (`Cmd+K`), dropdown menus, tooltips, and modals.
- **Tables & Lists:** Compact row height (`32px`–`36px`), sticky headers, subtle hover highlight (`#F4F4F5` / `#18181B`), full keyboard navigation support.
- **Iconography:** Use **Lucide Icons** exclusively at `14px`–`16px` with a consistent `1.5px` stroke width.

---

## 6. AI UX Guidelines ("Datenerschließung")
The AI assists interface developers in analyzing unknown SQL schemas or HL7 feeds and proposing FHIR mappings:
- **Inline Diffs over Chat:** Present AI proposals directly inside the mapping table as ghost/proposed rows with a confidence badge (e.g., `AI · 96%`) and one-click `Accept (Tab)` / `Reject` actions.
- **Explainable Reasoning:** Show *why* a field was matched in a compact, collapsible metadata line (e.g., *"Matched column `krea_wert` + unit `mg/dl` → LOINC `2160-0` (Creatinine)"*).
- **No Anthropomorphism:** Keep AI copy factual, terse, and technical. No "I found a great match for you!" — write *"Proposed mapping based on column semantics and sample values."*

---

## 7. Implementation & Workflow Guardrails
- Before building complex views, establish CSS custom properties / Tailwind theme tokens matching Section 3 & 4.
- When creating components, build reusable primitives first: `StatusBadge`, `SchemaTreeNode`, `FhirPathPill`, `ConfidenceTag`, `SplitWorkbench`.
- Every interactive control (buttons, table rows, mapping connectors, inputs) must have explicit `:hover`, `:focus-visible`, and `:disabled` states.

---

## 7a. Visual system (2026-11-14, supersedes Sections 2–5 and the Emergent mockup where they conflict)
The owner rejected the generic "AI-made SaaS" look. Do not drift back to it. Banned: tinted pill badges, rounded shadow cards on a grey canvas, navy sidebar with teal/indigo accents, Inter/Geist, all-caps eyebrow labels, strings joined with middle dots, decorative numbering.

- **Type:** Barlow Semi Condensed for the interface (DIN-like signage character, dense), IBM Plex Mono only for real data (paths, codes, values, payloads). Both are bundled under `src/stellwerk/web/fonts/` (OFL) and served locally. No web fonts from the network.
- **Surfaces:** three tones instead of cards: filing (steel `#E2E5E8`), source (`#EEF0F2`), work surface (white), output (dark `#17191C`). No panel shadows, no panel radius; controls have 3px radius. A 2px black rule under the header.
- **Colour is signal only:** green `#00875A`, yellow `#E5A800`, red `#D4002A`, blue `#0057D9` (selection, proposals). Text is black. Primary action is a black button.
- **State = lamp + word:** a 9px square in the signal colour followed by a plain word (`zugeordnet`, `Zu prüfen`). Proposals have a hollow blue lamp.
- **Signature element:** every mapping row is a route from source to target: solid line with arrow = set, dashed blue = proposal, yellow stub with buffer stop = open, short grey stub = left out. Proposal rows carry a fine diagonal hatch, never a colour fill.
- **Copy:** sentence case, plain German, no jargon for its own sake.
- Tokens are CSS custom properties at the top of `src/stellwerk/web/index.html`.
- The workbench is **one HTML file without a build step**, served by the Python service. No CDN, no Node: it must run in an air-gapped hospital network. Icons are inline SVG paths.
- Layout: explorer tree (user-defined folders and tasks) | source | mapping | FHIR output. Model proposals appear inline in the mapping pane with Accept/Reject, never as chat.
- Backlog: pipeline designer.

## 7b. Architecture (decided 2026-11-14)
- **Built:** FastAPI app in `app.py` (`create_app(cfg)`), SQLAlchemy 2 models and `Store` in `db.py`, Alembic migrations in `stellwerk/migrations` (applied on start; add a new revision for every schema change, never edit `0001`). SQLite on-premise, PostgreSQL in the cloud. Dockerfile and `compose.yaml`. Operational docs: `docs/betrieb.md`.
- The run log (`lauf`) and audit log never contain patient data or file names: only channel, system name, status, durations, finding codes.
- Database addresses with a literal password are refused; passwords come from `${STELLWERK_DB_*}` placeholders.
- Frontend becomes a **React** app. Built assets are committed/packaged so a hospital install needs no Node and no network. This supersedes "one HTML file without a build step" once the migration lands; the visual system in 7a stays.
- **Login via OIDC** (`auth.py`, `docs/anmeldung.md`): Authorization Code + PKCE, ID token fully verified (RS256 only, pure Python, checked against `cryptography` in tests), roles `lesen` < `bearbeiten` < `verwalten` from a group claim, server-side sessions (cookie value never stored, only its SHA-256), service token for `/v1/*`. No local password store. Access rules live in one table (`auth.REGELN`), deny by default; **every new route needs a rule** (`test_jede_route_hat_eine_regel`). State-changing requests from the UI must send `X-Stellwerk-Anfrage`. Without login the service only starts on loopback. Tested against a fake IdP (`tests/idp.py`) and a real Keycloak 26.8.2, not against AD FS / Entra.
- **Database targets** (`workbench.check_db_target`): the service connects with passwords from its environment, so an address pointing elsewhere would leak them. `quellen.erlaubte_hosts` binds everyone; without it only `verwalten` may enter a network database when login is on.
- LLM access may go through a gateway that shields clinic-to-cloud traffic; see `docs/anforderungen.md`.
- Routing via BridgeLink: `bridgelink.py` generates the channel XML (tested against BridgeLink 26.9.0) and talks to its REST API; routing headers `X-Stellwerk-*` never carry patient data. See `docs/bridgelink.md`.
- **Stellwerk is the design tool, the communication server is the runtime** (owner's picture, 2026-11-14): discovery proposes SELECTs (`discovery.propose_queries`, joins only towards master data, always LEFT JOIN, guessed joins marked), the admin takes the result over as a handover package (`uebergabe.py`, `docs/uebergabe.md`). Documents (PDF/OCR, sections) stay with Stellwerk at runtime.
- The handover package contains no rows or single values from the source unless explicitly requested (`mit_daten`); every package is written to the audit log. The rules in its `LIESMICH.md` must describe `fieldmap.build` exactly: `tests/nachbau.js` rebuilds the bundles from the package alone, so change rules and `build` together.
- **BridgeLink channel per interface** (`schnittstelle.py`, `kanal_bauen.js`, `docs/bridgelink.md`): the generated channel builds bundles in ES5 (Rhino) with the same file the tests run under Node. `kanal_bauen.js` must equal `fieldmap.build`; `tests/test_schnittstelle.py` runs every package test case through it, so change rules, `build`, `nachbau.js` and `kanal_bauen.js` together. Tested against the real BridgeLink 26.9.0 with SQLite and PostgreSQL, not with MSSQL/Oracle. The channel XML contains the DB password: the HTTP API only checks and installs, it never returns the XML; the CLI writes it with mode 0600. The channel sets message retention (`inhalt_tage`, `metadaten_tage`) because BridgeLink's message store holds patient data; the server-wide Data Pruner is off by default and must be enabled by the admin. Document tasks get a second channel kind (`dokument_kanal`): File Reader (folder on the BridgeLink server, binary) → HTTP Sender to `bridgelink.kanal.stellwerk_adresse` + `/v1/convert` (https unless localhost, service token from `anmeldung.dienst_token_env` embedded in the XML, never returned by the API) → Stellwerk delivers to the CDR itself. Rejections (400/413/415/422) put the original, never the file name, into `<pruefablage>/<task>/abgelehnt/`. Tested against BridgeLink 26.9.0 without login; not tested with the service token against an OIDC-enabled Stellwerk.
- Status and order of work: `docs/anforderungen.md`.

## 8. Engineering Principles (Pipeline)
- **Never convert clinical values:** value and unit go into the bundle exactly as delivered (only number notation changes: `1,10` → `1.10`, trailing zeros kept via `fieldmap.dump_json`). Ambiguity (mixed units, `<0,6`, unknown codes) is reported, never resolved by the tool. Mapping proposals are never applied without an explicit accept.
- **Extractive only:** Text in the FHIR document is always verbatim from the source. The local LLM returns `index -> section key`, never text.
- **Nothing is dropped silently:** every source line ends up in a section, in the catch-all section, or in a finding that lists it. `sectionize` verifies the count.
- **Repair only what is lossless:** encoding repairs must round-trip unambiguously; everything else is reported and left unchanged. No NFKC (`m²`, `µg` must survive).
- **Findings are actionable:** each `Finding` has where, what, and what to do. Severity `pruefen` is reserved for things a human must look at; avoid alert fatigue.
- **LLM data policy:** local models may see everything. An external model (API key from the env var named in `llm.schluessel_env`, https only) is refused unless `llm.extern.erlaubt` is set; with `daten: metadaten` it only ever receives table/column names and types, with `daten: alles` also row values and document text (owner's decision, requires a data processing agreement). Check `llm.patientendaten` before sending any value or text. No system proxies, no redirects. Model-assigned content is always `pruefen`.
- **Synthetic data only** in `beispiele/` and `tests/`. Never commit real patient data, `out/`, or `stellwerk.yaml`.
- User-facing strings are German; identifiers are English. Runtime dependencies stay minimal (air-gapped hospital installs).
- Run `python -m pytest` before committing. Regenerate examples with `python beispiele/erzeugen.py`.
