"""Fail closed for frozen facts; failures never mean successful ingestion."""


class PreStartDataRejectedError(ValueError):
    pass


class WearableHistoryMetadataUnavailableError(RuntimeError):
    pass


# The deferred wh_ingress checks (migration a29691004b01). A batch that passed the
# Python gate can still meet one at flush or commit when the freeze or a source row
# moves underneath it; the whole batch is rolled back, so it must be redelivered.
HISTORY_BOUNDARY_MESSAGES = frozenset(
    {
        "Wearable source history unavailable",
        "Provisional wearable samples must remain raw",
        "Archive contains pre-start data",
        "Frozen source rejects pre-start data",
    }
)


def is_history_boundary_failure(exc: BaseException) -> bool:
    """True for a wh_ingress rejection, raw or wrapped by decorated repositories and services."""
    # SQLAlchemy errors carry the driver error as ``orig``; psycopg errors carry ``diag``
    # themselves. Bulk repositories re-raise inside ``except``, so follow the context too.
    candidate: BaseException | None = exc
    while candidate is not None:
        driver = getattr(candidate, "orig", None) or candidate
        if getattr(getattr(driver, "diag", None), "message_primary", None) in HISTORY_BOUNDARY_MESSAGES:
            return True
        candidate = candidate.__cause__ or candidate.__context__
    return False


def raise_if_history_retriable(exc: BaseException) -> None:
    """Re-raise what a per-item catch must not swallow, so the caller can retry.

    A retriable history error passes through unchanged; a wh_ingress rejection met at
    flush or commit becomes ``WearableHistoryMetadataUnavailableError``. Anything else
    returns, and the catch keeps its existing handling.
    """
    if isinstance(exc, WearableHistoryMetadataUnavailableError):
        raise exc
    if is_history_boundary_failure(exc):
        raise WearableHistoryMetadataUnavailableError(
            "Wearable history boundary moved during the write; retry"
        ) from exc
