import sys
import types
import unittest


requests = types.ModuleType("requests")
requests.RequestException = Exception
requests.get = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests)

pypdf = types.ModuleType("pypdf")
pypdf.PdfReader = object
sys.modules.setdefault("pypdf", pypdf)

import scraper


class PdfClosureOverrideTests(unittest.TestCase):
    def setUp(self):
        self.center = {"id": "test_center", "label": "Test Center"}

    def test_holiday_date_without_pdf_closure_is_not_added(self):
        text = "\n".join([
            "July 2026 Gym Schedule",
            "3",
            "Open Basketball",
            "9:00am-12:00pm",
            "4",
            "Open Badminton",
            "9:00am-2:00pm",
        ])

        self.assertEqual(
            scraper.detect_pdf_closure_overrides(self.center, text, "July 2026"),
            [],
        )

    def test_pdf_closed_cell_creates_center_specific_override(self):
        text = "\n".join([
            "July 2026 Gym Schedule",
            "3",
            "Facility Closed",
            "4",
            "Open Badminton",
            "9:00am-2:00pm",
        ])

        self.assertEqual(
            scraper.detect_pdf_closure_overrides(self.center, text, "July 2026"),
            [{
                "date": "2026-07-03",
                "center": "test_center",
                "blocked": True,
                "all_day": True,
                "label": "Test Center Closed",
                "sh": 9,
                "sm": 0,
                "eh": 21,
                "em": 0,
                "sport": "Other",
                "_source": "pdf_closure",
            }],
        )

    def test_old_month_override_is_not_rolled_to_new_holiday(self):
        overrides = [{
            "date": "2026-06-19",
            "center": "test_center",
            "label": "Test Center Closed",
            "_source": "pdf_closure",
        }]

        self.assertEqual(
            scraper.roll_date_overrides(overrides, "June 2026", "July 2026"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
