"""Fail closed for frozen facts; failures never mean successful ingestion."""


class PreStartDataRejectedError(ValueError):
    pass


class WearableHistoryMetadataUnavailableError(RuntimeError):
    pass


class FirstNightAssociationUnavailableError(RuntimeError):
    pass
