from pydantic import BaseModel, Field, field_validator


class CoverLetterRequest(BaseModel):
    job_id: str


class ProfileUpdate(BaseModel):
    name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    keywords: list[str] = []
    location: str = "remote"
    job_type: str = "full-time"
    platforms: list[str] = ["remoteok"]
    writing_style: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    current_employer: str = ""
    current_title: str = ""
    country: str = ""
    school: str = ""
    degree: str = ""
    no_degree: bool = False
    salary_expectation: str = ""
    no_salary_expectation: bool = False


class LetterPreviewRequest(BaseModel):
    keywords: str
    style: str | None = ""
    job_description: str | None = ""
    # Where the job is, when the walk read it (Indeed). Otherwise the server looks the
    # posting up in the person's pool by `job_url` (ATS walks come from the pool).
    job_location: str | None = Field("", max_length=300)
    job_url: str | None = Field("", max_length=2000)
    # Separate fields, so the letter isn't addressed to "your company" when the
    # extension knows the name.
    job_title: str | None = Field("", max_length=300)
    company: str | None = Field("", max_length=300)


class AnswerQuestionRequest(BaseModel):
    # Caps are defense-in-depth: the extension already truncates, and the AI modules
    # re-truncate before the LLM, but a direct caller shouldn't be able to send megabytes.
    question: str = Field("", max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=60)
    job_title: str = Field("", max_length=300)
    company: str = Field("", max_length=300)
    # The pool row being applied to, when there is one. Only used to look up answers
    # the HUMAN already gave for this job on a previous hand-back — a question someone
    # answered by hand must never be re-derived by a model, and must never come back
    # with a different answer the second time.
    job_id: str | None = Field(None, max_length=64)
    # The posting text, for runs with no pool row (Indeed/ZipRecruiter live search):
    # without it the answerer sees only the company NAME and guesses the employer.
    job_description: str = Field("", max_length=8000)
    # Where the job is, when the walk read it — "live within 30 miles?" / "on-site?" are
    # answerable from the person's own place only when the job's place is known too.
    job_location: str = Field("", max_length=300)


class AssessFitRequest(BaseModel):
    job_title: str = Field("", max_length=300)
    company: str = Field("", max_length=300)
    description: str = Field("", max_length=8000)
    screener_questions: list[str] = Field(default_factory=list, max_length=40)
    # The pool row the ATS walk is on (the head of the server queue). With it the endpoint
    # reuses the verdict the queue was built from instead of judging the posting a second
    # time on the page text — 10-02: 38/42 stored, 22-32 live, three of five opens lost.
    # Absent on the Indeed/ZipRecruiter walks, which judge live as before.
    job_id: str | None = Field(None, max_length=64)


class AssessFitCard(BaseModel):
    """One search-result card with the posting text the extension read off the results page."""

    title: str = Field(..., max_length=300)
    link: str = Field(..., max_length=1000)
    company: str = Field("", max_length=300)
    location: str = Field("", max_length=300)
    platform: str = Field("indeed", max_length=40)
    description: str = Field("", max_length=20000)


class AssessFitBatchRequest(BaseModel):
    # One Indeed results page is ~15 cards after the title gate; 30 bounds a page dump.
    jobs: list[AssessFitCard] = Field(default_factory=list, max_length=30)
    # The search phrase that brought this page (app/db/keyword_yield). Older extensions
    # don't send it; the page is then judged exactly the same, just not counted.
    keyword: str = Field("", max_length=200)


class FormAnswer(BaseModel):
    q: str = ""
    a: str = ""

    # Trimmed, never refused: a long answer must not cost the application row (422).
    @field_validator("q", "a", mode="before")
    @classmethod
    def _clip(cls, v, info):
        return str(v or "")[: 200 if info.field_name == "q" else 500]


class ApplicationSaveRequest(BaseModel):
    job_title: str
    company: str
    platform: str = ""
    job_url: str = ""
    cover_letter: str = ""
    status: str = "applied"
    # The form's questions and the answers we gave (content.js collectFormAnswers).
    form_answers: list[FormAnswer] = Field(default_factory=list)

    @field_validator("form_answers", mode="before")
    @classmethod
    def _cap_answers(cls, v):
        return [x for x in (v or []) if isinstance(x, dict)][:60]


class ApplicationStatusRequest(BaseModel):
    status: str


class FindJobsRequest(BaseModel):
    platforms: list[str] = []


class SearchPrefsUpdate(BaseModel):
    # None = "not mentioned in this request": the saved value stays. "" is a real
    # answer meaning Any for location, job_type and work_setting.
    keywords: list[str] | None = None
    location: str | None = None
    job_type: str | None = None
    platforms: list[str] | None = None
    work_setting: str | None = None


class CampaignStartRequest(BaseModel):
    keywords: list[str] = []
    platforms: list[str] = []
    location: str = ""
    job_type: str = ""


class ConnectPlatformRequest(BaseModel):
    platform: str


class JobStatusUpdate(BaseModel):
    status: str


class DeadLinkReport(BaseModel):
    url: str = Field(..., max_length=2000)


class IngestJob(BaseModel):
    """One job card harvested in-browser by the extension during a campaign walk."""

    title: str
    link: str
    company: str = ""
    platform: str = "indeed"
    description: str = ""
    location: str = ""
    job_type: str = ""


class JobDescriptionRequest(BaseModel):
    """The posting text the extension is reading, for the job it is about to apply to."""

    link: str
    description: str
    title: str = ""
    company: str = ""
    platform: str = "unknown"
    location: str = ""


class IngestJobsRequest(BaseModel):
    jobs: list[IngestJob] = []


class ForgotPasswordRequest(BaseModel):
    email: str
