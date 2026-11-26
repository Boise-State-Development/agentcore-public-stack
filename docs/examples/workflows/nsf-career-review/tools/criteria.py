"""The CAREER proposal audit: 15 sections, 114 criteria, as checklist field sets.

Run: python3 -I tools/criteria.py [out_dir]
Writes field-set.<section>.md for each section and audit-answer-key.md. With no
out_dir the field sets go to skill/references/ and the key to proposal/, where
this example keeps them.

Section names and per-section counts follow the student reviewer's graph. The
criteria themselves are ours, written so each one can be judged from a draft
proposal. Expected answers: P pass, F fail, C cant_tell, N not_applicable; a
slash means either is acceptable.
"""

import sys
from pathlib import Path

VERDICT = "pass, fail, cant_tell, not_applicable"

SECTIONS = [
    ("eligibility", "Eligibility", [
        ("pi_doctorate", "PI holds a doctorate", "The PI holds a doctoral degree in a field supported by NSF, stated in the biosketch or departmental letter.", "P"),
        ("pi_untenured", "PI is untenured", "The PI is untenured (for example an assistant professor), stated in the departmental letter or biosketch.", "P"),
        ("pi_tenure_track", "Tenure-track position", "The PI holds a tenure-track (or tenure-track-equivalent) position at the submitting institution.", "P"),
        ("eligible_institution", "Eligible institution", "The submitting organization is a US institution of higher education or other organization eligible for CAREER.", "P"),
        ("no_prior_career", "No prior CAREER award", "The proposal or letter states the PI has not previously received a CAREER award.", "P"),
        ("submission_count", "Fewer than three prior CAREER submissions", "The proposal states how many times the PI has submitted to CAREER before, and the number is fewer than three.", "P"),
        ("single_pi", "Single PI", "The proposal lists exactly one PI and no co-PIs, as CAREER requires.", "P"),
        ("letter_confirms_eligibility", "Letter confirms eligibility", "The departmental letter explicitly confirms the PI meets the CAREER eligibility requirements.", "P"),
    ]),
    ("submission-timing", "Submission Timing", [
        ("target_deadline", "Target deadline named", "The proposal names the CAREER deadline or submission window it targets.", "P"),
        ("submitted_on_time", "Submitted before the deadline", "The proposal was submitted on or before its deadline. A draft cannot show a submission time.", "C"),
        ("start_date_stated", "Start date stated", "A requested start date for the award is stated.", "P"),
        ("start_date_lead_time", "Start date leaves time for review", "The requested start date is at least six months after the target deadline.", "P"),
        ("duration_stated", "Duration stated", "The requested award duration is stated in months or years.", "P"),
        ("timeline_matches_duration", "Timeline matches the duration", "The project timeline covers the requested duration and schedules nothing after the award ends.", "P"),
        ("field_seasons_in_period", "Field seasons inside the award", "Every field season in the timeline falls inside the award period.", "P"),
        ("annual_milestones", "Annual milestones", "The timeline states at least one milestone for every project year.", "P"),
        ("new_or_resubmission", "New or resubmission stated", "The proposal says whether it is a new submission or a resubmission.", "P"),
        ("internal_routing", "Internal routing noted", "The proposal notes the date it was routed to the institution's sponsored programs office.", "P"),
    ]),
    ("formatting", "Formatting", [
        ("project_description_page_limit", "Project Description within 15 pages", "The Project Description is at most 15 pages long. Count its pages using the page footers.", "F"),
        ("project_summary_one_page", "Project Summary within one page", "The Project Summary is at most one page.", "P"),
        ("font_size", "Font at least 10 point", "Body text uses a font of at least 10 point; judge from the document's text size and any stated format.", "P/C"),
        ("margins", "Margins at least one inch", "Page margins are at least one inch on every side.", "P/C"),
        ("page_numbers", "Pages are numbered", "The pages of each section carry page numbers.", "P"),
        ("no_urls_in_description", "No URLs in the Project Description", "The Project Description contains no web addresses (URLs).", "P"),
        ("references_full_citations", "References have full citations", "Every entry in References Cited lists authors, title, venue and year.", "P"),
    ]),
    ("cover-sheet", "Cover Sheet", [
        ("title_prefix", "Title starts with CAREER:", "The proposal title begins with the prefix 'CAREER:'.", "P"),
        ("title_consistent", "Title consistent", "The title is the same everywhere it appears.", "P"),
        ("pi_name_consistent", "PI name consistent", "The PI's name is the same everywhere it appears.", "P"),
        ("institution_consistent", "Institution consistent", "The institution's name is the same everywhere it appears.", "P"),
        ("amount_matches_budget", "Requested amount matches the budget", "The total amount requested on the cover matches the cumulative budget total.", "P"),
        ("directorate_named", "Directorate and program named", "The cover names the NSF directorate and program the proposal is submitted to.", "P"),
    ]),
    ("summary-and-description", "Summary and Description", [
        ("summary_headings", "Summary has the three headings", "The Project Summary has separate headings for Overview, Intellectual Merit and Broader Impacts.", "P"),
        ("summary_third_person", "Summary in the third person", "The Project Summary is written in the third person (no 'we' or 'I').", "P"),
        ("description_broader_impacts_section", "Distinct Broader Impacts section", "The Project Description has a distinct section headed Broader Impacts.", "P"),
        ("integrated_education_plan", "Integrated education plan", "The Project Description has an education plan that is integrated with the research, not a separate add-on.", "P"),
        ("prior_support_section", "Results from Prior NSF Support", "The Project Description has a Results from Prior NSF Support section, or states the PI has had no NSF support in the past five years.", "P"),
        ("prior_support_merit_impacts", "Prior support states merit and impacts", "If prior NSF support is reported, it states that award's Intellectual Merit and Broader Impacts.", "P/N"),
        ("research_objectives", "Research objectives stated", "The Project Description states specific research objectives or hypotheses.", "P"),
        ("evaluation_plan", "Evaluation plan", "The Project Description describes how the education activities will be evaluated.", "P"),
        ("references_cited_in_text", "References cited in the text", "Works in References Cited are cited in the Project Description.", "P"),
    ]),
    ("budget", "Budget", [
        ("five_year_budget", "Five annual budgets", "The budget has a column or table for each of the five years and a cumulative total.", "P"),
        ("minimum_total", "Meets the CAREER minimum", "The cumulative total request is at least $400,000.", "P"),
        ("totals_add_up", "Totals add up", "Each year's line items add up to that year's total, and the years add up to the cumulative total.", "P"),
        ("senior_salary_two_months", "Senior salary at most two months, or justified", "Senior personnel salary is at most two months a year in total, or the budget justification explains why more is needed.", "F"),
        ("fringe_rates_stated", "Fringe rates stated", "The budget justification states the fringe benefit rates used.", "P"),
        ("fa_rate_stated", "F&A rate stated", "The budget justification states the indirect cost (F&A) rate and its basis.", "P"),
        ("no_fa_on_participant_support", "No F&A on participant support", "Indirect costs (F&A) are not charged on participant support costs.", "P"),
        ("participant_support_justified", "Participant support justified", "Participant support costs are itemized and explained in the justification.", "P"),
        ("equipment_threshold", "Equipment over $5,000 a unit", "Every item listed as equipment costs at least $5,000 a unit.", "P"),
        ("equipment_justified", "Equipment justified", "Each equipment item is explained in the justification.", "P"),
        ("travel_justified", "Travel justified", "Each trip in the budget is explained with its purpose and destination.", "P"),
        ("justification_covers_categories", "Justification covers every category", "The budget justification covers every budget category that has a nonzero amount.", "P"),
        ("justification_length", "Justification within five pages", "The budget justification is at most five pages.", "P"),
    ]),
    ("facilities-and-personnel", "Facilities and Personnel", [
        ("facilities_section", "Facilities document present", "A Facilities, Equipment and Other Resources document is included.", "P"),
        ("facilities_no_quantified_cost_share", "No quantified cost sharing in facilities", "The facilities document describes resources without dollar amounts that would read as voluntary cost sharing.", "P"),
        ("biosketch_present", "Biosketch present", "A biographical sketch is included for the PI.", "P"),
        ("biosketch_length", "Biosketch within three pages", "The biosketch is at most three pages.", "P"),
        ("biosketch_sections", "Biosketch sections", "The biosketch has Professional Preparation, Appointments and Products sections.", "P"),
        ("personnel_roles", "Every funded person has a role", "Every person paid by the budget has a role described in the Project Description.", "P"),
        ("students_mentoring", "Student mentoring described", "Supervision or mentoring of the funded graduate students is described.", "P"),
        ("postdoc_mentoring_plan", "Postdoc mentoring plan", "A Postdoctoral Researcher Mentoring Plan is included if the budget funds a postdoc.", "N"),
    ]),
    ("supplementary-docs", "Supplementary Docs", [
        ("departmental_letter", "Departmental letter included", "A letter from the department head is included, as CAREER requires.", "P"),
        ("dmp_present", "Data Management Plan included", "A Data Management and Sharing Plan is included and is at most two pages.", "P"),
        ("dmp_repository", "DMP names a repository", "The Data Management Plan names the repository or archive where the project's data will be deposited.", "F"),
        ("letters_of_collaboration", "Letters of collaboration in the standard form", "Any letter of collaboration states only the intent to collaborate, without endorsing the proposal.", "P/N"),
    ]),
    ("org-certifications", "Org Certifications", [
        ("safe_fieldwork_plan", "Safe and inclusive fieldwork plan", "For off-campus fieldwork, the proposal says the institution has a plan for safe and inclusive working environments.", "P"),
        ("drug_free_workplace", "Drug-free workplace", "The organization's standard certifications (such as a drug-free workplace) are made at submission. A draft cannot show them.", "C"),
        ("debarment", "Debarment and suspension", "The organization certifies it is not debarred or suspended. A draft cannot show this.", "C"),
        ("lobbying", "Lobbying", "The organization certifies that no federal funds pay for lobbying. A draft cannot show this.", "C"),
        ("rcr_training", "Responsible conduct of research training", "The proposal describes responsible conduct of research training for the students it funds.", "P"),
        ("conflict_of_interest", "Conflict of interest policy", "The proposal or letter refers to the institution's conflict of interest policy.", "P/C"),
        ("harassment_policy", "Harassment policy", "The proposal or letter refers to the institution's policy on harassment and its reporting.", "P/C"),
    ]),
    ("special-research", "Special Research", [
        ("human_subjects", "Human subjects", "If the project involves human subjects, IRB approval or exemption is stated.", "P/N"),
        ("vertebrate_animals", "Vertebrate animals", "If the project uses vertebrate animals, IACUC approval is stated.", "N"),
        ("field_permits", "Field permits", "Permits for work on public or tribal land (for example well installation) are named, with their status.", "P"),
        ("drone_compliance", "Drone flights compliant", "Any drone or UAS flights name the FAA authority they fly under (for example Part 107).", "P"),
        ("international_activities", "International activities", "Any international activities are identified with the countries involved.", "N"),
        ("environmental_impact", "Environmental impact", "The proposal says whether the fieldwork may affect the environment and how that is handled.", "P"),
    ]),
    ("special-proposal-types", "Special Proposal Types", [
        ("proposal_type_career", "Submitted as CAREER", "The proposal identifies itself as a Faculty Early Career Development (CAREER) proposal.", "P"),
        ("duration_five_years", "Five-year duration", "The requested duration is five years.", "P"),
        ("not_collaborative", "Not a collaborative proposal", "The proposal is not split across institutions as a collaborative proposal.", "P"),
        ("not_rapid_eager", "Not RAPID or EAGER", "The proposal does not also present itself as a RAPID or EAGER.", "P"),
        ("career_development_plan", "Career development plan", "The proposal describes the PI's career goals and how the project builds a foundation for them.", "P"),
        ("research_education_integration", "Research and education integration", "The proposal explains how its research and education activities inform each other.", "P"),
    ]),
    ("post-award", "Post-Award", [
        ("data_sharing_timeline", "Data sharing timeline", "The proposal says when the data will be made available (for example within a year of collection).", "P"),
        ("equipment_ownership", "Equipment after the award", "The proposal says what happens to the equipment it buys after the award ends.", "P"),
        ("site_restoration", "Sites restored", "The proposal says how field sites (wells, instruments) will be removed or restored at the end.", "P"),
        ("no_cost_extension_not_assumed", "No extension assumed", "The timeline does not depend on a no-cost extension.", "P"),
        ("subawards_identified", "Subawards identified", "Any subaward is identified with its organization and budget.", "N"),
        ("partner_commitments", "Partner commitments", "Each external partner's commitment is stated and backed by a letter.", "P"),
        ("rebudgeting_flagged", "Major rebudgeting not planned", "The plan does not depend on moving funds between budget categories later.", "P"),
        ("key_personnel_changes", "Continuity of key personnel", "The proposal says how the work continues if a key student leaves.", "P/F"),
    ]),
    ("reporting", "Reporting", [
        ("measurable_outcomes", "Measurable outcomes", "The proposal names measurable outcomes that annual reports can track.", "P"),
        ("education_metrics", "Education metrics", "The education plan names how many students or participants it will reach.", "P"),
        ("assessment_instrument", "Assessment instrument", "The evaluation plan names its instrument or method (for example a survey before and after).", "P"),
        ("external_evaluator", "Evaluator named", "The person or office that evaluates the education activities is named.", "P"),
        ("annual_report_content", "Annual reporting planned", "The proposal says what it will report each year.", "P/C"),
        ("dissemination_plan", "Dissemination plan", "The proposal names where results will be published or presented.", "P"),
        ("public_outcomes", "Public outcomes", "The proposal plans a public-facing outcome or project page.", "P"),
        ("participant_tracking", "Participant tracking", "The proposal says how it will track participants in its education activities.", "P"),
    ]),
    ("financial-management", "Financial Management", [
        ("pre_award_costs", "Pre-award costs", "Any pre-award costs fall within 90 days before the start date.", "N"),
        ("cost_sharing_none", "No voluntary cost sharing", "The proposal shows no voluntary committed cost sharing.", "P"),
        ("rates_basis", "Rates follow the negotiated agreement", "The F&A rate is said to follow the institution's negotiated rate agreement.", "P"),
        ("escalation_stated", "Salary escalation stated", "The budget justification states the annual salary escalation used.", "P"),
        ("tuition_handling", "Tuition handled correctly", "Graduate student tuition is budgeted and excluded from the F&A base, or the proposal says none is charged.", "P"),
        ("unallowable_costs", "No unallowable costs", "The budget contains no unallowable costs such as entertainment, alcohol or general office supplies.", "P"),
        ("program_income", "Program income", "Any program income the project would earn is identified.", "N/P"),
    ]),
    ("publication-and-ip", "Publication and IP", [
        ("open_access", "Open access", "Publications will be made openly available (for example through NSF-PAR or open access journals).", "P"),
        ("data_formats", "Data formats", "The Data Management Plan names the file formats the data will use.", "P"),
        ("metadata_standard", "Metadata standard", "The Data Management Plan names a metadata standard.", "P"),
        ("code_sharing", "Code sharing", "The proposal says how the project's code or models will be shared.", "P"),
        ("ip_rights", "Intellectual property", "The proposal addresses who owns any inventions or intellectual property.", "P/N"),
        ("data_license", "Data license", "The proposal names a license for the data it shares.", "P"),
        ("acknowledgement", "NSF acknowledgement", "The proposal says publications will acknowledge NSF support.", "P"),
    ]),
]

EXPECTED_COUNTS = {
    "eligibility": 8, "submission-timing": 10, "formatting": 7, "cover-sheet": 6,
    "summary-and-description": 9, "budget": 13, "facilities-and-personnel": 8,
    "supplementary-docs": 4, "org-certifications": 7, "special-research": 6,
    "special-proposal-types": 6, "post-award": 8, "reporting": 8,
    "financial-management": 7, "publication-and-ip": 7,
}

CODE = {"P": "pass", "F": "fail", "C": "cant_tell", "N": "not_applicable"}


def field_set_markdown(title: str, fields) -> str:
    lines = [
        "---",
        f"title: {title}",
        "version: 1",
        "---",
        "",
        f"CAREER proposal audit: {title}. Each criterion is pass, fail, cant_tell or not_applicable.",
        "",
        "| name | title | type | find | enum |",
        "|------|-------|------|------|------|",
    ]
    for name, field_title, find, _expected in fields:
        lines.append(f"| {name} | {field_title} | enum | {find} | {VERDICT} |")
    return "\n".join(lines) + "\n"


def main(out: Path, key_dir: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    key = ["# CAREER audit: expected answers", "",
           "P pass · F fail · C cant_tell · N not_applicable · a slash accepts either.", "",
           "| Section | Criterion | Expected | Plant |", "|---|---|---|---|"]
    plants = {"project_description_page_limit": "A", "senior_salary_two_months": "B", "dmp_repository": "G"}
    for name, title, fields in SECTIONS:
        assert len(fields) == EXPECTED_COUNTS[name], (name, len(fields))
        assert len({f[0] for f in fields}) == len(fields), name
        total += len(fields)
        (out / f"field-set.{name}.md").write_text(field_set_markdown(title, fields))
        for field_name, field_title, _find, expected in fields:
            label = " or ".join(CODE[c] for c in expected.split("/"))
            key.append(f"| {title} | `{field_name}` {field_title} | {label} | {plants.get(field_name, '')} |")
    assert total == 114, total
    (key_dir / "audit-answer-key.md").write_text("\n".join(key) + "\n")
    print(f"wrote {len(SECTIONS)} field sets, {total} criteria to {out}, and the key to {key_dir}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(Path(sys.argv[1]), Path(sys.argv[1]))
    else:
        root = Path(__file__).resolve().parent.parent
        main(root / "skill" / "references", root / "proposal")
