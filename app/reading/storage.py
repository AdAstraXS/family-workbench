from pathlib import Path
from django.conf import settings
from django.core.files.storage import FileSystemStorage


def storage():
    # A subdirectory of the existing protected volume, included in its backups.
    return FileSystemStorage(location=Path(settings.KNOWLEDGE_FILE_ROOT) / "reading")


def max_upload_bytes():
    return getattr(settings, "READING_MAX_UPLOAD_BYTES", 256 * 1024 * 1024)
