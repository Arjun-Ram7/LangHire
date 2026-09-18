from datetime import date

from backend.core.workday_experience import (
    choose_work_row,
    degree_option_rank,
    dropdown_answer,
    education_field_updates,
    education_plan,
    parse_experience_text,
    phone_type_rank,
    option_rank,
    signature_date_parts,
    text_answer,
    with_locations,
)

RESUME = """ARJUN RAMACHANDRAN
Blacksburg, VA  ·  +1-540-914-3128
EDUCATION
Virginia Tech — Bachelor of Science in Computer Science, Minor in Artificial Intelligence
Expected May 2028
GPA: 3.71/4.00
EXPERIENCE
AI/ML Intern — Marsh McLennan (Mercer Marsh Benefits)
Jun 2026 – Aug 2026
• Trained separate XGBoost regression models in Azure ML to forecast renewal insurance revenue across 16
sales representative portfolios.
• Automated an end-to-end reporting pipeline in under a minute.
Undergraduate Researcher, NSF BEELINE Project — Virginia Tech  ·  Advisor: Dr. T. M. Murali
Jan 2026 – Present
• Engineered a Python web crawler and NLP pipeline.
Software Engineering Intern — BITS Pilani, Dubai Campus
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
        "Marsh McLennan (Mercer Marsh Benefits)",
        "Virginia Tech",
        "BITS Pilani, Dubai Campus",
    ]
    assert [e["title"] for e in entries] == [
        "AI/ML Intern",
        "Undergraduate Researcher, NSF BEELINE Project",
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
    "address": {"city": "Blacksburg", "state": "Virginia"},
    "education": {"school": "Virginia Tech"},
    "work_locations": {"BITS Pilani": "Dubai, United Arab Emirates", "emax": "Dubai, UAE"},
}


def _job(company, title="Intern"):
    return {"title": title, "company": company}


def test_location_comes_from_the_profile_map_by_company_name():
    jobs = with_locations([_job("BITS Pilani, Dubai Campus"), _job("EMAX (Landmark Group)")], PROFILE)

    assert [j["location"] for j in jobs] == ["Dubai, United Arab Emirates", "Dubai, UAE"]


def test_a_role_at_the_candidates_own_university_is_located_in_their_home_city():
    jobs = with_locations([_job("Virginia Tech", "Undergraduate Researcher, ChainSentinel")], PROFILE)

    assert jobs[0]["location"] == "Blacksburg, Virginia"


def test_unknown_employer_is_left_blank_rather_than_guessed():
    jobs = with_locations([_job("Marsh McLennan (Mercer Marsh Benefits)")], PROFILE)

    assert jobs[0]["location"] == ""


def _row(row_id, title="", company="", job=""):
    return {"id": row_id, "title": title, "company": company, "job": job}


ENTRY = {"title": "AI/ML Intern", "company": "Marsh McLennan"}


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
