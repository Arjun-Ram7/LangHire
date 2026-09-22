from datetime import date

from backend.core.workday_experience import (
    choose_work_row,
    degree_option_rank,
    dropdown_answer,
    dropdown_choice,
    hear_pick,
    options_expr,
    is_hear_question,
    school_option_rank,
    education_field_updates,
    education_plan,
    parse_experience_text,
    phone_type_rank,
    option_rank,
    signature_date_parts,
    text_answer,
    veteran_option_rank,
    with_locations,
)

RESUME = """JORDAN ELLIS
Fairview, OH  ·  +1-555-201-4477
EDUCATION
Virginia Tech — Bachelor of Science in Computer Science, Minor in Artificial Intelligence
Expected May 2028
GPA: 3.71/4.00
EXPERIENCE
AI/ML Intern — Meridian Group (Meridian Benefits Consulting)
Jun 2026 – Aug 2026
• Trained separate XGBoost regression models in Azure ML to forecast renewal insurance revenue across 16
sales representative portfolios.
• Automated an end-to-end reporting pipeline in under a minute.
Undergraduate Researcher, NSF Trailhead Project — Virginia Tech  ·  Advisor: Dr. R. K. Alvarez
Jan 2026 – Present
• Engineered a Python web crawler and NLP pipeline.
Software Engineering Intern — Redwood Institute, Dubai Campus
Feb 2023 – Mar 2023
• Designed SQL schemas and queries.
PROJECTS
• Greenlight — Traffic Prediction Service. C++ inference microservice.
TECHNICAL SKILLS
Languages  ·  Python, Java, C++
"""


def test_parses_every_experience_entry_and_stops_at_next_section():
    entries = parse_experience_text(RESUME)

    assert [e["company"] for e in entries] == [
        "Meridian Group (Meridian Benefits Consulting)",
        "Virginia Tech",
        "Redwood Institute, Dubai Campus",
    ]
    assert [e["title"] for e in entries] == [
        "AI/ML Intern",
        "Undergraduate Researcher, NSF Trailhead Project",
        "Software Engineering Intern",
    ]


def test_dates_are_split_into_month_and_year_and_present_means_current():
    marsh, research, bits = parse_experience_text(RESUME)

    assert (marsh["start_month"], marsh["start_year"]) == ("06", "2026")
    assert (marsh["end_month"], marsh["end_year"], marsh["current"]) == ("08", "2026", False)
    assert (research["start_month"], research["start_year"]) == ("01", "2026")
    assert (research["end_month"], research["end_year"], research["current"]) == ("", "", True)
    assert (bits["end_month"], bits["end_year"]) == ("03", "2023")


def test_wrapped_bullet_lines_are_rejoined_into_one_description():
    marsh = parse_experience_text(RESUME)[0]

    assert marsh["description"].splitlines()[0].endswith("across 16 sales representative portfolios.")
    assert len(marsh["description"].splitlines()) == 2
    assert "•" not in marsh["description"]


def test_text_without_an_experience_section_yields_nothing():
    assert parse_experience_text("EDUCATION\nVirginia Tech\nExpected May 2028\n") == []


def test_education_plan_uses_year_only_dates_and_numeric_gpa():
    plan = education_plan({
        "school": "Virginia Tech",
        "degree": "Bachelor of Science in Computer Science",
        "major": "Computer Science",
        "gpa": "3.71/4.00",
        "education_start_date": "Fall 2024",
        "education_end_date": "Fall 2027",
    })

    assert plan == {
        "school": "Virginia Tech",
        "degree": "Bachelor of Science in Computer Science",
        "major": "Computer Science",
        "gpa": "3.71",
        "first_year": "2024",
        "last_year": "2027",
    }


def test_degree_options_prefer_the_closest_bachelor_match():
    options = ["Select One", "High School", "Associate's Degree", "Bachelor's Degree", "Bachelor of Science", "Master's Degree"]
    degree = "Bachelor of Science in Computer Science"

    ranked = sorted(options, key=lambda o: degree_option_rank(degree, o), reverse=True)

    assert ranked[0] == "Bachelor of Science"
    assert degree_option_rank(degree, "Master's Degree") == 0
    assert degree_option_rank(degree, "Select One") == 0


def test_workday_degree_list_picks_bachelor_of_science_not_a_lookalike_row():
    # Verbatim from a live Workday tenant: the intended row sits far down a long
    # list whose other rows also say "Degree" or "Bachelor".
    options = [
        "Select One", "Up to Middle School + 1 Year", "University Degree or Equivalent (BAC+2)",
        "Master\u00b4s Degree or Equivalent (BAC+5)", "Vocational Degree (CAP or BEP)",
        "Bachelor\u00b4s Degree or Equivalent (BAC+3 or 4)", "High School", "GED",
        "Associates of Science (A.S)", "Bachelor of Arts (B.A)", "Bachelor of Science (B.S)",
        "Master of Science (M.S)", "Doctor of Philosophy (Ph.D)", "Other",
    ]
    degree = "Bachelor of Science in Computer Science"

    assert max(options, key=lambda o: degree_option_rank(degree, o)) == "Bachelor of Science (B.S)"


PROFILE = {
    "address": {"city": "Fairview", "state": "Ohio"},
    "education": {"school": "Virginia Tech"},
    "work_locations": {"Redwood Institute": "Dubai, United Arab Emirates", "solace retail": "Dubai, UAE"},
}


def _job(company, title="Intern"):
    return {"title": title, "company": company}


def test_location_comes_from_the_profile_map_by_company_name():
    jobs = with_locations([_job("Redwood Institute, Dubai Campus"), _job("Solace Retail (Northstar Group)")], PROFILE)

    assert [j["location"] for j in jobs] == ["Dubai, United Arab Emirates", "Dubai, UAE"]


def test_a_role_at_the_candidates_own_university_is_located_in_their_home_city():
    jobs = with_locations([_job("Virginia Tech", "Undergraduate Researcher, ChainSentinel")], PROFILE)

    assert jobs[0]["location"] == "Fairview, Ohio"


def test_unknown_employer_is_left_blank_rather_than_guessed():
    jobs = with_locations([_job("Meridian Group (Meridian Benefits Consulting)")], PROFILE)

    assert jobs[0]["location"] == ""


def _row(row_id, title="", company="", job=""):
    return {"id": row_id, "title": title, "company": company, "job": job}


ENTRY = {"title": "AI/ML Intern", "company": "Meridian Group"}


def test_a_row_we_already_filled_is_reused_even_if_its_text_was_changed():
    rows = [_row("a", "Something else", "Someone else", job="0"), _row("b")]

    assert choose_work_row(rows, ENTRY, 0, 2) == ("row", rows[0])


def test_a_blank_row_is_filled_before_a_new_one_is_added():
    rows = [_row("a", "Other", "Other", job="1"), _row("b")]

    assert choose_work_row(rows, ENTRY, 0, 3) == ("row", rows[1])


def test_a_row_is_added_only_while_fewer_rows_than_entries_exist():
    assert choose_work_row([_row("a", "X", "Y", job="1")], ENTRY, 0, 2) == ("add", None)


def test_rows_never_grow_past_the_entry_count():
    # The live loop: every pass added five more rows because none "matched".
    rows = [_row(str(i), f"T{i}", f"C{i}") for i in range(5)]

    assert choose_work_row(rows, ENTRY, 0, 5) == ("full", None)


def test_signature_date_is_todays_date_as_zero_padded_month_day_year():
    assert signature_date_parts(date(2026, 9, 8)) == ("09", "08", "2026")
    assert signature_date_parts(date(2026, 12, 25)) == ("12", "25", "2026")


def test_phone_device_type_prefers_mobile_over_other_kinds():
    options = ["Select One", "Home", "Work", "Mobile", "Telephone"]

    assert max(options, key=phone_type_rank) == "Mobile"
    assert phone_type_rank("Home") == 0
    assert phone_type_rank("Select One") == 0
    assert phone_type_rank("Cell Phone") > 0


FACTS = {
    "age_over_18": "yes",
    "authorized_to_work_us": "yes",
    "visa_sponsorship_needed": "yes",
    "previously_worked_for_company": "no",
    "desired_pay": "Negotiable",
}

# Verbatim from live WEX application-question pages.
QUESTIONS = [
    ("Are you 18 years of age or older?", "Yes"),
    ("Do you have a high school diploma or GED?", "Yes"),
    ("Are you legally authorized to work in the United States?", "Yes"),
    ("Do you now, or in the future, require sponsorship (for example, an H-1B petition, F-1 STEM OPT I-983 "
     "training plan, adjustment of status portability through Form I-485, Supplement J, etc.) to work legally "
     "for WEX Inc. in the United States?", "Yes"),
    ("Have you previously worked at WEX?", "No"),
    ("Have you previously been employed by WEX Inc.? **CURRENT EMPLOYEES: Please apply via your internal "
     "Workday account instead.**", "No"),
]


def test_workday_yes_no_questions_are_answered_from_the_candidates_facts():
    for question, expected in QUESTIONS:
        assert dropdown_answer(question, FACTS) == expected, question


def test_the_sponsorship_question_is_not_mistaken_for_work_authorization():
    # It also says "to work legally ... in the United States".
    assert dropdown_answer(QUESTIONS[3][0], {**FACTS, "visa_sponsorship_needed": "no"}) == "No"


def test_an_unknown_question_or_a_missing_fact_is_left_alone():
    assert dropdown_answer("What is your favourite colour?", FACTS) is None
    assert dropdown_answer("Are you 18 years of age or older?", {}) is None


def test_salary_expectation_uses_the_desired_pay_fact():
    assert text_answer("What is your salary expectation?", FACTS) == "Negotiable"
    assert text_answer("What is your salary expectation?", {}) is None
    assert text_answer("Describe a project", FACTS) is None


def test_yes_no_options_match_exactly_not_by_prefix():
    options = ["Select One", "Yes", "No", "Not sure"]

    assert max(options, key=lambda o: option_rank("No", o)) == "No"
    assert option_rank("Yes", "Select One") == 0
    assert option_rank("No", "Not sure") < option_rank("No", "No")


PLAN = {"school": "Virginia Tech", "degree": "Bachelor of Science in Computer Science", "major": "Computer Science",
        "gpa": "3.71", "first_year": "2024", "last_year": "2028"}


def _edu_row(**over):
    return {"school": "Virginia Tech", "degree": "Bachelor of Science (B.S)", "fieldSelected": 1,
            "gpa": "3.71", "firstYear": "2024", "lastYear": "2028", **over}


def test_a_correct_education_row_needs_no_changes():
    assert education_field_updates(PLAN, _edu_row()) == []


def test_saved_wrong_values_from_an_earlier_run_are_corrected():
    # A previous run saved these to Workday's draft; only blank fields used to be filled,
    # so a wrong degree or graduation year stayed forever.
    row = _edu_row(degree="Bachelor of Arts (B.A)", lastYear="2027", gpa="3.5", school="Virginia Polytechnic")

    assert education_field_updates(PLAN, row) == ["school", "degree", "gpa", "last_year"]


def test_blank_fields_are_filled_and_a_close_degree_is_not_churned():
    row = _edu_row(school="", gpa="", firstYear="", degree="Bachelor of Science (B.S)")

    assert education_field_updates(PLAN, row) == ["school", "gpa", "first_year"]


import random

HEAR_OPTIONS = ["Select One", "Career Websites", "Employee Referral", "Internal", "Job Fair/Event",
                "Recruiting Agency", "Social Media", "Other", "United States of America (+1)"]


def test_how_did_you_hear_prefers_linkedin_when_the_list_has_it():
    assert hear_pick(["Select One", "Facebook", "LinkedIn", "Indeed"], random.Random(0)) == "LinkedIn"


def test_how_did_you_hear_otherwise_picks_any_harmless_option_at_random():
    picks = {hear_pick(HEAR_OPTIONS, random.Random(seed)) for seed in range(40)}

    assert picks <= {"Career Websites", "Social Media"}
    assert picks, "something must be chosen"


def test_how_did_you_hear_never_picks_a_referral_or_a_stray_phone_code():
    # Referral / internal / agency options ask for a name; the phone-code rows are another widget's list.
    unsafe = ["Employee Referral", "Internal", "Recruiting Agency", "United States of America (+1)", "Other", "Select One"]
    for seed in range(60):
        assert hear_pick(HEAR_OPTIONS, random.Random(seed)) not in unsafe


def test_how_did_you_hear_falls_back_to_any_real_option_when_nothing_else_is_left():
    assert hear_pick(["Select One", "Employee Referral"], random.Random(0)) == "Employee Referral"
    assert hear_pick(["Select One"], random.Random(0)) is None


def test_veteran_options_rank_not_a_veteran_first_and_never_the_ones_that_claim_service():
    fact = "Not a veteran"
    options = [
        "I identify as one or more of the classifications of protected veteran",
        "I identify as a veteran, just not a protected veteran",
        "I am not a protected veteran",
        "I am not a veteran",
        "I do not wish to self identify",
    ]
    ranks = {o: veteran_option_rank(fact, o) for o in options}

    assert ranks["I am not a protected veteran"] > 0 and ranks["I am not a veteran"] > 0
    assert ranks["I identify as one or more of the classifications of protected veteran"] == 0
    assert ranks["I identify as a veteran, just not a protected veteran"] == 0
    assert ranks["I do not wish to self identify"] == 0


def test_dropdown_choice_covers_the_questions_workday_asks_on_page_one_and_disclosures():
    facts = {**FACTS, "veteran_status": "Not a veteran"}

    veteran = dropdown_choice("Veteran Status", facts)
    assert veteran is not None and veteran("I am not a protected veteran") > veteran("I identify as a veteran, just not a protected veteran")
    assert dropdown_choice("How Did You Hear About Us?", facts) is None  # chosen from the whole list
    assert is_hear_question("How Did You Hear About Us?") and not is_hear_question("Veteran Status")
    worked = dropdown_choice("Have you previously worked for Plexus?", facts)
    assert worked is not None and worked("No") > worked("Yes")
    assert dropdown_choice("Favourite colour", facts) is None


def test_free_text_questions_are_answered_from_the_facts():
    facts = {**FACTS, "earliest_start_date": "May 16, 2027", "interest_statement": "I want to build reliable software."}

    assert text_answer("What is your base salary range expectations?", facts) == "Negotiable"
    assert text_answer("When are you available to start a new position?", facts) == "May 16, 2027"
    assert text_answer("What is the best way to contact you?", facts) == "Email"
    assert text_answer("Why are you looking for new opportunities?", facts) == "I want to build reliable software."
    assert text_answer("Describe your biggest weakness", facts) is None


def test_options_belong_to_the_popup_nearest_the_clicked_control_not_to_another_widgets_list():
    # Live: choosing "How did you hear about us?" clicked "Massachusetts", a row of the State list.
    from playwright.sync_api import sync_playwright

    html = """
    <div style="position:absolute; top:100px; left:50px; width:300px; height:40px" id="source">source</div>
    <ul role="listbox" style="position:absolute; top:150px; left:50px; width:300px; height:80px; margin:0">
      <li role="option">Career Websites</li><li role="option">Social Media</li></ul>
    <ul role="listbox" style="position:absolute; top:600px; left:50px; width:300px; height:80px; margin:0">
      <li role="option">Massachusetts</li><li role="option">Michigan</li></ul>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        near = page.evaluate(f"{options_expr('document.getElementById(\"source\")')}.map(e => e.innerText)")
        anywhere = page.evaluate(f"{options_expr(None)}.map(e => e.innerText)")
        browser.close()

    assert near == ["Career Websites", "Social Media"]
    assert set(anywhere) == {"Career Websites", "Social Media", "Massachusetts", "Michigan"}


def test_a_school_picked_from_a_search_list_counts_even_when_its_official_name_differs():
    # Some tenants list "Virginia Polytechnic Institute and State University", not "Virginia Tech".
    row = _edu_row(school="Virginia Polytechnic Institute and State University")

    assert "school" not in education_field_updates(PLAN, row)
    assert "school" in education_field_updates(PLAN, _edu_row(school="Virginia Union University"))
    assert "school" in education_field_updates(PLAN, _edu_row(school=""))


def test_school_options_prefer_the_exact_name_then_the_official_one_and_never_a_lookalike():
    options = ["Virginia Union University", "Virginia Polytechnic Institute and State University",
               "Virginia Tech", "West Virginia University"]

    ranked = sorted(options, key=lambda o: school_option_rank("Virginia Tech", o), reverse=True)

    assert ranked[:2] == ["Virginia Tech", "Virginia Polytechnic Institute and State University"]
    assert school_option_rank("Virginia Tech", "Virginia Union University") == 0
    assert school_option_rank("Virginia Tech", "West Virginia University") == 0


def test_education_level_questions_get_a_bachelors_never_an_associates():
    facts = {**FACTS, "degree": "Bachelor of Science in Computer Science"}
    options = ["Select One", "High School", "Associate's Degree", "Associates of Science (A.S)",
               "Bachelor's Degree", "Bachelor of Science (B.S)", "Master's Degree"]

    for question in ("What is your highest level of education?", "Education Level", "Degree", "What degree are you pursuing?"):
        choose = dropdown_choice(question, facts)
        assert choose is not None, question
        assert max(options, key=choose) == "Bachelor of Science (B.S)", question
        assert choose("Associate's Degree") == 0 and choose("Associates of Science (A.S)") == 0
