from django.conf import settings
from django.core.files.storage import FileSystemStorage


class ProtectedKnowledgeStorage(FileSystemStorage):
    """Filesystem storage that deliberately has no public media URL."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("location", settings.KNOWLEDGE_FILE_ROOT)
        kwargs.setdefault("base_url", None)
        super().__init__(*args, **kwargs)


protected_knowledge_storage = ProtectedKnowledgeStorage()
