from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from notes.models import InvestmentNote

from .models import KnowledgeDocument
from .search import index_document, index_investment_note, remove_investment_note_index


@receiver(
    post_save,
    sender=InvestmentNote,
    dispatch_uid="knowledge_index_investment_note",
)
def investment_note_saved(sender, instance, **kwargs):
    index_investment_note(instance)


@receiver(
    post_delete,
    sender=InvestmentNote,
    dispatch_uid="knowledge_remove_investment_note",
)
def investment_note_deleted(sender, instance, **kwargs):
    remove_investment_note_index(instance)


@receiver(
    post_save,
    sender=KnowledgeDocument,
    dispatch_uid="knowledge_index_document",
)
def knowledge_document_saved(sender, instance, **kwargs):
    index_document(instance)
