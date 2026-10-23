# Audible marketplace lookup and review

Manual book URLs accept only `audible.com`, `.co.uk`, `.com.au`, `.ca`, `.de`,
`.fr`, `.it`, `.es`, `.co.jp`, and `.in` (optionally `www.`). The ASIN must be in
`/pd/ASIN` or `/pd/book-slug/ASIN`, with an optional trailing slash. Unsupported
hosts, credentials/ports, malformed paths and query-only identifiers are rejected.
Query parameters never choose an ASIN or API host. Audible API redirects are not
followed. The URL marketplace overrides Settings only for that manual lookup;
raw ASINs and automatic discovery retain the configured region.

Candidates record the actual API region. Explicit URL overrides additionally
retain `region_source=audible_url` and `lookup_region`, without storing URL query
parameters. Manual approval warnings accept matching explicit lookup provenance;
automatic matching still requires the configured region. Existing saved jobs are
not migrated: repeat search and select the refreshed candidate to update provenance.

## Reported identifiers are different

The user typed `B0FM6GST96`, but their pasted audible.in URL contains
`B0G2FHRFH1`. These are **different ASINs**, not aliases. The supplied public API
reproduction found no usable title/author for `B0FM6GST96` in COM or IN;
`B0G2FHRFH1` returned Six Strikes / Antonia Grave in IN but blank metadata in COM.
Title/author searches returned no results in either market. These observations
are not an identity guarantee or proof that an edition is absent from a storefront.
This change does not substitute identifiers, infer an ISBN, or invent metadata.

A direct 404 or product without a usable title now returns an actionable error
naming the requested ASIN and region. It does not produce a blank candidate or
overwrite existing selected metadata. Verify the book and choose its actual URL.
Verification uses mocked HTTP responses; no further live catalog calls are needed.

Review warnings are visible inline beside Save & approve and refreshed from save
responses. They do not require a repeated popup. Explicit manual approval remains
an override; grouping validation and automatic edition-safety gates are unchanged.
