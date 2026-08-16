import unicodedata

from django.db import migrations, models
import django.db.models.deletion


def _normalized(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.strip().split()).casefold()


def backfill_taxonomy_and_runs(apps, schema_editor):
    Category = apps.get_model("knowledge", "KnowledgeCategory")
    CurationRevision = apps.get_model("knowledge", "KnowledgeCurationRevision")
    Document = apps.get_model("knowledge", "KnowledgeDocument")
    Proposal = apps.get_model("knowledge", "KnowledgeProposal")
    ProposalRun = apps.get_model("knowledge", "KnowledgeProposalRun")
    SearchEntry = apps.get_model("knowledge", "KnowledgeSearchEntry")
    Tag = apps.get_model("knowledge", "KnowledgeTag")

    category_names = {}
    tag_names = {}
    for document in Document.objects.all().order_by("family_id", "id").iterator():
        changed = []
        category = " ".join(str(document.category or "").strip().split())[:100]
        if category:
            key = (document.family_id, _normalized(category))
            canonical = category_names.get(key)
            if canonical is None:
                item, _ = Category.objects.get_or_create(
                    family_id=document.family_id,
                    normalized_name=key[1],
                    defaults={"name": category},
                )
                canonical = item.name
                category_names[key] = canonical
            if document.category != canonical:
                document.category = canonical
                changed.append("category")
        canonical_tags = []
        seen = set()
        for value in document.tags or []:
            name = " ".join(str(value or "").strip().split())[:30]
            normalized = _normalized(name)
            if not normalized:
                continue
            key = (document.family_id, normalized)
            canonical = tag_names.get(key)
            if canonical is None:
                item, _ = Tag.objects.get_or_create(
                    family_id=document.family_id,
                    normalized_name=normalized,
                    defaults={"name": name},
                )
                canonical = item.name
                tag_names[key] = canonical
            if canonical not in seen:
                canonical_tags.append(canonical)
                seen.add(canonical)
        if list(document.tags or []) != canonical_tags:
            document.tags = canonical_tags
            changed.append("tags")
        if changed:
            document.save(update_fields=changed)
            SearchEntry.objects.filter(document_id=document.pk).update(
                category=document.category,
                tags=document.tags,
                tags_text=" ".join(document.tags),
            )
        if document.confirmed_summary or document.category or document.tags:
            CurationRevision.objects.get_or_create(
                document_id=document.pk,
                sequence=1,
                defaults={
                    "summary": document.confirmed_summary,
                    "category": document.category,
                    "tags": document.tags,
                    "change_type": "legacy",
                },
            )

    sequences = {}
    runs = {}
    proposals = Proposal.objects.all().order_by("document_id", "created_at", "id")
    for proposal in proposals.iterator():
        key = (
            proposal.document_id,
            proposal.revision_id,
            proposal.prompt_version,
            proposal.model_name,
        )
        run = runs.get(key)
        if run is None:
            sequence = sequences.get(proposal.document_id, 0) + 1
            sequences[proposal.document_id] = sequence
            run = ProposalRun.objects.create(
                document_id=proposal.document_id,
                revision_id=proposal.revision_id,
                sequence=sequence,
                model_name=proposal.model_name,
                prompt_version=proposal.prompt_version,
                content_hash=proposal.content_hash,
            )
            runs[key] = run
        proposal.run_id = run.pk
        proposal.save(update_fields=["run"])


class Migration(migrations.Migration):
    dependencies = [
        ("ai_analysis", "0004_add_zhipu_vision_provider"),
        ("knowledge", "0009_knowledgeimportbatch_person_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="KnowledgeCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=100, verbose_name="分类名称")),
                ("normalized_name", models.CharField(max_length=100, verbose_name="规范名称")),
                ("description", models.CharField(blank=True, max_length=300, verbose_name="说明")),
                ("aliases", models.JSONField(blank=True, default=list, verbose_name="历史名称与别名")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_knowledge_categories", to="family_core.familymember", verbose_name="创建人")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="knowledge_categories", to="family_core.family", verbose_name="所属家庭")),
                ("merged_into", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="merged_categories", to="knowledge.knowledgecategory", verbose_name="已合并到")),
            ],
            options={"verbose_name": "知识分类", "verbose_name_plural": "知识分类", "ordering": ["name", "id"]},
        ),
        migrations.CreateModel(
            name="KnowledgeTag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("name", models.CharField(max_length=30, verbose_name="标签名称")),
                ("normalized_name", models.CharField(max_length=30, verbose_name="规范名称")),
                ("description", models.CharField(blank=True, max_length=300, verbose_name="说明")),
                ("aliases", models.JSONField(blank=True, default=list, verbose_name="历史名称与别名")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_knowledge_tags", to="family_core.familymember", verbose_name="创建人")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="knowledge_tags", to="family_core.family", verbose_name="所属家庭")),
                ("merged_into", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="merged_tags", to="knowledge.knowledgetag", verbose_name="已合并到")),
            ],
            options={"verbose_name": "知识标签", "verbose_name_plural": "知识标签", "ordering": ["name", "id"]},
        ),
        migrations.CreateModel(
            name="KnowledgeProposalRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField(verbose_name="整理轮次")),
                ("model_name", models.CharField(max_length=200, verbose_name="模型")),
                ("prompt_version", models.CharField(max_length=50, verbose_name="提示词版本")),
                ("content_hash", models.CharField(max_length=64, verbose_name="输入内容哈希")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="生成时间")),
                ("analysis_request", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="knowledge_proposal_run", to="ai_analysis.aianalysisrequest", verbose_name="AI 请求")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="proposal_runs", to="knowledge.knowledgedocument", verbose_name="知识文档")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="knowledge_proposal_runs", to="family_core.familymember", verbose_name="发起成员")),
                ("revision", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="proposal_runs", to="knowledge.knowledgerevision", verbose_name="对应内容版本")),
            ],
            options={"verbose_name": "知识 AI 整理轮次", "verbose_name_plural": "知识 AI 整理轮次", "ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="KnowledgeCurationRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField(verbose_name="整理版本")),
                ("summary", models.TextField(blank=True, verbose_name="正式摘要")),
                ("category", models.CharField(blank=True, max_length=100, verbose_name="正式分类")),
                ("tags", models.JSONField(blank=True, default=list, verbose_name="正式标签")),
                ("change_type", models.CharField(choices=[("manual", "人工整理"), ("ai_confirmed", "确认 AI 建议"), ("legacy", "既有整理结果")], max_length=30, verbose_name="整理方式")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="保存时间")),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="knowledge_curation_revisions", to="family_core.familymember", verbose_name="整理人")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="curation_revisions", to="knowledge.knowledgedocument", verbose_name="知识文档")),
                ("proposal_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="curation_revisions", to="knowledge.knowledgeproposalrun", verbose_name="采用的 AI 整理轮次")),
            ],
            options={"verbose_name": "知识正式整理版本", "verbose_name_plural": "知识正式整理版本", "ordering": ["-sequence", "-id"]},
        ),
        migrations.AddField(
            model_name="knowledgeproposal",
            name="run",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="proposals", to="knowledge.knowledgeproposalrun", verbose_name="整理轮次"),
        ),
        migrations.AddConstraint(
            model_name="knowledgecategory",
            constraint=models.UniqueConstraint(fields=("family", "normalized_name"), name="unique_knowledge_category_name_per_family"),
        ),
        migrations.AddConstraint(
            model_name="knowledgetag",
            constraint=models.UniqueConstraint(fields=("family", "normalized_name"), name="unique_knowledge_tag_name_per_family"),
        ),
        migrations.AddConstraint(
            model_name="knowledgeproposalrun",
            constraint=models.UniqueConstraint(fields=("document", "sequence"), name="unique_knowledge_proposal_run_sequence"),
        ),
        migrations.AddConstraint(
            model_name="knowledgecurationrevision",
            constraint=models.UniqueConstraint(fields=("document", "sequence"), name="unique_knowledge_curation_revision_sequence"),
        ),
        migrations.AddIndex(model_name="knowledgecategory", index=models.Index(fields=["family", "is_active", "name"], name="knowledge_k_family__7ea225_idx")),
        migrations.AddIndex(model_name="knowledgetag", index=models.Index(fields=["family", "is_active", "name"], name="knowledge_k_family__189868_idx")),
        migrations.AddIndex(model_name="knowledgeproposalrun", index=models.Index(fields=["document", "created_at"], name="knowledge_k_documen_a13fb8_idx")),
        migrations.AddIndex(model_name="knowledgeproposalrun", index=models.Index(fields=["revision", "created_at"], name="knowledge_k_revisio_931804_idx")),
        migrations.RunPython(backfill_taxonomy_and_runs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="knowledgeproposal",
            name="run",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="proposals", to="knowledge.knowledgeproposalrun", verbose_name="整理轮次"),
        ),
        migrations.RemoveConstraint(
            model_name="knowledgeproposal",
            name="unique_knowledge_proposal_per_revision_type_prompt",
        ),
        migrations.AddConstraint(
            model_name="knowledgeproposal",
            constraint=models.UniqueConstraint(fields=("run", "proposal_type"), name="unique_knowledge_proposal_type_per_run"),
        ),
    ]
