import unittest

from app import format_lead, parse_lead


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
        self.assertEqual(format_lead("+79990000000", 4), "Телефон: <code>+79990000000</code>\nЛид №4")


if __name__ == "__main__":
    unittest.main()
