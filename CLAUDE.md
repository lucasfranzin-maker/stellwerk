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

## 8. Engineering Principles (Pipeline)
- **Never convert clinical values:** value and unit go into the bundle exactly as delivered (only number notation changes: `1,10` → `1.10`, trailing zeros kept via `fieldmap.dump_json`). Ambiguity (mixed units, `<0,6`, unknown codes) is reported, never resolved by the tool. Mapping proposals are never applied without an explicit accept.
- **Extractive only:** Text in the FHIR document is always verbatim from the source. The local LLM returns `index -> section key`, never text.
- **Nothing is dropped silently:** every source line ends up in a section, in the catch-all section, or in a finding that lists it. `sectionize` verifies the count.
- **Repair only what is lossless:** encoding repairs must round-trip unambiguously; everything else is reported and left unchanged. No NFKC (`m²`, `µg` must survive).
- **Findings are actionable:** each `Finding` has where, what, and what to do. Severity `pruefen` is reserved for things a human must look at; avoid alert fatigue.
- **LLM stays local:** `llm.py` refuses non-local hosts, ignores system proxies and redirects. LLM-assigned content is always `pruefen`.
- **Synthetic data only** in `beispiele/` and `tests/`. Never commit real patient data, `out/`, or `stellwerk.yaml`.
- User-facing strings are German; identifiers are English. Runtime dependencies stay minimal (air-gapped hospital installs).
- Run `python -m pytest` before committing. Regenerate examples with `python beispiele/erzeugen.py`.
