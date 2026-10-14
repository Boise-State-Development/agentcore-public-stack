"""Owns folder attachment rows and the expansion tasks that fill them."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

from apis.shared.auth import User
from apis.shared.files.folder_attachments import (
    FOLDER_STATUS_EXPANDING,
    FolderAttachment,
    new_folder_attach_id,
)
from apis.shared.files.folder_repository import (
    FolderAttachmentRepository,
    get_folder_attachment_repository,
)

from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.files.folders.expansion import ExpansionDeps, expand_folder
from apis.app_api.files.service import FileUploadService, get_file_upload_service

logger = logging.getLogger(__name__)


class FolderAttachmentService:
    """Create, read, remove and resume folder attachments."""

    # Strong refs to the running expansions: the event loop holds tasks
    # weakly, so a bare ``create_task`` can be collected mid-run (the
    # ``_digest_tasks`` pattern in ``files/service.py``).
    _tasks: Set["asyncio.Task[object]"] = set()
    # Folder ids with a live task in this process, so ``resume`` cannot start
    # a second walk beside one that is still running.
    _running: Set[str] = set()

    def __init__(
        self,
        repository: Optional[FolderAttachmentRepository] = None,
        upload_service: Optional[FileUploadService] = None,
    ):
        self.repository = repository or get_folder_attachment_repository()
        self.upload_service = upload_service or get_file_upload_service()

    @classmethod
    def is_running(cls, folder_attach_id: str) -> bool:
        return folder_attach_id in cls._running

    async def attach(
        self,
        user: User,
        session_id: str,
        *,
        connector_id: str,
        provider_folder_id: str,
        folder_name: str,
        folder_path: Optional[str],
        adapter: FileSourceAdapter,
        access_token: str,
    ) -> FolderAttachment:
        """Create the row ``expanding`` and start its expansion."""
        folder = FolderAttachment(
            folder_attach_id=new_folder_attach_id(),
            session_id=session_id,
            user_id=user.user_id,
            connector_id=connector_id,
            provider_folder_id=provider_folder_id,
            folder_name=folder_name,
            folder_path=folder_path,
            status=FOLDER_STATUS_EXPANDING,
        )
        await self.repository.create(folder)
        self._schedule(folder, adapter, access_token)
        logger.info("Folder attachment created; expansion scheduled")
        return folder

    async def get(self, user_id: str, session_id: str, folder_attach_id: str) -> Optional[FolderAttachment]:
        """The row, when it belongs to this user and session."""
        folder = await self.repository.get(user_id, folder_attach_id)
        if folder is None or folder.session_id != session_id:
            return None
        return folder

    async def remove(self, user_id: str, session_id: str, folder_attach_id: str) -> bool:
        """Delete the row and every file it attached.

        Each file goes through ``FileUploadService.delete_file`` so its S3
        object, thumbnail, ``_derived/`` sidecars and quota share go with it
        (and, through the bucket's delete event, its KB index entry). A file
        the user already removed from the Files page is simply absent.
        """
        folder = await self.get(user_id, session_id, folder_attach_id)
        if folder is None:
            return False
        for entry in folder.files:
            if not entry.upload_id:
                continue
            try:
                await self.upload_service.delete_file(user_id, entry.upload_id)
            except Exception:  # noqa: BLE001 - best-effort, like the session cascade
                logger.warning("Failed to delete a folder file", exc_info=True)
        await self.repository.delete(user_id, folder_attach_id)
        logger.info("Folder attachment removed")
        return True

    async def resume(self, folder: FolderAttachment, adapter: FileSourceAdapter, access_token: str) -> FolderAttachment:
        """Re-run the walk, skipping files already attached (spec §4.9).

        The caller has checked :meth:`is_running`. The row goes back to
        ``expanding`` with its error cleared; the task keeps what was attached.
        """
        row = await self.repository.update(
            folder.user_id, folder.folder_attach_id, status=FOLDER_STATUS_EXPANDING, clear_error=True
        )
        if row is None:
            return folder
        self._schedule(row, adapter, access_token)
        logger.info("Folder attachment expansion resumed")
        return row

    def _schedule(self, folder: FolderAttachment, adapter: FileSourceAdapter, access_token: str) -> "asyncio.Task[object]":
        deps = ExpansionDeps(
            adapter=adapter,
            access_token=access_token,
            repository=self.repository,
            upload_service=self.upload_service,
        )
        task = asyncio.get_running_loop().create_task(expand_folder(folder, deps))
        self._tasks.add(task)
        self._running.add(folder.folder_attach_id)

        def _done(t: "asyncio.Task[object]") -> None:
            self._tasks.discard(t)
            self._running.discard(folder.folder_attach_id)

        task.add_done_callback(_done)
        return task


_service_instance: Optional[FolderAttachmentService] = None


def get_folder_attachment_service() -> FolderAttachmentService:
    """Get or create the process-wide service."""
    global _service_instance
    if _service_instance is None:
        _service_instance = FolderAttachmentService()
    return _service_instance
