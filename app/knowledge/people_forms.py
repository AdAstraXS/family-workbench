from intelligence.forms import IntelligenceSubjectForm
from intelligence.models import IntelligenceSubject


class PersonProfileForm(IntelligenceSubjectForm):
    """Maintain existing person identities without enabling the news pipeline."""

    class Meta(IntelligenceSubjectForm.Meta):
        fields = ["canonical_name", "display_name", "category", "aliases", "profile_summary"]
        labels = {"canonical_name": "标准姓名", "display_name": "显示姓名", "profile_summary": "人物简介"}
        help_texts = {"canonical_name": "用于区分人物，中英文均可；建立后保持不变。"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["knowledge_author_names"].required = True
        self.fields["knowledge_author_names"].label = "关联的历史作者名称"
        self.fields["knowledge_author_names"].help_text = "每行一个，明确归到此人物；不会改写原文作者或启动新闻采集。"
        person_categories = {
            IntelligenceSubject.CATEGORY_TECH_LEADER,
            IntelligenceSubject.CATEGORY_INVESTOR,
            IntelligenceSubject.CATEGORY_POLICY_LEADER,
            IntelligenceSubject.CATEGORY_OTHER,
        }
        self.fields["category"].choices = [
            choice for choice in IntelligenceSubject.CATEGORY_CHOICES if choice[0] in person_categories
        ]
        if self.instance.pk:
            self.fields["canonical_name"].disabled = True
            shared = (
                self.instance.knowledge_identities.exclude(family=self.family).exists()
                or self.instance.family_follows.exclude(family=self.family).exists()
            )
            if shared:
                for name in self.Meta.fields:
                    self.fields[name].disabled = True
                self.fields["display_name"].help_text = "此人物被多个家庭共用，基本资料保持不变；可维护本家庭的作者关联。"
