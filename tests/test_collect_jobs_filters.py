import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cli.collect_jobs import (
    _authorization_assessment,
    _build_search_url,
    _classify_us_location,
    _ensure_internship_search_title,
    _is_relevant_tech_internship_title,
    _matches_any_requested_internship_role,
    _matches_requested_internship_role,
    _linkedin_job_id,
    _matches_authorization_constraints,
    _screening_fields,
    _validate_linkedin_details,
    _wait_for_linkedin_login,
    quarantine_legacy_unscreened_jobs,
)

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
        self.assertIn("U.S. citizenship", reason)

    def test_blocks_citizenship_required_phrasing(self):
        ok, reason = _matches_authorization_constraints(
            "U.S. Citizenship is required for this position.", PROFILE_NEEDS_SPONSORSHIP
        )
        self.assertFalse(ok)
        self.assertIn("U.S. citizenship", reason)

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
        self.assertIn("F-1/OPT/CPT", reason)

    def test_blocks_existing_no_sponsorship_language(self):
        ok, reason = _matches_authorization_constraints(
            "We do not sponsor visas for this position.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertFalse(ok)
        self.assertIn("no current or future visa sponsorship", reason)

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

    def test_future_sponsorship_field_activates_filtering(self):
        state, reason, evidence = _authorization_assessment(
            "Applicants must not now or in the future require employment sponsorship.",
            {"future_sponsorship_needed": True},
        )
        self.assertEqual(state, "ineligible")
        self.assertIn("sponsorship", reason)
        self.assertIn("future", evidence)

    def test_f1_work_authorization_text_activates_filtering(self):
        state, _, _ = _authorization_assessment(
            "Permanent U.S. work authorization is required.",
            {"work_authorization": "F-1 CPT/OPT work authorization"},
        )
        self.assertEqual(state, "ineligible")

    def test_recognizes_explicit_student_visa_support(self):
        state, reason, evidence = _authorization_assessment(
            "F-1 OPT and CPT candidates are welcome. H-1B sponsorship may be available.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertEqual(state, "compatible")
        self.assertEqual(reason, "")
        self.assertTrue(evidence)

    def test_silence_is_needs_review_not_confirmed_support(self):
        state, reason, evidence = _authorization_assessment(
            "Build backend services with Python.", PROFILE_NEEDS_SPONSORSHIP
        )
        self.assertEqual(state, "needs_review")
        self.assertEqual(reason, "")
        self.assertIn("No explicit", evidence)

    def test_negative_language_wins_over_positive_keyword(self):
        state, _, _ = _authorization_assessment(
            "We cannot support OPT candidates and do not provide visa sponsorship.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertEqual(state, "ineligible")

    def test_no_h1b_sponsorship_is_not_a_false_positive(self):
        state, _, _ = _authorization_assessment(
            "No H-1B sponsorship is available for this internship.",
            PROFILE_NEEDS_SPONSORSHIP,
        )
        self.assertEqual(state, "ineligible")

    def test_blocks_additional_f1_h1b_and_clearance_wording(self):
        postings = [
            "Applicants on OPT will not be considered for this internship.",
            "The company does not provide H-1B sponsorship for this position.",
            "Candidates must possess and maintain a Secret clearance.",
            "A security clearance is required for this role.",
        ]
        for posting in postings:
            with self.subTest(posting=posting):
                state, _, evidence = _authorization_assessment(
                    posting, PROFILE_NEEDS_SPONSORSHIP
                )
                self.assertEqual(state, "ineligible")
                self.assertTrue(evidence)


class InternshipCollectionFilterTests(unittest.TestCase):
    def test_accepts_target_tech_internships(self):
        accepted = [
            "Software Engineer Intern",
            "Frontend Developer Internship",
            "Back-End Engineering Co-op",
            "Full Stack Software Intern",
            "Machine Learning Engineer Intern",
            "AI Research Internship",
            "Data Science Intern",
            "Computer Vision Co-op",
            "Cloud Platform Engineering Intern",
            "Cybersecurity Engineering Intern",
            "Robotics Software Intern",
            "Data Analyst Intern",
            "Site Reliability Engineering Co-op",
        ]
        for title in accepted:
            with self.subTest(title=title):
                self.assertTrue(_is_relevant_tech_internship_title(title))

    def test_rejects_full_time_and_unrelated_internships(self):
        rejected = [
            "Software Engineer",
            "AI Engineer",
            "Machine Learning Engineer",
            "Engineering Intern",
            "Marketing Intern",
            "Product Manager Intern",
            "Student Intern",
        ]
        for title in rejected:
            with self.subTest(title=title):
                self.assertFalse(_is_relevant_tech_internship_title(title))

    def test_result_must_match_requested_role_family(self):
        self.assertTrue(
            _matches_requested_internship_role(
                "Software Engineering Intern", "Backend Software Developer Intern"
            )
        )
        self.assertFalse(
            _matches_requested_internship_role(
                "Software Engineering Intern", "Data Analytics Intern"
            )
        )
        self.assertTrue(
            _matches_requested_internship_role(
                "Machine Learning Intern", "Computer Vision Research Intern"
            )
        )
        self.assertFalse(
            _matches_requested_internship_role(
                "Machine Learning Intern", "Data Science Intern"
            )
        )
        self.assertTrue(
            _matches_requested_internship_role(
                "AI Engineer Intern", "Data Engineering Intern - AI & Analytics"
            )
        )

    def test_result_can_match_any_family_in_the_complete_run(self):
        allowed = [
            "Software Engineering Intern",
            "Machine Learning Intern",
            "AI Engineer Intern",
        ]
        self.assertTrue(
            _matches_any_requested_internship_role(
                "Machine Learning Intern", "Spring 2027 Software Engineering Intern", allowed
            )
        )
        self.assertTrue(
            _matches_any_requested_internship_role(
                "Software Engineering Intern", "AI Engineering Intern (Remote)", allowed
            )
        )
        self.assertFalse(
            _matches_any_requested_internship_role(
                "Machine Learning Intern", "Data Analytics Intern", allowed
            )
        )

    def test_internship_search_forces_linkedin_internship_filters(self):
        url = _build_search_url(
            "Software Engineering Intern",
            {"country": "US", "target_locations": []},
            {"experience_level": "2", "job_type": "F"},
        )
        self.assertIn("f_E=1", url)
        self.assertIn("f_JT=I", url)
        self.assertNotIn("f_E=2", url)
        self.assertNotIn("f_JT=F", url)

    def test_non_internship_search_is_forced_to_internships(self):
        url = _build_search_url(
            "Software Engineer",
            {"country": "US", "target_locations": []},
            {"experience_level": "2", "job_type": "F"},
        )
        self.assertIn("keywords=Software+Engineer+Intern", url)
        self.assertIn("f_E=1", url)
        self.assertIn("f_JT=I", url)
        self.assertNotIn("f_E=2", url)
        self.assertNotIn("f_JT=F", url)

    def test_typed_title_gets_an_internship_marker_once(self):
        self.assertEqual(_ensure_internship_search_title("AI Engineer"), "AI Engineer Intern")
        self.assertEqual(_ensure_internship_search_title("AI Engineer Internship"), "AI Engineer Internship")

    def test_remote_only_location_requires_review(self):
        self.assertEqual(_classify_us_location("Remote", {"country": "US"}), "unknown")

    def test_us_and_foreign_locations_are_distinguished(self):
        self.assertEqual(_classify_us_location("New York, NY", {"country": "US"}), "us")
        self.assertEqual(_classify_us_location("Toronto, Canada", {"country": "US"}), "non_us")


class LinkedInPageValidationTests(unittest.TestCase):
    def test_extracts_real_id_from_slugged_linkedin_url(self):
        self.assertEqual(
            _linkedin_job_id(
                "https://www.linkedin.com/jobs/view/2027-summer-software-engineer-intern-at-chaos-4291234567?position=1"
            ),
            "4291234567",
        )
        self.assertEqual(_linkedin_job_id("https://www.linkedin.com/jobs/view/2027/"), "")

    def test_rejects_wrong_job_and_error_pages(self):
        valid_details = {"id": "123", "title": "Software Engineer Intern", "has_job_root": True}
        self.assertEqual(_validate_linkedin_details(valid_details, "123"), (True, ""))
        self.assertFalse(_validate_linkedin_details({**valid_details, "id": "999"}, "123")[0])
        self.assertFalse(_validate_linkedin_details({**valid_details, "error_page": True}, "123")[0])
        self.assertFalse(_validate_linkedin_details({**valid_details, "title": "This page isn't working"}, "123")[0])


class LinkedInLoginWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_repeatedly_navigate_while_waiting_for_login(self):
        page = MagicMock()
        page.goto = AsyncMock()
        states = [
            {"ready": "complete", "url": "https://www.linkedin.com/login", "text": "Sign in Email or phone"},
            {"ready": "complete", "url": "https://www.linkedin.com/jobs/search/", "text": "Jobs"},
        ]
        with (
            patch("cli.collect_jobs._wait_for_ready", new=AsyncMock(side_effect=states)),
            patch("cli.collect_jobs.asyncio.sleep", new=AsyncMock()),
        ):
            ok = await _wait_for_linkedin_login(page, "https://www.linkedin.com/jobs/search/")
        self.assertTrue(ok)
        page.goto.assert_awaited_once_with("https://www.linkedin.com/jobs/search/")

    async def test_stop_request_interrupts_login_wait(self):
        page = MagicMock()
        page.goto = AsyncMock()
        with patch("cli.collect_jobs._wait_for_ready", new=AsyncMock()) as ready:
            ok = await _wait_for_linkedin_login(
                page,
                "https://www.linkedin.com/jobs/search/",
                cancel_flag={"cancel_requested": True},
            )
        self.assertFalse(ok)
        ready.assert_not_awaited()


class ScreeningFieldsTests(unittest.TestCase):
    def test_unverified_job_cannot_enter_application_queue(self):
        fields = _screening_fields(
            {"title": "Software Engineer Intern", "location": "Austin, TX", "description": ""},
            PROFILE_NEEDS_SPONSORSHIP,
            description_complete=False,
        )
        self.assertEqual(fields["status"], "manual_review")
        self.assertEqual(fields["screening_status"], "pending")

    def test_explicit_support_can_enter_application_queue(self):
        fields = _screening_fields(
            {
                "title": "Software Engineer Intern",
                "location": "Austin, TX",
                "description": "We welcome F-1 OPT candidates and can sponsor H-1B visas.",
            },
            PROFILE_NEEDS_SPONSORSHIP,
            description_complete=True,
        )
        self.assertEqual(fields["status"], "pending")
        self.assertEqual(fields["screening_status"], "compatible")

    def test_foreign_location_is_blocked_even_with_sponsorship(self):
        fields = _screening_fields(
            {
                "title": "Software Engineer Intern",
                "location": "Toronto, Canada",
                "description": "Visa sponsorship is available for interns." * 10,
            },
            PROFILE_NEEDS_SPONSORSHIP,
            description_complete=True,
        )
        self.assertEqual(fields["status"], "blocked")
        self.assertEqual(fields["screening_status"], "ineligible")

    def test_legacy_collector_jobs_are_quarantined_in_one_batch(self):
        jobs = {
            "https://example.com/old": {
                "status": "pending",
                "collection_method": "deterministic_linkedin_dom",
                "title": "Software Engineer Intern",
                "location": "Austin, TX",
                "description": "",
            },
            "https://example.com/manual": {"status": "pending", "source": "manual"},
        }
        with unittest.mock.patch("cli.collect_jobs.update_jobs_bulk", return_value=1) as bulk:
            changed = quarantine_legacy_unscreened_jobs(jobs, PROFILE_NEEDS_SPONSORSHIP)
        self.assertEqual(changed, 1)
        self.assertEqual(jobs["https://example.com/old"]["status"], "manual_review")
        self.assertEqual(jobs["https://example.com/manual"]["status"], "pending")
        self.assertEqual(set(bulk.call_args.args[0]), {"https://example.com/old"})


if __name__ == "__main__":
    unittest.main()


class CoopOnlyModeTests(unittest.TestCase):
    """Temporary collection mode: settings.json "collect_role_kind": "coop" (LinkedIn, last week)."""

    def setUp(self):
        patcher = patch("cli.collect_jobs._coop_only", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def query(self, title, filters=None):
        from urllib.parse import parse_qs, urlsplit
        url = _build_search_url(title, {"target_locations": ["United States"]}, filters)
        return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

    def test_search_titles_become_co_op_searches(self):
        self.assertEqual(_ensure_internship_search_title("Software Engineer Intern"), "Software Engineer Co-op")
        self.assertEqual(_ensure_internship_search_title("Backend Internship"), "Backend Co-op")
        self.assertEqual(_ensure_internship_search_title("Data Engineer"), "Data Engineer Co-op")
        self.assertEqual(_ensure_internship_search_title("Software Engineer Co-op"), "Software Engineer Co-op")

    def test_the_search_asks_linkedin_for_the_past_week_whatever_the_collect_tab_says(self):
        self.assertEqual(self.query("Software Engineer Co-op")["f_TPR"], "r604800")
        self.assertEqual(self.query("Software Engineer Co-op", {"date_posted": "r2592000"})["f_TPR"], "r604800")

    def test_the_internship_only_filters_are_not_forced_so_co_ops_filed_elsewhere_are_kept(self):
        params = self.query("Software Engineer Co-op")

        self.assertNotIn("f_JT", params)
        self.assertNotIn("f_E", params)
        self.assertEqual(params["keywords"], "Software Engineer Co-op")

    def test_only_co_op_titles_are_kept_and_plain_internships_are_dropped(self):
        self.assertTrue(_is_relevant_tech_internship_title("Software Engineering Co-op - Fall 2027"))
        self.assertTrue(_is_relevant_tech_internship_title("Backend Developer Coop"))
        self.assertFalse(_is_relevant_tech_internship_title("Software Engineer Intern"))
        self.assertFalse(_is_relevant_tech_internship_title("Software Engineering Internship"))
        self.assertFalse(_is_relevant_tech_internship_title("Marketing Co-op"))  # not a tech role

    def test_a_requested_co_op_search_matches_co_op_results_only(self):
        self.assertTrue(_matches_requested_internship_role("Software Engineer Co-op", "Software Developer Co-op"))
        self.assertFalse(_matches_requested_internship_role("Software Engineer Co-op", "Software Developer Intern"))


class InternshipModeIsUnchangedTests(unittest.TestCase):
    def test_default_mode_still_forces_the_internship_filters_and_accepts_both_words(self):
        with patch("cli.collect_jobs._coop_only", return_value=False):
            from urllib.parse import parse_qs, urlsplit
            params = {k: v[0] for k, v in parse_qs(urlsplit(
                _build_search_url("Software Engineer Intern", {"target_locations": ["United States"]})).query).items()}

            self.assertEqual((params["f_JT"], params["f_E"], params["f_TPR"]), ("I", "1", "r604800"))
            self.assertEqual(_ensure_internship_search_title("Data Engineer"), "Data Engineer Intern")
            self.assertTrue(_is_relevant_tech_internship_title("Software Engineer Intern"))
            self.assertTrue(_is_relevant_tech_internship_title("Software Engineering Co-op"))
