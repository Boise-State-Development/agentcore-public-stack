"""The shared folder walk (``file_sources/folder_walk.py``): what the folder
attachment task and KB sync's ``drive_folder`` runs both list a folder with.

The attachment task's own suite (``test_folder_expansion.py``) covers the walk
through the expansion; this pins the parts only the sync path reads: the error
kinds, ``complete``, and the paths it returns.
"""

import pytest

from apis.app_api.file_sources.folder_walk import (
    WALK_AUTH,
    WALK_FAILED,
    WALK_NOT_FOUND,
    walk_folder,
)
from apis.app_api.file_sources.models import (
    BrowseResult,
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
)

pytestmark = pytest.mark.asyncio


def _file(file_id, name, mime="application/pdf"):
    return FileEntry(id=file_id, name=name, type=FileEntryType.FILE, mime_type=mime)


def _folder(folder_id, name):
    return FileEntry(id=folder_id, name=name, type=FileEntryType.FOLDER, selectable=False)


class Adapter:
    def __init__(self, tree, errors=None):
        self.tree = tree
        self.errors = errors or {}

    async def browse(self, access_token, folder_id, cursor=None):
        if folder_id in self.errors:
            raise self.errors[folder_id]
        return BrowseResult(entries=self.tree.get(folder_id, []))


async def test_lists_subfolders_with_their_paths_and_counts_what_it_skips():
    adapter = Adapter({
        "root": [_folder("sub", "Week 3"), _file("a", "a.pdf"), _file("img", "p.png", "image/png")],
        "sub": [_file("b", "b.pdf")],
    })

    walk = await walk_folder(adapter, "t", "root", max_files=10, max_pages=10)

    assert [(f.entry.id, f.relative_path) for f in walk.files] == [("a", "a.pdf"), ("b", "Week 3/b.pdf")]
    assert walk.listed == 3
    assert walk.skipped_type == 1
    assert walk.complete


async def test_the_file_cap_truncates():
    adapter = Adapter({"root": [_file(str(i), f"{i}.pdf") for i in range(3)]})

    walk = await walk_folder(adapter, "t", "root", max_files=2, max_pages=10)

    assert len(walk.files) == 2
    assert walk.truncated
    assert not walk.complete


async def test_the_page_cap_truncates():
    adapter = Adapter({"root": [_folder("sub", "s")], "sub": [_file("a", "a.pdf")]})

    walk = await walk_folder(adapter, "t", "root", max_files=10, max_pages=1)

    assert walk.files == []
    assert walk.truncated


@pytest.mark.parametrize(
    "where, error, expected",
    [
        ("root", FileSourceNotFoundError("404"), WALK_NOT_FOUND),
        ("sub", FileSourceNotFoundError("404"), WALK_FAILED),  # removed mid-walk: proves nothing
        ("root", FileSourceAuthError("401"), WALK_AUTH),
        ("sub", FileSourceError("500"), WALK_FAILED),
    ],
)
async def test_listing_errors(where, error, expected):
    adapter = Adapter({"root": [_file("a", "a.pdf"), _folder("sub", "s")]}, errors={where: error})

    walk = await walk_folder(adapter, "t", "root", max_files=10, max_pages=10)

    assert walk.error == expected
    assert not walk.complete
