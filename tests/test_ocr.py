import io
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from captcha_ocr import CaptchaRecognizer, prepare_image
from court_client import CourtClient, CourtError


def transparent_image():
    image = Image.new("RGBA", (120, 40), (0, 0, 0, 0))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


class OcrTests(unittest.TestCase):
    def test_transparency_is_composited_to_white(self):
        with Image.open(io.BytesIO(prepare_image(transparent_image()))) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_local_ocr_uses_digits_and_rejects_incomplete_read(self):
        engine = Mock()
        engine.classification.side_effect = ["123456", "1234"]
        with patch.dict("sys.modules", {"ddddocr": types.SimpleNamespace(DdddOcr=Mock(return_value=engine))}):
            reader = CaptchaRecognizer()
            self.assertEqual(reader.recognize(transparent_image()), "123456")
            engine.set_ranges.assert_called_once_with("0123456789")
            with self.assertRaisesRegex(CourtError, "6자리"):
                reader.recognize(transparent_image())

    def test_server_rate_limit_is_not_retried(self):
        client = CourtClient()
        client.validate = Mock()
        client.captcha = Mock(return_value=b"image")
        client.search = Mock(side_effect=CourtError("일시적인 접속 제한입니다.", "RATE_LIMIT"))
        with self.assertRaises(CourtError):
            client.search_auto("법원", "2025고합1", "당사자", Mock())
        self.assertEqual(client.search.call_count, 1)

    def test_explicit_captcha_error_retries_three_times_with_fresh_images(self):
        client = CourtClient()
        client.validate = Mock()
        client._captcha_token, client._captcha_image = "token", b"image"
        reader = Mock()
        reader.recognize.return_value = "123456"
        def search(*args):
            client._captcha_token, client._captcha_image = "", b""
            raise CourtError("보안문자가 틀렸습니다.")
        def captcha():
            client._captcha_token, client._captcha_image = "fresh", b"fresh-image"
            return b"fresh-image"
        client.search, client.captcha = Mock(side_effect=search), Mock(side_effect=captcha)
        with self.assertRaisesRegex(CourtError, "3회"):
            client.search_auto("법원", "2025고합1", "당사자", reader)
        self.assertEqual(client.search.call_count, 3)
        self.assertEqual(client.captcha.call_count, 3)
        reader.recognize.assert_any_call(b"fresh-image")

    def test_existing_captcha_is_replaced_on_each_search(self):
        client = CourtClient()
        client.validate = Mock()
        client._captcha_token, client._captcha_image = "stale", b"stale-image"
        client.captcha = Mock(side_effect=[b"first-fresh", b"second-fresh"])
        client.search = Mock(return_value={"ok": True})
        reader = Mock()
        reader.recognize.return_value = "123456"
        client.search_auto("법원", "2025고합1", "당사자", reader)
        client.search_auto("법원", "2025고합1", "당사자", reader)
        self.assertEqual(client.captcha.call_count, 2)
        self.assertEqual(reader.recognize.call_args_list[0].args, (b"first-fresh",))
        self.assertEqual(reader.recognize.call_args_list[1].args, (b"second-fresh",))

    def test_ocr_incomplete_read_retries_without_submitting_that_answer(self):
        client = CourtClient()
        client.validate = Mock()
        client.captcha = Mock(side_effect=[b"first", b"second"])
        client.search = Mock(return_value={"ok": True})
        reader = Mock()
        reader.recognize.side_effect = [CourtError("보안문자 6자리를 인식하지 못했습니다."), "123456"]
        client.search_auto("법원", "2025고합1", "당사자", reader)
        self.assertEqual(client.captcha.call_count, 2)
        client.search.assert_called_once_with("법원", "2025고합1", "당사자", "123456")


if __name__ == "__main__":
    unittest.main()
