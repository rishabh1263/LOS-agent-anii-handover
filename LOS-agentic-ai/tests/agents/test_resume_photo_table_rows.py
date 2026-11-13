"""
PHOTOGRAPHED TABLE RESUMES (2026-10-19): a photo / screenshot of a table CV loses its cell gaps -- the row's dates,
employer and designation still come out, a key-value row still gives its value, and a state is never a language.
Made-up people only.
"""

from __future__ import annotations

import pytest

from app.agents.resume import parse as P

ROWS = [
    "CURRICULUM VITAE",
    "Name: Kiran Yadav",
    "Email: kiran.yadav31@example.com",
    "PERSONAL DETAILS",
    "Languages Known English, Hindi",
    "Address   Flat 27, MG Road, Coimbatore, Tamil Nadu - 641559",
    "EMPLOYMENT HISTORY",
    "Organisation   Designation   From   To",
    "Mahindra Finance Sales Executive Feb 2019 Feb 2021",
    "Tech Mahindra   Credit Manager Sep 2017 Jan 2019",
    "ACADEMIC QUALIFICATIONS",
    "B.Sc   University of Mumbai   2015   65%",
]


def test_a_row_without_gaps_gives_dates_employer_and_designation():
    jobs = P.parse(ROWS).public()["experience"]
    assert [(j["employer"], j["designation"], j["start"], j["end"]) for j in jobs] == [
        ("Mahindra Finance", "Sales Executive", "2019-02", "2021-02"),
        ("Tech Mahindra", "Credit Manager", "2017-09", "2019-01")]


def test_a_languages_row_without_a_separator_is_read_and_a_state_is_not_a_language():
    assert P.parse(ROWS).public()["languages"] == ["English", "Hindi"]


def test_tamil_nadu_alone_is_never_tamil():
    lines = ["RESUME", "Name: A Person", "LANGUAGES", "Address: Chennai, Tamil Nadu"]
    assert "Tamil" not in P.parse(lines).public()["languages"]


@pytest.mark.parametrize("part, parts", [
    ("Mahindra Finance Sales Executive", ["Mahindra Finance", "Sales Executive"]),
    ("Mahindra Finance Customer Relationship Officer", ["Mahindra Finance", "Customer Relationship Officer"]),
    ("Capgemini India Pvt Ltd Customer Service Executive", ["Capgemini India Pvt Ltd", "Customer Service Executive"]),
    ("HDFC Bank Relationship Manager", ["HDFC Bank", "Relationship Manager"]),
    ("Tech Mahindra", ["Tech Mahindra"]),
    ("Tata Capital Housing Finance", ["Tata Capital Housing Finance"]),
    ("State Bank of India", ["State Bank of India"]),
    ("Branch Manager", ["Branch Manager"]),
])
def test_an_employer_and_a_designation_glued_together_are_split_only_at_a_company_marker(part, parts):
    assert P._split_glued(part) == parts


@pytest.mark.parametrize("line", ["B.SC   UNIVERSITY OF MUMBAI   2015 2017", "HSC 2012 2014", "2019 2021"])
def test_bare_years_side_by_side_are_never_a_job_range(line):
    assert P.RANGE_TAIL.search(line) is None


@pytest.mark.parametrize("line", ["SALES EXECUTIVE FEB 2019 FEB 2021", "OFFICER AUG 2013 PRESENT",
                                  "ANALYST SEPT 2020 TILL DATE"])
def test_two_month_points_at_the_end_of_a_row_are_its_range(line):
    assert P.RANGE_TAIL.search(line) is not None


# ---- a modern one-page PDF resume (2026-10-19, found on a real resume; made-up person here) ----------------------
def test_a_role_before_its_company_is_never_split_into_two_words():
    lines = ["Asha Verma", "Software Engineer | Aspiring Data Engineer", "Experience",
             "Software Engineer | Acme Finance Ltd. | Mar 2024 - Present", "• Built APIs.", "Education"]
    job = P.parse(lines).public()["experience"][0]
    assert (job["employer"], job["designation"]) == ("Acme Finance Ltd", "Software Engineer")


@pytest.mark.parametrize("part", ["Software Engineer", "Bank Manager", "Finance Executive"])
def test_a_role_made_only_of_marker_and_role_words_stays_whole(part):
    assert P._split_glued(part) == [part]


def test_a_graduation_label_gives_the_year_to_the_degree_above_and_is_not_a_second_degree():
    lines = ["Asha Verma", "a.verma@example.com", "Education", "B.Sc. Information Technology",
             "Example College of Science | CGPA: 9.1 | Graduation: 2024"]
    edu = P.parse(lines).public()["education"]
    assert [(e["level"], e["year"]) for e in edu] == [("graduation", 2024)]


def test_grouped_skills_keep_the_skill_not_its_group_label():
    lines = ["Asha Verma", "a.verma@example.com", "Technical Skills", "Languages: C#, Python",
             "Backend: .NET 8, FastAPI", "AI/ML: NumPy, Pandas"]
    assert P.parse(lines).public()["skills"] == ["C#", "Python", ".NET 8", "FastAPI", "NumPy", "Pandas"]


def test_an_email_and_linkedin_behind_pdf_links_are_read():
    import pymupdf

    from app.agents.resume import reader as R

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Asha Verma")
    page.insert_text((72, 96), "Email | LinkedIn | Mumbai, India")
    page.insert_text((72, 130), "Experience")
    page.insert_text((72, 150), "Software Engineer | Acme Finance Ltd. | Mar 2024 - Present")
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 86, 100, 100), "uri": "mailto:a.verma@example.com"})
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(110, 86, 160, 100),
                      "uri": "https://www.linkedin.com/in/asha-verma-example/"})
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(170, 86, 200, 100), "uri": "https://evil.example/x"})
    text = R.read_pdf(doc.tobytes())
    out = P.parse(text.lines).public()
    assert out["email"] == "a.verma@example.com" and "linkedin.com/in/asha-verma-example" in (out["linkedin"] or "")
    assert "evil.example" not in "\n".join(text.lines)          # only e-mail and LinkedIn targets are read


# ---- an employer header above its roles; a section after experience (2026-10-19, a real resume; made-up here) -----
US_STYLE = ["SAM RIVERA", "(555) 010-2000 | sam.rivera@example.com", "EXPERIENCE",
            "Northwind Group (Contoso Affiliate)   Tempe, AZ",
            "Operations Manager   2019 - 2020", "Operations Associate   2018 - 2020",
            "•   Ran the acquisitions process from sourcing to closing", "Fabrikam Capital   Pune",
            "Credit Analyst   2016 - 2018", "•   Built credit models",
            "ADDITIONAL INFORMATION", "•   Volunteer teacher in Nagpur (2010-2012); fluent in Marathi"]


def test_roles_under_an_employer_header_take_its_name():
    jobs = P.parse(US_STYLE).public()["experience"]
    assert [(j["employer"], j["designation"]) for j in jobs] == [
        ("Northwind Group (Contoso Affiliate)", "Operations Manager"),
        ("Northwind Group (Contoso Affiliate)", "Operations Associate"),
        ("Fabrikam Capital", "Credit Analyst")]


def test_a_period_in_a_later_section_is_never_a_job_or_a_gap():
    out = P.parse(US_STYLE).public()
    assert len(out["experience"]) == 3 and out["employment_gaps"] == []


@pytest.mark.parametrize("line", ["Sales Manager   Mumbai", "•   Led a team   Pune, MH", "Axis Bank   2019 - 2021"])
def test_a_role_a_bullet_or_a_dated_line_is_never_an_employer_header(line):
    assert P._employer_header(line) is None


# ---- institution above its degree; a one-month internship; bulleted skill groups (2026-10-19, real; made-up here) --
EDU_ABOVE = ["Ravi Kale", "ravi.kale@example.com", "EXPERIENCE",
             "Software Developer Intern | Contoso Pvt. Ltd. | Remote", "Rust, Git, Testing| January2026",
             "● Built automation scripts", "EDUCATION",
             "● Example College of Arts, Science and Commerce   (August 2023 - April 2026)",
             "Bachelor of Science in Information Technology   (CGPA: 9.80/10)",
             "● Example Junior College   (June 2022 - April 2023)", "Higher Secondary Certificate   (Percentage: 72%)",
             "● Example High School   (June 2020- April 2021)", "Secondary School Certificate   (Percentage:86%)",
             "TECHNICAL SKILLS", "● Programming: JavaScript, C++", "● Tools:Git, Postman"]


def test_dates_on_the_institution_line_above_belong_to_the_degree_below():
    edu = P.parse(EDU_ABOVE).public()["education"]
    assert [(e["level"], e["year"]) for e in edu] == [("graduation", 2026), ("12th", 2023), ("10th", 2021)]


def test_the_older_layout_degree_above_its_dated_college_line_still_reads():
    lines = ["A Person", "a@example.com", "EDUCATION", "B.Com", "Example College, Pune 2015 - 2018", "HSC",
             "Example Junior College 2013 - 2015"]
    assert [(e["level"], e["year"]) for e in P.parse(lines).public()["education"]] == [("graduation", 2018),
                                                                                        ("12th", 2015)]


def test_a_one_month_internship_with_role_and_company_is_a_job_that_has_ended():
    job = P.parse(EDU_ABOVE).public()["experience"]
    assert [(j["employer"], j["designation"], j["start"], j["end"], j["internship"]) for j in job] == [
        ("Contoso Pvt. Ltd", "Software Developer Intern", "2026-01", "2026-01", True)]


@pytest.mark.parametrize("lines", [
    ["EXPERIENCE", "● Joined the team in January 2024 and shipped two releases"],          # a bullet
    ["EXPERIENCE", "Volunteer work, March 2022"],                                            # no role and company
    ["EXPERIENCE", "Contoso Pvt. Ltd.", "Summer 2018"],                                      # a season, not a month
])
def test_a_lone_month_without_a_named_role_and_company_is_never_a_job(lines):
    assert P.parse(["A Person", "a@example.com", *lines]).public()["experience"] == []


def test_bulleted_skill_groups_and_a_label_without_a_space_keep_only_the_skills():
    assert P.parse(EDU_ABOVE).public()["skills"] == ["JavaScript", "C++", "Git", "Postman"]


def test_the_resume_timeout_comes_from_config_and_the_environment(monkeypatch):
    from app.agents.resume import service

    monkeypatch.delenv("RESUME_TIMEOUT_SECONDS", raising=False)
    assert service.timeout_seconds() == float(P.cfg()["limits"]["timeout_seconds"])
    monkeypatch.setenv("RESUME_TIMEOUT_SECONDS", "35")
    assert service.timeout_seconds() == 35.0
    monkeypatch.setenv("RESUME_TIMEOUT_SECONDS", "not a number")
    assert service.timeout_seconds() == float(service.TIMEOUT_SECONDS)


def test_a_range_whose_end_ocr_garbled_is_never_a_one_month_job():
    lines = ["A Person", "a@example.com", "PROFESSIONAL JOURNEY", "Aditya Birla Capital | Underwriter | Aug'12 - Jul*14"]
    assert all(j["end"] != j["start"] for j in P.parse(lines).public()["experience"])
