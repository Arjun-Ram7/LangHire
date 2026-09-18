from backend.core.workday_experience import (
    degree_option_rank,
    education_plan,
    parse_experience_text,
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
