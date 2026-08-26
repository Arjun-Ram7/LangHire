import unittest

from cli.collect_jobs import _matches_authorization_constraints

PROFILE_NEEDS_SPONSORSHIP = {"visa_sponsorship_needed": True}
PROFILE_NO_SPONSORSHIP_NEEDED = {"visa_sponsorship_needed": False}


class MatchesAuthorizationConstraintsTests(unittest.TestCase):
    def test_allows_when_profile_does_not_need_sponsorship(self):
        ok, reason = _matches_authorization_constraints(
            "Must be a U.S. citizen to apply.", PROFILE_NO_SPONSORSHIP_NEEDED
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_blocks_us_citizen_only_posting(self):
        ok, reason = _matches_authorization_constraints(
            "Candidates must be a U.S. citizen due to the nature of this role.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires U.S. citizenship")

    def test_blocks_citizenship_required_phrasing(self):
        ok, reason = _matches_authorization_constraints(
            "U.S. Citizenship is required for this position.", PROFILE_NEEDS_SPONSORSHIP
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires U.S. citizenship")

    def test_blocks_security_clearance_requirement(self):
        ok, reason = _matches_authorization_constraints(
            "Applicants must be able to obtain and maintain a security clearance.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires security clearance")

    def test_blocks_active_clearance_requirement(self):
        ok, reason = _matches_authorization_constraints(
            "This role requires an active Secret clearance.", PROFILE_NEEDS_SPONSORSHIP
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires security clearance")

    def test_blocks_no_opt_cpt_support(self):
        ok, reason = _matches_authorization_constraints(
            "We are unable to sponsor CPT or OPT for this position.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: posting rejects OPT/CPT")

    def test_blocks_existing_no_sponsorship_language(self):
        ok, reason = _matches_authorization_constraints(
            "We do not sponsor visas for this position.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: posting says no visa sponsorship")

    def test_blocks_l3harris_clearance_phrasing(self):
        ok, reason = _matches_authorization_constraints(
            "Please be aware many of our positions require the ability to obtain a "
            "security clearance. Security clearances may only be granted to U.S. citizens.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires security clearance")

    def test_blocks_palantir_clearance_phrasing(self):
        ok, reason = _matches_authorization_constraints(
            "Active US Security clearance, or eligibility and willingness to obtain "
            "a US Security clearance.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires security clearance")

    def test_blocks_mitre_clearance_implies_citizenship_phrasing(self):
        ok, reason = _matches_authorization_constraints(
            "Department of Defense's adjudicative guidelines for receiving a clearance, "
            "to include U.S. citizenship.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Filtered out: requires security clearance")

    def test_allows_incidental_mention_of_citizen(self):
        ok, reason = _matches_authorization_constraints(
            "We are proud to be a good citizen of the communities we serve.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_allows_eeo_non_discrimination_boilerplate(self):
        ok, reason = _matches_authorization_constraints(
            "We do not discriminate based on race, color, religion, sex, national "
            "origin, veteran status, disability, genetic information, or citizenship status.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_allows_ordinary_posting(self):
        ok, reason = _matches_authorization_constraints(
            "We are looking for a software engineer to join our growing team.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
