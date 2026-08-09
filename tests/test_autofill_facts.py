import unittest
from unittest.mock import AsyncMock, patch

from playwright.sync_api import sync_playwright

from backend.core.autofill_facts import (
    _autofill_script,
    _default_facts,
    _workday_human_checkpoint_script,
    wait_for_workday_human_checkpoint,
)
from backend.core.shared_config import browser_session_kwargs


WORKDAY_FACTS = {
    "first_name": "Arjun",
    "last_name": "Ramachandran",
    "email": "arjun@example.com",
    "phone_full": "+15409143128",
    "country": "USA",
    "citizenship": "India",
    "nationality": "India",
    "country_of_residence": "United Arab Emirates",
    "gpa": "3.71",
    "graduation": "Fall 2027",
    "graduation_date": "12/15/2027",
    "graduation_date_iso": "2027-12-15",
    "graduation_year": "2027",
    "earliest_start_date": "May 16, 2027",
    "earliest_start_date_date": "05/16/2027",
    "earliest_start_date_iso": "2027-05-16",
    "internship_end_date": "August 22, 2027",
    "internship_end_date_iso": "2027-08-22",
    "visa_expiration_date": "09/03/2029",
    "visa_expiration_date_iso": "2029-09-03",
    "authorized_to_work_us": "yes",
    "visa_sponsorship_needed": "yes",
    "race_ethnicity": "Asian Indian",
    "hispanic_latino": "no",
    "gender": "Male",
    "veteran_status": "Not a veteran",
    "disability_status": "No",
}


class StaticAutofillWorkdayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def run_fixture(self, html: str, facts: dict[str, str] | None = None):
        page = self.browser.new_page()
        try:
            page.set_content(html)
            result = page.evaluate(_autofill_script(facts or WORKDAY_FACTS))
            values = page.locator("input, select").evaluate_all(
                "els => Object.fromEntries(els.map(el => [el.id, el.value]))"
            )
            return result, values
        finally:
            page.close()

    def test_common_workday_identity_residency_and_dates(self):
        result, values = self.run_fixture(
            """
            <main>
              <div class="field"><label for="first">First Name</label><input id="first" required></div>
              <div class="field"><label for="last">Last Name</label><input id="last" required></div>
              <div class="field"><label for="citizenship">Country of Citizenship</label>
                <select id="citizenship" required><option value="">Select</option><option>India</option><option>United States</option></select></div>
              <div class="field"><label for="residence">Country of Residence</label>
                <select id="residence" required><option value="">Select</option><option>United Arab Emirates</option><option>United States</option></select></div>
              <div class="field"><label for="mailing-country">Mailing Address Country</label>
                <select id="mailing-country" required><option value="">Select</option><option>United Arab Emirates</option><option>United States</option></select></div>
              <div class="field"><label for="visa-expiry">Visa Expiration Date</label><input id="visa-expiry" type="date" required></div>
              <div class="field"><label for="start">Available Start Date</label><input id="start" type="date" required></div>
              <div class="field"><label for="end">Internship End Date</label><input id="end" type="date" required></div>
              <div class="field"><label for="graduation">Expected Graduation Date</label><input id="graduation" type="date" required></div>
            </main>
            """
        )

        self.assertEqual(values["first"], "Arjun")
        self.assertEqual(values["last"], "Ramachandran")
        self.assertEqual(values["citizenship"], "India")
        self.assertEqual(values["residence"], "United Arab Emirates")
        self.assertEqual(values["mailing-country"], "United States")
        self.assertEqual(values["visa-expiry"], "2029-09-03")
        self.assertEqual(values["start"], "2027-05-16")
        self.assertEqual(values["end"], "2027-08-22")
        self.assertEqual(values["graduation"], "2027-12-15")
        self.assertEqual(result["requiredEmpty"], 0)

    def test_guarded_fuzzy_match_accepts_typo_at_eighty_percent(self):
        result, values = self.run_fixture(
            '<div class="field"><label for="first">Frist Name</label><input id="first" required></div>'
        )

        self.assertEqual(values["first"], "Arjun")
        self.assertTrue(any("first_name | fuzzy" in match for match in result["matches"]))

    def test_common_workday_screening_and_demographics(self):
        result, values = self.run_fixture(
            """
            <main>
              <div class="field"><label for="authorized">Are you legally authorized to work in the United States?</label>
                <select id="authorized" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
              <div class="field"><label for="sponsor">Will you now or in the future require employment sponsorship?</label>
                <select id="sponsor" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
              <div class="field"><label for="race">Race / Ethnicity</label>
                <select id="race" required><option value="">Select</option><option>Asian</option><option>American Indian or Alaska Native</option></select></div>
              <div class="field"><label for="hispanic">Are you Hispanic or Latino?</label>
                <select id="hispanic" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
              <div class="field"><label for="gender">Gender</label>
                <select id="gender" required><option value="">Select</option><option>Female</option><option>Male</option></select></div>
              <div class="field"><label for="veteran">Veteran Status</label>
                <select id="veteran" required><option value="">Select</option><option>I am not a protected veteran</option><option>I identify as a protected veteran</option></select></div>
              <div class="field"><label for="disability">Disability Status</label>
                <select id="disability" required><option value="">Select</option><option>No, I do not have a disability</option><option>Yes, I have a disability</option></select></div>
            </main>
            """
        )

        self.assertEqual(values["authorized"], "Yes")
        self.assertEqual(values["sponsor"], "Yes")
        self.assertEqual(values["race"], "Asian")
        self.assertEqual(values["hispanic"], "No", result)
        self.assertEqual(values["gender"], "Male")
        self.assertEqual(values["veteran"], "I am not a protected veteran")
        self.assertEqual(values["disability"], "No, I do not have a disability")
        self.assertEqual(result["requiredEmpty"], 0)

    def test_fuzzy_match_is_guarded_by_input_type(self):
        result, values = self.run_fixture(
            '<div class="field"><label for="wrong">Email Adress</label><input id="wrong" type="date" required></div>'
        )

        self.assertEqual(values["wrong"], "")
        self.assertEqual(result["requiredEmpty"], 1)

    def test_unknown_required_date_is_not_guessed_as_start_date(self):
        result, values = self.run_fixture(
            '<div class="field"><label for="certification">Certification Date</label><input id="certification" type="date" required></div>'
        )

        self.assertEqual(values["certification"], "")
        self.assertEqual(result["requiredEmpty"], 1)
        self.assertTrue(any("Certification Date" in label for label in result["requiredEmptyLabels"]))

    def test_workday_applicant_privacy_policy_is_safely_accepted(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                '<label for="privacy">I have read the Gen Applicant Privacy Policy.</label>'
                '<input id="privacy" type="checkbox" required>'
                '<label for="candidate-privacy">I acknowledge and agree to Salesforce\'s Candidate Privacy Notice.</label>'
                '<input id="candidate-privacy" type="checkbox" required>'
            )
            result = page.evaluate(_autofill_script(WORKDAY_FACTS))
            self.assertTrue(page.locator("#privacy").is_checked())
            self.assertTrue(page.locator("#candidate-privacy").is_checked())
            self.assertEqual(result["requiredEmpty"], 0)
            self.assertGreaterEqual(result["choices"], 1)
        finally:
            page.close()

    def test_postal_code_is_not_mistaken_for_verification_code(self):
        result, values = self.run_fixture(
            '<div class="field"><label for="postalCode">Postal Code</label>'
            '<input id="postalCode" name="postalCode" required></div>',
            {**WORKDAY_FACTS, "postal_code": "24060"},
        )

        self.assertEqual(values["postalCode"], "24060")
        self.assertFalse(result["verificationCodeRequired"])

    def test_workday_source_search_selects_linkedin(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field" aria-required="true">
                  <label for="source--source">How Did You Hear About Us?</label>
                  <input id="source--source" role="combobox" aria-haspopup="listbox" required>
                  <div id="options" role="listbox"></div>
                </div>
                <script>
                  const source = document.getElementById('source--source');
                  let inExternalSources = false;
                  source.addEventListener('input', () => {
                    const option = document.createElement('div');
                    option.setAttribute('role', 'option');
                    option.textContent = inExternalSources ? 'LinkedIn Connection Post' : 'External Career Site Sources';
                    option.addEventListener('click', () => {
                      if (inExternalSources) window.selectedSource = 'LinkedIn Connection Post';
                      else inExternalSources = true;
                    });
                    document.getElementById('options').replaceChildren(option);
                  });
                </script>
                """
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "heard_about": "LinkedIn"}))
            self.assertEqual(page.evaluate("window.selectedSource"), "LinkedIn Connection Post")
            self.assertGreaterEqual(result["selects"], 1)
            self.assertFalse(result["verificationCodeRequired"])
        finally:
            page.close()

    def test_workday_phone_state_and_previous_worker_controls(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field">
                  <label for="phoneNumber--countryPhoneCode">Country Phone Code</label>
                  <div>1 item selected, United States of America (+1)</div>
                  <input id="phoneNumber--countryPhoneCode" required>
                </div>
                <div class="field"><label for="phoneNumber--phoneNumber">Phone Number</label>
                  <input id="phoneNumber--phoneNumber" required></div>
                <div class="field"><label for="phoneNumber--extension">Phone Extension</label>
                  <input id="phoneNumber--extension" value="+15409143128"></div>
                <div class="field"><label for="address--countryRegion">State</label>
                  <button id="address--countryRegion" aria-label="State Select One Required">Select One</button>
                  <div role="option" onclick="window.selectedState = 'Virginia'">Virginia</div>
                </div>
                <div class="field">Have you previously worked for this company as an employee or contractor?
                  <label><input id="previous-yes" type="radio" name="previous" value="true" required>Yes</label>
                  <label><input id="previous-no" type="radio" name="previous" value="false" required>No</label>
                </div>
                """
            )
            result = page.evaluate(
                _autofill_script(
                    {
                        **WORKDAY_FACTS,
                        "phone": "540 914 3128",
                        "phone_full": "+15409143128",
                        "state": "Virginia",
                        "previously_worked_for_company": "no",
                    }
                )
            )
            self.assertEqual(page.locator("#phoneNumber--phoneNumber").input_value(), "5409143128")
            self.assertEqual(page.locator("#phoneNumber--extension").input_value(), "")
            self.assertEqual(page.evaluate("window.selectedState"), "Virginia")
            self.assertTrue(page.locator("#previous-no").is_checked())
            self.assertFalse(result["verificationCodeRequired"])
        finally:
            page.close()

    def test_workday_education_years_and_gpa(self):
        result, values = self.run_fixture(
            """
            <div class="field"><label for="education--gradeAverage">Overall Result (GPA)</label>
              <input id="education--gradeAverage" required></div>
            <div class="field"><label for="education--firstYearAttended-dateSectionYear-input">From</label>
              <input id="education--firstYearAttended-dateSectionYear-input" required></div>
            <div class="field"><label for="education--lastYearAttended-dateSectionYear-input">To (Actual or Expected)</label>
              <input id="education--lastYearAttended-dateSectionYear-input" required></div>
            """,
            {
                **WORKDAY_FACTS,
                "gpa": "3.71",
                "education_start_date": "Fall 2024",
                "graduation_year": "2027",
            },
        )

        self.assertEqual(values["education--gradeAverage"], "3.71")
        self.assertEqual(values["education--firstYearAttended-dateSectionYear-input"], "2024")
        self.assertEqual(values["education--lastYearAttended-dateSectionYear-input"], "2027")
        self.assertEqual(result["requiredEmpty"], 0)

    def test_profile_defaults_do_not_invent_availability_dates(self):
        facts = _default_facts({})
        self.assertEqual(facts["earliest_start_date_date"], "")
        self.assertEqual(facts["earliest_start_date_iso"], "")

    def test_ready_workday_create_account_shows_human_checkpoint(self):
        page = self.browser.new_page()
        try:
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                    <form>
                      <label>Email <input type="email" name="email" value="candidate@example.com" required></label>
                      <label>Password <input type="password" name="password" value="secret-value" required></label>
                      <label>Verify Password <input type="password" name="verifyPassword" value="secret-value" required></label>
                      <label><input type="checkbox" data-automation-id="createAccountCheckbox" checked required>
                        I agree to the Candidate Privacy Notice</label>
                      <div data-automation-id="noCaptchaWrapper" style="width:240px;height:44px">
                        <button type="submit" data-automation-id="createAccountSubmitButton" style="display:none">
                          Create Account
                        </button>
                        <div data-automation-id="click_filter" aria-label="Create Account"
                             style="width:240px;height:44px;opacity:0"></div>
                      </div>
                    </form>
                    """,
                ),
            )
            page.goto("https://acme.wd1.myworkdayjobs.com/en-US/job/apply")

            result = page.evaluate(_workday_human_checkpoint_script())

            self.assertTrue(result["required"], result)
            self.assertEqual(result["action"], "Create Account")
            self.assertEqual(result["reason"], "workday_protected_account_action")
            banner = page.locator("#langhire-human-checkpoint")
            self.assertEqual(banner.count(), 1)
            self.assertIn("click Create Account", banner.inner_text())
            self.assertEqual(banner.evaluate("el => getComputedStyle(el).pointerEvents"), "none")
        finally:
            page.close()

    def test_workday_checkpoint_waits_until_account_form_is_ready(self):
        page = self.browser.new_page()
        try:
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                    <form>
                      <input type="email" aria-label="Email" value="candidate@example.com" required>
                      <input type="password" aria-label="Password" value="secret-value" required>
                      <input type="password" aria-label="Verify Password" value="secret-value" required>
                      <input type="checkbox" data-automation-id="createAccountCheckbox" required>
                      <div data-automation-id="noCaptchaWrapper" style="width:240px;height:44px">
                        <button type="submit" data-automation-id="createAccountSubmitButton">Create Account</button>
                        <div data-automation-id="click_filter" aria-label="Create Account" style="width:240px;height:44px"></div>
                      </div>
                    </form>
                    """,
                ),
            )
            page.goto("https://acme.wd1.myworkdayjobs.com/en-US/job/apply")

            result = page.evaluate(_workday_human_checkpoint_script())

            self.assertFalse(result["required"], result)
            self.assertEqual(result["reason"], "account_form_not_ready")
            self.assertIn("terms/privacy checkbox", result["missing"])
            self.assertEqual(page.locator("#langhire-human-checkpoint").count(), 0)
        finally:
            page.close()

    def test_ready_workday_sign_in_uses_same_human_checkpoint(self):
        page = self.browser.new_page()
        try:
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                    <form>
                      <input type="email" aria-label="Email" value="candidate@example.com" required>
                      <input type="password" aria-label="Password" value="secret-value" required>
                      <div data-automation-id="noCaptchaWrapper" style="width:240px;height:44px">
                        <button type="submit" data-automation-id="signInSubmitButton" style="display:none">Sign In</button>
                        <div data-automation-id="click_filter" aria-label="Sign In" style="width:240px;height:44px"></div>
                      </div>
                    </form>
                    """,
                ),
            )
            page.goto("https://acme.wd1.myworkdayjobs.com/en-US/job/login")
            result = page.evaluate(_workday_human_checkpoint_script())
            self.assertTrue(result["required"], result)
            self.assertEqual(result["action"], "Sign In")
            self.assertIn("click Sign In", page.locator("#langhire-human-checkpoint").inner_text())
        finally:
            page.close()

    def test_checkpoint_does_not_trigger_off_workday(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                '<form><input type="email" value="a@example.com"><input type="password" value="x">'
                '<button data-automation-id="createAccountSubmitButton">Create Account</button></form>'
            )
            result = page.evaluate(_workday_human_checkpoint_script())
            self.assertFalse(result["required"])
            self.assertEqual(result["reason"], "not_workday")
        finally:
            page.close()

    def test_ready_workday_create_account_checkpoint_triggers_on_myworkdaysite_host(self):
        # Real posting observed in production: Magna's career site is hosted on
        # wd3.myworkdaysite.com, not *.myworkdayjobs.com/*.workday.com. The
        # checkpoint must still fire there or the human "click Create Account"
        # banner/notification never appears and the run stalls silently.
        page = self.browser.new_page()
        try:
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                    <form>
                      <label>Email <input type="email" name="email" value="candidate@example.com" required></label>
                      <label>Password <input type="password" name="password" value="secret-value" required></label>
                      <label>Verify Password <input type="password" name="verifyPassword" value="secret-value" required></label>
                      <label><input type="checkbox" data-automation-id="createAccountCheckbox" checked required>
                        I agree to the Candidate Privacy Notice</label>
                      <div data-automation-id="noCaptchaWrapper" style="width:240px;height:44px">
                        <button type="submit" data-automation-id="createAccountSubmitButton" style="display:none">
                          Create Account
                        </button>
                        <div data-automation-id="click_filter" aria-label="Create Account"
                             style="width:240px;height:44px;opacity:0"></div>
                      </div>
                    </form>
                    """,
                ),
            )
            page.goto("https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/apply")

            result = page.evaluate(_workday_human_checkpoint_script())

            self.assertTrue(result["required"], result)
            self.assertEqual(result["action"], "Create Account")
            self.assertEqual(result["reason"], "workday_protected_account_action")
        finally:
            page.close()


class WorkdayHumanCheckpointWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_requires_stable_clear_before_resuming(self):
        initial = {
            "required": True,
            "action": "Create Account",
            "key": "Create Account:https://acme.wd1.myworkdayjobs.com/apply",
        }
        still_waiting = {**initial, "reason": "workday_protected_account_action"}
        cleared = {"required": False, "reason": "account_action_absent"}
        with (
            patch(
                "backend.core.autofill_facts.probe_workday_human_checkpoint",
                new=AsyncMock(side_effect=[still_waiting, cleared, cleared]),
            ) as probe,
            patch(
                "backend.core.autofill_facts._notify_workday_human_checkpoint",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await wait_for_workday_human_checkpoint(
                object(),
                initial=initial,
                timeout_seconds=1.0,
                poll_interval=0.01,
            )

        self.assertTrue(result["completed"], result)
        self.assertFalse(result["timed_out"])
        self.assertGreaterEqual(probe.await_count, 3)

    async def test_wait_reports_timeout_for_queue_skip(self):
        checkpoint = {
            "required": True,
            "reason": "workday_protected_account_action",
            "action": "Sign In",
            "key": "Sign In:https://acme.wd1.myworkdayjobs.com/login",
        }
        with (
            patch(
                "backend.core.autofill_facts.probe_workday_human_checkpoint",
                new=AsyncMock(return_value=checkpoint),
            ),
            patch(
                "backend.core.autofill_facts._notify_workday_human_checkpoint",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await wait_for_workday_human_checkpoint(
                object(),
                initial=checkpoint,
                timeout_seconds=0.12,
                poll_interval=0.05,
            )

        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["completed"])


class InteractiveBrowserHandoffTests(unittest.TestCase):
    def test_automation_browser_remains_focusable_for_human_click(self):
        kwargs = browser_session_kwargs()
        self.assertIs(kwargs["headless"], False)
        self.assertIn("--disable-focus-on-load", kwargs["ignore_default_args"])
        self.assertIn("--disable-window-activation", kwargs["ignore_default_args"])


if __name__ == "__main__":
    unittest.main()
