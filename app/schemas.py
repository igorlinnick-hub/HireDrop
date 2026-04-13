from pydantic import BaseModel
from typing import List, Optional


class CoverLetterRequest(BaseModel):
    job_id: str


class ProfileUpdate(BaseModel):
    name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    keywords: List[str] = []
    location: str = "remote"
    job_type: str = "full-time"
    platforms: List[str] = ["remoteok"]
    writing_style: str = ""


class LetterPreviewRequest(BaseModel):
    keywords: str
    style: Optional[str] = ""
    job_description: Optional[str] = ""


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
    platforms: List[str] = []


class CampaignStartRequest(BaseModel):
    keywords: List[str] = []
    platforms: List[str] = []
    location: str = ""
    job_type: str = ""


class ConnectPlatformRequest(BaseModel):
    platform: str


class JobStatusUpdate(BaseModel):
    status: str
