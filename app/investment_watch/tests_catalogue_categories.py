from django.test import SimpleTestCase
from .catalogue import classify


class CategoryTests(SimpleTestCase):
    def test_precious_metals_and_coal_do_not_fall_back_to_company(self):
        for title in ['白银价格下跌', '煤矿发生事故影响供给', 'silver prices decline', 'coal supply disruption']:
            self.assertEqual(classify(title, '', '全球')[1], '商品')

    def test_unknown_item_is_not_assumed_to_be_a_company(self):
        self.assertEqual(classify('不明财经动态', '', '全球')[1], '行业')
        self.assertEqual(classify('某公司发布财报', '', '全球')[1], '公司')

