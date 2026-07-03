from pydantic import BaseModel


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
    question: str
    options: list[str] = []
    job_title: str = ""
    company: str = ""


class AssessFitRequest(BaseModel):
    job_title: str = ""
    company: str = ""
    description: str = ""
    screener_questions: list[str] = []


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
