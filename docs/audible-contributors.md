# Audible contributor mapping

Public catalog checks on 2026-10-11 used the same response groups as the app,
without credentials. Direct ASIN `B0GFB7WK22` (The Sixth Faction) on audible.com
lists only Veronica Roth and English narrators Emma Love, Charlie Thurston and
Max Meyers. Title/author search also returns German edition `B0HCW29JJZ`,
The Sixth Faction 01, with different narrators and runtime.

The German edition's `authors` array contains Veronica Roth plus Teresa Englert,
Anna-Saida Jessen and Susanne Seidita, each of the latter explicitly suffixed
` - translator` in `name`. These contributor objects have no separate role field;
Veronica Roth also has an author ASIN. That ASIN is not evidence of authorship
for other contributors and is not used as a filter. A reduced response is kept
in `tests/fixtures/audible_sixth_faction.json`.

`metadata.audible_product` excludes only explicit terminal translator labels
(case-insensitive; hyphen/en dash/em dash) from the authored field. Unlabeled
names and other/ambiguous roles remain unchanged; all coauthors and narrators
are preserved. No inference is made from a person's identity. Both catalog
search and direct-ASIN lookup use this mapping.

Initial inspection (`worker.inspect_pending`) searches, normalizes and ranks
candidates, storing the resulting flat metadata in the job. Manual search uses
the same path. Selection/auto-match copies these normalized fields. Scoring
still compares the full author string (25 base points); exact title/author and
close runtime can qualify for the existing 95-point floor. Filtering does not
bypass narrator/language/identifier/edition conflicts, competing-edition margin,
confirmed grouping or the auto-approval setting. A translated edition is not
automatically a match for English audio.

**Existing jobs:** stored candidates contain flat author strings, not the raw
contributors. Opening a review recalculates scores but does not rerun provider
normalization. Repeat metadata search (prefer the exact ASIN and correct region)
to refresh candidates after this fix. If an old candidate was already selected,
select the refreshed candidate or explicitly edit the authored field; search
alone does not replace the job's selected metadata. Auto-match preserves manual
edits. Existing jobs, database records and published library files are not
migrated or modified by this fix.
