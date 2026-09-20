import requests
import json

# 1) Webhook URL (e.g.: Slack, Discord, or an internal company webhook)
webhook_url = "https://example.com/webhooks/YOUR_WEBHOOK_TOKEN"

# 2) Data to send, as a Python dict (will be auto-converted to JSON)
payload = {
    "event": "order_completed",
    "order_id": 12345,
    "amount": 300.0,
    "customer": "Le Van C",
}

# 3) Headers - declare a token/API key here if the webhook requires one
headers = {
    "Content-Type": "application/json",      # requests sets this automatically when using json=
    "Authorization": "Bearer YOUR_API_TOKEN", # remove this line if the webhook doesn't need auth
}

try:
    # IMPORTANT: use the json=... parameter (NOT data=...)
    # so requests automatically serializes the dict -> JSON and sets the right Content-Type
    response = requests.post(
        webhook_url,
        json=payload,
        headers=headers,
        timeout=10,           # always set a timeout to avoid hanging forever if the server doesn't respond
    )
    response.raise_for_status()   # raises an error immediately on a 4xx/5xx status code

    print(f"Sent successfully! Status: {response.status_code}")
    print(f"Response body: {response.text}")

except requests.exceptions.Timeout:
    print("Error: the webhook did not respond in time (timeout)")
except requests.exceptions.HTTPError as e:
    print(f"HTTP error from server: {e}")
except requests.exceptions.RequestException as e:
    print(f"Connection error: {e}")
