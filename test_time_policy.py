import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import extended_care_fee_service
import routers.attendance as attendance_router
import routers.daily_contacts as daily_contacts_router


class BusinessTimePolicyTests(unittest.TestCase):
    def test_jst_business_date_is_used_while_utc_is_previous_day(self):
        fixed_utc = datetime(2026, 7, 7, 22, 30, tzinfo=timezone.utc)
        with patch("time_utils.utc_now", return_value=fixed_utc):
            self.assertEqual(extended_care_fee_service.parse_month(None)[0], "2026-07")
            self.assertEqual(
                attendance_router._parse_target_date(None).isoformat(),
                "2026-07-08",
            )
            self.assertEqual(
                daily_contacts_router._parse_target_date(None).isoformat(),
                "2026-07-08",
            )


if __name__ == "__main__":
    unittest.main()
