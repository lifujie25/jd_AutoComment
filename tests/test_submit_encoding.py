import logging
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote

import requests

import auto_comment_plus as app


class SubmitEncodingTests(unittest.TestCase):
    def setUp(self):
        self.opts = {"logger": logging.getLogger("test-submit")}
        self.text = "这个商品收到后检查过，外观和使用体验都符合预期。"

    @patch.object(app.requests, "post")
    def test_original_percent_encoded_content_is_sent(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"success": True})
        self.assertTrue(app.submit_evaluation(
            "https://club.jd.com/myJdcomments/saveProductComment.action",
            {"content": self.text, "productId": "12345"}, {}, self.opts, "测试",
        ))
        sent = post.call_args.kwargs["data"]
        self.assertNotIn("这个商品", sent["content"])
        self.assertEqual(unquote(sent["content"]), self.text)
        wire = requests.Request("POST", "https://club.jd.com/", data=sent).prepare().body
        self.assertIn("content=%25E8", wire)
        self.assertEqual(unquote(parse_qs(wire)["content"][0]), self.text)

    @patch.object(app.requests, "post")
    def test_dry_run_does_not_submit(self, post):
        self.assertFalse(app.submit_evaluation(
            "https://club.jd.com/myJdcomments/saveProductComment.action",
            {"content": self.text}, {}, {**self.opts, "dry_run": True}, "测试",
        ))
        post.assert_not_called()

    @patch.object(app.requests, "post")
    def test_short_comment_does_not_submit(self, post):
        self.assertFalse(app.submit_evaluation(
            "https://club.jd.com/myJdcomments/saveProductComment.action",
            {"content": "短评"}, {}, self.opts, "测试",
        ))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
