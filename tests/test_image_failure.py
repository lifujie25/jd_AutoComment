import logging
import unittest
from unittest.mock import Mock, patch

import requests

import auto_comment_plus as app


OLD_URL = 'https://club.jd.com/discussion/getProductPageImageCommentList.action?productId=100091755723'


class ImageRetryTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('test-image-retry')
        self.get = self.enterContext(patch.object(app.requests, 'get'))
        self.sleep = self.enterContext(patch.object(app.time, 'sleep'))

    def response(self, payload=None, error=None):
        result = Mock(status_code=200, headers={'Content-Type': 'application/json'}, content=b'{}')
        result.json.return_value = payload
        if error:
            result.json.side_effect = error
        return result

    def test_success_only_calls_old_endpoint_once(self):
        images = [{'imageUrl': '//img30.360buyimg.com/shaidan/jfs/a.jpg'}]
        self.get.return_value = self.response({'imgComments': {'imgList': images}})
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), images)
        self.get.assert_called_once_with(OLD_URL, headers={}, timeout=20)
        self.sleep.assert_not_called()

    def test_invalid_json_waits_ten_seconds_then_retries_same_endpoint(self):
        images = [{'imageUrl': '//img30.360buyimg.com/shaidan/jfs/a.jpg'}]
        self.get.side_effect = [self.response(error=ValueError('system busy')),
                               self.response({'imgComments': {'imgList': images}})]
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), images)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.get.call_args_list[0], self.get.call_args_list[1])
        self.sleep.assert_called_once_with(10)

    def test_second_failure_returns_no_images(self):
        self.get.return_value = self.response(error=ValueError('system busy'))
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), [])
        self.assertEqual(self.get.call_count, 2)
        self.sleep.assert_called_once_with(10)

    def test_connection_error_retries_once(self):
        self.get.side_effect = [requests.ConnectionError('down'),
                               self.response({'imgComments': {'imgList': []}})]
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), [])
        self.assertEqual(self.get.call_count, 2)
        self.sleep.assert_called_once_with(10)

    def test_http_error_retries_once(self):
        forbidden = self.response()
        forbidden.status_code = 403
        forbidden.raise_for_status.side_effect = requests.HTTPError('403')
        self.get.side_effect = [forbidden, self.response({'imgComments': {'imgList': []}})]
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), [])
        self.sleep.assert_called_once_with(10)

    def test_empty_list_does_not_retry(self):
        self.get.return_value = self.response({'imgComments': {'imgCommentCount': 0}})
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), [])
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_malformed_list_is_failure(self):
        self.get.return_value = self.response({'imgComments': {'imgList': {}}})
        self.assertEqual(app.fetch_image_comment_list(OLD_URL, {}, self.logger), [])
        self.assertEqual(self.get.call_count, 2)
        self.sleep.assert_called_once_with(10)


if __name__ == '__main__':
    unittest.main()
