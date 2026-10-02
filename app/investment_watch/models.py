from django.db import models
from django.utils import timezone
from family_core.models import TimestampedModel


class NewsSource(TimestampedModel):
    family = models.ForeignKey("family_core.Family", on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    key = models.SlugField(max_length=80)
    url = models.URLField(max_length=1000)
    adapter = models.CharField(max_length=30, default="rss")
    market = models.CharField(max_length=30, default="全球")
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=120)
    max_items = models.PositiveIntegerField(default=40)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    cursor = models.JSONField(default=dict)
    public_metadata_only = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["family", "key"], name="watch_source_family_key"
            )
        ]

    def __str__(self):
        return self.name


class InvestmentEvent(TimestampedModel):
    family = models.ForeignKey("family_core.Family", on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=500)
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT
    )
    previous = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="developments",
        on_delete=models.PROTECT,
    )
    merge_reason = models.CharField(max_length=500, blank=True)
    merged_by = models.ForeignKey(
        "family_core.FamilyMember", null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["family", "fingerprint"], name="watch_event_fingerprint"
            )
        ]


class NewsMaterial(TimestampedModel):
    source = models.ForeignKey(
        NewsSource, on_delete=models.PROTECT, related_name="materials"
    )
    external_id = models.CharField(max_length=500)
    event = models.ForeignKey(
        InvestmentEvent, on_delete=models.PROTECT, related_name="materials"
    )
    current_version = models.ForeignKey(
        "MaterialVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    official_document = models.ForeignKey(
        "investment_research.OfficialResearchDocument",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="watch_material_identity"
            )
        ]


class EventAction(models.Model):
    event = models.ForeignKey(
        InvestmentEvent, on_delete=models.PROTECT, related_name="actions"
    )
    target = models.ForeignKey(
        InvestmentEvent,
        null=True,
        on_delete=models.PROTECT,
        related_name="target_actions",
    )
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    action = models.CharField(max_length=20)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)


class MaterialVersion(models.Model):
    material = models.ForeignKey(
        NewsMaterial, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=500)
    summary = models.TextField()
    url = models.URLField(max_length=1000)
    content_hash = models.CharField(max_length=64)
    published_at = models.DateTimeField(null=True, blank=True)
    published_precision = models.CharField(max_length=12, default="time")
    occurred_at = models.DateTimeField(null=True, blank=True)
    found_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=20,
        choices=[("active", "有效"), ("corrected", "已更正"), ("withdrawn", "已撤回")],
        default="active",
    )
    market = models.CharField(max_length=30, default="全球")
    category = models.CharField(max_length=40, default="公司")
    topics = models.JSONField(default=list)
    original_chain = models.CharField(max_length=100)
    official_version = models.ForeignKey(
        "investment_research.OfficialResearchContentVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["material", "number"], name="watch_material_version"
            )
        ]
        indexes = [
            models.Index(fields=["-published_at", "-id"], name="watch_version_date")
        ]


class WatchRule(TimestampedModel):
    dossier = models.OneToOneField(
        "investment_research.ResearchDossier",
        on_delete=models.PROTECT,
        related_name="watch_rule",
    )
    aliases = models.JSONField(default=list)
    topics = models.JSONField(default=list)
    include = models.JSONField(default=list)
    exclude = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)


class ResearchCandidate(TimestampedModel):
    dossier = models.ForeignKey(
        "investment_research.ResearchDossier",
        on_delete=models.PROTECT,
        related_name="news_candidates",
    )
    material_version = models.ForeignKey(MaterialVersion, on_delete=models.PROTECT)
    revision = models.ForeignKey(
        "investment_research.ResearchThesisRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    manual = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)
    rule_version = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    selected_for_research = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dossier", "material_version", "revision"],
                name="watch_candidate_unique",
            )
        ]


class ThesisEvidence(models.Model):
    candidate = models.ForeignKey(
        ResearchCandidate, on_delete=models.PROTECT, related_name="evidence"
    )
    revision = models.ForeignKey(
        "investment_research.ResearchThesisRevision", on_delete=models.PROTECT
    )
    assumption_key = models.CharField(max_length=40)
    direction = models.CharField(
        max_length=20,
        choices=[
            ("support", "有支持"),
            ("weaken", "可能削弱"),
            ("mixed", "影响有分歧"),
            ("unknown", "证据不足"),
        ],
    )
    explanation = models.TextField()
    source_claim = models.TextField(blank=True)
    author_opinion = models.TextField(blank=True)
    input_relation = models.ForeignKey(
        "MaterialRelation", null=True, blank=True, on_delete=models.PROTECT
    )
    quote = models.TextField(blank=True)
    locator = models.CharField(max_length=100, blank=True)
    conditions = models.TextField(blank=True)
    gaps = models.TextField(blank=True)
    method = models.CharField(max_length=20, default="model")
    created_at = models.DateTimeField(auto_now_add=True)
    input_key = models.CharField(max_length=64)
    input_body = models.ForeignKey(
        "BodySnapshot", null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["input_key", "assumption_key"], name="watch_evidence_input"
            )
        ]


class EvidenceReview(models.Model):
    evidence = models.ForeignKey(
        ThesisEvidence, on_delete=models.PROTECT, related_name="reviews"
    )
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    direction = models.CharField(max_length=20)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class MemberAnnotation(TimestampedModel):
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    event = models.ForeignKey(InvestmentEvent, on_delete=models.PROTECT)
    saved = models.BooleanField(default=False)
    read = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["member", "event"], name="watch_annotation_owner"
            )
        ]


class WatchRun(TimestampedModel):
    dossier = models.ForeignKey(
        "investment_research.ResearchDossier", on_delete=models.PROTECT
    )
    revision = models.ForeignKey(
        "investment_research.ResearchThesisRevision",
        null=True,
        on_delete=models.PROTECT,
    )
    input_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=30, default="queued")
    message = models.CharField(max_length=500, blank=True)
    count = models.PositiveIntegerField(default=0)


class BudgetReceipt(TimestampedModel):
    family = models.ForeignKey("family_core.Family", on_delete=models.PROTECT)
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    provider = models.ForeignKey("ai_analysis.AiProvider", on_delete=models.PROTECT)
    input_key = models.CharField(max_length=64, unique=True)
    reserved_cny = models.DecimalField(max_digits=12, decimal_places=6)
    actual_cny = models.DecimalField(max_digits=12, decimal_places=6, null=True)
    status = models.CharField(max_length=20, default="reserved")


class WatchConsent(TimestampedModel):
    dossier = models.OneToOneField(
        "investment_research.ResearchDossier", on_delete=models.PROTECT
    )
    provider = models.ForeignKey("ai_analysis.AiProvider", on_delete=models.PROTECT)
    active = models.BooleanField(default=False)
    # Changing endpoint/model requires renewed consent; provider ID alone is insufficient.
    provider_signature = models.CharField(max_length=64)
    authorized_by = models.ForeignKey(
        "family_core.FamilyMember", on_delete=models.PROTECT
    )


class OperationReceipt(models.Model):
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    operation = models.CharField(max_length=80)
    key = models.CharField(max_length=100)
    body_hash = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["member", "operation", "key"], name="watch_operation_unique"
            )
        ]


class WorkerLease(models.Model):
    key = models.CharField(max_length=100, unique=True)
    token = models.CharField(max_length=64)
    expires_at = models.DateTimeField()


class MaterialRelation(models.Model):
    source = models.ForeignKey(
        MaterialVersion, on_delete=models.PROTECT, related_name="relation_history"
    )
    target = models.ForeignKey(
        MaterialVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="related_reports",
    )
    kind = models.CharField(
        max_length=20,
        choices=[
            ("duplicate", "同一证据，无新增信息"),
            ("followup", "新增进展"),
            ("conflict", "存在矛盾"),
            ("independent", "独立材料 / 撤销关系"),
        ],
    )
    reason = models.CharField(max_length=500)
    member = models.ForeignKey("family_core.FamilyMember", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class WatchPipelineState(models.Model):
    family = models.OneToOneField("family_core.Family", on_delete=models.PROTECT)
    started_at = models.DateTimeField(default=timezone.now)


class ScreeningBatch(TimestampedModel):
    dossier = models.ForeignKey("investment_research.ResearchDossier", on_delete=models.PROTECT)
    input_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, default="reserved")
    message = models.CharField(max_length=500, blank=True)


class CandidateScreening(models.Model):
    candidate = models.ForeignKey(ResearchCandidate, on_delete=models.PROTECT, related_name="screenings")
    batch = models.ForeignKey(ScreeningBatch, on_delete=models.PROTECT)
    input_key = models.CharField(max_length=64, unique=True)
    selected = models.BooleanField(default=False)
    priority = models.PositiveSmallIntegerField(default=0)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)


class BodySnapshot(models.Model):
    material_version = models.OneToOneField(MaterialVersion, on_delete=models.PROTECT, related_name="body_snapshot")
    source_url = models.URLField(max_length=1000)
    text = models.TextField()
    content_hash = models.CharField(max_length=64)
    raw_gzip = models.BinaryField()
    fetched_at = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=30, default="firecrawl-v2")


class BodyAttempt(TimestampedModel):
    family = models.ForeignKey("family_core.Family", on_delete=models.PROTECT)
    security = models.ForeignKey("portfolio.Security", on_delete=models.PROTECT)
    candidate = models.ForeignKey(ResearchCandidate, on_delete=models.PROTECT)
    material_version = models.ForeignKey(MaterialVersion, on_delete=models.PROTECT)
    day = models.DateField()
    status = models.CharField(max_length=20, default="reserved")
    message = models.CharField(max_length=500, blank=True)
    snapshot = models.ForeignKey(BodySnapshot, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["family", "security", "material_version"], name="watch_body_attempt_once"
        )]
        indexes = [models.Index(fields=["family", "security", "day"], name="watch_body_daily")]
