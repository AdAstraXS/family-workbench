import hashlib
import json
import re

from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from family_core.audit import stamp_actor
from family_core.models import FamilyMember
from knowledge.models import (
    KnowledgeDocument,
    KnowledgeRevision,
    KnowledgeSource,
    KnowledgeVisibility,
)
from knowledge.search import index_document

from .models import (
    CollectionRun,
    EventEvidence,
    EventKnowledgeArchive,
    EventSubject,
    IntelligenceEvent,
    IntelligenceSource,
    SourceItem,
    SubjectKnowledgeIdentity,
    normalize_knowledge_author_name,
)
from .scoring import POLICY_VERSION, SOURCE_QUALITY_SCORES, calculate_event_scores


def _normalized_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _fingerprint(*parts):
    payload = "|".join(_normalized_text(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IntelligenceArchivePermissionError(PermissionError):
    pass


class IntelligenceArchiveError(RuntimeError):
    pass


def _plain_event_snapshot(event, subjects, evidences):
    lines = [event.title, "", "事实摘要", event.summary]
    if event.why_it_matters:
        lines.extend(["", "为什么重要", event.why_it_matters])
    if subjects:
        lines.extend(
            [
                "",
                "关联人物与主题",
                "、".join(
                    f"{link.subject.display_name}（{link.get_role_display()}）"
                    for link in subjects
                ),
            ]
        )
    lines.extend(["", "原始证据"])
    for index, evidence in enumerate(evidences, start=1):
        item = evidence.source_item
        excerpt = evidence.excerpt or item.excerpt or "未保存摘录"
        lines.extend(
            [
                f"{index}. {item.title}",
                f"来源：{item.source.name} · {item.source.source_tier}级 · {evidence.get_evidence_type_display()}",
                f"摘录：{excerpt}",
                f"原文：{item.canonical_url or '未提供'}",
            ]
        )
    lines.extend(
        [
            "",
            "保存说明",
            "此文档是归档操作发生时的情报与证据快照；后续情报编辑不会静默改写本版本。",
        ]
    )
    return "\n".join(lines).strip()


def _event_snapshot_html(event, subjects, evidences):
    def paragraph(value):
        escaped = escape(value or "")
        return f"<p>{escaped.replace(chr(10), '<br>')}</p>"

    chunks = ["<h2>事实摘要</h2>", paragraph(event.summary)]
    if event.why_it_matters:
        chunks.extend(["<h2>为什么重要</h2>", paragraph(event.why_it_matters)])
    if subjects:
        chunks.extend(
            [
                "<h2>关联人物与主题</h2>",
                paragraph(
                    "、".join(
                        f"{link.subject.display_name}（{link.get_role_display()}）"
                        for link in subjects
                    )
                ),
            ]
        )
    chunks.append("<h2>原始证据</h2>")
    if evidences:
        chunks.append("<ol>")
        for evidence in evidences:
            item = evidence.source_item
            excerpt = evidence.excerpt or item.excerpt or "未保存摘录"
            source_line = (
                f"{item.source.name} · {item.source.source_tier}级 · "
                f"{evidence.get_evidence_type_display()}"
            )
            chunks.append(
                "<li>"
                f"<strong>{escape(item.title)}</strong>"
                f"{paragraph(source_line)}"
                f"{paragraph(excerpt)}"
                f"{paragraph('原文：' + (item.canonical_url or '未提供'))}"
                "</li>"
            )
        chunks.append("</ol>")
    else:
        chunks.append(paragraph("该事件没有可归档的来源证据。"))
    chunks.extend(
        [
            "<h2>保存说明</h2>",
            paragraph("此文档是归档操作发生时的情报与证据快照；后续情报编辑不会静默改写本版本。"),
        ]
    )
    return "".join(chunks)


def _event_snapshot_payload(event, subjects, evidences):
    return {
        "schema": "family-workbench-intelligence-archive-v1",
        "archived_at": timezone.now().isoformat(),
        "event": {
            "id": event.pk,
            "path": reverse("intelligence:event_detail", kwargs={"pk": event.pk}),
            "title": event.title,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "occurred_precision": event.occurred_precision,
            "summary": event.summary,
            "why_it_matters": event.why_it_matters,
            "review_status": event.review_status,
            "selection_status": event.selection_status,
            "importance_score": event.importance_score,
            "confidence_score": event.confidence_score,
        },
        "subjects": [
            {
                "id": link.subject_id,
                "slug": link.subject.slug,
                "display_name": link.subject.display_name,
                "canonical_name": link.subject.canonical_name,
                "role": link.role,
                "is_primary": link.is_primary,
            }
            for link in subjects
        ],
        "evidence": [
            {
                "source_item_id": evidence.source_item_id,
                "evidence_type": evidence.evidence_type,
                "is_primary": evidence.is_primary,
                "source": {
                    "name": evidence.source_item.source.name,
                    "tier": evidence.source_item.source.source_tier,
                    "group": evidence.source_item.source.source_group,
                },
                "title": evidence.source_item.title,
                "author_name": evidence.source_item.author_name,
                "canonical_url": evidence.source_item.canonical_url,
                "published_at": (
                    evidence.source_item.published_at.isoformat()
                    if evidence.source_item.published_at
                    else None
                ),
                "excerpt": evidence.excerpt or evidence.source_item.excerpt,
                "content_hash": evidence.source_item.content_hash,
                "content_depth": evidence.source_item.content_depth,
            }
            for evidence in evidences
        ],
    }


def _ensure_subject_knowledge_identity(*, family, subject, user):
    normalized = normalize_knowledge_author_name(subject.display_name)
    if not normalized:
        return None
    identity, created = SubjectKnowledgeIdentity.objects.get_or_create(
        family=family,
        normalized_author_name=normalized,
        defaults={
            "subject": subject,
            "author_name": subject.display_name,
            "created_by": user,
            "updated_by": user,
        },
    )
    if created:
        return identity
    if identity.subject_id == subject.pk and not identity.is_active:
        identity.is_active = True
        identity.updated_by = user
        identity.save(update_fields=["is_active", "updated_by", "updated_at"])
    return identity if identity.subject_id == subject.pk else None


def archive_event_to_knowledge(*, event, member, user, add_to_pending=False):
    if event.family_id != member.family_id:
        raise IntelligenceArchivePermissionError("不能归档其他家庭的情报。")
    requested_mode = (
        EventKnowledgeArchive.MODE_ORGANIZE
        if add_to_pending
        else EventKnowledgeArchive.MODE_ARCHIVE
    )
    stored_file = None
    storage = None
    try:
        with transaction.atomic():
            event = IntelligenceEvent.objects.select_for_update().get(pk=event.pk)
            existing = (
                EventKnowledgeArchive.objects.select_related(
                    "document", "document__owner"
                )
                .filter(event=event)
                .first()
            )
            if existing is not None:
                upgraded = False
                if (
                    add_to_pending
                    and existing.document.library_tier
                    != KnowledgeDocument.LIBRARY_KNOWLEDGE
                    and existing.document.knowledge_status
                    != KnowledgeDocument.KNOWLEDGE_PENDING
                ):
                    if (
                        existing.document.owner_id != member.pk
                        and member.role != FamilyMember.ROLE_ADMIN
                    ):
                        raise IntelligenceArchivePermissionError(
                            "这条情报已由其他成员归档；只有资料所有者或家庭管理员可以加入待整理。"
                        )
                    document = existing.document
                    document.knowledge_status = KnowledgeDocument.KNOWLEDGE_PENDING
                    document.library_tier = KnowledgeDocument.LIBRARY_ARCHIVE
                    document.curation_status = KnowledgeDocument.CURATION_INBOX
                    document.save(
                        update_fields=[
                            "knowledge_status",
                            "library_tier",
                            "curation_status",
                            "updated_at",
                        ]
                    )
                    existing.archive_mode = EventKnowledgeArchive.MODE_ORGANIZE
                    existing.last_updated_by = member
                    existing.save(
                        update_fields=["archive_mode", "last_updated_by", "updated_at"]
                    )
                    index_document(document)
                    upgraded = True
                elif (
                    add_to_pending
                    and existing.archive_mode != EventKnowledgeArchive.MODE_ORGANIZE
                ):
                    existing.archive_mode = EventKnowledgeArchive.MODE_ORGANIZE
                    existing.last_updated_by = member
                    existing.save(
                        update_fields=["archive_mode", "last_updated_by", "updated_at"]
                    )
                return existing, False, upgraded

            subjects = list(
                event.subject_links.select_related("subject").order_by(
                    "-is_primary", "pk"
                )
            )
            evidences = list(
                event.evidence_links.select_related("source_item__source").order_by(
                    "-is_primary", "pk"
                )
            )
            if not evidences:
                raise IntelligenceArchiveError(
                    "这条情报还没有可核查的来源证据，暂时不能保存为知识。"
                )
            primary_subject = subjects[0].subject if subjects else None
            if primary_subject is not None:
                _ensure_subject_knowledge_identity(
                    family=member.family,
                    subject=primary_subject,
                    user=user,
                )

            source, _ = KnowledgeSource.objects.get_or_create(
                family=member.family,
                key="intelligence:people",
                defaults={
                    "kind": KnowledgeSource.KIND_INTELLIGENCE,
                    "name": "AI 情报 · 关注人物",
                    "visibility": KnowledgeVisibility.FAMILY,
                    "allow_cloud_ai": False,
                    "status": KnowledgeSource.STATUS_ACTIVE,
                    "is_enabled": True,
                },
            )
            if source.kind != KnowledgeSource.KIND_INTELLIGENCE:
                raise IntelligenceArchiveError(
                    "知识来源键 intelligence:people 已被其他来源占用。"
                )

            primary_url = ""
            if event.primary_source_item_id:
                primary_url = event.primary_source_item.canonical_url
            if not primary_url and evidences:
                primary_url = evidences[0].source_item.canonical_url
            knowledge_status = (
                KnowledgeDocument.KNOWLEDGE_PENDING
                if add_to_pending
                else KnowledgeDocument.KNOWLEDGE_INCLUDED
            )
            curation_status = (
                KnowledgeDocument.CURATION_INBOX
                if add_to_pending
                else KnowledgeDocument.CURATION_NORMALIZED
            )
            document = KnowledgeDocument.objects.create(
                family=member.family,
                source=source,
                owner=member,
                external_id=f"intelligence-event:{event.pk}",
                title=event.title,
                author=primary_subject.display_name if primary_subject else "",
                section_name="关注人物",
                hierarchy={
                    "source_group": "关注人物",
                    "intelligence_event_id": event.pk,
                    "intelligence_event_path": reverse(
                        "intelligence:event_detail", kwargs={"pk": event.pk}
                    ),
                    "subject_slugs": [link.subject.slug for link in subjects],
                },
                source_url=primary_url,
                visibility=KnowledgeVisibility.FAMILY,
                sync_status=KnowledgeDocument.SYNC_AVAILABLE,
                curation_status=curation_status,
                knowledge_status=knowledge_status,
                library_tier=KnowledgeDocument.LIBRARY_ARCHIVE,
                content_created_at=event.occurred_at,
                content_modified_at=event.occurred_at,
            )
            plain_text = _plain_event_snapshot(event, subjects, evidences)
            normalized_html = _event_snapshot_html(event, subjects, evidences)
            payload = _event_snapshot_payload(event, subjects, evidences)
            raw_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            revision = KnowledgeRevision.objects.create(
                document=document,
                revision_number=1,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
                normalized_hash=hashlib.sha256(plain_text.encode("utf-8")).hexdigest(),
                raw_file="",
                normalized_html=normalized_html,
                plain_text=plain_text,
                converter_version="intelligence-event-v1",
                source_modified_at=event.updated_at,
            )
            revision.raw_file.save(
                f"intelligence-event-{event.pk}.json",
                ContentFile(raw_bytes),
                save=True,
            )
            stored_file = revision.raw_file.name
            storage = revision.raw_file.storage
            document.current_revision = revision
            document.save(update_fields=["current_revision", "updated_at"])
            link = EventKnowledgeArchive.objects.create(
                event=event,
                document=document,
                archive_mode=requested_mode,
                archived_by=member,
                last_updated_by=member,
            )
            index_document(document)
            return link, True, False
    except Exception:
        if stored_file and storage:
            storage.delete(stored_file)
        raise


@transaction.atomic
def create_manual_event(*, cleaned_data, member, user):
    subject = cleaned_data["subject"]
    source, _ = IntelligenceSource.objects.get_or_create(
        subject=subject,
        name=cleaned_data["source_name"].strip(),
        defaults={
            "source_type": IntelligenceSource.TYPE_MANUAL,
            "adapter_key": "manual",
            "source_group": cleaned_data["source_group"],
            "source_tier": cleaned_data["source_tier"],
            "is_active": True,
        },
    )
    source.topics.add(subject)

    source_item_hash = _fingerprint(
        cleaned_data["source_url"],
        cleaned_data["source_title"],
        cleaned_data["occurred_at"].isoformat(),
    )
    source_item = SourceItem.objects.filter(
        source=source,
        content_hash=source_item_hash,
    ).first()
    if source_item is None:
        source_item = SourceItem(
            source=source,
            canonical_url=cleaned_data["source_url"],
            title=cleaned_data["source_title"],
            author_name=cleaned_data["source_author"],
            published_at=cleaned_data["occurred_at"],
            excerpt=cleaned_data["evidence_excerpt"],
            content_hash=source_item_hash,
            content_depth=SourceItem.DEPTH_MANUAL,
            classification_labels=[cleaned_data["event_type"], subject.category],
            relevance_score=cleaned_data["relevance_score"],
            processing_status=SourceItem.STATUS_PUBLISHED,
            processing_reason="人工录入，已完成确定性评分。",
            processed_at=timezone.now(),
        )
        stamp_actor(source_item, user)
        source_item.save()
    source_item.matched_subjects.add(subject)

    cluster_key = _fingerprint(
        str(member.family_id),
        str(subject.pk),
        cleaned_data["title"],
        cleaned_data["occurred_at"].date().isoformat(),
        cleaned_data["source_url"],
    )
    event = IntelligenceEvent.objects.filter(
        family=member.family,
        channel=IntelligenceEvent.CHANNEL_PEOPLE,
        cluster_key=cluster_key,
    ).first()
    created = event is None
    if event is None:
        scoring = calculate_event_scores(
            relevance=cleaned_data["relevance_score"],
            impact=cleaned_data["impact_score"],
            novelty=cleaned_data["novelty_score"],
            actionability=cleaned_data["actionability_score"],
            timeliness=cleaned_data["timeliness_score"],
            source_tier=cleaned_data["source_tier"],
            has_url=bool(cleaned_data["source_url"]),
            has_excerpt=bool(cleaned_data["evidence_excerpt"]),
            source_count=1,
            extraction_confidence=100,
            change_type=cleaned_data["change_type"],
            review_status=cleaned_data["review_status"],
        )
        event = IntelligenceEvent(
            family=member.family,
            channel=IntelligenceEvent.CHANNEL_PEOPLE,
            event_type=cleaned_data["event_type"],
            title=cleaned_data["title"],
            occurred_at=cleaned_data["occurred_at"],
            occurred_precision=cleaned_data["occurred_precision"],
            summary=cleaned_data["summary"],
            why_it_matters=cleaned_data["why_it_matters"],
            relevance_score=cleaned_data["relevance_score"],
            impact_score=cleaned_data["impact_score"],
            novelty_score=cleaned_data["novelty_score"],
            actionability_score=cleaned_data["actionability_score"],
            timeliness_score=cleaned_data["timeliness_score"],
            importance_score=scoring.importance_score,
            confidence_score=scoring.confidence_score,
            change_type=cleaned_data["change_type"],
            review_status=cleaned_data["review_status"],
            selection_status=scoring.selection_status,
            scoring_policy_version=POLICY_VERSION,
            scoring_breakdown=scoring.breakdown,
            score_origin=IntelligenceEvent.SCORE_ORIGIN_MANUAL,
            cluster_key=cluster_key,
            primary_source_item=source_item,
        )
        stamp_actor(event, user)
        event.save()
    else:
        event.last_seen_at = timezone.now()
        stamp_actor(event, user)
        event.save(update_fields=["last_seen_at", "updated_by", "updated_at"])

    EventSubject.objects.get_or_create(
        event=event,
        subject=subject,
        role=EventSubject.ROLE_SUBJECT,
        defaults={"confidence_score": 100, "is_primary": True},
    )
    EventEvidence.objects.get_or_create(
        event=event,
        source_item=source_item,
        defaults={
            "evidence_type": cleaned_data["evidence_type"],
            "excerpt": cleaned_data["evidence_excerpt"],
            "source_quality_score": SOURCE_QUALITY_SCORES[cleaned_data["source_tier"]],
            "is_primary": True,
        },
    )
    CollectionRun.objects.create(
        family=member.family,
        run_kind=CollectionRun.KIND_MANUAL,
        status=CollectionRun.STATUS_SUCCESS,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        discovered_count=1,
        created_count=1 if created else 0,
        updated_count=0 if created else 1,
        normalized_count=1,
        classified_count=1,
        noise_count=1 if event.selection_status == IntelligenceEvent.SELECTION_NOISE else 0,
        clustered_count=1,
        selected_count=1 if event.selection_status == IntelligenceEvent.SELECTION_SELECTED else 0,
        review_count=1 if event.selection_status == IntelligenceEvent.SELECTION_REVIEW else 0,
        parameters={"subject_id": subject.pk, "source_id": source.pk},
        created_by=user,
    )
    return event, created
