import os
import json
import asyncio
import csv
import re
from typing import List
from dotenv import load_dotenv

from pydantic import BaseModel, ValidationError
from litellm import completion
from litellm.exceptions import APIError, APIConnectionError, RateLimitError, Timeout, AuthenticationError

# Nouveaux imports Crawl4AI
from crawl4ai import AsyncWebCrawler, BrowserConfig

from models import LeadContract
from notion_client import NotionClient
from politeness import PoliteCrawler, RobotsDisallowed, USER_AGENT

# Charger les variables d'environnement (écrase le terminal)
load_dotenv(override=True)

# Toutes les requetes sortantes passent par ce limiteur : 1 req/s par hote,
# robots.txt verifie avant chaque fetch, User-Agent identifiable.
POLITE = PoliteCrawler()

# 🔄 SYSTÈME DE FALLBACK (Cascade de LLMs)
LLM_PROVIDERS = [
    {
        "provider": "openai/auto:free", # Routage automatique vers un modèle gratuit (zéro-coût)
        "env_key": "BAZAARLINK_API_KEY",
        "api_base": "https://api.bazaarlink.ai/v1"
    },
    {
        "provider": "groq/llama-3.1-70b-versatile", 
        "env_key": "GROQ_API_KEY",
        "api_base": None
    },
    {
        "provider": "huggingface/Qwen/Qwen3.8-27B", # Modèle demandé par l'utilisateur
        "env_key": "HUGGINGFACE_API_KEY",
        "api_base": None
    },
]

from pydantic import BaseModel
class LeadList(BaseModel):
    leads: List[LeadContract]

import json
from litellm import completion
from litellm.exceptions import APIError, APIConnectionError, RateLimitError, Timeout, AuthenticationError

def parse_llm_json(raw_text: str):
    """Extrait et parse le JSON généré par un LLM de manière robuste."""
    # Nettoyage des blocs Markdown potentiels (ex: ```json ... ```)
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        raw_text = match.group(1)
        
    raw_text = raw_text.strip()
    
    # Tentative de récupérer uniquement ce qui ressemble à un objet ou tableau JSON
    if not (raw_text.startswith('{') or raw_text.startswith('[')):
        match = re.search(r'(\{.*?\}|\[.*?\])', raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1)
            
    return json.loads(raw_text)

async def extract_with_fallback(crawler: AsyncWebCrawler, url: str) -> list:
    """Tente l'extraction LLM en basculant d'un fournisseur à l'autre en cas d'erreur."""
    
    # Code JavaScript pour scroller automatiquement vers le bas de la page (Infinite Scroll / Load More)
    scroll_js = """
    const scrollInterval = setInterval(() => window.scrollBy(0, 1000), 500);
    setTimeout(() => clearInterval(scrollInterval), 5000);
    """
    
    # 1. On récupère le Markdown pur via Crawl4AI (avec le scrolling)
    try:
        result = await POLITE.arun(
            crawler,
            url,
            js_code=scroll_js,
            bypass_cache=True,
            magic=True,
            word_count_threshold=10,
            exclude_external_links=True,
        )
    except RobotsDisallowed as exc:
        print(f"\U0001F6D1 {exc}")
        return []
    
    if not result.success or not result.markdown:
        print(f"❌ Impossible de récupérer la page {url}")
        return []
        
    markdown_content = result.markdown[:30000] # On limite la taille pour ne pas exploser le contexte
    schema = LeadList.model_json_schema()
    instruction = (
        "You are an expert data extractor. Extract all agency/expert partners listed on the following directory page. "
        "Return ONLY a valid JSON object with a single key 'leads' containing an array of these partners. "
        f"Map them to the following schema: {json.dumps(schema)}. "
        "Ensure 'Spécialités' matches the allowed enums. If a field is unavailable, leave it null. Do not hallucinate."
    )
    
    for config in LLM_PROVIDERS:
        api_key = os.getenv(config["env_key"])
        if not api_key:
            print(f"⏭️ Clé {config['env_key']} introuvable, passage au suivant...")
            continue
            
        print(f"🤖 Tentative d'extraction avec {config['provider']}...")
        try:
            # 2. On interroge l'IA directement via LiteLLM pour contourner les bugs de Crawl4AI
            response = completion(
                model=config["provider"],
                api_key=api_key,
                api_base=config.get("api_base"),
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": markdown_content}
                ],
                response_format={"type": "json_object"}
            )
            
            raw_text = response.choices[0].message.content
            
            try:
                parsed = parse_llm_json(raw_text)
                leads = parsed.get("leads", []) if isinstance(parsed, dict) else parsed
                print(f"✅ Extraction réussie avec {config['provider']} ! ({len(leads)} leads)")
                return leads
            except json.JSONDecodeError as e:
                print(f"⚠️ Erreur de parsing JSON avec {config['provider']} : {e}. Contenu: {raw_text[:200]}...")
                continue
                
        except (APIError, APIConnectionError, RateLimitError, Timeout, AuthenticationError) as e:
            print(f"⚠️ Erreur LLM avec {config['provider']} : {str(e)[:200]}")
            continue
            
    print("🚨 Tous les LLMs ont échoué ou aucune clé n'est disponible.")
    return []

async def deep_scrape(crawler: AsyncWebCrawler, url: str) -> dict:
    """Visite le site officiel du lead pour trouver un email / LinkedIn de façon asynchrone et enrichir avec l'IA."""
    print(f"🕵️ Deep Scraping du site : {url} ...")
    result_data = {"email": None, "linkedin": None, "pitch": None, "tech_stack": [], "cible": None}
    
    # On utilise le crawler rapide (sans LLM) pour récupérer le markdown
    try:
        res = await POLITE.arun(crawler, url, bypass_cache=True, magic=True)
    except RobotsDisallowed as exc:
        print(f"\U0001F6D1 {exc}")
        return result_data
    text = res.markdown or ""
    if not text:
        text = res.html or ""
        
    # Regex Email stricte sur le texte nettoyé
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text)
    for found_email in emails:
        # On ignore les faux positifs fréquents (fichiers, packages NPM, sentry)
        if not any(bad in found_email.lower() for bad in ['.png', '.jpg', '.jpeg', '.svg', '.js', 'sentry.io', 'example.com', '@v']):
            result_data["email"] = found_email
            break # On garde le premier VRAI email trouvé
            
    # Regex LinkedIn
    li_match = re.search(r'https?://(?:www\.)?linkedin\.com/(?:company|in)/[^\s"\'<>]+', text)
    if li_match:
        result_data["linkedin"] = li_match.group(0)
        
    # Extraction IA (Pitch, Tech Stack, Cible)
    instruction = (
        "You are an expert data extractor analyzing a website. Extract the following information: "
        "1. 'pitch': The one-sentence value proposition or main pitch of the company. "
        "2. 'tech_stack': An array of strings representing the technology tools or software they use or specialize in. "
        "3. 'cible': The target audience (e.g., B2B, E-commerce, Startups, etc.). "
        "Return ONLY a valid JSON object with the keys 'pitch', 'tech_stack', and 'cible'."
    )
    
    for config in LLM_PROVIDERS:
        api_key = os.getenv(config["env_key"])
        if not api_key:
            continue
            
        try:
            print(f"🤖 Enrichissement avec {config['provider']} pour {url}...")
            response = completion(
                model=config["provider"],
                api_key=api_key,
                api_base=config.get("api_base"),
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": text}
                ],
                response_format={"type": "json_object"}
            )
            
            raw_text = response.choices[0].message.content
            
            try:
                parsed = parse_llm_json(raw_text)
                if isinstance(parsed, dict):
                    result_data["pitch"] = parsed.get("pitch")
                    result_data["tech_stack"] = parsed.get("tech_stack", [])
                    result_data["cible"] = parsed.get("cible")
                    print(f"✅ Enrichissement IA réussi !")
                    break
                else:
                    print(f"⚠️ Le LLM {config['provider']} n'a pas retourné un dictionnaire.")
                    continue
            except json.JSONDecodeError as e:
                print(f"⚠️ Erreur de parsing JSON avec {config['provider']} : {e}. Contenu: {raw_text[:200]}...")
                continue
                
        except (APIError, APIConnectionError, RateLimitError, Timeout, AuthenticationError) as e:
            print(f"⚠️ Erreur LLM lors de l'enrichissement de {url} avec {config['provider']}: {str(e)[:200]}")
            continue
            
    return result_data

def save_to_csv(leads: List[LeadContract], filename="leads.csv"):
    if not leads: return
    keys = list(LeadContract.model_fields.keys())
    aliases = [LeadContract.model_fields[k].alias or k for k in keys]
    file_exists = os.path.isfile(filename)
    
    with open(filename, 'a', newline='', encoding='utf-8') as f:
        dict_writer = csv.DictWriter(f, fieldnames=aliases)
        if not file_exists:
            dict_writer.writeheader()
        for lead in leads:
            dump = lead.model_dump(by_alias=True, mode='json')
            dump["Spécialités"] = ", ".join(dump.get("Spécialités") or [])
            dump["Tech Stack"] = ", ".join(dump.get("Tech Stack") or [])
            dict_writer.writerow(dump)
    print(f"💾 {len(leads)} nouveau(x) lead(s) sauvegardé(s) dans {filename}")

async def main():
    print("🚀 Démarrage de l'orchestrateur IA Crawl4AI + Notion...")
    notion = NotionClient()
    
    import random
    
    # 🌍 Rotation des sources pour éviter d'avoir toujours les mêmes
    all_urls = [
        "https://experts.webflow.com/services/custom-code",
        "https://experts.webflow.com/region/europe",
        "https://experts.webflow.com/services/website-migrations",
        "https://zapier.com/experts",
        "https://bubble.io/agencies",
        "https://www.make.com/en/partners"
    ]
    # On sélectionne 3 URLs au hasard à chaque lancement pour diversifier le scraping
    urls_to_scrape = random.sample(all_urls, min(3, len(all_urls)))
    
    valid_leads: List[LeadContract] = []
    
    # 1. Lancement du super-crawler
    browser = BrowserConfig(user_agent=USER_AGENT, verbose=True)
    async with AsyncWebCrawler(config=browser) as crawler:
        for url in urls_to_scrape:
            print(f"\n======================================")
            print(f"🌐 Analyse de l'annuaire : {url}")
            print(f"======================================")
            raw_leads = await extract_with_fallback(crawler, url)
            
            for raw_lead in raw_leads:
                if not isinstance(raw_lead, dict):
                    print(f"⚠️ raw_lead n'est pas un dictionnaire: {type(raw_lead)}. Ignoré.")
                    continue
                try:
                    # Pydantic valide le JSON généré par le LLM
                    valid_lead = LeadContract(**raw_lead)
                    valid_leads.append(valid_lead)
                except ValidationError as e:
                    nom_lead = raw_lead.get('Nom', 'Inconnu')
                    print(f"⚠️ Le LLM a généré un lead invalide ({nom_lead}) - Ignoré.")

        print(f"\n📊 {len(valid_leads)} leads totalement valides générés par l'IA.")
        
        # 2. Traitement Notion & Deep Scraping
        new_leads = []
        for lead in valid_leads:
            print(f"⏳ Vérification du doublon Notion pour {lead.nom}...")
            is_duplicate = notion.check_duplicate(str(lead.profil_source))
            
            if is_duplicate is True:
                print(f"⏭️ Doublon détecté, on ignore : {lead.profil_source}")
            elif is_duplicate is None and (notion.token and notion.database_id):
                print(f"⚠️ Erreur Notion, on ignore par sécurité.")
            else:
                if lead.site_web:
                    enrichment = await deep_scrape(crawler, str(lead.site_web))
                    if enrichment.get("email"):
                        lead.contact = enrichment["email"]
                    if enrichment.get("linkedin"):
                        lead.contact = (lead.contact + " | " + enrichment["linkedin"]) if lead.contact else enrichment["linkedin"]
                        lead.canal = "LinkedIn"
                    if enrichment.get("pitch"):
                        lead.pitch = enrichment["pitch"]
                    if enrichment.get("tech_stack"):
                        lead.tech_stack = enrichment["tech_stack"]
                    if enrichment.get("cible"):
                        lead.cible = enrichment["cible"]
                        
                new_leads.append(lead)
                
    # 3. Sauvegarde
    if new_leads:
        save_to_csv(new_leads)
        for lead in new_leads:
            notion.add_lead(lead)
    else:
        print("🤷 Aucun nouveau lead à ajouter dans le CRM.")

    print("🏁 Fin du script.")

if __name__ == "__main__":
    asyncio.run(main())
