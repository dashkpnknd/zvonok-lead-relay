import io
import unittest
from unittest.mock import patch

from app import download_bot_photo, format_lead, format_report, message_to_html, parse_lead


class ParseLeadTests(unittest.TestCase):
    def test_parses_zvonok_email_style_payload(self):
        lead = parse_lead(
            {
                "phone": "+79001234567",
                "call_id": "123",
                "campaign_id": "456",
                "completed_date": "2026-09-15T12:00:00+00:00",
                "recorded_audio_url": "https://example.test/record.mp3",
            }
        )
        self.assertEqual(lead.event_id, "123")
        self.assertEqual(lead.phone, "+79001234567")
        self.assertEqual(lead.audio_url, "https://example.test/record.mp3")

    def test_requires_phone(self):
        with self.assertRaises(ValueError):
            parse_lead({"call_id": "123"})

    def test_parses_advanced_campaign_postback_fields(self):
        lead = parse_lead(
            {
                "ct_phone": "+79990000000",
                "ct_call_id": "42",
                "ct_campaign_id": "99",
                "ct_completed": "2026-09-15 10:00:00",
                "ct_record_url": "https://example.test/record.wav",
            }
        )
        self.assertEqual(lead.phone, "+79990000000")
        self.assertEqual(lead.event_id, "42")
        self.assertEqual(lead.campaign_id, "99")

    def test_expands_relative_recording_url(self):
        lead = parse_lead({"phone": "+79990000000", "audio_url": "/record_cdr/example/"})
        self.assertEqual(lead.audio_url, "https://zvonok.com/record_cdr/example/")

    def test_formats_lead_with_counter(self):
        self.assertEqual(format_lead("+79990000000", 4), "Телефон: +79990000000\nЛид №4")

    def test_preserves_bold_formatting_from_bot_message(self):
        message = {"text": "Добрый день, тест", "entities": [{"type": "bold", "offset": 13, "length": 4}]}
        self.assertEqual(message_to_html(message), "Добрый день, <b>тест</b>")

    def test_formats_short_outreach_report(self):
        self.assertEqual(
            format_report(4, "+79990000000", "123", "sent", None, "Очередь: нет"),
            "Лид №4\nТелефон: +79990000000\nСтатус: Отправлено\nID: <code>123</code>\nОчередь: нет",
        )
        self.assertEqual(
            format_report(4, "+79990000000", "123", "routed_no_tg", None, "Очередь: нет"),
            "Лид №4\nТелефон: +79990000000\nСтатус: Не отправлено → второй чат\nID: <code>123</code>\nОчередь: нет",
        )

    @patch("app.urllib.request.urlopen")
    @patch("app.telegram_request", return_value={"result": {"file_path": "photos/image.jpg"}})
    def test_downloaded_photo_keeps_jpeg_filename_for_telethon(self, _request, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b"jpeg-bytes"

        photo, filename = download_bot_photo("photo-id")

        self.assertIsInstance(photo, io.BytesIO)
        self.assertEqual(filename, "image.jpg")
        self.assertEqual(photo.name, "image.jpg")


if __name__ == "__main__":
    unittest.main()
