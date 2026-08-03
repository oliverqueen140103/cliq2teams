import requests

# Zoho India DC Endpoint
TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"

# Replace these 3 values with your actual credentials:
CLIENT_ID = "YOUR_ZOHO_CLIENT_ID"
CLIENT_SECRET = "YOUR_ZOHO_CLIENT_SECRET"
GRANT_CODE = "YOUR_ZOHO_GRANT_CODE" 

payload = {
    "grant_type": "authorization_code",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": GRANT_CODE
}

response = requests.post(TOKEN_URL, data=payload)
data = response.json()

print("\n--- ZOHO API RESPONSE ---")
print(data)

if "refresh_token" in data:
    print("\n✅ YOUR PERMANENT REFRESH TOKEN IS:")
    print(data["refresh_token"])
else:
    print("\n❌ FAILED. Error details:", data.get("error"))