from pydantic import BaseModel, Field


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


class LetterPreviewRequest(BaseModel):
    keywords: str
    style: str | None = ""
    job_description: str | None = ""


class AnswerQuestionRequest(BaseModel):
    # Caps are defense-in-depth: the extension already truncates, and the AI modules
    # re-truncate before the LLM, but a direct caller shouldn't be able to send megabytes.
    question: str = Field("", max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=60)
    job_title: str = Field("", max_length=300)
    company: str = Field("", max_length=300)


class AssessFitRequest(BaseModel):
    job_title: str = Field("", max_length=300)
    company: str = Field("", max_length=300)
    description: str = Field("", max_length=8000)
    screener_questions: list[str] = Field(default_factory=list, max_length=40)


class TemplateRequest(BaseModel):
    template: str


class ApplicationSaveRequest(BaseModel):
    job_title: str
    company: str
    platform: str = ""
    job_url: str = ""
    cover_letter: str = ""
    status: str = "applied"


class FindJobsRequest(BaseModel):
    platforms: list[str] = []


class SearchPrefsUpdate(BaseModel):
    keywords: list[str] = []
    location: str = "remote"
    job_type: str = "full-time"
    platforms: list[str] = ["remoteok"]


class CampaignStartRequest(BaseModel):
    keywords: list[str] = []
    platforms: list[str] = []
    location: str = ""
    job_type: str = ""


class ConnectPlatformRequest(BaseModel):
    platform: str


class JobStatusUpdate(BaseModel):
    status: str


class ForgotPasswordRequest(BaseModel):
    email: str
