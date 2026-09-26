from django.db.models import Q
from .models import Book, ReadingArtifact


def accessible_books(member):
    return Book.objects.filter(family_id=member.family_id).filter(
        Q(owner=member) | Q(visibility=Book.FAMILY)
    ).select_related("file", "owner")


def accessible_reading_artifacts(member):
    return ReadingArtifact.objects.filter(book__in=accessible_books(member)).filter(
        Q(owner=member)|Q(visibility=Book.FAMILY)).select_related("book", "owner", "current_version")
