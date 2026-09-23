"""Resolve optional identity overrides without reviving cleared values."""


def configured_identity(entry, key):
    """Return the configured identity, or None for explicit autodetection.

    Args:
        entry: Config entry containing options and original setup data.
        key: Identity field to resolve.

    Returns:
        Trimmed configured value, or None when unset or explicitly cleared.
    """
    value = entry.options.get(key, entry.data.get(key))
    return (value or "").strip() or None
