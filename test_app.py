import unittest

from app import parse_lead


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


if __name__ == "__main__":
    unittest.main()
