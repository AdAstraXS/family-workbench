from django.contrib import admin
from family_core.private_admin import PrivateContentAdmin
from .models import Book, BookFile, ReadingImportRun, ReadingPosition
from .permissions import accessible_books
from .models import Annotation, AnnotationComment, ReadingPlan, ReadingPlanItem, ReadingArtifact, ReadingArtifactVersion, ReadingArchive, ReadingAiJob
from .annotations import accessible_annotations
from .permissions import accessible_reading_artifacts


@admin.register(Annotation)
class AnnotationAdmin(PrivateContentAdmin):
    list_display=("book","author","visibility","updated_at")
    def allowed_objects(self,member):return accessible_annotations(member)


@admin.register(AnnotationComment)
class CommentAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):return AnnotationComment.objects.filter(annotation__in=accessible_annotations(member))


@admin.register(ReadingPlan)
class PlanAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):return ReadingPlan.objects.filter(member=member)


@admin.register(ReadingPlanItem)
class PlanItemAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):
        from django.db.models import Q
        return ReadingPlanItem.objects.filter(plan__member=member).filter(Q(book__isnull=True)|Q(book__in=accessible_books(member)))


@admin.register(ReadingArtifact)
class ArtifactAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):return accessible_reading_artifacts(member)


@admin.register(ReadingArtifactVersion)
class ArtifactVersionAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):return ReadingArtifactVersion.objects.filter(artifact__in=accessible_reading_artifacts(member))


@admin.register(ReadingArchive)
class ArchiveAdmin(PrivateContentAdmin):
    def allowed_objects(self,member):return ReadingArchive.objects.filter(version__artifact__in=accessible_reading_artifacts(member))


@admin.register(ReadingAiJob)
class JobAdmin(PrivateContentAdmin):
    list_display=("book","member","status","created_at")
    def allowed_objects(self,member):return ReadingAiJob.objects.filter(member=member,book__in=accessible_books(member))


@admin.register(Book)
class BookAdmin(PrivateContentAdmin):
    list_display = ("title", "author", "owner", "visibility", "created_at")
    def allowed_objects(self, member):
        return accessible_books(member)


@admin.register(BookFile)
class BookFileAdmin(PrivateContentAdmin):
    list_display = ("book", "format", "status", "size", "updated_at")
    def allowed_objects(self, member):
        return BookFile.objects.filter(book__in=accessible_books(member))


@admin.register(ReadingPosition)
class ReadingPositionAdmin(PrivateContentAdmin):
    list_display = ("book", "member", "progress", "updated_at")
    def allowed_objects(self, member):
        return ReadingPosition.objects.filter(member=member, book__in=accessible_books(member))


@admin.register(ReadingImportRun)
class ReadingImportRunAdmin(PrivateContentAdmin):
    list_display = ("file", "status", "started_at", "finished_at")
    def allowed_objects(self, member):
        return ReadingImportRun.objects.filter(file__book__in=accessible_books(member).filter(owner=member))
