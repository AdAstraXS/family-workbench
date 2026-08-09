from collections import Counter

from django.db import transaction

from .models import (
    KnowledgeCategory,
    KnowledgeDocument,
    KnowledgeTag,
    normalize_taxonomy_name,
)
from .search import index_document


def _clean_name(value, max_length):
    return " ".join(str(value or "").strip().split())[:max_length]


def _find_catalog_item(model, family, value):
    normalized = normalize_taxonomy_name(value)
    if not normalized:
        return None
    item = model.objects.filter(
        family=family,
        normalized_name=normalized,
    ).select_related("merged_into").first()
    if item:
        return item.merged_into or item
    for candidate in model.objects.filter(family=family).select_related("merged_into"):
        if normalized in {
            normalize_taxonomy_name(alias) for alias in (candidate.aliases or [])
        }:
            return candidate.merged_into or candidate
    return None


def ensure_category(family, value, *, created_by=None):
    name = _clean_name(value, 100)
    if not name:
        return None, False
    existing = _find_catalog_item(KnowledgeCategory, family, name)
    if existing:
        return existing, False
    return KnowledgeCategory.objects.get_or_create(
        family=family,
        normalized_name=normalize_taxonomy_name(name),
        defaults={"name": name, "created_by": created_by},
    )


def ensure_tags(family, values, *, created_by=None):
    items = []
    seen = set()
    for value in values or []:
        name = _clean_name(value, 30)
        normalized = normalize_taxonomy_name(name)
        if not normalized or normalized in seen:
            continue
        item = _find_catalog_item(KnowledgeTag, family, name)
        created = False
        if item is None:
            item, created = KnowledgeTag.objects.get_or_create(
                family=family,
                normalized_name=normalized,
                defaults={"name": name, "created_by": created_by},
            )
        seen.add(item.normalized_name)
        items.append((item, created))
    return items


def canonicalize_document_taxonomy(document, *, created_by=None):
    category, _ = ensure_category(
        document.family,
        document.category,
        created_by=created_by,
    )
    tag_items = ensure_tags(
        document.family,
        document.tags,
        created_by=created_by,
    )
    document.category = category.name if category else ""
    document.tags = [item.name for item, _ in tag_items]
    return document


def taxonomy_choices(document, *, tag_limit=80):
    categories = list(
        KnowledgeCategory.objects.filter(
            family=document.family,
            is_active=True,
            merged_into__isnull=True,
        ).order_by("name", "id")
    )
    tags = list(
        KnowledgeTag.objects.filter(
            family=document.family,
            is_active=True,
            merged_into__isnull=True,
        ).order_by("name", "id")[:500]
    )
    revision_text = ""
    if document.current_revision_id:
        revision_text = document.current_revision.plain_text or ""
    haystack = f"{document.title}\n{document.author}\n{revision_text[:30000]}".casefold()
    current = {
        normalize_taxonomy_name(value) for value in (document.tags or []) if value
    }
    tags.sort(
        key=lambda item: (
            0
            if item.normalized_name in current
            else 1
            if item.name.casefold() in haystack
            else 2,
            -len(item.name),
            item.name,
        )
    )
    return categories, tags[:tag_limit]


def category_usage(item):
    return KnowledgeDocument.objects.filter(
        family=item.family,
        category=item.name,
    ).count()


def tag_usage(item):
    return sum(
        1
        for values in KnowledgeDocument.objects.filter(family=item.family).values_list(
            "tags", flat=True
        )
        if item.name in (values or [])
    )


def _reindex_documents(documents):
    for document in documents.select_related("source", "owner", "current_revision"):
        index_document(document)


@transaction.atomic
def rename_category(item, new_name):
    old_name = item.name
    new_name = _clean_name(new_name, 100)
    aliases = list(item.aliases or [])
    if old_name != new_name and old_name not in aliases:
        aliases.append(old_name)
    item.name = new_name
    item.aliases = aliases
    item.save()
    documents = KnowledgeDocument.objects.filter(
        family=item.family,
        category=old_name,
    )
    document_ids = list(documents.values_list("pk", flat=True))
    documents.update(category=item.name)
    _reindex_documents(KnowledgeDocument.objects.filter(pk__in=document_ids))
    return item


@transaction.atomic
def rename_tag(item, new_name):
    old_name = item.name
    new_name = _clean_name(new_name, 30)
    aliases = list(item.aliases or [])
    if old_name != new_name and old_name not in aliases:
        aliases.append(old_name)
    item.name = new_name
    item.aliases = aliases
    item.save()
    documents = KnowledgeDocument.objects.filter(family=item.family)
    changed = []
    for document in documents:
        values = list(document.tags or [])
        if old_name not in values:
            continue
        document.tags = list(dict.fromkeys(item.name if value == old_name else value for value in values))
        document.save(update_fields=["tags", "updated_at"])
        changed.append(document.pk)
    if changed:
        _reindex_documents(KnowledgeDocument.objects.filter(pk__in=changed))
    return item


@transaction.atomic
def merge_category(source, target):
    if source.family_id != target.family_id or source.pk == target.pk:
        raise ValueError("分类合并目标无效。")
    documents = KnowledgeDocument.objects.filter(
        family=source.family,
        category=source.name,
    )
    document_ids = list(documents.values_list("pk", flat=True))
    documents.update(category=target.name)
    aliases = list(target.aliases or [])
    for value in [source.name, *(source.aliases or [])]:
        if value and value != target.name and value not in aliases:
            aliases.append(value)
    target.aliases = aliases
    target.save(update_fields=["aliases", "updated_at"])
    source.is_active = False
    source.merged_into = target
    source.save(update_fields=["is_active", "merged_into", "updated_at"])
    _reindex_documents(KnowledgeDocument.objects.filter(pk__in=document_ids))


@transaction.atomic
def merge_tag(source, target):
    if source.family_id != target.family_id or source.pk == target.pk:
        raise ValueError("标签合并目标无效。")
    changed = []
    for document in KnowledgeDocument.objects.filter(family=source.family):
        values = list(document.tags or [])
        if source.name not in values:
            continue
        document.tags = list(
            dict.fromkeys(target.name if value == source.name else value for value in values)
        )
        document.save(update_fields=["tags", "updated_at"])
        changed.append(document.pk)
    aliases = list(target.aliases or [])
    for value in [source.name, *(source.aliases or [])]:
        if value and value != target.name and value not in aliases:
            aliases.append(value)
    target.aliases = aliases
    target.save(update_fields=["aliases", "updated_at"])
    source.is_active = False
    source.merged_into = target
    source.save(update_fields=["is_active", "merged_into", "updated_at"])
    if changed:
        _reindex_documents(KnowledgeDocument.objects.filter(pk__in=changed))


def taxonomy_usage_counts(family):
    category_counts = Counter(
        KnowledgeDocument.objects.filter(family=family)
        .exclude(category="")
        .values_list("category", flat=True)
    )
    tag_counts = Counter()
    for values in KnowledgeDocument.objects.filter(family=family).values_list(
        "tags", flat=True
    ):
        tag_counts.update(values or [])
    return category_counts, tag_counts
