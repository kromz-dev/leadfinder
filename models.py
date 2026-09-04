from pydantic import BaseModel, HttpUrl, Field
from typing import List, Optional, Literal

class LeadContract(BaseModel):
    nom: str = Field(alias="Nom")
    profil_source: HttpUrl = Field(alias="Profil source")
    site_web: Optional[HttpUrl] = Field(default=None, alias="Site web")
    localisation: Optional[str] = Field(default=None, alias="Localisation")
    specialites: List[Literal["automation", "integration", "api", "webflow", "bubble", "make", "zapier", "n8n", "ia", "deploiement"]] = Field(default_factory=list, alias="Spécialités")
    source: Literal["Webflow Experts", "Bubble Agencies", "Make Partners", "Zapier Experts", "Slack No-Code FR", "Manuel"] = Field(alias="Source")
    contact: Optional[str] = Field(default=None, alias="Contact")
    canal: Optional[Literal["Insta", "X", "LinkedIn", "Malt", "Contra", "Bouche-à-oreille", "Autre"]] = Field(default=None, alias="Canal")
    pitch: Optional[str] = Field(default=None, alias="Pitch")
    tech_stack: List[str] = Field(default_factory=list, alias="Tech Stack")
    cible: Optional[str] = Field(default=None, alias="Cible")
