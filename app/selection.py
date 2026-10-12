"""Apply provider metadata without carrying identifiers across editions."""

from app import metadata


EDITION_FIELDS = frozenset({'isbn', 'asin', 'language'})


def apply_candidate(body, candidate, keep_manual_fields=(), *, automatic=False):
    """Retention is explicit for this match, never inferred from old provenance.

    Non-identifier merge behavior is unchanged, including contributor strings.
    An absent edition field means unknown, not evidence for the previous edition.
    """
    keep = set(keep_manual_fields)
    body['selected_candidate'] = candidate.copy()
    body['automatic_approval'] = automatic and not keep
    provenance = body['provenance']
    if any(key not in metadata.FIELDS or provenance.get(key) != 'Manual' for key in keep):
        raise ValueError('Only explicitly reviewed Manual fields can be retained')
    for key in metadata.FIELDS:
        if key in keep:
            continue
        if automatic and key not in EDITION_FIELDS and provenance.get(key) == 'Manual':
            continue
        value = candidate.get(key)
        if value or key in EDITION_FIELDS:
            body['metadata'][key] = str(value) if value else ''
            provenance[key] = candidate['provider'] if value else f"{candidate['provider']} (not supplied)"
