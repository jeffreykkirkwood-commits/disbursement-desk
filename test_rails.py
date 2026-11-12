# Behavior of simulated rail timing: cutoffs, weekend and holiday rollover,
# per-rail demo limits, and each return path. Pure-function tests do not
# open a database; one wiring test checks that get_payout exposes rail_timing
# and that the Desk HTML still does not mention settlement or return codes.
"""Tests for rails.py settlement, cutoff, holiday and return-path behavior."""
import unittest
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
import desk
import payout_rules
import payouts
import rails
from rails import ET


# Tuesday 10 November 2026 is a business day in the demo calendar.
# Wednesday 11 November 2026 is a synthetic holiday.
TUE = date(2026, 11, 10)
WED_HOLIDAY = date(2026, 11, 11)
THU = date(2026, 11, 12)
FRI = date(2026, 11, 13)
SAT = date(2026, 11, 14)
SUN = date(2026, 11, 15)
MON = date(2026, 11, 16)
TUE_AFTER = date(2026, 11, 17)
# Friday 6 November 2026, used for weekend rollover from late Friday.
FRI_BEFORE = date(2026, 11, 6)
MON_AFTER_WEEKEND = date(2026, 11, 9)


def et(day, hour, minute=0, second=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET)


def settlement_day(rail, submitted_at):
    return rails.expected_settlement(rail, submitted_at).date()


class RailTableTest(unittest.TestCase):

    def test_specs_cover_every_stored_rail_and_no_others(self):
        self.assertEqual(set(rails.RAIL_SPECS), set(payouts.RAILS))

    def test_fednow_is_separate_from_rtp(self):
        rtp, fednow = rails.RAIL_SPECS['RTP'], rails.RAIL_SPECS['FEDNOW']
        self.assertTrue(rtp['immediate'] and fednow['immediate'])
        self.assertIsNone(rtp['cutoff_et'])
        self.assertIsNone(fednow['cutoff_et'])
        self.assertNotEqual(rtp['limit_cents'], fednow['limit_cents'])
        self.assertEqual(rtp['return_path'], fednow['return_path'])


class CutoffBoundaryTest(unittest.TestCase):

    def test_ach_exactly_at_cutoff_is_on_time_and_settles_next_business_day(self):
        submitted = et(TUE, 17, 0, 0)
        settled = rails.expected_settlement('ACH', submitted)
        self.assertEqual(settled.date(), THU)  # Wed is a synthetic holiday
        self.assertEqual(settled.timetz().replace(tzinfo=None), time(17, 0))
        self.assertEqual(settled.tzinfo, ET)

    def test_ach_one_second_after_cutoff_rolls_to_the_following_window(self):
        # Late Tuesday counts as Wednesday, but Wednesday is a holiday, so
        # the effective date is Thursday; ACH then adds one business day.
        settled = rails.expected_settlement('ACH', et(TUE, 17, 0, 1))
        self.assertEqual(settled.date(), FRI)

    def test_ach_one_second_before_cutoff_is_on_time(self):
        self.assertEqual(settlement_day('ACH', et(TUE, 16, 59, 59)), THU)

    def test_same_day_ach_exactly_at_cutoff_settles_same_business_day(self):
        settled = rails.expected_settlement('SAME_DAY_ACH', et(TUE, 14, 45, 0))
        self.assertEqual(settled.date(), TUE)
        self.assertEqual(settled.timetz().replace(tzinfo=None), time(14, 45))

    def test_same_day_ach_one_second_after_cutoff_rolls_to_next_business_day(self):
        # Late Tuesday -> Wednesday holiday -> Thursday, same-day window (0 days).
        self.assertEqual(settlement_day('SAME_DAY_ACH', et(TUE, 14, 45, 1)), THU)

    def test_wire_exactly_at_cutoff_is_on_time_same_business_day(self):
        settled = rails.expected_settlement('WIRE', et(TUE, 16, 0, 0))
        self.assertEqual(settled.date(), TUE)
        self.assertEqual(settled.timetz().replace(tzinfo=None), time(16, 0))

    def test_wire_one_second_after_cutoff_rolls_to_next_business_day(self):
        self.assertEqual(settlement_day('WIRE', et(TUE, 16, 0, 1)), THU)

    def test_naive_datetime_is_treated_as_et(self):
        naive = datetime(2026, 11, 10, 17, 0, 0)
        self.assertEqual(rails.expected_settlement('ACH', naive),
                         rails.expected_settlement('ACH', et(TUE, 17, 0, 0)))

    def test_utc_string_converts_into_et_before_the_cutoff_check(self):
        # 21:59:59 UTC is 16:59:59 EST on 10 November 2026, still on time.
        on_time = rails.expected_settlement('ACH', '2026-11-10T21:59:59+00:00')
        late = rails.expected_settlement('ACH', '2026-11-10T22:00:01+00:00')
        self.assertEqual(on_time.date(), THU)
        self.assertEqual(late.date(), FRI)


class WeekendAndHolidayRolloverTest(unittest.TestCase):

    def test_synthetic_holiday_is_not_a_business_day(self):
        self.assertFalse(rails.is_business_day(WED_HOLIDAY))
        self.assertTrue(rails.is_business_day(TUE))
        self.assertFalse(rails.is_business_day(SAT))
        self.assertFalse(rails.is_business_day(SUN))

    def test_ach_on_time_tuesday_skips_wednesday_holiday(self):
        self.assertEqual(settlement_day('ACH', et(TUE, 10, 0)), THU)

    def test_saturday_submission_rolls_to_monday_then_ach_adds_a_business_day(self):
        self.assertEqual(settlement_day('ACH', et(SAT, 10, 0)), TUE_AFTER)

    def test_sunday_submission_rolls_the_same_way_as_saturday(self):
        self.assertEqual(settlement_day('ACH', et(SUN, 18, 0)), TUE_AFTER)

    def test_friday_after_ach_cutoff_settles_tuesday(self):
        # Late Friday -> Monday effective date, then ACH +1 business day.
        self.assertEqual(settlement_day('ACH', et(FRI_BEFORE, 17, 0, 1)), TUE)

    def test_friday_after_same_day_cutoff_settles_monday(self):
        self.assertEqual(settlement_day('SAME_DAY_ACH', et(FRI_BEFORE, 14, 45, 1)),
                         MON_AFTER_WEEKEND)

    def test_friday_after_wire_cutoff_settles_monday(self):
        self.assertEqual(settlement_day('WIRE', et(FRI_BEFORE, 16, 0, 1)),
                         MON_AFTER_WEEKEND)

    def test_holiday_submission_itself_rolls_forward(self):
        self.assertEqual(settlement_day('SAME_DAY_ACH', et(WED_HOLIDAY, 9, 0)), THU)
        self.assertEqual(settlement_day('WIRE', et(WED_HOLIDAY, 9, 0)), THU)
        self.assertEqual(settlement_day('ACH', et(WED_HOLIDAY, 9, 0)), FRI)

    def test_rtp_and_fednow_settle_immediately_on_weekend_and_holiday(self):
        saturday = et(SAT, 23, 15, 30)
        holiday = et(WED_HOLIDAY, 3, 0, 0)
        for rail in ('RTP', 'FEDNOW'):
            self.assertEqual(rails.expected_settlement(rail, saturday), saturday)
            self.assertEqual(rails.expected_settlement(rail, holiday), holiday)


class ReturnPathTest(unittest.TestCase):

    def test_ach_and_same_day_ach_simulate_r01_r02_r03_returns(self):
        for rail in ('ACH', 'SAME_DAY_ACH'):
            path = rails.return_path(rail)
            self.assertEqual(path, {'kind': 'return', 'codes': ['R01', 'R02', 'R03']})
            # Callers cannot mutate the module table through the copy.
            path['codes'].append('R99')
            self.assertEqual(rails.return_path(rail)['codes'], ['R01', 'R02', 'R03'])

    def test_rtp_is_final_and_request_for_return_only(self):
        self.assertEqual(rails.return_path('RTP'),
                         {'kind': 'request_for_return', 'codes': []})

    def test_fednow_is_final_and_request_for_return_only(self):
        self.assertEqual(rails.return_path('FEDNOW'),
                         {'kind': 'request_for_return', 'codes': []})

    def test_wire_is_a_recall_request(self):
        self.assertEqual(rails.return_path('WIRE'),
                         {'kind': 'recall', 'codes': []})

    def test_unknown_rail_is_rejected(self):
        with self.assertRaises(ValueError):
            rails.expected_settlement('SWIFT', et(TUE, 10, 0))
        with self.assertRaises(ValueError):
            rails.return_path('SWIFT')


class DemoLimitTest(unittest.TestCase):

    def test_labelled_demo_limits(self):
        self.assertEqual(rails.payment_limit('ACH'), '1000000.00')
        self.assertEqual(rails.payment_limit('SAME_DAY_ACH'), '1000000.00')
        self.assertEqual(rails.payment_limit('RTP'), '1000000.00')
        self.assertEqual(rails.payment_limit('FEDNOW'), '500000.00')
        self.assertEqual(rails.payment_limit('WIRE'), '5000000.00')


class PayoutJsonWiringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'payments.sqlite3'
        payouts.ensure_seeded(self.db)

    def test_get_payout_includes_settlement_and_return_path(self):
        payout = payouts.get_payout(self.db, 'PO-1001')
        timing = payout['rail_timing']
        self.assertEqual(set(timing),
                         {'expected_settlement', 'return_path', 'payment_limit'})
        expected = rails.describe(payout['rail'], payout['created_at'])
        self.assertEqual(timing, expected)

    def test_new_rails_on_a_created_payout_use_their_own_paths(self):
        for index, (rail, kind) in enumerate((
            ('FEDNOW', 'request_for_return'),
            ('WIRE', 'recall'),
            ('SAME_DAY_ACH', 'return'),
        )):
            created = payout_rules.create(
                self.db, f'PO-51{index:02d}', 'Pat Taylor', 'DEMO-RAIL-TIMING',
                2500, rail, now=et(TUE, 10, 0))
            payout = payouts.get_payout(self.db, created['payout_id'])
            self.assertEqual(payout['rail_timing']['return_path']['kind'], kind)
            self.assertEqual(
                payout['rail_timing']['expected_settlement'],
                rails.expected_settlement(rail, payout['created_at']).isoformat())

    def test_desk_html_does_not_surface_timing_or_return_codes(self):
        queue = payouts.current_queue(self.db)
        self.assertTrue(any('rail_timing' in row for row in queue))
        html = desk.render_desk(queue)
        for needle in ('expected_settlement', 'rail_timing', 'R01', 'R02', 'R03',
                       'request_for_return', 'payment_limit'):
            self.assertNotIn(needle, html)


if __name__ == '__main__':
    unittest.main()
