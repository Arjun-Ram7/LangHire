import unittest
from unittest.mock import AsyncMock, patch

from playwright.sync_api import sync_playwright

from backend.core.workday_flow import (
    WorkdayDeterministicUnavailable,
    _SUBMISSION_RISK_SCAN_JS,
    _application_tab_score,
    _can_run_llm_cleanup,
    _cleanup_succeeded,
    save_open_questions,
    is_workday_url,
    run_workday_deterministic,
    _fill_deterministic_widgets,
    _fill_experience_step,
    _static_fill_passes,
)


class SubmissionRiskScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_routine_email_nag_banner_is_not_a_verification_challenge(self):
        # A live LinkedIn Easy Apply run left a job in manual_review over a
        # "CAPTCHA/human verification challenge" that did not exist. The
        # actual page had a routine LinkedIn account nag ("update or confirm
        # your email") sitting in the page's background chrome, outside the
        # Easy Apply modal -- but live inspection showed the modal did not
        # match any of the guessed modal selectors, so scoping alone did not
        # save it. The real fix: "confirm/verify your email" are ambiguous
        # phrases that show up in routine account UI, not just verification
        # challenges, so they are no longer in the pattern list at all.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div>Emails aren't getting through to one of your email addresses.
                  Please update or confirm your email. More info</div>
                <div>
                  <div>Contact info</div>
                  <input id="email" value="arjun@example.com">
                  <button>Next</button>
                </div>
                """
            )
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertNotIn("human_verification", result["flags"], result)
        finally:
            page.close()

    def test_real_captcha_inside_the_modal_is_still_caught(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div role="dialog">
                  <div>Please complete the CAPTCHA to continue</div>
                </div>
                """
            )
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertIn("human_verification", result["flags"], result)
        finally:
            page.close()

    def test_full_page_form_with_no_modal_still_scans_the_body(self):
        page = self.browser.new_page()
        try:
            page.set_content("<div>Enter the verification code we emailed you</div>")
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertIn("human_verification", result["flags"], result)
        finally:
            page.close()


class CanRunLlmCleanupTests(unittest.TestCase):
    def test_allows_bounded_cleanup_on_external_account_or_sign_in_surface(self):
        summary = {
            "url": "https://careers.example.com/create-account",
            "surface": {"accountish": True, "formish": True},
        }
        self.assertTrue(_can_run_llm_cleanup(summary, {"clicked_linkedin": True}))

    def test_refuses_cleanup_on_linkedin_account_surface(self):
        summary = {
            "url": "https://www.linkedin.com/login",
            "surface": {"accountish": True, "formish": True},
        }
        self.assertFalse(_can_run_llm_cleanup(summary, {"clicked_linkedin": True, "easy_apply": False}))

    def test_allows_cleanup_on_an_opened_easy_apply_modal(self):
        # Preflight already clicked LinkedIn's own Easy Apply button and
        # opened its modal -- the cleanup agent should be allowed to
        # continue it, same as it would on any external ATS page. Blanket-
        # refusing every linkedin.com URL left the agent stuck on page one
        # of the modal, unable to click Next, for every Easy Apply job.
        summary = {"url": "https://www.linkedin.com/jobs/view/12345/", "surface": {}}
        preflight = {"clicked_linkedin": True, "easy_apply": True}
        self.assertTrue(_can_run_llm_cleanup(summary, preflight))

    def test_refuses_cleanup_for_a_non_easy_apply_job_stuck_on_linkedin(self):
        # An external-apply job that never left LinkedIn (button not found,
        # no redirect) is still the stuck case this guard exists for.
        summary = {"url": "https://www.linkedin.com/jobs/view/12345/", "surface": {}}
        preflight = {"clicked_linkedin": True, "easy_apply": False}
        self.assertFalse(_can_run_llm_cleanup(summary, preflight))


class ApplicationTabScoreTests(unittest.TestCase):
    def test_prefers_real_tesla_apply_tab(self):
        apply_score = _application_tab_score(
            "https://www.tesla.com/careers/search/job/apply/282340",
            "Job Application",
            "Tesla",
        )
        posting_score = _application_tab_score(
            "https://www.tesla.com/careers/search/job/282340",
            "Internship, Embedded Software",
            "Tesla",
        )
        self.assertGreater(apply_score, posting_score)

    def test_ignores_blank_popup(self):
        self.assertLess(_application_tab_score("about:blank", "", "Tesla"), 0)


class IsWorkdayUrlTests(unittest.TestCase):
    def test_matches_myworkdayjobs_host(self):
        self.assertTrue(is_workday_url("https://acme.wd1.myworkdayjobs.com/en-US/job/apply"))

    def test_matches_myworkdaysite_host(self):
        self.assertTrue(is_workday_url("https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/apply"))

    def test_matches_bare_workday_host(self):
        self.assertTrue(is_workday_url("https://acme.workday.com/apply"))

    def test_rejects_linkedin(self):
        self.assertFalse(is_workday_url("https://www.linkedin.com/jobs/view/123"))

    def test_rejects_lookalike_domain(self):
        # "workday" appearing as a path segment or unrelated host must not match.
        self.assertFalse(is_workday_url("https://notworkday.example.com/apply"))
        self.assertFalse(is_workday_url("https://example.com/workday/apply"))

    def test_rejects_empty_or_malformed(self):
        self.assertFalse(is_workday_url(""))
        self.assertFalse(is_workday_url("not a url"))


class RunWorkdayDeterministicTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_workday_steps_with_same_url_continue_to_review(self):
        steps = iter(['My Information', 'My Experience', 'Application Questions', 'Disclosures', 'Review'])
        current = {'step': next(steps)}
        async def surface(*args, **kwargs):
            return {'step': current['step'], 'formish': True, 'ready': 'complete', 'body_length': 500}
        async def progress(*args):
            if current['step'] == 'Review':
                return {'clicked': False, 'reason': 'final_submit_guard_blocked'}
            current['step'] = next(steps)
            return {'clicked': True, 'reason': 'form_continue', 'label': 'Save and Continue', 'url': 'https://acme.myworkdayjobs.com/apply'}
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', new=AsyncMock(return_value={'url': 'https://acme.myworkdayjobs.com/apply'})),
            patch('backend.core.workday_flow.try_safe_progress_step', side_effect=progress) as advance,
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            result = await _static_fill_passes(object(), {}, '', 8, 1)
        self.assertEqual(advance.await_count, 5)
        self.assertEqual(result['surface']['step'], 'Review')
        self.assertEqual(sum(bool(p['clicked']) for p in result['safe_progress']), 4)

    async def test_my_experience_step_fills_history_and_education_before_static_autofill(self):
        steps = iter(['My Information', 'My Experience', 'Review'])
        current = {'step': next(steps)}
        calls = []
        async def surface(*args, **kwargs):
            return {'step': current['step'], 'formish': True, 'ready': 'complete', 'body_length': 500}
        async def progress(*args):
            if current['step'] == 'Review':
                return {'clicked': False, 'reason': 'final_submit_guard_blocked'}
            current['step'] = next(steps)
            return {'clicked': True, 'reason': 'form_continue', 'label': 'Save and Continue', 'url': 'u'}
        async def static(*args, **kwargs):
            calls.append(('static', current['step']))
            return {'url': 'u'}
        async def history(browser, entries, worker_id=0):
            calls.append(('history', current['step']))
            seen_entries.extend(entries)
            return {}
        async def education(browser, plan, worker_id=0):
            calls.append(('education', current['step']))
            return {}
        seen_entries = []
        entries = [{'title': 'Intern', 'company': 'Acme'}]
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', side_effect=static),
            patch('backend.core.workday_flow.try_safe_progress_step', side_effect=progress),
            patch('backend.core.workday_flow.load_work_experience', return_value=entries) as load,
            patch('backend.core.workday_flow.fill_work_history', side_effect=history),
            patch('backend.core.workday_flow.fill_education', side_effect=education),
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            await _static_fill_passes(
                object(), {'school': 'Virginia Tech'}, '/tmp/resume.pdf', 8, 1,
                profile={'work_locations': {'Acme': 'Reston, VA'}},
            )
        load.assert_called_once_with('/tmp/resume.pdf')
        self.assertEqual(seen_entries[0]['location'], 'Reston, VA')
        self.assertEqual(
            [c for c in calls if c[1] == 'My Experience'],
            [('history', 'My Experience'), ('education', 'My Experience'), ('static', 'My Experience')],
        )
        self.assertFalse([c for c in calls if c[0] != 'static' and c[1] != 'My Experience'])

    async def test_experience_fill_loads_the_saved_profile_when_the_caller_passes_none(self):
        # manual_review_queue and apply_jobs call the engine without a profile;
        # locations must still come from the saved one.
        seen = []
        async def history(browser, entries, worker_id=0):
            seen.extend(entries)
            return {}
        with (
            patch('backend.core.workday_flow.load_work_experience', return_value=[{'title': 'Intern', 'company': 'Acme'}]),
            patch('backend.core.workday_flow.load_profile', return_value={'work_locations': {'Acme': 'Reston, VA'}}),
            patch('backend.core.workday_flow.fill_work_history', side_effect=history),
            patch('backend.core.workday_flow.fill_education', new=AsyncMock()),
        ):
            await _fill_experience_step(object(), {}, '/tmp/resume.pdf', 1)
        self.assertEqual(seen[0]['location'], 'Reston, VA')

    async def test_experience_fill_runs_at_most_twice_per_visit_to_the_step(self):
        # However many passes the page needs, the row filler must not keep re-running.
        calls = []
        counter = iter(range(100))
        async def surface(*args, **kwargs):
            return {'step': 'My Experience', 'formish': True, 'ready': 'complete', 'body_length': 500}
        async def history(browser, entries, worker_id=0):
            calls.append('history')
            return {}
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', side_effect=lambda *a, **k: {'url': 'u', 'filled': next(counter)}),
            patch('backend.core.workday_flow.try_safe_progress_step', side_effect=lambda *a: {'clicked': True, 'reason': 'form_continue', 'label': 'Save and Continue', 'url': f'u{next(counter)}'}),
            patch('backend.core.workday_flow.load_work_experience', return_value=[{'title': 'T', 'company': 'C'}]),
            patch('backend.core.workday_flow.fill_work_history', side_effect=history),
            patch('backend.core.workday_flow.fill_education', new=AsyncMock()),
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            await _static_fill_passes(object(), {}, '/tmp/r.pdf', 6, 1, profile={})
        self.assertEqual(len(calls), 2)

    async def test_empty_signature_dates_are_filled_on_every_pass_before_the_static_fill(self):
        # Self Identify's required Date has no fact behind it; a live run stalled on
        # "The field Date is required" until the AI typed it by hand.
        order = []
        async def surface(*args, **kwargs):
            return {'step': 'Self Identify', 'formish': True, 'ready': 'complete', 'body_length': 500}
        async def dates(browser, worker_id=0):
            order.append('dates')
            return 1
        async def static(*args, **kwargs):
            order.append('static')
            return {'url': 'u'}
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', side_effect=static),
            patch('backend.core.workday_flow.try_safe_progress_step', new=AsyncMock(return_value={'clicked': False, 'reason': 'final_submit_guard_blocked'})),
            patch('backend.core.workday_flow.fill_signature_dates', side_effect=dates),
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            await _static_fill_passes(object(), {}, '', 1, 1)
        self.assertEqual(order, ['dates', 'static'])

    async def test_widget_fillers_run_together_and_one_failing_does_not_stop_the_rest(self):
        # Used by both the deterministic loop and the LLM cleanup's per-step hook.
        calls = []
        async def boom(browser, worker_id=0):
            calls.append('dates')
            raise RuntimeError('cdp gone')
        async def phone(browser, worker_id=0):
            calls.append('phone')
            return True
        with (
            patch('backend.core.workday_flow.fill_signature_dates', side_effect=boom),
            patch('backend.core.workday_flow.fill_phone_device_type', side_effect=phone),
            patch('backend.core.workday_flow._fill_experience_step', new=AsyncMock()) as experience,
        ):
            counters = {}
            await _fill_deterministic_widgets(object(), {}, '/tmp/r.pdf', 1, {}, 'My Information', counters)
            experience.assert_not_awaited()
            await _fill_deterministic_widgets(object(), {}, '/tmp/r.pdf', 1, {}, 'My Experience', counters)
            experience.assert_awaited_once()
        self.assertEqual(calls, ['dates', 'phone', 'dates', 'phone'])

    async def test_known_workday_question_answers_are_filled_before_the_ai_is_asked(self):
        # Live run: the static pass could not open Workday's listbox buttons, reported the
        # questions blank, and the AI (out of credits) then abandoned the job.
        seen = []
        async def questions(browser, facts, worker_id=0):
            seen.append((dict(facts), worker_id))
            return 0
        with (
            patch('backend.core.workday_flow.fill_workday_questions', side_effect=questions),
            patch('backend.core.workday_flow.fill_signature_dates', new=AsyncMock()),
            patch('backend.core.workday_flow.fill_phone_device_type', new=AsyncMock()),
        ):
            await _fill_deterministic_widgets(object(), {'age_over_18': 'yes'}, '', 3, {}, 'Application Questions', {})
        self.assertEqual(seen, [({'age_over_18': 'yes'}, 3)])

    async def test_a_page_still_rendering_is_retried_not_treated_as_the_end_of_the_form(self):
        # Live: right after Save and Continue the next page had no footer button for a moment,
        # the probe said no_safe_progress_control with nothing blank, the loop gave up, and the
        # (credit-less) AI took over and abandoned the job before Self Identify.
        steps = iter(['My Experience', 'Application Questions', 'Review'])
        current = {'step': next(steps)}
        replies = iter([
            {'clicked': False, 'reason': 'no_safe_progress_control'},
            {'clicked': False, 'reason': 'no_safe_progress_control'},
            {'clicked': True, 'reason': 'form_continue', 'label': 'Save and Continue', 'url': 'u1'},
            {'clicked': False, 'reason': 'final_submit_guard_blocked'},
        ])
        async def surface(*args, **kwargs):
            return {'step': current['step'], 'formish': True, 'ready': 'complete', 'body_length': 500, 'input_count': 3, 'button_count': 4}
        async def progress(*args):
            reply = next(replies)
            if reply['clicked']:
                current['step'] = next(steps)
            return reply
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', new=AsyncMock(return_value={'url': 'u'})),
            patch('backend.core.workday_flow.try_safe_progress_step', side_effect=progress),
            patch('backend.core.workday_flow._fill_deterministic_widgets', new=AsyncMock()),
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            result = await _static_fill_passes(object(), {}, '', 8, 1)
        self.assertEqual(sum(bool(p['clicked']) for p in result['safe_progress']), 1)
        self.assertEqual(result['surface']['step'], 'Application Questions')

    async def test_a_form_that_never_shows_a_continue_button_still_ends(self):
        async def surface(*args, **kwargs):
            return {'step': 'My Experience', 'formish': True, 'ready': 'complete', 'body_length': 500, 'input_count': 3, 'button_count': 4}
        advance = AsyncMock(return_value={'clicked': False, 'reason': 'no_safe_progress_control'})
        with (
            patch('backend.core.workday_flow._wait_for_visible_surface', side_effect=surface),
            patch('backend.core.workday_flow.run_static_autofill', new=AsyncMock(return_value={'url': 'u'})),
            patch('backend.core.workday_flow.try_safe_progress_step', new=advance),
            patch('backend.core.workday_flow._fill_deterministic_widgets', new=AsyncMock()),
            patch('backend.core.workday_flow.wait_while_ai_paused', new=AsyncMock()),
            patch('backend.core.workday_flow._wait_for_page_settle', new=AsyncMock()),
            patch('backend.core.workday_flow.asyncio.sleep', new=AsyncMock()),
        ):
            await _static_fill_passes(object(), {}, '', 12, 1)
        self.assertLessEqual(advance.await_count, 5)

    async def test_cancelled_run_does_not_launch_llm(self):
        flag = {'cancel_requested': True}
        with (
            patch('backend.core.workday_flow._static_fill_passes', new=AsyncMock(return_value={
                'url': 'https://acme.myworkdayjobs.com/apply', 'requiredEmpty': 1,
            })) as fill,
            patch('backend.core.workday_flow._run_llm_cleanup', new=AsyncMock()) as llm,
        ):
            await run_workday_deterministic(object(), facts={}, resume_path='', worker_id=1, cancel_flag=flag)
        self.assertIs(fill.call_args.kwargs['cancel_flag'], flag)
        llm.assert_not_awaited()

    async def test_clean_run_has_no_blockers_and_never_submits(self):
        review = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/review",
            "requiredEmpty": 0,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": [],
            "visibleErrors": [],
            "safe_progress": [],
        }
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review)),
            patch("backend.core.workday_flow._run_llm_cleanup") as cleanup,
            patch("backend.core.workday_flow.try_controlled_final_submit") as submit,
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={"job_title": "SWE Intern", "job_company": "Acme"},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["blockers"], ["ready for final review; static engine stopped before Submit"])
        cleanup.assert_not_called()
        submit.assert_not_called()

    async def test_stuck_field_triggers_bounded_llm_cleanup(self):
        review_before = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/apply",
            "requiredEmpty": 1,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": ["why do you want to work here"],
            "visibleErrors": [],
            "requiredEmptyLabels": ["Why do you want to work here?"],
            "safe_progress": [],
        }
        cleaned_review = {**review_before, "requiredEmpty": 0, "needsLlm": []}
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review_before)),
            patch(
                "backend.core.workday_flow._run_llm_cleanup",
                new=AsyncMock(return_value={
                    "attempted": True,
                    "steps": 4,
                    "timeout": False,
                    "success": True,
                    "stop_reason": "",
                    "last_review": cleaned_review,
                }),
            ) as cleanup,
            patch("backend.core.workday_flow.try_controlled_final_submit") as submit,
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={"job_title": "SWE Intern", "job_company": "Acme"},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        cleanup.assert_awaited_once()
        submit.assert_not_called()
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["blockers"], ["ready for final review; static engine stopped before Submit"])

    async def test_raises_unavailable_when_no_progress_made(self):
        with patch(
            "backend.core.workday_flow._static_fill_passes",
            new=AsyncMock(side_effect=RuntimeError("cdp session lost")),
        ):
            with self.assertRaises(WorkdayDeterministicUnavailable):
                await run_workday_deterministic(
                    browser=object(),
                    facts={},
                    resume_path="/tmp/resume.pdf",
                    worker_id=1,
                )

    async def test_late_failure_is_folded_into_manual_review_result(self):
        review = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/apply",
            "requiredEmpty": 0,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": ["stock question"],
            "visibleErrors": [],
            "safe_progress": [],
        }
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review)),
            patch(
                "backend.core.workday_flow._run_llm_cleanup",
                new=AsyncMock(side_effect=RuntimeError("agent crashed")),
            ),
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        self.assertEqual(result["status"], "manual_review")
        self.assertIn("deterministic engine error", result["blockers"][0])


if __name__ == "__main__":
    unittest.main()


class CleanupSucceededTests(unittest.TestCase):
    def test_agent_success_with_required_blanks_is_not_a_success(self):
        # The cleanup agent reported success after four steps while eight
        # required fields were still empty, so the job was recorded as finished
        # when it was not.
        review = {"requiredEmpty": 8, "needsLlm": ["required-empty: cards[abc][field0]"]}

        self.assertFalse(_cleanup_succeeded(True, review))

    def test_agent_success_with_nothing_left_is_a_success(self):
        self.assertTrue(_cleanup_succeeded(True, {"requiredEmpty": 0, "needsLlm": []}))

    def test_agent_failure_is_never_upgraded_to_success(self):
        self.assertFalse(_cleanup_succeeded(False, {"requiredEmpty": 0, "needsLlm": []}))

    def test_abandoned_fields_do_not_block_success(self):
        # A field abandoned after three attempts is a deliberate hand-off to the
        # human, not unfinished cleanup work.
        review = {"requiredEmpty": 1, "needsLlm": [], "abandoned": 1}

        self.assertTrue(_cleanup_succeeded(True, review))


class SaveOpenQuestionsTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.added = []

        def qa_add(self, question, answer="", question_type="text", source_domain=""):
            self.added.append((question, answer, question_type, source_domain))
            return {"id": len(self.added)}

    def test_open_questions_are_banked_with_type_and_domain(self):
        # Review mode never wrote to the Q&A bank, so every question the
        # candidate answered by hand was discarded and asked again next time.
        store = self.FakeStore()
        summary = {
            "open_questions": [
                {"question": "What is the hardest technical challenge you have faced?",
                 "type": "textarea", "required": True},
            ]
        }

        saved = save_open_questions(
            summary, "https://jobs.lever.co/palantir/abc/apply", store=store
        )

        self.assertEqual(saved, 1)
        question, answer, qtype, domain = store.added[0]
        self.assertEqual(question, "What is the hardest technical challenge you have faced?")
        self.assertEqual(answer, "")
        self.assertEqual(qtype, "textarea")
        self.assertEqual(domain, "jobs.lever.co")

    def test_nothing_is_banked_when_there_are_no_open_questions(self):
        store = self.FakeStore()

        self.assertEqual(save_open_questions({"open_questions": []}, "https://x.com", store=store), 0)
        self.assertEqual(store.added, [])

    def test_a_missing_store_is_not_an_error(self):
        summary = {"open_questions": [{"question": "Why this company?", "type": "textarea"}]}

        self.assertEqual(save_open_questions(summary, "https://x.com", store=None), 0)
