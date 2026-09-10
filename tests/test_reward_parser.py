import unittest

from tg_checkin import extract_balance, extract_points, extract_reward


class RewardParserTests(unittest.TestCase):
    def test_extracts_bet_reward_instead_of_balance(self):
        text = "🎉 签到成功 | 6 bet 💴 当前持有 | 137 bet ⏳ 签到日期 | 2026-09-11"
        self.assertEqual(extract_reward(text), {"amount": 6, "unit": "bet"})
        self.assertEqual(extract_balance(text), {"amount": 137, "unit": "bet"})

    def test_extracts_named_reward_after_monthly_count(self):
        text = "🎉 签到成功 | 本月已签 3 天 🎁 获得奖励 | 7 咪咪 💴 当前持有 | 148 咪咪"
        self.assertEqual(extract_reward(text), {"amount": 7, "unit": "咪咪"})
        self.assertEqual(extract_balance(text), {"amount": 148, "unit": "咪咪"})

    def test_prefers_follow_up_reward_over_an_earlier_balance(self):
        self.assertEqual(
            extract_reward("当前持有 137 bet", "签到成功 | 6 bet"),
            {"amount": 6, "unit": "bet"},
        )

    def test_legacy_points_remain_compatible(self):
        text = "签到成功，今日获得 5 积分，累计 120 积分"
        self.assertEqual(extract_reward(text), {"amount": 5, "unit": "积分"})
        self.assertEqual(extract_points(text), 5)
        self.assertIsNone(extract_points("签到成功 | 6 bet"))

    def test_balance_supports_legacy_wording_and_ignores_missing_value(self):
        self.assertEqual(extract_balance("账户余额：25.5 points"), {"amount": 25.5, "unit": "points"})
        self.assertIsNone(extract_balance("签到成功，获得 5 积分"))


if __name__ == "__main__":
    unittest.main()
