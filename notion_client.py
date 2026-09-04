import os
import time
import requests
from models import LeadContract
from typing import Optional

class NotionClient:
    def __init__(self, token: Optional[str] = None, database_id: Optional[str] = None):
        self.token = token or os.getenv("NOTION_TOKEN")
        self.database_id = database_id or os.getenv("NOTION_DATABASE_ID")
        self.version = "2022-06-28"
        self.session = requests.Session()
        if self.token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
                "Content-Type": "application/json"
            })

    def rate_limit(self):
        time.sleep(1)

    def check_duplicate(self, profile_url: str) -> Optional[bool]:
        """Returns True if duplicate, False if not, None if error or unconfigured."""
        if not self.token or not self.database_id:
            print("⚠️ Variables d'environnement Notion manquantes, impossible de vérifier les doublons.")
            return None
        
        self.rate_limit()
        
        url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        payload = {
            "filter": {
                "property": "Profil source",
                "url": {
                    "equals": profile_url
                }
            }
        }
        
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return len(results) > 0
        except requests.exceptions.RequestException as e:
            print(f"❌ Erreur lors de la vérification de doublon Notion: {e}")
            return None

    def add_lead(self, lead: LeadContract) -> bool:
        """Returns True on success, False on failure."""
        if not self.token or not self.database_id:
            print(f"⚠️ [MOCK NOTION] Ajout du lead : {lead.nom} ({lead.profil_source})")
            return False
            
        self.rate_limit()
        url = "https://api.notion.com/v1/pages"
        
        properties = {
            "Nom": {"title": [{"text": {"content": lead.nom}}]},
            "Profil source": {"url": str(lead.profil_source)},
            "Source": {"select": {"name": lead.source}},
            "Statut": {"status": {"name": "Not started"}}
        }
        
        if lead.site_web:
            properties["Site web"] = {"url": str(lead.site_web)}
        if lead.localisation:
            properties["Localisation"] = {"rich_text": [{"text": {"content": lead.localisation}}]}
        if lead.specialites:
            properties["Spécialités"] = {"multi_select": [{"name": spec} for spec in lead.specialites]}
        if lead.contact:
            properties["Contact"] = {"rich_text": [{"text": {"content": lead.contact}}]}
        if lead.canal:
            properties["Canal"] = {"select": {"name": lead.canal}}

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties
        }
        
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            print(f"✅ Lead ajouté dans Notion : {lead.nom}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ Erreur lors de l'ajout dans Notion: {e}")
            return False
