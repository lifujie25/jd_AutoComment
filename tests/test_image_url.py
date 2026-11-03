import unittest

import auto_comment_plus as app


class ImageUrlTests(unittest.TestCase):
    def test_raw_jfs_path_is_used_without_resize_prefix(self):
        value = "jfs/t1/519734/13/14261/91412/6abc/001532a4383b7c12.jpg"
        self.assertEqual(
            app.build_uploaded_image_url(value),
            "//img30.360buyimg.com/shaidan/jfs/t1/519734/13/14261/91412/6abc/001532a4383b7c12.jpg",
        )

    def test_existing_resize_prefix_is_removed(self):
        value = "s645x515_jfs/t1/519734/13/14261/91412/6abc/001532a4383b7c12.jpg"
        self.assertNotIn("s645x515_", app.build_uploaded_image_url(value))

    def test_full_url_is_normalized(self):
        value = "https://img30.360buyimg.com/shaidan/s645x515_jfs/t1/a.jpg.dpg"
        self.assertEqual(
            app.build_uploaded_image_url(value),
            "//img30.360buyimg.com/shaidan/jfs/t1/a.jpg",
        )

    def test_invalid_response_is_rejected(self):
        with self.assertRaises(ValueError):
            app.build_uploaded_image_url("e20005")


if __name__ == "__main__":
    unittest.main()
