from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from family_core.models import Family, FamilyMember
from .models import KnowledgeSource, KnowledgeDocument, KnowledgeRevision
from .search import index_document


class KnowledgePageEfficiencyTests(TestCase):
    def test_long_body_is_trimmed_in_list_but_search_can_find_tail(self):
        user = get_user_model().objects.create_user(username='knowledge-efficiency')
        family = Family.objects.create(name='Efficiency')
        member = FamilyMember.objects.create(user=user, family=family, display_name='Owner')
        source = KnowledgeSource.objects.create(family=family,owner=member,key='efficiency',kind='onenote',name='Source')
        for i in range(25):
            document = KnowledgeDocument.objects.create(family=family,owner=member,source=source,
                external_id=str(i),title=f'Document {i}',knowledge_status='included',library_tier='knowledge')
            revision = KnowledgeRevision.objects.create(document=document,revision_number=1,
                content_hash=str(i).zfill(64),plain_text='长正文' * 10000 + '末尾目标')
            document.current_revision=revision
            document.save(update_fields=['current_revision'])
            index_document(document)
        self.client.force_login(user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('knowledge:library'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 25)
        self.assertLessEqual(len(queries), 24)
        for entry in response.context['page_obj']:
            self.assertIn('body', entry.get_deferred_fields())
            self.assertLessEqual(len(entry.list_body), 220)
        result = self.client.get(reverse('knowledge:library'), {'q':'末尾目标'})
        self.assertEqual(result.context['page_obj'].paginator.count, 25)
        self.assertIn('末尾目标', result.context['page_obj'][0].hit_snippet)

