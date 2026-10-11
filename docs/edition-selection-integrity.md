# Edition selection integrity

Selecting a candidate, explicitly auto-matching a review job, and initial worker
auto-matching now replace ISBN, ASIN and language as one edition-specific set.
Missing or empty candidate values clear the corresponding field. Provenance says
`<provider> (not supplied)` rather than attributing a stale value to that provider.
Other metadata merge/scoring/contributor behavior is unchanged.

Changed fields saved in the metadata form still receive `Manual` provenance.
Manual ISBN/ASIN/language no longer survive a subsequent match by implication.
The UI asks whether each manual override belongs to the new edition. API callers
must send `keep_manual_fields: ["isbn", ...]` with the select/auto-match request
to retain reviewed overrides; only fields currently marked `Manual` qualify.
Confirmation applies to that action only. Manual non-identifier edits retain the
existing auto-match behavior; candidate selection offers confirmation for those
too. Edits made after selection remain deliberate manual edits and can be saved
and approved without another selection.

## Existing jobs: manual review required

There is **no automatic data migration**. Startup, listing and viewing jobs do
not rewrite saved metadata. Previously mixed identifiers may still exist in
REVIEW, READY or published outputs. Before processing an affected saved job,
return queued jobs to review, select the correct edition again, inspect ISBN,
ASIN and language, and retain manual overrides only after checking that edition.
Then save any further deliberate corrections and approve. Simply saving or
approving an old job does not repair it. Published files and external ABS,
Selector or Goodreads records need separate audited correction; this change
does not retag, republish or change external records automatically.
