"""
Webhook that receives leads from a lead form.

Run locally for testing:
    uvicorn erp.webhook_app:app --reload --port 8000

Test with curl:
    curl -X POST http://127.0.0.1:8000/webhooks/leads ^
      -H "Content-Type: application/json" ^
      -d "{\"full_name\": \"Jane Doe\", \"email\": \"test@example.com\", \"company\": \"ABC Corp\", \"source\": \"landing_page\"}"
"""
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator

from erp.leads import process_new_lead

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ERP Lead Webhook")


class LeadPayload(BaseModel):
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    source: Optional[str] = "lead_form"

    @model_validator(mode="after")
    def require_contact(self):
        if not self.email and not self.phone:
            raise ValueError("At least one of email or phone is required.")
        return self


@app.post("/webhooks/leads")
def receive_lead(payload: LeadPayload):
    try:
        result = process_new_lead(payload.model_dump())
    except Exception as e:
        logger.exception("Failed to process lead")
        raise HTTPException(status_code=500, detail=str(e))
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
