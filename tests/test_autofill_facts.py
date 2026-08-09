import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from playwright.sync_api import sync_playwright

from backend.core.autofill_facts import (
    _age_from_dob,
    _autofill_script,
    load_autofill_facts,
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

    def test_lever_style_authorization_and_sponsorship_are_not_shadowed_by_country_select(self):
        # Lever's standard phrasing embeds the word "country" inside the
        # authorization question, and "visa status" inside the sponsorship
        # question. Both previously matched an earlier, unrelated <select>
        # branch (country / visa_status) and were left blank.
        result, values = self.run_fixture(
            """
            <div class="field"><label for="auth">Are you legally authorized to work in the country for which you are applying?</label>
              <select id="auth" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
            <div class="field"><label for="sponsor">Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, etc.)?</label>
              <select id="sponsor" required><option value="">Select</option><option>Yes</option><option>No</option></select></div>
            """
        )

        self.assertEqual(values["auth"], "Yes", result)
        self.assertEqual(values["sponsor"], "Yes", result)
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

    def test_combobox_with_no_matching_option_is_left_blank_not_arrowed_into(self):
        # The ArrowDown/aria-activedescendant fallback ran for every field, so a
        # combobox whose options do not contain the answer committed whatever
        # happened to be highlighted first. A run picked "Aalborg University"
        # for a candidate whose school is Virginia Tech that way. Taking the
        # first suggestion is only acceptable for the fields where it is a
        # deliberate strategy (location, school); elsewhere blank is correct,
        # because a wrong answer is worse than one the human can finish.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field">
                  <label for="degree">Degree</label>
                  <input id="degree" role="combobox" class="select__input"
                         aria-activedescendant="degree-opt-0" required>
                  <ul role="listbox">
                    <li id="degree-opt-0" role="option">Associate's Degree</li>
                    <li id="degree-opt-1" role="option">Doctorate</li>
                  </ul>
                </div>
                """
            )

            result = page.evaluate(
                _autofill_script({**WORKDAY_FACTS, "degree": "Bachelor of Science"})
            )

            self.assertEqual(result["selects"], 0, result)
            self.assertIsNone(
                page.locator("#degree").get_attribute("data-static-autocomplete-selected")
            )
        finally:
            page.close()

    def test_school_question_mentioning_country_is_not_filled_with_the_country(self):
        # "We recruit from universities across the country" contains "country",
        # which matched the country rule first and typed "USA" into the school
        # field. A wrong answer is worse than a blank one.
        result, values = self.run_fixture(
            """
            <div class="q"><label for="school">We recruit from universities across the country.
              If your school isn't listed please select Other.</label>
              <input id="school" placeholder="Start typing..." role="combobox" required></div>
            """,
            {**WORKDAY_FACTS, "school": "Virginia Tech", "country": "USA"},
        )

        self.assertNotEqual(values["school"], "USA", result["debugInputs"])

    def test_signature_date_is_not_filled_with_a_location(self):
        # A MM/DD/YYYY signature date matched the location rule and received
        # "Blacksburg, Virginia".
        result, values = self.run_fixture(
            """
            <div class="q"><label for="sig">Signature date</label>
              <input id="sig" placeholder="MM/DD/YYYY" required></div>
            """,
            {**WORKDAY_FACTS, "current_location": "Blacksburg, Virginia", "city": "Blacksburg"},
        )

        self.assertNotIn("Blacksburg", values["sig"], result["debugInputs"])

    def test_preferred_name_is_answered_with_the_first_name(self):
        result, values = self.run_fixture(
            """
            <div class="q"><label for="pname">Preferred name - what would you like us to call you?</label>
              <input id="pname" required></div>
            """,
            {**WORKDAY_FACTS, "first_name": "Arjun"},
        )

        self.assertEqual(values["pname"], "Arjun", result["debugInputs"])

    def test_legal_address_is_answered_with_the_street_address(self):
        result, values = self.run_fixture(
            """
            <div class="q"><label for="addr">Legal address:</label>
              <input id="addr" required></div>
            """,
            {**WORKDAY_FACTS, "street_address": "504 Hunt Club Rd"},
        )

        self.assertEqual(values["addr"], "504 Hunt Club Rd", result["debugInputs"])

    def test_veteran_and_disability_comboboxes_are_answered(self):
        # Veteran and disability rules existed only on the <select> path, so the
        # combobox form of the same questions was never matched at all.
        result, _ = self.run_fixture(
            """
            <div class="q"><label for="vet">Veteran status</label>
              <input id="vet" role="combobox" class="select__input" required></div>
            <div class="q"><label for="dis">Disability status</label>
              <input id="dis" role="combobox" class="select__input" required></div>
            """,
            {**WORKDAY_FACTS, "veteran_status": "Not a veteran", "disability_status": "No"},
        )

        picked = {item["id"]: item["picked"] for item in result["debugInputs"]}
        self.assertEqual(picked.get("vet"), "veteran_status", result["debugInputs"])
        self.assertEqual(picked.get("dis"), "disability_status", result["debugInputs"])

    def test_sexual_orientation_select_is_answered_from_facts(self):
        result, values = self.run_fixture(
            """
            <div class="q"><label for="so">How would you describe your sexual orientation?</label>
              <select id="so" required><option value="">Select</option>
                <option>Heterosexual</option><option>Gay</option>
                <option>I don't wish to answer</option></select></div>
            """,
            {**WORKDAY_FACTS, "sexual_orientation": "Heterosexual"},
        )

        self.assertEqual(values["so"], "Heterosexual", result["debugInputs"])

    def test_yes_no_f1_visa_question_is_answered_yes(self):
        # "Are you currently on an F-1 visa?" offers Yes/No, but the rule
        # returned the visa_status text ("F-1 student visa"), which is not one
        # of the options, so the question was left blank.
        result, values = self.run_fixture(
            """
            <div class="field"><label for="f1">Are you currently on an F-1 visa?</label>
              <select id="f1" required><option value="">Select</option>
                <option>Yes</option><option>No</option></select></div>
            """,
            {**WORKDAY_FACTS, "f1_visa_status": "yes", "visa_status": "F-1 student visa"},
        )

        self.assertEqual(values["f1"], "Yes", result)
        self.assertEqual(result["requiredEmpty"], 0)

    def test_gender_radio_does_not_select_female_for_a_male_candidate(self):
        # valueMatchesFact compared with substrings, and "female" contains
        # "male", so the Female option matched a Male candidate and a wrong
        # demographic answer was submitted.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <fieldset><legend>Gender</legend>
                  <label><input type="radio" name="g" value="Male" required> Male</label>
                  <label><input type="radio" name="g" value="Female"> Female</label>
                </fieldset>
                """
            )

            page.evaluate(_autofill_script(WORKDAY_FACTS))

            checked = page.eval_on_selector_all(
                'input[name="g"]', "els => els.filter(e => e.checked).map(e => e.value)"
            )
            self.assertEqual(checked, ["Male"])
        finally:
            page.close()

    def test_value_rejected_by_the_page_is_not_counted_as_filled(self):
        # React-controlled inputs discard a programmatic value and re-render
        # their own. Counting the write instead of the result is what produced
        # reports like filled=17 on a form that still had 7 required blanks,
        # and it is why autofill and manual review disagreed.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="first">First Name</label>
                  <input id="first" required></div>
                <script>
                  const el = document.getElementById('first');
                  el.addEventListener('input', () => { el.value = ''; });
                </script>
                """
            )

            result = page.evaluate(_autofill_script(WORKDAY_FACTS))

            self.assertEqual(page.locator("#first").input_value(), "")
            self.assertEqual(result["filled"], 0, result)
        finally:
            page.close()

    def test_only_the_first_unresolved_field_stays_interactive(self):
        # Order is enforced structurally, not by asking the model to behave:
        # every unresolved field below the topmost one is taken out of the
        # interactive element index, so the agent has exactly one field it can
        # act on and cannot bounce between them.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="q1">First custom question</label>
                  <input id="q1" required></div>
                <div class="field"><label for="q2">Second custom question</label>
                  <input id="q2" required></div>
                <div class="field"><label for="q3">Third custom question</label>
                  <input id="q3" required></div>
                """
            )

            page.evaluate(_autofill_script(WORKDAY_FACTS))

            self.assertIsNone(page.locator("#q1").get_attribute("aria-disabled"))
            self.assertEqual(page.locator("#q2").get_attribute("aria-disabled"), "true")
            self.assertEqual(page.locator("#q3").get_attribute("aria-disabled"), "true")
        finally:
            page.close()

    def test_next_field_opens_up_once_the_one_above_it_is_resolved(self):
        # Answering the top field must hand the turn to the next one down.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="q1">First custom question</label>
                  <input id="q1" required></div>
                <div class="field"><label for="q2">Second custom question</label>
                  <input id="q2" required></div>
                """
            )
            script = _autofill_script(WORKDAY_FACTS)

            page.evaluate(script)
            page.locator("#q1").fill("an answer")
            page.evaluate(script)

            self.assertIsNone(page.locator("#q2").get_attribute("aria-disabled"))
        finally:
            page.close()

    def test_deferred_fields_do_not_burn_attempts_before_their_turn(self):
        # Only the topmost unresolved field is interactive, so the ones below it
        # have not been attempted yet. Counting passes against them would
        # abandon a question before the agent could ever reach it.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="q1">First custom question</label>
                  <input id="q1" required></div>
                <div class="field"><label for="q2">Second custom question</label>
                  <input id="q2" required></div>
                """
            )
            script = _autofill_script(WORKDAY_FACTS)

            for _ in range(4):
                page.evaluate(script)

            self.assertEqual(
                page.locator("#q1").get_attribute("data-static-abandoned"), "true"
            )
            self.assertIsNone(
                page.locator("#q2").get_attribute("data-static-abandoned")
            )
        finally:
            page.close()

    def test_unanswerable_field_is_abandoned_after_three_attempts(self):
        # The agent must not loop on one field forever. Static autofill runs
        # after every agent step, so a field that is still blank on three
        # consecutive passes has had three attempts and is abandoned: it is
        # dropped from the agent's interactive element index (pointer-events,
        # aria-disabled, tabindex) so the next unresolved field below it
        # becomes the only thing left to act on.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field">
                  <label for="essay">Describe the most impressive thing you have ever built.</label>
                  <textarea id="essay" required></textarea>
                </div>
                """
            )
            script = _autofill_script(WORKDAY_FACTS)

            first = page.evaluate(script)
            second = page.evaluate(script)
            self.assertEqual(first["abandoned"], 0, first)
            self.assertEqual(second["abandoned"], 0, second)
            self.assertTrue(
                any("essay" in entry for entry in second["needsLlm"]), second["needsLlm"]
            )

            third = page.evaluate(script)

            self.assertEqual(third["abandoned"], 1, third)
            self.assertEqual(
                page.locator("#essay").get_attribute("aria-disabled"), "true"
            )
            self.assertFalse(
                any("essay" in entry for entry in third["needsLlm"]), third["needsLlm"]
            )
        finally:
            page.close()

    def test_field_filled_before_the_cap_is_never_abandoned(self):
        # Abandonment must only ever apply to fields nothing could fill. A field
        # the agent completes on its second attempt stays live and answered.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field">
                  <label for="essay">Describe the most impressive thing you have ever built.</label>
                  <textarea id="essay" required></textarea>
                </div>
                """
            )
            script = _autofill_script(WORKDAY_FACTS)

            page.evaluate(script)
            page.locator("#essay").fill("A distributed build cache.")
            page.evaluate(script)
            result = page.evaluate(script)

            self.assertEqual(result["abandoned"], 0, result)
            self.assertIsNone(page.locator("#essay").get_attribute("aria-disabled"))
            self.assertEqual(
                page.locator("#essay").input_value(), "A distributed build cache."
            )
        finally:
            page.close()

    def test_lever_card_question_text_reaches_the_matcher(self):
        # Lever wraps each custom question as
        #   li.application-question > div.application-label (question text)
        #                           > div.application-field > div > <control>
        # labelText's ancestor lookup listed a bare "div" alongside the specific
        # question containers, and closest() returns the *nearest* match, so it
        # always stopped at the innermost wrapper div. The question text sits
        # above that, so the matcher only ever saw "cards[uuid][field1]" plus a
        # CSS class and could not answer anything.
        result, values = self.run_fixture(
            """
            <li class="application-question custom-question">
              <div class="application-label"><div class="text">Expected graduation year</div></div>
              <div class="application-field">
                <div class="card-field-wrapper">
                  <select id="cards-grad-year" name="cards[abc][field1]" class="card-field-input" required>
                    <option value="">Select...</option>
                    <option>2025</option><option>2026</option><option>2027</option><option>2028</option>
                  </select>
                </div>
              </div>
            </li>
            """
        )

        self.assertEqual(values["cards-grad-year"], "2027", result["debugInputs"])

    def test_greenhouse_disability_question_is_not_matched_as_major(self):
        # Greenhouse's standard disability question contains the phrase
        # "one or more of your major life activities". The word "major" matched
        # the field-of-study rule, so the question was consumed by the major
        # rule: never answered, and never offered to the LLM either.
        result, _ = self.run_fixture(
            """
            <div class="field">
              <label for="disability-q">Do you have a disability or chronic condition (physical, visual,
                auditory, cognitive, mental, emotional, or other) that substantially limits one or more
                of your major life activities?</label>
              <input id="disability-q" class="select__input" role="combobox" required>
            </div>
            """,
            {**WORKDAY_FACTS, "major": "Computer Science"},
        )

        picked = {item["id"]: item["picked"] for item in result["debugInputs"]}
        self.assertNotEqual(picked.get("disability-q"), "major", result["debugInputs"])

    def test_restrictive_covenant_question_is_not_matched_as_current_employer(self):
        # "...any job duties for a company by any restrictive covenants..."
        # contains "company", which matched the current-employer rule.
        result, values = self.run_fixture(
            """
            <div class="field">
              <label for="covenant-q">Are you prohibited or limited in your performance of any job duties
                for a company by any restrictive covenants not to compete or confidentiality agreements?</label>
              <input id="covenant-q" required>
            </div>
            """,
            {**WORKDAY_FACTS, "current_employer": "Acme Corp"},
        )

        self.assertNotEqual(values["covenant-q"], "Acme Corp", result["debugInputs"])

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

    def test_school_combobox_falls_back_to_first_suggestion_on_name_mismatch(self):
        # Typed "Virginia Tech" but the ATS's own suggestion list spells out
        # the official name. No exact/loose text match should exist, but a
        # visible suggestion means the search was recognized — take the
        # first option rather than leaving the field for the LLM.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="school">School</label>
                  <input id="school" role="combobox" aria-haspopup="listbox" required>
                  <div id="options" role="listbox"></div></div>
                <script>
                  const input = document.getElementById('school');
                  input.addEventListener('input', () => {
                    const opt = document.createElement('div');
                    opt.setAttribute('role', 'option');
                    opt.textContent = 'Virginia Polytechnic Institute and State University';
                    opt.addEventListener('click', () => { window.selectedSchool = opt.textContent; });
                    document.getElementById('options').replaceChildren(opt);
                  });
                </script>
                """
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "school": "Virginia Tech"}))
            self.assertEqual(page.evaluate("window.selectedSchool"), "Virginia Polytechnic Institute and State University")
            self.assertGreaterEqual(result["selects"], 1)
        finally:
            page.close()

    def test_locked_plain_text_field_becomes_read_only(self):
        # Read-only stops the LLM agent from wasting steps re-typing a value
        # static autofill already filled correctly.
        page = self.browser.new_page()
        try:
            page.set_content('<label for="first">First Name</label><input id="first">')
            page.evaluate(_autofill_script(WORKDAY_FACTS))
            self.assertEqual(page.locator("#first").input_value(), "Arjun")
            self.assertTrue(page.locator("#first").evaluate("el => el.readOnly"))
        finally:
            page.close()

    def test_locked_combobox_field_stays_editable_for_a_second_search_pass(self):
        # Combobox-style widgets are excluded from the read-only lock because
        # some (e.g. Workday's "How did you hear about us") need a second
        # setNativeValue()+search pass on the same input to resolve a nested
        # suggestion list — see test_workday_source_search_selects_linkedin.
        page = self.browser.new_page()
        try:
            page.set_content(
                '<label for="loc">Current Location</label>'
                '<input id="loc" role="combobox" aria-haspopup="listbox">'
            )
            result = page.evaluate(
                _autofill_script({**WORKDAY_FACTS, "current_location": "Blacksburg, Virginia"})
            )
            self.assertEqual(page.locator("#loc").input_value(), "Blacksburg, Virginia", result)
            self.assertFalse(page.locator("#loc").evaluate("el => el.readOnly"))
        finally:
            page.close()

    def test_label_text_crosses_shadow_boundary_for_greenhouse_style_questions(self):
        # Real bug found on a live Greenhouse application: each custom-question
        # <input> is wrapped in its own open shadow root, while the actual
        # visible question text ("How did you hear about this job?") lives
        # outside it in the light DOM. closest()/previousElementSibling/
        # parentElement all stop dead at a shadow boundary, so labelText()
        # read almost nothing for these inputs and fuzzy-matched them onto
        # unrelated fields — this exact field was locked in as `linkedin_url`
        # on the live run, and a "restrictive covenants" question elsewhere
        # on the same page was locked as `current_employer`.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field">
                  <div>How did you hear about this job?</div>
                  <div id="host"></div>
                </div>
                """
            )
            page.evaluate(
                """() => {
                    const host = document.getElementById('host');
                    const root = host.attachShadow({ mode: 'open' });
                    const input = document.createElement('input');
                    input.id = 'question_18208291008';
                    input.type = 'text';
                    input.setAttribute('role', 'combobox');
                    root.appendChild(input);
                }"""
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "heard_about": "LinkedIn"}))
            picked = next(
                (item.get("picked") for item in result.get("debugInputs", []) if item.get("id") == "question_18208291008"),
                None,
            )
            self.assertEqual(picked, "heard_about", result)
        finally:
            page.close()

    def test_resolved_combobox_becomes_noninteractive_but_stays_in_form_submission(self):
        # This is the fix for the real oscillation bug: an LLM cleanup agent
        # kept re-clicking an already-correctly-answered veteran-status field
        # on The Nuclear Company application because the field remained fully
        # clickable after static autofill resolved it, and a weak vision
        # model reading a page full of legal boilerplate never reliably
        # noticed the data-staticAutocompleteSelected hint. Once resolved,
        # the element must become unclickable (pointer-events/aria-disabled/
        # tabindex) so it drops out of the agent's own interactive-element
        # list — but never `disabled`, which would silently drop its value
        # from the real form submission.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="race">How would you describe your racial/ethnic background?</label>
                  <input id="race" name="race" role="combobox" aria-haspopup="listbox"></div>
                <div id="options" role="listbox"></div>
                <script>
                  const input = document.getElementById('race');
                  input.addEventListener('input', () => {
                    const opt = document.createElement('div');
                    opt.setAttribute('role', 'option');
                    opt.textContent = 'Asian (Not Hispanic or Latino)';
                    opt.addEventListener('click', () => { window.selectedRace = opt.textContent; });
                    document.getElementById('options').replaceChildren(opt);
                  });
                </script>
                """
            )
            page.evaluate(_autofill_script({**WORKDAY_FACTS, "race_ethnicity": "Asian Indian"}))
            self.assertEqual(page.evaluate("window.selectedRace"), "Asian (Not Hispanic or Latino)")
            el = page.locator("#race")
            self.assertEqual(el.evaluate("e => e.style.pointerEvents"), "none")
            self.assertEqual(el.evaluate("e => e.getAttribute('aria-disabled')"), "true")
            self.assertEqual(el.evaluate("e => e.getAttribute('tabindex')"), "-1")
            self.assertFalse(el.evaluate("e => e.disabled"), "must not be `disabled` — that drops it from form submission")

            # A second pass (what on_step does every LLM step) must not
            # attempt to re-click it — pointer-events:none makes any such
            # click a no-op, so the recorded answer can't be clobbered.
            page.evaluate("window.selectedRace = null")
            page.evaluate(_autofill_script({**WORKDAY_FACTS, "race_ethnicity": "Asian Indian"}))
            self.assertIsNone(page.evaluate("window.selectedRace"))
        finally:
            page.close()

    def test_school_native_select_resolves_nickname_to_precise_official_name(self):
        # Real live failure on a Palantir application: a 3300-option native
        # <select> world-university list had no "Virginia Tech" entry (real
        # entry: "Virginia Polytechnic Institute and State University"). With
        # no alias, the LLM cleanup agent guessed and picked "Virginia
        # Commonwealth University" instead — a different, unrelated school —
        # which is worse than leaving the field blank. The alias map must
        # resolve precisely and never accidentally match the wrong Virginia
        # school, even though both names literally contain "Virginia".
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <label for="school">University</label>
                <select id="school">
                  <option value="">Select...</option>
                  <option value="vcu">Virginia Commonwealth University</option>
                  <option value="uva">University of Virginia</option>
                  <option value="vt">Virginia Polytechnic Institute and State University</option>
                  <option value="other">Other (School Not Listed)</option>
                </select>
                """
            )
            page.evaluate(_autofill_script({**WORKDAY_FACTS, "school": "Virginia Tech"}))
            self.assertEqual(page.locator("#school").input_value(), "vt")
        finally:
            page.close()

    def test_gender_combobox_matches_man_option_when_profile_says_male(self):
        # Greenhouse-style EEO combobox where the visible option text is
        # "Man" rather than the literal profile value "Male" — this used to
        # fail with "Menu item with text or value 'Male' not found" during
        # LLM cleanup because the static engine never even recognized this
        # as a resolvable dropdown field.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="gender">How would you describe your gender identity?</label>
                  <input id="gender" role="combobox" aria-haspopup="listbox"></div>
                <div id="options" role="listbox"></div>
                <script>
                  const input = document.getElementById('gender');
                  input.addEventListener('input', () => {
                    const opts = ['Man', 'Woman', 'I prefer to self-describe', 'Decline to self identify'];
                    document.getElementById('options').replaceChildren(
                      ...opts.map(text => {
                        const opt = document.createElement('div');
                        opt.setAttribute('role', 'option');
                        opt.textContent = text;
                        opt.addEventListener('click', () => { window.selectedGender = text; });
                        return opt;
                      })
                    );
                  });
                </script>
                """
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "gender": "Male"}))
            self.assertEqual(page.evaluate("window.selectedGender"), "Man", result)
        finally:
            page.close()

    def test_race_ethnicity_combobox_matches_eeo_label_variant(self):
        # Greenhouse/Lever EEO race question renders "Asian (Not Hispanic or
        # Latino)" while the profile stores "Asian Indian" — the two must be
        # recognized as the same category via alias matching, not left blank
        # or (worse) resolved to an unrelated option.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="race">How would you describe your racial/ethnic background?</label>
                  <input id="race" role="combobox" aria-haspopup="listbox"></div>
                <div id="options" role="listbox"></div>
                <script>
                  const input = document.getElementById('race');
                  input.addEventListener('input', () => {
                    const opts = ['White (Not Hispanic or Latino)', 'Asian (Not Hispanic or Latino)', 'Black or African American (Not Hispanic or Latino)', 'Two or More Races'];
                    document.getElementById('options').replaceChildren(
                      ...opts.map(text => {
                        const opt = document.createElement('div');
                        opt.setAttribute('role', 'option');
                        opt.textContent = text;
                        opt.addEventListener('click', () => { window.selectedRace = text; });
                        return opt;
                      })
                    );
                  });
                </script>
                """
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "race_ethnicity": "Asian Indian"}))
            self.assertEqual(page.evaluate("window.selectedRace"), "Asian (Not Hispanic or Latino)", result)
        finally:
            page.close()

    def test_gender_combobox_leaves_blank_rather_than_substituting_wrong_value(self):
        # If no option even loosely matches the profile's gender, the field
        # must stay unresolved for manual/LLM review — never silently pick
        # an unrelated option (e.g. "I prefer to self-describe" when the
        # profile actually says "Male").
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="gender">Gender identity</label>
                  <input id="gender" role="combobox" aria-haspopup="listbox"></div>
                <div id="options" role="listbox"></div>
                <script>
                  const input = document.getElementById('gender');
                  input.addEventListener('input', () => {
                    const opt = document.createElement('div');
                    opt.setAttribute('role', 'option');
                    opt.textContent = 'Prefer to self-describe';
                    opt.addEventListener('click', () => { window.selectedGender = opt.textContent; });
                    document.getElementById('options').replaceChildren(opt);
                  });
                </script>
                """
            )
            page.evaluate(_autofill_script({**WORKDAY_FACTS, "gender": "Male"}))
            self.assertIsNone(page.evaluate("window.selectedGender"))
        finally:
            page.close()

    def test_education_end_month_combobox_resolves_month_name_from_iso_date(self):
        # Greenhouse renders education end date as two separate comboboxes
        # (end-month--0 / end-year--0) rather than one text field. This is
        # the exact field/id shape that got stuck looping during LLM cleanup
        # on The Nuclear Company application.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div class="field"><label for="end-month--0">Month</label>
                  <input id="end-month--0" role="combobox" aria-haspopup="listbox"></div>
                <div id="options" role="listbox"></div>
                <script>
                  const input = document.getElementById('end-month--0');
                  input.addEventListener('input', () => {
                    const opts = ['September', 'October', 'November', 'December'];
                    document.getElementById('options').replaceChildren(
                      ...opts.map(text => {
                        const opt = document.createElement('div');
                        opt.setAttribute('role', 'option');
                        opt.textContent = text;
                        opt.addEventListener('click', () => { window.selectedMonth = text; });
                        return opt;
                      })
                    );
                  });
                </script>
                """
            )
            result = page.evaluate(_autofill_script({**WORKDAY_FACTS, "education_end_date": "2027-11"}))
            self.assertEqual(page.evaluate("window.selectedMonth"), "November", result)
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


class FactDerivationTests(unittest.TestCase):
    def test_age_is_derived_from_date_of_birth(self):
        self.assertEqual(_age_from_dob("2006-10-28", date(2026, 8, 9)), 19)

    def test_age_increments_on_the_birthday_itself(self):
        self.assertEqual(_age_from_dob("2006-10-28", date(2026, 10, 28)), 20)

    def test_age_is_blank_without_a_usable_date_of_birth(self):
        self.assertEqual(_age_from_dob("", date(2026, 8, 9)), "")

    def test_stored_age_is_replaced_by_the_derived_one(self):
        # A written-down age is right only until the next birthday, so the
        # stored value must never win over the value derived from the DOB.
        with (
            patch("backend.core.autofill_facts.load_settings", return_value={}),
            patch("backend.core.autofill_facts.ensure_autofill_facts_file"),
            patch(
                "backend.core.autofill_facts._parse_facts_file",
                return_value={"date_of_birth": "10/28/2006", "age": "11"},
            ),
            patch("backend.core.autofill_facts._age_from_dob", return_value=20),
        ):
            facts = load_autofill_facts({})

        self.assertEqual(facts["age"], "20")
        self.assertEqual(facts["age_over_18"], "yes")
