from uuid import UUID

from sqlalchemy.orm import Session

from app.repositories.wearable_history_repository import WearableHistoryRepository
from app.schemas.wearable_history import WearableHistorySourceRead
from app.services.user_service import user_service


class WearableHistorySourcesService:
    def __init__(self) -> None:
        self.repository = WearableHistoryRepository()

    def begin_source(self, db: Session, user_id: UUID, provider: str) -> None:
        user_service.get(db, user_id, raise_404=True)
        self.repository.begin_source(db, user_id, provider)

    def list_sources(self, db: Session, user_id: UUID) -> list[WearableHistorySourceRead]:
        user_service.get(db, user_id, raise_404=True)
        return [
            WearableHistorySourceRead.model_validate(source) for source in self.repository.list_sources(db, user_id)
        ]


wearable_history_sources_service = WearableHistorySourcesService()
