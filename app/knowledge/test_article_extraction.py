import os
import subprocess
from unittest.mock import patch

from django.test import SimpleTestCase

from .article_extraction import MAX_INPUT_BYTES, extract_article, runtime_environment, runtime_ready
from .web_capture import normalize_web_html
from .web_fetch import WebCaptureError

PARAGRAPH = "家庭知识是一种长期认知资产。阅读时保留连续段落和图注，整理时保留来源。正文中的广告研究、评论分析等词语属于原文，不能因为出现关键词而误删。"


def page(body, url="https://example.com/article"):
    return {"url": url, "html": '<html><head><title>长期认知资产</title></head><body>' + body + '</body></html>', "rawHtml": "", "metadata": {}}


class ArticleExtractionTests(SimpleTestCase):
    def test_runtime_is_installed(self):
        self.assertTrue(runtime_ready())

    def test_generic_article_keeps_body_figures_tables_inline_links(self):
        snapshot = page('<header>网站页头</header><nav>导航菜单</nav><article><h1>长期认知资产</h1>'
                        + '<p>' + PARAGRAPH * 4 + '<a href="/citation">原文出处</a></p>'
                        + '<figure><img data-src="/chart.jpg"><figcaption>正文图注</figcaption></figure>'
                        + '<table><tr><th>年份</th><td>2026</td></tr></table></article>'
                        + '<aside><img src="/ad.jpg">侧栏广告</aside><div class="comments"><p>评论留言</p></div>'
                        + '<section class="related-articles"><img src="/rec.jpg">相关新闻</section><footer>网站页脚</footer>')
        article = extract_article(snapshot)
        self.assertEqual(article["method"], "mozilla-readability")
        self.assertEqual(article["image_count"], 1)
        self.assertIn('https://example.com/chart.jpg', article["html"])
        for expected in ["正文图注", "<table", "原文出处", "广告研究", "评论分析"]:
            self.assertIn(expected, article["html"])
        for noise in ["评论留言", "相关新闻", "网站页脚", "侧栏广告"]:
            self.assertNotIn(noise, article["html"])

    def test_unknown_div_layout_and_english_blog_are_supported(self):
        for text in [PARAGRAPH, "This is a continuous paragraph about preserving original evidence, clear attribution, and long term reading. "]:
            with self.subTest(text=text[:10]):
                article = extract_article(page('<div class="post-body"><h2>Article</h2><p>' + text * 6 + '</p></div>'))
                self.assertIn(text.strip(), article["html"])

    def test_futunn_scopes_body_excludes_comments_recommendation_and_boundary_pixel(self):
        snapshot = page('<div id="newsDetail"><h1>富途文章</h1><div id="content"><div class="origin_content">'
                        + '<p>' + PARAGRAPH * 4 + '</p><img class="boundary-pic" src="https://record.futunn.com/pixel.png">'
                        + '<figure><img data-src="https://postimg.futunn.com/chart.webp"><figcaption>图表说明</figcaption></figure>'
                        + '</div></div><div id="feedCommentBox">留言<img src="/avatar.png"></div>'
                        + '<section class="recommendBox">热点推荐<img src="/rec.jpg"></section></div>', "https://news.futunn.com/post/123")
        article = extract_article(snapshot)
        self.assertIn("+futunn", article["method"])
        self.assertEqual(article["image_count"], 1)
        self.assertNotIn("record.futunn", article["html"])
        self.assertNotIn("热点推荐", article["html"])
        self.assertIn("图表说明", article["html"])

    def test_site_rule_miss_falls_back_to_readability_not_whole_page(self):
        article = extract_article(page('<article><p>' + PARAGRAPH * 4 + '</p></article><nav>网站改版</nav>', "https://news.futunn.com/post/123"))
        self.assertEqual(article["method"], "mozilla-readability")
        self.assertNotIn("网站改版", article["html"])

    def test_images_are_not_removed_just_because_small_or_lazy(self):
        article = extract_article(page('<article><p>' + PARAGRAPH * 4 + '</p><img width="80" height="40" data-original="/formula.png">'
                                      + '<picture><source srcset="/small.jpg 1x, /large.jpg 2x"><img data-srcset="/small.jpg 1x, /large.jpg 2x"></picture>'
                                      + '<img width="1" height="1" src="/tracker.gif"><div style="display: none"><img src="/hidden.jpg"></div></article>'))
        self.assertEqual(article["image_count"], 2)
        self.assertIn("formula.png", article["html"])
        self.assertIn("large.jpg", article["html"])
        self.assertNotIn("tracker.gif", article["html"])
        self.assertNotIn("hidden.jpg", article["html"])

    def test_raw_html_used_before_upstream_filtered_html(self):
        snapshot = page('<p>不完整摘要</p>')
        snapshot["rawHtml"] = page('<article><p>' + PARAGRAPH * 5 + '</p></article>')["html"]
        self.assertIn(PARAGRAPH, extract_article(snapshot)["html"])

    def test_empty_render_shell_can_use_acquired_rendered_html(self):
        snapshot = page('<article><p>' + PARAGRAPH * 5 + '</p></article>')
        snapshot["rawHtml"] = '<html><body><div id="app"></div></body></html>'
        self.assertIn(PARAGRAPH, extract_article(snapshot)["html"])

    def test_scripts_are_inert_and_output_is_sanitized(self):
        snapshot = page('<article><p>' + PARAGRAPH * 4 + '</p><script>process.exit(9)</script><img src="/chart.jpg" onerror="steal()">'
                        + '<a href="javascript:steal()">原文链接</a></article><iframe src="http://127.0.0.1/"></iframe>')
        snapshot["article"] = extract_article(snapshot)
        safe, _ = normalize_web_html(snapshot, {"https://example.com/chart.jpg": "/knowledge/assets/1/download/"})
        for unsafe in ["process.exit", "onerror", "javascript:", "iframe", "steal()"]:
            self.assertNotIn(unsafe, safe)

    def test_noise_only_short_text_and_login_are_rejected(self):
        for body in ['<nav><a href="/a">菜单</a></nav>', '<p>正文过短</p>', '<article><p>登录后阅读' + '订阅提醒。' * 40 + '</p></article>']:
            with self.subTest(body=body[:20]), self.assertRaisesMessage(WebCaptureError, "正文识别待确认"):
                extract_article(page(body))

    def test_old_snapshot_is_not_implicitly_reextracted(self):
        snapshot = page('<p>旧版本仍保留原文</p><img src="/old.png">')
        with patch('knowledge.article_extraction.subprocess.run') as runner:
            safe, text = normalize_web_html(snapshot)
        self.assertIn("旧版本仍保留原文", text)
        runner.assert_not_called()

    def test_runtime_failure_timeout_and_malformed_response_are_safe(self):
        cases = [(FileNotFoundError(), "运行环境尚未安装"), (subprocess.TimeoutExpired('node', 15), "超时"),
                 (None, "正文识别待确认")]
        for failure, message in cases:
            with self.subTest(message=message), patch('knowledge.article_extraction.subprocess.run', side_effect=failure,
                                                     return_value=subprocess.CompletedProcess([], 0, b'not-json')):
                with self.assertRaisesMessage(WebCaptureError, message):
                    extract_article(page('<p>' + PARAGRAPH * 4 + '</p>'))

    def test_input_limit_and_secret_environment(self):
        with self.assertRaisesMessage(WebCaptureError, "网页过大"):
            extract_article(page('x' * MAX_INPUT_BYTES))
        with patch.dict(os.environ, {'SECRET_KEY': 'private-test-only', 'DATABASE_URL': 'private-test-only', 'NODE_OPTIONS': '--inspect'}):
            env = runtime_environment()
            for secret in ['SECRET_KEY', 'DATABASE_URL', 'NODE_OPTIONS']:
                self.assertNotIn(secret, env)
