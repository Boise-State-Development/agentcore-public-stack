"""Export targets: connectors usable as a *destination* to save content to.

The mirror image of `file_sources`. Where a file-source adapter reads files
*into* the system, an export-target adapter writes a rendered document *out*
to a connected app (e.g. saving a conversation transcript to Google Drive).

The connector + OAuth layer is shared with file sources — only the capability
(write vs. read) differs, so this package mirrors the file-source adapter
contract and registry rather than extending them.
"""
