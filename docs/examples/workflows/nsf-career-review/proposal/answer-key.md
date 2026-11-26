# Answer key: `marsh_career_draft3.pdf`

**CAREER: Snow Drought and Groundwater Recharge in Semi-Arid Headwaters**. Dr. Jordan Marsh, Owyhee State University. Everything in the proposal is fictional: the PI, the institution, the national forest and catchments, the partner school, the letter writers, and every reference (authors, titles and journals).

32 PDF pages. Every page carries a footer naming its section and its page within that section.

| PDF pages | Section | Footer |
|---|---|---|
| 1 | Cover Page | page 1 of 1 |
| 2 | Project Summary | page 1 of 1 |
| 3–18 | Project Description | page 1–16 of 16 |
| 19–20 | References Cited | page 1–2 of 2 |
| 21–22 | Biographical Sketch | page 1–2 of 2 |
| 23 | Budget | page 1 of 1 |
| 24–27 | Budget Justification | page 1–4 of 4 |
| 28 | Facilities, Equipment and Other Resources | page 1 of 1 |
| 29–30 | Data Management and Sharing Plan | page 1–2 of 2 |
| 31 | Departmental Letter | page 1 of 1 |
| 32 | Letter of Collaboration | page 1 of 1 |

Project Description page *k* is PDF page *k* + 2.

## 1. The seven planted problems

| Plant | Problem | Where (PDF page) | Exact text | Expected to catch it |
|---|---|---|---|---|
| **A** | Project Description runs 16 pages against a 15-page limit | PDF pp. 3–18; footers | Every Project Description footer reads `Project Description — page N of 16`, ending with `Project Description — page 16 of 16` on PDF p. 18. The cover's contents table (p. 1) also lists "Project Description 16 pages". | The audit (`project_description_page_limit` → fail) |
| **B** | Senior personnel salary is 3.0 months a year with no reason given for exceeding two months | Budget, PDF p. 23; Budget Justification, PDF p. 24 | Budget row: "A. Senior personnel salary: 3.0 months per year — Dr. Jordan Marsh, PI (summer)" (28,500 / 29,355 / 30,236 / 31,143 / 32,077; cumulative 151,311). Justification §A: "Senior personnel salary: 3.0 months per year. Dr. Jordan Marsh (PI) requests 3.0 months of summer salary in each of Years 1–5, at a nine-month academic-year base salary of $85,500 ($9,500 a month) in Year 1 … No academic-year salary is requested." No reason for the third month appears anywhere. | The audit (`senior_salary_two_months` → fail) |
| **C** | Project Summary opens with methods and instruments; why it matters and who is affected arrives only in the last lines | Project Summary, PDF p. 2 | Opens (Overview): "This project will deploy paired snow pillows and shallow groundwater wells across three semi-arid headwater catchments on the Juniper Peaks National Forest in southwestern Idaho: one snow pillow station and six monitoring wells in each catchment, logging snow water equivalent and water-table depth every 15 minutes for five winters." The stakes come only in the final sentence of Broader Impacts: "The results matter most downstream: ranchers, irrigators and the town water systems below these headwaters depend on late-summer streamflow and shallow wells that are recharged by snowmelt, and annual briefings will give them the first direct measurements of how a snow-drought winter carries through to the water they draw in August." The summary is in the third person throughout. | Generalist; Emotional Outsider |
| **D** | "nival-pluvial transition zone" is used four times before it is defined on Project Description page 4 | PD pp. 1–3 (PDF pp. 3–5); definition PD p. 4 (PDF p. 6) | Uses before the definition: (1) PDF p. 3: "they are expected to be most consequential in the nival-pluvial transition zone." (2) PDF p. 3: "Streamflow records already show that catchments in the nival-pluvial transition zone respond to snow drought more strongly than higher catchments (Marrero-Quist and Haldane-Obi, 2023)." (3) PDF p. 4: "Studies in the nival-pluvial transition zone have so far relied mostly on streamflow and on the snowpack itself." (4) PDF p. 5: "Snow drought is already frequent in the nival-pluvial transition zone, but whether its recharge consequences are proportional to the loss of snow … is an open question …" Definition, PDF p. 6 (§3 Study Area): "Here we define the term used throughout this proposal: by the nival-pluvial transition zone we mean the elevation band in which winter precipitation shifts between snow (nival) and rain (pluvial) from storm to storm and from year to year …". The term does not appear in the Project Summary or on the cover. | Generalist |
| **E** | 18 wells (six per catchment), n = 18, with no detectable effect size, pilot variance or power analysis | PD p. 6 (PDF p. 8), §4.2 "Testing H1" | "The primary test of H1 uses all 18 wells (six per catchment, n = 18) in a linear mixed-effects model of seasonal recharge efficiency, with winter class (normal, dry or warm snow drought) and seasonal mean melt rate as fixed effects and well nested within catchment as random effects." No power, effect size or variance appears anywhere; §2.4 (PDF p. 5) states "No groundwater measurements have yet been made." | Colleague |
| **F** | Drone LiDAR is budgeted and justified in Year 1 ($48,000 equipment), but the first LiDAR flights are scheduled for Year 2 | Budget PDF p. 23; Justification PDF p. 25; PD §4.3 PDF p. 9; timeline PDF p. 18 | Budget row "D. Equipment: drone LiDAR system" 48,000 / 0 / 0 / 0 / 0. Justification §D: "Drone LiDAR system, Year 1: $48,000. … It is required for the snow-depth surveys of Objective 2 …". PD §4.3: "The first drone LiDAR flights will be flown in Year 2: a snow-off reference survey of all three blocks in September 2029, followed by four snow-on surveys of each block in winter 2029–30". Timeline Year 2 row: "First drone LiDAR flights: snow-off survey (Sep 2029) and four snow-on surveys per block (winter 2029–30)"; the Year 1 row has no LiDAR. Year 1 is Sep 2028–Aug 2029. | Fiscal Outsider |
| **G** | The Data Management and Sharing Plan names no repository | DMP, PDF p. 30 (§5 and §7) | "All observational data and derived products will be deposited in a public repository, where they will be freely available without registration and assigned a persistent identifier …" and "Deposited data will be preserved by the public repository for at least ten years after the end of the award." Formats (CSV, NetCDF-4/CF-1.10, LAZ; p. 29), metadata standard (ISO 19115-1; p. 29), license (CC BY 4.0; p. 30) and timeline (within 12 months; p. 30) are all present. Code: "released under the MIT license with each publication" (p. 30). No repository or archive (GitHub, Zenodo, HydroShare, CUAHSI or similar) is named anywhere in the proposal. The only named system is NSF-PAR, for publications, in the Project Description (PDF p. 15). | The audit (`dmp_repository` → fail) |

No other problems were planted. The budget arithmetic is exact (Section 3), and the remaining facts were cross-checked for consistency: counts of wells, loggers and stations; survey counts (3 snow-off, 48 snow-on); participant numbers; travel by year; and dates (deadline, routing, letters, IRB, permits, award period, decommissioning). One judgement call outside the plants: the $994,162 request is on the high side for a CAREER award, but nothing in the audit tests for that.

## 2. All 114 criteria

P pass · F fail · C cant_tell · N not_applicable. Where the criteria file allows two answers, the one this draft supports is given first.

| # | Section | Criterion | Expected | PDF page: evidence, or why N/C |
|---|---|---|---|---|
| 1 | Eligibility | `pi_doctorate` PI holds a doctorate | P | p. 1 "PhD, Hydrologic Sciences, 2021"; p. 21 Professional Preparation; p. 31 letter |
| 2 | Eligibility | `pi_untenured` PI is untenured | P | p. 1 "Assistant Professor (untenured, tenure-track)"; p. 31 "Dr. Marsh is untenured" |
| 3 | Eligibility | `pi_tenure_track` Tenure-track position | P | p. 1; p. 21 "Assistant Professor (tenure-track)"; p. 31 "tenure-track appointment … since August 2023" |
| 4 | Eligibility | `eligible_institution` Eligible institution | P | p. 1 "a U.S. public institution of higher education that awards doctoral degrees" |
| 5 | Eligibility | `no_prior_career` No prior CAREER award | P | p. 31 "has not previously received a CAREER award" |
| 6 | Eligibility | `submission_count` Fewer than three prior submissions | P | p. 1 "This is the PI's first CAREER submission; the PI has made no prior CAREER submissions"; p. 31 "submitting … for the first time" |
| 7 | Eligibility | `single_pi` Single PI | P | p. 1 "Sole PI; there are no co-PIs" |
| 8 | Eligibility | `letter_confirms_eligibility` Letter confirms eligibility | P | p. 31 "I confirm that Dr. Marsh meets all of the eligibility requirements of the … CAREER Program" |
| 9 | Submission Timing | `target_deadline` Target deadline named | P | p. 1 "CAREER deadline, Monday, July 26, 2027" |
| 10 | Submission Timing | `submitted_on_time` Submitted before the deadline | C | A draft shows no submission time |
| 11 | Submission Timing | `start_date_stated` Start date stated | P | p. 1 "September 1, 2028" |
| 12 | Submission Timing | `start_date_lead_time` Start ≥ 6 months after deadline | P | p. 1: July 26, 2027 → September 1, 2028 (about 13 months) |
| 13 | Submission Timing | `duration_stated` Duration stated | P | p. 1 "60 months (five years): September 1, 2028 – August 31, 2033" |
| 14 | Submission Timing | `timeline_matches_duration` Timeline matches duration | P | p. 18 timeline Years 1–5, Sep 2028–Aug 2033; "all work ends by August 31, 2033" |
| 15 | Submission Timing | `field_seasons_in_period` Field seasons inside the award | P | p. 18: installation Sep–Oct 2028; snow seasons winters 2028–29 to 2032–33; decommissioning June 15–July 31, 2033 |
| 16 | Submission Timing | `annual_milestones` Annual milestones | P | p. 18 Milestones column, one or more per year |
| 17 | Submission Timing | `new_or_resubmission` New or resubmission stated | P | p. 1 "New submission (not a resubmission)" |
| 18 | Submission Timing | `internal_routing` Internal routing noted | P | p. 1 "routed to the Owyhee State University Office of Sponsored Programs on June 28, 2027" |
| 19 | Formatting | `project_description_page_limit` PD within 15 pages | **F** (plant A) | pp. 3–18; footers "page N of 16" |
| 20 | Formatting | `project_summary_one_page` Summary within one page | P | p. 2, "Project Summary — page 1 of 1" |
| 21 | Formatting | `font_size` Font ≥ 10 pt | P or C | 11 pt Helvetica body and 10 pt tables (not stated in the text; visible on the page) |
| 22 | Formatting | `margins` Margins ≥ 1 inch | P or C | 1-inch margins on all sides; footer sits inside the text block (not stated in the text) |
| 23 | Formatting | `page_numbers` Pages numbered | P | Every page's footer, e.g. "Budget Justification — page 2 of 4" |
| 24 | Formatting | `no_urls_in_description` No URLs in PD | P | pp. 3–18 contain no web address (none anywhere in the document) |
| 25 | Formatting | `references_full_citations` Full citations | P | pp. 19–20: 20 entries, each with authors, year, title, journal, volume and pages |
| 26 | Cover Sheet | `title_prefix` Title starts "CAREER:" | P | p. 1 |
| 27 | Cover Sheet | `title_consistent` Title consistent | P | Identical on pp. 1, 2, 31, 32 |
| 28 | Cover Sheet | `pi_name_consistent` PI name consistent | P | "Jordan Marsh" / "Dr. Jordan Marsh" / "Dr. Marsh" throughout; "Marsh, J." only in citation-style product entries (pp. 21–22) |
| 29 | Cover Sheet | `institution_consistent` Institution consistent | P | "Owyhee State University" everywhere; no abbreviation is used |
| 30 | Cover Sheet | `amount_matches_budget` Amount matches budget | P | p. 1 $994,162 = p. 23 row J cumulative 994,162 = p. 24 justification total |
| 31 | Cover Sheet | `directorate_named` Directorate and program named | P | p. 1 "Directorate for Geosciences (GEO), Division of Earth Sciences (EAR), Hydrologic Sciences program" |
| 32 | Summary and Description | `summary_headings` Three summary headings | P | p. 2 Overview / Intellectual Merit / Broader Impacts |
| 33 | Summary and Description | `summary_third_person` Summary in third person | P | p. 2, no "we/I/our"; "This project will…", "the PI" |
| 34 | Summary and Description | `description_broader_impacts_section` Distinct BI section | P | p. 14 "7. Broader Impacts" (continues p. 15, §7.1–7.2) |
| 35 | Summary and Description | `integrated_education_plan` Integrated education plan | P | p. 12 §5 "built into the research rather than added to it"; p. 14 §6 |
| 36 | Summary and Description | `prior_support_section` Results from Prior NSF Support | P | p. 18 §12 "The PI has received no NSF support in the past five years." |
| 37 | Summary and Description | `prior_support_merit_impacts` Prior support merit/impacts | N or P | No prior support is reported, so there is nothing to describe |
| 38 | Summary and Description | `research_objectives` Objectives stated | P | p. 3 Objectives O1–O3 with hypotheses H1–H3 |
| 39 | Summary and Description | `evaluation_plan` Evaluation plan | P | p. 13 §5.2 Evaluation plan |
| 40 | Summary and Description | `references_cited_in_text` References cited in text | P | All 20 references are cited author-year in pp. 3–14 (e.g. "(Abernathy-Cole and Pellerin, 2020)", p. 3) |
| 41 | Budget | `five_year_budget` Five annual budgets | P | p. 23 Year 1–5 columns plus Cumulative |
| 42 | Budget | `minimum_total` ≥ $400,000 | P | p. 23 cumulative $994,162 |
| 43 | Budget | `totals_add_up` Totals add up | P | p. 23; see Section 3 below |
| 44 | Budget | `senior_salary_two_months` Senior salary ≤ 2 months or justified | **F** (plant B) | p. 23 "Senior personnel salary: 3.0 months per year"; p. 24 justification gives no reason |
| 45 | Budget | `fringe_rates_stated` Fringe rates stated | P | p. 24 §C: 28.0% faculty summer, 9.0% graduate, 2.0% undergraduate |
| 46 | Budget | `fa_rate_stated` F&A rate and basis | P | p. 27 §I "44.5% of modified total direct costs (MTDC)" |
| 47 | Budget | `no_fa_on_participant_support` No F&A on participant support | P | p. 26 "No indirect costs are charged on participant support costs"; p. 23 note: MTDC excludes participant support |
| 48 | Budget | `participant_support_justified` Participant support itemized | P | p. 26 §F: stipends, travel, subsistence and other, each itemized |
| 49 | Budget | `equipment_threshold` Equipment ≥ $5,000 a unit | P | p. 25: $5,000 threshold stated; LiDAR $48,000; snow pillow station $7,400 a unit; loggers ($780) are under supplies |
| 50 | Budget | `equipment_justified` Equipment justified | P | p. 25 §D, both items |
| 51 | Budget | `travel_justified` Travel justified | P | p. 25 §E: field travel (destination: the catchments on the Owyhee Ranger District), AGU (San Francisco rates), Western Snow Conference (Reno rates), each with its purpose |
| 52 | Budget | `justification_covers_categories` Every category justified | P | pp. 24–27 cover A, B, C, D, E, F, G1, G2, G6 and I; G3–G5 marked as none (p. 27) |
| 53 | Budget | `justification_length` Justification ≤ 5 pages | P | 4 pages (pp. 24–27) |
| 54 | Facilities and Personnel | `facilities_section` Facilities document | P | p. 28 |
| 55 | Facilities and Personnel | `facilities_no_quantified_cost_share` No quantified cost share | P | p. 28 has no dollar amounts |
| 56 | Facilities and Personnel | `biosketch_present` Biosketch present | P | pp. 21–22 |
| 57 | Facilities and Personnel | `biosketch_length` Biosketch ≤ 3 pages | P | 2 pages |
| 58 | Facilities and Personnel | `biosketch_sections` Biosketch sections | P | p. 21 Professional Preparation, Appointments and Positions, Products; p. 22 Synergistic Activities |
| 59 | Facilities and Personnel | `personnel_roles` Every funded person has a role | P | p. 16 §9 "Personnel and roles": the PI, the doctoral student, two undergraduates a year (and the evaluation office and the teachers) |
| 60 | Facilities and Personnel | `students_mentoring` Student mentoring | P | p. 16 "Mentoring." individual development plan, weekly meetings, committee |
| 61 | Facilities and Personnel | `postdoc_mentoring_plan` Postdoc mentoring plan | N | No postdoc is budgeted or mentioned |
| 62 | Supplementary Docs | `departmental_letter` Departmental letter | P | p. 31 from the Head of the Department of Geosciences |
| 63 | Supplementary Docs | `dmp_present` DMP ≤ 2 pages | P | pp. 29–30 |
| 64 | Supplementary Docs | `dmp_repository` DMP names a repository | **F** (plant G) | p. 30 "a public repository", with no name |
| 65 | Supplementary Docs | `letters_of_collaboration` Standard-form letters | P or N | p. 32: the standard NSF intent-to-collaborate sentence only, with no endorsement |
| 66 | Org Certifications | `safe_fieldwork_plan` Safe and inclusive fieldwork plan | P | p. 16 "Safe and inclusive fieldwork"; p. 31 letter |
| 67 | Org Certifications | `drug_free_workplace` Drug-free workplace | C | Made at submission; a draft cannot show it |
| 68 | Org Certifications | `debarment` Debarment and suspension | C | Made at submission |
| 69 | Org Certifications | `lobbying` Lobbying | C | Made at submission |
| 70 | Org Certifications | `rcr_training` RCR training | P | p. 16 "Responsible conduct of research": all paid students, within 60 days |
| 71 | Org Certifications | `conflict_of_interest` COI policy | P or C | p. 31 "maintains and enforces a policy on financial conflicts of interest" |
| 72 | Org Certifications | `harassment_policy` Harassment policy | P or C | p. 31 "policy prohibiting sexual and other forms of harassment, with procedures for reporting" |
| 73 | Special Research | `human_subjects` IRB approval/exemption | P or N | p. 13 "exempt under 45 CFR 46.104(d)(1) … (protocol 2027-0412)", May 20, 2027 |
| 74 | Special Research | `vertebrate_animals` IACUC | N | No vertebrate animals are used (p. 17 only keeps flights away from sage-grouse leks) |
| 75 | Special Research | `field_permits` Field permits with status | P | p. 17: Forest Service special use authorization (submitted May 3, 2027, pending); state monitoring-well permits (to be filed July 2028) |
| 76 | Special Research | `drone_compliance` Drone FAA authority | P | p. 17 "14 CFR Part 107"; p. 9 PI holds a Part 107 certificate |
| 77 | Special Research | `international_activities` International activities | N | None; p. 25 "No foreign travel is requested" |
| 78 | Special Research | `environmental_impact` Environmental impact | P | p. 17 "Environmental impact." (NEPA, categorical exclusion expected, heritage review) |
| 79 | Special Proposal Types | `proposal_type_career` Submitted as CAREER | P | p. 1 "Proposal type: Faculty Early Career Development Program (CAREER)" |
| 80 | Special Proposal Types | `duration_five_years` Five-year duration | P | p. 1 "60 months (five years)" |
| 81 | Special Proposal Types | `not_collaborative` Not collaborative | P | p. 1 "Single-institution proposal; not a collaborative proposal" |
| 82 | Special Proposal Types | `not_rapid_eager` Not RAPID/EAGER | P | p. 1 type is CAREER only; RAPID/EAGER never mentioned |
| 83 | Special Proposal Types | `career_development_plan` Career development plan | P | pp. 15–16 §8 Career Development Plan (career goals; building the foundation) |
| 84 | Special Proposal Types | `research_education_integration` Research–education integration | P | p. 14 §6 "Integration of Research and Education" |
| 85 | Post-Award | `data_sharing_timeline` Data sharing timeline | P | p. 30 "within 12 months of the end of that water year"; p. 11 |
| 86 | Post-Award | `equipment_ownership` Equipment after the award | P | p. 17 "Equipment after the award"; p. 25 |
| 87 | Post-Award | `site_restoration` Sites restored | P | p. 17 "Site restoration." decommission per state standards, reseed, remove stations |
| 88 | Post-Award | `no_cost_extension_not_assumed` No extension assumed | P | p. 18 "The plan does not depend on a no-cost extension" |
| 89 | Post-Award | `subawards_identified` Subawards identified | N | None (p. 23 "G5. Subawards" all 0; p. 27); the drilling contractor is a vendor service, stated as not a subaward |
| 90 | Post-Award | `partner_commitments` Partner commitments | P | Silver Basin High School: p. 13 §5.1, p. 28, letter p. 32 |
| 91 | Post-Award | `rebudgeting_flagged` No major rebudgeting planned | P | p. 18 "no planned movement of funds between budget categories" |
| 92 | Post-Award | `key_personnel_changes` Continuity of key personnel | P (P/F allowed) | p. 16 "Continuity if a student leaves." |
| 93 | Reporting | `measurable_outcomes` Measurable outcomes | P | p. 11 §4.6 table of targets |
| 94 | Reporting | `education_metrics` Education metrics | P | p. 13 "60 high school students and 10 teacher participations … 10 undergraduate researchers … about 140 undergraduates" |
| 95 | Reporting | `assessment_instrument` Assessment instrument | P | p. 13 "pre/post survey" (12-item content test + 15-item science-identity scale) |
| 96 | Reporting | `external_evaluator` Evaluator named | P | p. 13 "Owyhee State University Office of Assessment and Evaluation, which is independent of the PI" |
| 97 | Reporting | `annual_report_content` Annual reporting planned | P or C | p. 18 "Annual reporting." lists the contents |
| 98 | Reporting | `dissemination_plan` Dissemination plan | P | p. 15 §7.1 (named journals, AGU, Western Snow Conference) |
| 99 | Reporting | `public_outcomes` Public outcomes | P | p. 15 "Public outcomes." project web page; p. 14 annual community briefings |
| 100 | Reporting | `participant_tracking` Participant tracking | P | p. 13 "Participant tracking." unique codes and 12-month follow-up |
| 101 | Financial Management | `pre_award_costs` Pre-award costs | N | None requested (p. 27) |
| 102 | Financial Management | `cost_sharing_none` No voluntary cost sharing | P | p. 27 "No cost sharing is included or committed"; no dollar figures in Facilities or the letter |
| 103 | Financial Management | `rates_basis` Rates follow negotiated agreement | P | p. 27 "negotiated rate agreement with the U.S. Department of Health and Human Services, dated April 2, 2025" |
| 104 | Financial Management | `escalation_stated` Salary escalation stated | P | p. 24 "escalated 3% a year" |
| 105 | Financial Management | `tuition_handling` Tuition handled | P | p. 27 tuition remission budgeted; "excluded from the F&A base" |
| 106 | Financial Management | `unallowable_costs` No unallowable costs | P | pp. 23–27: no entertainment, alcohol or meals at meetings; p. 26 "no general office supplies are requested" |
| 107 | Financial Management | `program_income` Program income | N or P | p. 27 "not expected to earn program income" (a stated "none" reads as either) |
| 108 | Publication and IP | `open_access` Open access | P | p. 15 manuscripts deposited in NSF-PAR; OA charges budgeted (p. 26) |
| 109 | Publication and IP | `data_formats` Data formats | P | p. 29 §2 CSV, NetCDF-4 (CF), LAZ |
| 110 | Publication and IP | `metadata_standard` Metadata standard | P | p. 29 §3 ISO 19115-1 (+ CF conventions) |
| 111 | Publication and IP | `code_sharing` Code sharing | P | p. 30 "released under the MIT license with each publication"; p. 10 |
| 112 | Publication and IP | `ip_rights` Intellectual property | P or N | p. 15 §7.2: no inventions expected; university IP policy |
| 113 | Publication and IP | `data_license` Data license | P | p. 30 "CC BY 4.0" |
| 114 | Publication and IP | `acknowledgement` NSF acknowledgement | P | p. 15 "will acknowledge NSF support with the award number" |

Totals: 91 pass; 3 fail (#19, #44, #64 = plants A, B, G); 4 cant_tell (#10, #67, #68, #69); 6 not_applicable (#61, #74, #77, #89, #101, #107); 11 with two accepted answers (P/C: #21, #22, #71, #72, #97; P/N: #37, #65, #73, #107, #112; P/F: #92).

## 3. Budget numbers

From the Budget (PDF p. 23). Every row is whole dollars; each total is the sum of the rows shown above it, and each cumulative is the sum of the five years.

| Line | Year 1 | Year 2 | Year 3 | Year 4 | Year 5 | Cumulative |
|---|--:|--:|--:|--:|--:|--:|
| A. Senior personnel salary: 3.0 months per year (Dr. Jordan Marsh, summer) | 28,500 | 29,355 | 30,236 | 31,143 | 32,077 | 151,311 |
| B. Graduate student (PhD), 12 months | 29,000 | 29,870 | 30,766 | 31,689 | 32,640 | 153,965 |
| B. Undergraduate students (2), 300 h each | 9,600 | 9,888 | 10,185 | 10,490 | 10,805 | 50,968 |
| C. Fringe: PI summer salary, 28.0% | 7,980 | 8,219 | 8,466 | 8,720 | 8,982 | 42,367 |
| C. Fringe: graduate student, 9.0% | 2,610 | 2,688 | 2,769 | 2,852 | 2,938 | 13,857 |
| C. Fringe: undergraduates, 2.0% | 192 | 198 | 204 | 210 | 216 | 1,020 |
| **Total fringe benefits (C)** | **10,782** | **11,105** | **11,439** | **11,782** | **12,136** | **57,244** |
| **Total salaries, wages and fringe (A + B + C)** | **77,882** | **80,218** | **82,626** | **85,104** | **87,658** | **413,488** |
| D. Drone LiDAR system | 48,000 | 0 | 0 | 0 | 0 | 48,000 |
| D. Snow pillow stations (3 at $7,400) | 22,200 | 0 | 0 | 0 | 0 | 22,200 |
| **Total equipment (D)** | **70,200** | **0** | **0** | **0** | **0** | **70,200** |
| E. Field vehicle mileage (8,000 mi at $0.70) | 5,600 | 5,600 | 5,600 | 5,600 | 5,600 | 28,000 |
| E. Field lodging and per diem (30 nights at $100) | 3,000 | 3,000 | 3,000 | 3,000 | 3,000 | 15,000 |
| E. Winter oversnow access (25 days at $40) | 1,000 | 1,000 | 1,000 | 1,000 | 1,000 | 5,000 |
| E. AGU Fall Meeting ($2,300 per traveler) | 2,300 | 4,600 | 4,600 | 4,600 | 4,600 | 20,700 |
| E. Western Snow Conference | 0 | 1,400 | 1,400 | 1,400 | 0 | 4,200 |
| **Total travel (E)** | **11,900** | **15,600** | **15,600** | **15,600** | **14,200** | **72,900** |
| F1. Stipends (12 × $400 + 2 × $750) | 6,300 | 6,300 | 6,300 | 6,300 | 6,300 | 31,500 |
| F2. Travel (2 bus trips at $650) | 1,300 | 1,300 | 1,300 | 1,300 | 1,300 | 6,500 |
| F3. Subsistence (14 × 3 days × $45 + 2 nights × $120) | 2,130 | 2,130 | 2,130 | 2,130 | 2,130 | 10,650 |
| F4. Other (12 × $55) | 660 | 660 | 660 | 660 | 660 | 3,300 |
| **Total participant support (F)** | **10,390** | **10,390** | **10,390** | **10,390** | **10,390** | **51,950** |
| G1. Materials and supplies | 25,000 | 2,400 | 2,400 | 2,400 | 3,200 | 35,400 |
| G2. Publication costs | 0 | 0 | 2,600 | 2,600 | 5,200 | 10,400 |
| G3. Consultant services | 0 | 0 | 0 | 0 | 0 | 0 |
| G4. Computer services | 0 | 0 | 0 | 0 | 0 | 0 |
| G5. Subawards | 0 | 0 | 0 | 0 | 0 | 0 |
| G6. Well installation (18 × $950) | 17,100 | 0 | 0 | 0 | 0 | 17,100 |
| G6. Station telemetry (3 × $25 × 12) | 900 | 900 | 900 | 900 | 900 | 4,500 |
| G6. Evaluation services | 2,000 | 2,000 | 2,000 | 2,000 | 2,000 | 10,000 |
| G6. Graduate tuition remission | 10,800 | 11,124 | 11,458 | 11,801 | 12,155 | 57,338 |
| **Total other direct costs (G)** | **55,800** | **16,424** | **19,358** | **19,701** | **23,455** | **134,738** |
| **H. Total direct costs (A–G)** | **226,172** | **122,632** | **127,974** | **130,795** | **135,703** | **743,276** |
| MTDC (H − D − F − tuition) | 134,782 | 101,118 | 106,126 | 108,604 | 113,158 | 563,788 |
| **I. Indirect costs, 44.5% of MTDC (rounded)** | **59,978** | **44,998** | **47,226** | **48,329** | **50,355** | **250,886** |
| **J. Total (H + I)** | **286,150** | **167,630** | **175,200** | **179,124** | **186,058** | **994,162** |

Checks (all exact):

- Salaries and wages (A + B) per year: 67,100, 69,113, 71,187, 73,322, 75,522 (cumulative 356,244); with fringe, row 'Total salaries, wages and fringe'.
- H = (A + B + C) + D + E + F + G for every year, e.g. Year 1: 77,882 + 70,200 + 11,900 + 10,390 + 55,800 = 226,172.
- MTDC = H − equipment − participant support − tuition, e.g. Year 1: 226,172 − 70,200 − 10,390 − 10,800 = 134,782; F&A = 0.445 × MTDC rounded = 59,978.
- J by year: 286,150, 167,630, 175,200, 179,124, 186,058; sum = 994,162, the amount on the cover (p. 1) and in the justification (p. 24).
- Salaries, stipends, wages and tuition escalate 3% a year from Year 1 (each year's figure rounded to the dollar); travel, participant support and other costs are flat.
- Year 1 materials: 8,100 (18 wells × $450) + 14,040 (18 loggers × $780) + 1,260 (3 barometric loggers × $420) + 1,600 field supplies = 25,000.
- Year 1 equipment is the only nonzero equipment year (plant F: the LiDAR is bought in Year 1 but first flown in Year 2).
