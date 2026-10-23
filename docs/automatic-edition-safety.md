# Automatic edition safety (pending coordinator review)

Future discovery and explicit auto-match require exact normalized title and author,
explicit source/provider language, positive runtimes within both 1% and 120 seconds,
an Audible ASIN, and recorded provenance for the configured Audible region. Known
source identifiers must be supplied and agree. Conflicting evidence always vetoes
automation, irrespective of the configured score threshold. Competing editions
must satisfy the configured separation margin. Legacy candidates without region
provenance need a fresh search; viewing a job never repairs or queues it.

Source language is taken from explicit format/audio stream tags, never filenames,
author nationality, a region default, or candidate metadata. Missing/undefined
language leaves imports in REVIEW. Provider failure leaves the package inspected,
with a REVIEW job and a retry/manual-edit warning. Disagreement among known source
file author/language/identifier tags vetoes automatic matching without changing
grouping or ordering.

New automatic approvals retain the selected provider edition and recheck the
actual metadata before processing and after any source-runtime correction. Failed
rechecks return to REVIEW without new publication. Existing live jobs are not
migrated or rewritten. Explicitly retained manual fields and Save & approve are
human overrides, not automatic correctness claims. The web review displays inline
warnings when selected provenance is absent, weak, conflicted, or edited, and
refreshes them after saving. Save & approve is a single-click manual action with
no blocking warning popup; backend automatic matching gates are unchanged.

Manual Audible book URLs use their allowlisted marketplace, retained as selected
edition provenance. Approval warnings compare that explicit lookup region rather
than falsely reporting a Settings-region mismatch. Raw ASINs and title searches
still use Settings. Automatic matching always requires the Settings region, even
for a candidate manually retrieved from another marketplace. See
[marketplace lookup notes](audible-marketplaces.md).

Explicit terminal `- translator` contributor labels are excluded from Audible
authors; unlabeled people/coauthors and ambiguous roles remain intact. Output
author is written to both aART and Artist, narrator to Composer (©wrt), with ASIN
and language freeform atoms. Tests read the resulting MP4 tags and ffprobe source
metadata back; this is not a guarantee that an independently configured ABS
scanner or Selector exporter will preserve every value.

Human checks remain unavoidable for missing/wrong embedded tags, mislabeled audio,
provider catalog errors, manual overrides, unreturned alternate editions, and
ABS/Selector configuration or existing metadata precedence. No automatic gate can
prove spoken content from matching tags and runtime. No repair of existing books,
live DB mutation, ABS edit/scan, deployment, commit, or push is included here.
